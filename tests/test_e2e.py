"""Testy end-to-end: prawdziwe procesy, prawdziwe sockety, loopback.

Klient i serwer są uruchamiane jako podprocesy i sprawdzamy **faktycznie
przesłane bajty** (``sha256``), a nie tylko kody wyjścia.

Ograniczenie tego środowiska, warte zapisania: sandbox, w którym jadą te
testy, przechwytuje ruch wychodzący, więc połączenie do dowolnego adresu
zewnętrznego „udaje się" niezależnie od tego, czy coś tam nasłuchuje.
Dlatego **wszystkie** testy trzymają się ``127.0.0.1``, a timeout połączenia
testujemy przez zapchanie kolejki ``accept()`` lokalnego listenera, nie przez
adres-czarną-dziurę.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time

import pytest
from conftest import (
    HOST,
    PYNCAT_CMD,
    binary_blob,
    free_port,
    sha256,
    wait_until_bound,
)


def has_ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
    except OSError:
        return False
    return True


requires_ipv6 = pytest.mark.skipif(
    not has_ipv6_loopback(), reason="brak działającego loopbacku IPv6"
)


class TestTransferDanych:
    def test_klient_do_serwera_1mib_binarnie(self, runner, port, tmp_path) -> None:
        """Pełny transfer w kierunku klient → serwer, dane binarne."""
        blob = binary_blob()
        src = tmp_path / "in.bin"
        src.write_bytes(blob)

        server = runner.spawn([*PYNCAT_CMD, "-l", str(port)], close_stdin=True)
        assert wait_until_bound(port), "serwer nie zbindował portu"

        client = runner.spawn(
            [*PYNCAT_CMD, "-N", "-w", "15", HOST, str(port)], stdin_path=src
        )
        assert client.wait(40) == 0
        assert server.wait(40) == 0
        assert sha256(server.stdout_bytes) == sha256(blob)

    def test_serwer_do_klienta_1mib_binarnie(self, runner, port, tmp_path) -> None:
        """Kierunek odwrotny: serwer nadaje, klient odbiera.

        Klient dostaje ``-d``, żeby nie czekał na własny stdin — bez tego
        nie wyszedłby po zamknięciu połączenia przez serwer (warunek 1
        wymaga obu wejść martwych).
        """
        blob = binary_blob()
        src = tmp_path / "in.bin"
        src.write_bytes(blob)

        server = runner.spawn(
            [*PYNCAT_CMD, "-l", "-N", str(port)], stdin_path=src
        )
        assert wait_until_bound(port)

        client = runner.spawn(
            [*PYNCAT_CMD, "-d", "-w", "15", HOST, str(port)], close_stdin=True
        )
        assert client.wait(40) == 0
        server.wait(40)
        assert sha256(client.stdout_bytes) == sha256(blob)

    def test_dwukierunkowo(self, runner, port) -> None:
        server = runner.spawn(
            [*PYNCAT_CMD, "-l", str(port)], stdin_data=b"ODPOWIEDZ-SERWERA"
        )
        assert wait_until_bound(port)
        client = runner.spawn(
            [*PYNCAT_CMD, "-N", "-w", "10", HOST, str(port)],
            stdin_data=b"ZAPYTANIE-KLIENTA",
        )
        assert client.wait(20) == 0
        server.wait(20)
        assert client.stdout_bytes == b"ODPOWIEDZ-SERWERA"
        assert server.stdout_bytes == b"ZAPYTANIE-KLIENTA"

    def test_transfer_zerobajtowy(self, runner, port) -> None:
        """Pusty plik ma przejść jako pusty, a procesy mają wyjść z 0."""
        server = runner.spawn([*PYNCAT_CMD, "-l", str(port)], close_stdin=True)
        assert wait_until_bound(port)
        client = runner.spawn(
            [*PYNCAT_CMD, "-N", "-w", "10", HOST, str(port)], stdin_data=b""
        )
        assert client.wait(20) == 0
        assert server.wait(20) == 0
        assert server.stdout_bytes == b""

    def test_same_zera(self, runner, port, tmp_path) -> None:
        """Bajt 0x00 kończy łańcuch w C — klasyczne miejsce na błąd."""
        blob = b"\x00" * (256 * 1024)
        src = tmp_path / "zeros.bin"
        src.write_bytes(blob)
        server = runner.spawn([*PYNCAT_CMD, "-l", str(port)], close_stdin=True)
        assert wait_until_bound(port)
        client = runner.spawn(
            [*PYNCAT_CMD, "-N", "-w", "15", HOST, str(port)], stdin_path=src
        )
        assert client.wait(30) == 0
        server.wait(30)
        assert server.stdout_bytes == blob

    def test_same_wysokie_bajty(self, runner, port, tmp_path) -> None:
        blob = b"\xff" * (256 * 1024)
        src = tmp_path / "ff.bin"
        src.write_bytes(blob)
        server = runner.spawn([*PYNCAT_CMD, "-l", str(port)], close_stdin=True)
        assert wait_until_bound(port)
        client = runner.spawn(
            [*PYNCAT_CMD, "-N", "-w", "15", HOST, str(port)], stdin_path=src
        )
        assert client.wait(30) == 0
        server.wait(30)
        assert server.stdout_bytes == blob


class TestHalfClose:
    def test_N_konczy_transfer(self, runner, port) -> None:
        server = runner.spawn([*PYNCAT_CMD, "-l", str(port)], close_stdin=True)
        assert wait_until_bound(port)
        start = time.monotonic()
        client = runner.spawn(
            [*PYNCAT_CMD, "-N", HOST, str(port)], stdin_data=b"DANE"
        )
        assert client.wait(15) == 0
        elapsed = time.monotonic() - start
        assert elapsed < 10, f"z -N transfer ma się kończyć szybko, było {elapsed:.1f}s"
        assert server.wait(15) == 0
        assert server.stdout_bytes == b"DANE"

    def test_bez_N_klient_nie_konczy(self, runner, port) -> None:
        """Bez ``-N`` klient nie wysyła FIN-u, więc serwer nie wie, że to
        koniec — i nikt nikogo nie rozłącza. To nie jest błąd, to
        udokumentowane zachowanie netcata (i powód istnienia ``-N``)."""
        server = runner.spawn([*PYNCAT_CMD, "-l", str(port)], close_stdin=True)
        assert wait_until_bound(port)
        client = runner.spawn([*PYNCAT_CMD, HOST, str(port)], stdin_data=b"DANE")
        time.sleep(2.0)
        assert client.popen.poll() is None, "bez -N klient powinien wisieć"
        client.kill()
        server.kill()


class TestKodyWyjscia:
    """Tablica decyzyjna z docs/tablice-decyzyjne.md §4."""

    def test_z_port_otwarty(self, runner, port) -> None:
        server = runner.spawn([*PYNCAT_CMD, "-l", str(port)], close_stdin=True)
        assert wait_until_bound(port)
        scan = runner.spawn(
            [*PYNCAT_CMD, "-z", "-v", "-w", "5", "-n", HOST, str(port)],
            close_stdin=True,
        )
        assert scan.wait(20) == 0
        assert "udane" in scan.stderr_text
        server.kill()

    def test_z_port_zamkniety(self, runner) -> None:
        closed = free_port()
        scan = runner.spawn(
            [*PYNCAT_CMD, "-z", "-v", "-w", "5", "-n", HOST, str(closed)],
            close_stdin=True,
        )
        assert scan.wait(20) == 1

    def test_z_zakres_jeden_otwarty_daje_zero(self, runner) -> None:
        """Kwirk oryginału: nieudane połączenia nie resetują ``ret``.

        Wystarczy JEDEN otwarty port w zakresie, żeby proces skończył się
        z 0 — nawet jeśli pozostałe są zamknięte. Zweryfikowane na
        nc.openbsd 1.226 (GROUND_TRUTH §4).
        """
        base = free_port()
        server = runner.spawn([*PYNCAT_CMD, "-l", str(base)], close_stdin=True)
        assert wait_until_bound(base)
        scan = runner.spawn(
            [
                *PYNCAT_CMD,
                "-z",
                "-w",
                "5",
                "-n",
                HOST,
                f"{base}-{base + 2}",
            ],
            close_stdin=True,
        )
        assert scan.wait(30) == 0
        server.kill()

    def test_z_zakres_wszystkie_zamkniete(self, runner) -> None:
        base = free_port()
        scan = runner.spawn(
            [
                *PYNCAT_CMD,
                "-z",
                "-w",
                "3",
                "-n",
                HOST,
                f"{base}-{base + 2}",
            ],
            close_stdin=True,
        )
        assert scan.wait(30) == 1

    def test_blad_uzycia(self, runner) -> None:
        proc = runner.spawn([*PYNCAT_CMD], close_stdin=True)
        assert proc.wait(10) == 1
        assert "użycie" in proc.stderr_text

    def test_help_konczy_zerem(self, runner) -> None:
        proc = runner.spawn([*PYNCAT_CMD, "-h"], close_stdin=True)
        assert proc.wait(10) == 0
        assert b"OSTRZE" in proc.stdout_bytes

    def test_tryb_listen_zawsze_zero(self, runner, port) -> None:
        """[netcat.c:562] ``ret = 0`` od razu — serwer kończy z 0 nawet wtedy,
        gdy klient tylko się połączył i rozłączył."""
        server = runner.spawn(
            [*PYNCAT_CMD, "-l", "-w", "2", str(port)], close_stdin=True
        )
        assert wait_until_bound(port)
        with socket.create_connection((HOST, port), timeout=5) as s:
            s.shutdown(socket.SHUT_WR)
        assert server.wait(20) == 0


class TestSkanowaniePortow:
    def test_zakres_skanuje_wszystkie(self, runner) -> None:
        base = free_port()
        scan = runner.spawn(
            [
                *PYNCAT_CMD,
                "-z",
                "-v",
                "-w",
                "3",
                "-n",
                HOST,
                f"{base}-{base + 4}",
            ],
            close_stdin=True,
        )
        scan.wait(30)
        err = scan.stderr_text
        for p in range(base, base + 5):
            assert str(p) in err, f"port {p} nie został odwiedzony"

    def test_zakres_odwrotny_daje_te_same_porty(self, runner) -> None:
        base = free_port()
        hi = base + 3
        forward = runner.spawn(
            [*PYNCAT_CMD, "-z", "-v", "-w", "3", "-n", HOST, f"{base}-{hi}"],
            close_stdin=True,
        )
        forward.wait(30)
        reverse = runner.spawn(
            [*PYNCAT_CMD, "-z", "-v", "-w", "3", "-n", HOST, f"{hi}-{base}"],
            close_stdin=True,
        )
        reverse.wait(30)
        extract = lambda text: sorted(  # noqa: E731
            p for p in range(base, hi + 1) if str(p) in text
        )
        assert extract(forward.stderr_text) == extract(reverse.stderr_text)

    def test_i_opoznia_miedzy_portami(self, runner) -> None:
        """ODSTĘPSTWO od kodu upstream, udokumentowane w app.py.

        nc(1) obiecuje opóźnienie między portami, kod upstream go nie ma,
        a Debian 1.226 jest tu mierzalnym no-opem. pyncat implementuje to,
        co obiecuje dokumentacja — i ten test tego pilnuje.
        """
        base = free_port()
        spec = f"{base}-{base + 2}"  # 3 porty → 2 przerwy

        start = time.monotonic()
        fast = runner.spawn(
            [*PYNCAT_CMD, "-z", "-w", "2", "-n", HOST, spec], close_stdin=True
        )
        fast.wait(30)
        fast_elapsed = time.monotonic() - start

        start = time.monotonic()
        slow = runner.spawn(
            [*PYNCAT_CMD, "-z", "-w", "2", "-i", "1", "-n", HOST, spec],
            close_stdin=True,
        )
        slow.wait(30)
        slow_elapsed = time.monotonic() - start

        assert slow_elapsed - fast_elapsed >= 1.5, (
            f"-i 1 na 3 portach ma dodać ~2 s; bez={fast_elapsed:.2f}s "
            f"z={slow_elapsed:.2f}s"
        )


class TestTimeouty:
    def test_w_jako_timeout_connect(self, runner) -> None:
        """Zapchana kolejka ``accept()`` to jedyny pewny sposób wywołania
        timeoutu połączenia na loopbacku: przy pełnej kolejce jądro porzuca
        SYN-y (``tcp_abort_on_overflow=0``), więc ``connect()`` wisi.
        """
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((HOST, 0))
        port = srv.getsockname()[1]
        srv.listen(1)
        hogs = []
        try:
            for _ in range(12):
                c = socket.socket()
                c.settimeout(0.4)
                try:
                    c.connect((HOST, port))
                except OSError:
                    c.close()
                    break
                hogs.append(c)

            start = time.monotonic()
            scan = runner.spawn(
                [*PYNCAT_CMD, "-z", "-w", "2", "-n", HOST, str(port)],
                close_stdin=True,
            )
            assert scan.wait(30) == 1
            elapsed = time.monotonic() - start
            assert 1.5 <= elapsed <= 8.0, (
                f"-w 2 miało przerwać connect po ~2 s, było {elapsed:.2f} s"
            )
        finally:
            for h in hogs:
                h.close()
            srv.close()

    def test_w_jako_idle_timeout(self, runner, port) -> None:
        server = runner.spawn([*PYNCAT_CMD, "-l", str(port)], close_stdin=True)
        assert wait_until_bound(port)
        start = time.monotonic()
        client = runner.spawn(
            [*PYNCAT_CMD, "-d", "-w", "2", HOST, str(port)], close_stdin=True
        )
        client.wait(20)
        elapsed = time.monotonic() - start
        assert 1.5 <= elapsed <= 8.0, f"oczekiwano ~2 s, było {elapsed:.2f} s"
        server.kill()

    def test_q_zero_konczy_po_eof(self, runner, port) -> None:
        server = runner.spawn([*PYNCAT_CMD, "-l", str(port)], close_stdin=True)
        assert wait_until_bound(port)
        start = time.monotonic()
        client = runner.spawn(
            [*PYNCAT_CMD, "-q", "0", HOST, str(port)], stdin_data=b"X"
        )
        assert client.wait(20) == 0
        assert time.monotonic() - start < 6
        server.kill()

    def test_q_dodatni_czeka(self, runner, port) -> None:
        server = runner.spawn([*PYNCAT_CMD, "-l", str(port)], close_stdin=True)
        assert wait_until_bound(port)
        start = time.monotonic()
        client = runner.spawn(
            [*PYNCAT_CMD, "-q", "2", HOST, str(port)], stdin_data=b"X"
        )
        client.wait(25)
        elapsed = time.monotonic() - start
        assert elapsed >= 1.5, f"-q 2 miało czekać ~2 s, było {elapsed:.2f} s"
        server.kill()


class TestKeepOpen:
    def test_k_obsluguje_kolejne_polaczenia(self, runner, port) -> None:
        server = runner.spawn(
            [*PYNCAT_CMD, "-l", "-k", str(port)], close_stdin=True
        )
        assert wait_until_bound(port)
        for msg in (b"C1", b"C2", b"C3"):
            client = runner.spawn(
                [*PYNCAT_CMD, "-N", "-w", "5", HOST, str(port)], stdin_data=msg
            )
            assert client.wait(15) == 0
        time.sleep(0.5)
        server.kill()
        assert server.stdout_bytes == b"C1C2C3"

    def test_bez_k_serwer_konczy_po_pierwszym(self, runner, port) -> None:
        server = runner.spawn([*PYNCAT_CMD, "-l", str(port)], close_stdin=True)
        assert wait_until_bound(port)
        client = runner.spawn(
            [*PYNCAT_CMD, "-N", "-w", "5", HOST, str(port)], stdin_data=b"C1"
        )
        assert client.wait(15) == 0
        assert server.wait(15) == 0
        assert server.stdout_bytes == b"C1"


class TestUDP:
    def test_udp_transfer(self, runner, udp_port) -> None:
        server = runner.spawn(
            [*PYNCAT_CMD, "-u", "-l", str(udp_port)], close_stdin=True
        )
        assert wait_until_bound(udp_port, socket.SOCK_DGRAM)
        client = runner.spawn(
            [*PYNCAT_CMD, "-u", "-w", "3", HOST, str(udp_port)],
            stdin_data=b"DATAGRAM",
        )
        client.wait(15)
        time.sleep(0.6)
        server.kill()
        assert server.stdout_bytes == b"DATAGRAM"

    def test_udp_z_na_zamknietym_porcie(self, runner) -> None:
        """``udptest()``: cztery zapisy wymuszają ICMP port unreachable.

        Na loopbacku ICMP wraca natychmiast, więc test jest stabilny.
        Przez prawdziwą sieć (albo za firewallem tnącym ICMP) ten mechanizm
        jest zawodny — to ograniczenie oryginału, nie nasze.
        """
        closed = free_port(socket.SOCK_DGRAM)
        scan = runner.spawn(
            [*PYNCAT_CMD, "-u", "-z", "-v", "-w", "3", "-n", HOST, str(closed)],
            close_stdin=True,
        )
        assert scan.wait(20) == 1
        assert "udane" not in scan.stderr_text

    def test_udp_z_na_otwartym_porcie(self, runner, udp_port) -> None:
        server = runner.spawn(
            [*PYNCAT_CMD, "-u", "-l", str(udp_port)], close_stdin=True
        )
        assert wait_until_bound(udp_port, socket.SOCK_DGRAM)
        scan = runner.spawn(
            [
                *PYNCAT_CMD,
                "-u",
                "-z",
                "-v",
                "-w",
                "3",
                "-n",
                HOST,
                str(udp_port),
            ],
            close_stdin=True,
        )
        assert scan.wait(20) == 0
        assert "udane" in scan.stderr_text
        server.kill()


class TestRodzinaAdresow:
    def test_4_wymusza_ipv4(self, runner, port) -> None:
        server = runner.spawn(
            [*PYNCAT_CMD, "-4", "-l", str(port)], close_stdin=True
        )
        assert wait_until_bound(port)
        client = runner.spawn(
            [*PYNCAT_CMD, "-4", "-N", "-w", "5", HOST, str(port)],
            stdin_data=b"IPV4",
        )
        assert client.wait(15) == 0
        server.wait(15)
        assert server.stdout_bytes == b"IPV4"

    @requires_ipv6
    def test_6_wymusza_ipv6(self, runner, port) -> None:
        server = runner.spawn(
            [*PYNCAT_CMD, "-6", "-l", "::1", str(port)], close_stdin=True
        )
        assert wait_until_bound(port)
        client = runner.spawn(
            [*PYNCAT_CMD, "-6", "-N", "-w", "5", "::1", str(port)],
            stdin_data=b"IPV6",
        )
        assert client.wait(15) == 0
        server.wait(15)
        assert server.stdout_bytes == b"IPV6"


class TestAdresZrodlowy:
    def test_p_wymusza_port_zrodlowy(self, runner, port) -> None:
        src_port = free_port()
        server = runner.spawn(
            [*PYNCAT_CMD, "-l", "-v", str(port)], close_stdin=True
        )
        assert wait_until_bound(port)
        client = runner.spawn(
            [*PYNCAT_CMD, "-N", "-w", "5", "-p", str(src_port), HOST, str(port)],
            stdin_data=b"SRCPORT",
        )
        assert client.wait(15) == 0
        server.wait(15)
        assert server.stdout_bytes == b"SRCPORT"
        # -v na serwerze raportuje adres i port drugiej strony.
        assert str(src_port) in server.stderr_text

    def test_s_wymusza_adres_zrodlowy(self, runner, port) -> None:
        server = runner.spawn([*PYNCAT_CMD, "-l", str(port)], close_stdin=True)
        assert wait_until_bound(port)
        client = runner.spawn(
            [*PYNCAT_CMD, "-N", "-w", "5", "-s", HOST, HOST, str(port)],
            stdin_data=b"SRCADDR",
        )
        assert client.wait(15) == 0
        server.wait(15)
        assert server.stdout_bytes == b"SRCADDR"


class TestZeroIO:
    def test_z_nie_przesyla_danych(self, runner, port) -> None:
        """``-z`` ma tylko sprawdzić port. Dane ze stdin nie mogą polecieć."""
        server = runner.spawn([*PYNCAT_CMD, "-l", str(port)], close_stdin=True)
        assert wait_until_bound(port)
        scan = runner.spawn(
            [*PYNCAT_CMD, "-z", "-w", "5", "-n", HOST, str(port)],
            stdin_data=b"TEGO-NIE-WOLNO-WYSLAC",
        )
        assert scan.wait(20) == 0
        time.sleep(0.5)
        server.kill()
        assert server.stdout_bytes == b"", "-z nie ma prawa nic wysłać"


class TestWersjaIModul:
    def test_uruchomienie_przez_m(self) -> None:
        out = subprocess.run(
            [sys.executable, "-m", "pyncat", "-h"],
            capture_output=True,
            timeout=20,
        )
        assert out.returncode == 0
        assert b"pyncat" in out.stdout

    def test_brak_zaleznosci_zewnetrznych(self) -> None:
        """Pakiet ma stać na samej bibliotece standardowej."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "pyncat"
        stdlib_ok = {
            "dataclasses",
            "errno",
            "getopt",
            "os",
            "random",
            "re",
            "select",
            "signal",
            "socket",
            "sys",
            "threading",
            "time",
            "typing",
            "__future__",
        }
        import ast

        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        assert top in stdlib_ok, f"{path.name}: import {top}"
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    if node.module is None:  # pragma: no cover
                        continue
                    top = node.module.split(".")[0]
                    assert top in stdlib_ok, f"{path.name}: from {top}"
