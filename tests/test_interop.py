"""Testy interoperacyjności: pyncat ↔ OpenBSD netcat ↔ wersja w C.

To jest najmocniejszy rodzaj testu w tym repozytorium. Testy jednostkowe
sprawdzają, czy kod robi to, co sobie wyobraziliśmy; te sprawdzają, czy robi
to, co robi **prawdziwy netcat** — po obu stronach połączenia i w obu
kierunkach transferu.

Są pomijane, gdy w systemie nie ma ``netcat-openbsd`` albo gdy nie zbudowano
wersji w C (``make -C c_reference``). CI gwarantuje, że oba są obecne, więc
pominięcie na maszynie deweloperskiej nie ukryje regresji.

Uwaga na wariant netcata: ``nc`` z busyboxa i ``netcat-traditional`` mają inne
flagi i inną semantykę half-close. ``conftest.find_system_nc`` sprawdza
sygnaturę „OpenBSD netcat" w ``-h``, więc testy nie puszczą się przeciwko
czemuś niezgodnemu.
"""

from __future__ import annotations

import socket
import time

from conftest import (
    C_REF_BIN,
    HOST,
    PYNCAT_CMD,
    SYSTEM_NC,
    binary_blob,
    free_port,
    requires_c_ref,
    requires_system_nc,
    sha256,
    wait_until_bound,
)

#: Rozmiar transferu w testach interop. 1 MiB wymusza wiele iteracji pętli
#: (BUFSIZE = 16 KiB → ≥ 64 przejścia) i ujawnia błędy w obsłudze zapisów
#: częściowych, których transfer kilkubajtowy nie pokaże.
BLOB_SIZE = 1 << 20


def _nc(*args: str) -> list[str]:
    assert SYSTEM_NC is not None
    return [SYSTEM_NC, *args]


def _cref(*args: str) -> list[str]:
    return [str(C_REF_BIN), *args]


def _transfer(
    runner,
    tmp_path,
    server_argv: list[str],
    client_argv_factory,
    port: int,
) -> None:
    """Wspólny szkielet: serwer nasłuchuje, klient wysyła blob, porównujemy sha256."""
    blob = binary_blob(BLOB_SIZE)
    src = tmp_path / "blob.bin"
    src.write_bytes(blob)

    server = runner.spawn(server_argv, close_stdin=True)
    assert wait_until_bound(port), f"serwer {server_argv[:3]} nie zbindował portu"

    client = runner.spawn(client_argv_factory(), stdin_path=src)
    assert client.wait(60) == 0, f"klient zwrócił błąd: {client.stderr_text}"
    server.wait(60)
    assert sha256(server.stdout_bytes) == sha256(blob), (
        f"dane się nie zgadzają: {len(server.stdout_bytes)} B zamiast {len(blob)} B"
    )


@requires_system_nc
class TestPyncatZNetcatem:
    def test_pyncat_klient_nc_serwer(self, runner, tmp_path) -> None:
        port = free_port()
        _transfer(
            runner,
            tmp_path,
            _nc("-l", "-p", str(port)),
            lambda: [*PYNCAT_CMD, "-N", "-w", "20", HOST, str(port)],
            port,
        )

    def test_nc_klient_pyncat_serwer(self, runner, tmp_path) -> None:
        port = free_port()
        _transfer(
            runner,
            tmp_path,
            [*PYNCAT_CMD, "-l", str(port)],
            lambda: _nc("-N", "-w", "20", HOST, str(port)),
            port,
        )

    def test_dwukierunkowo_pyncat_klient(self, runner) -> None:
        port = free_port()
        server = runner.spawn(
            _nc("-l", "-p", str(port)), stdin_data=b"OD-NETCATA"
        )
        assert wait_until_bound(port)
        client = runner.spawn(
            [*PYNCAT_CMD, "-N", "-w", "15", HOST, str(port)],
            stdin_data=b"OD-PYNCATA",
        )
        assert client.wait(30) == 0
        server.wait(30)
        assert client.stdout_bytes == b"OD-NETCATA"
        assert server.stdout_bytes == b"OD-PYNCATA"

    def test_dwukierunkowo_nc_klient(self, runner) -> None:
        port = free_port()
        server = runner.spawn(
            [*PYNCAT_CMD, "-l", str(port)], stdin_data=b"OD-PYNCATA"
        )
        assert wait_until_bound(port)
        client = runner.spawn(
            _nc("-N", "-w", "15", HOST, str(port)), stdin_data=b"OD-NETCATA"
        )
        assert client.wait(30) == 0
        server.wait(30)
        assert client.stdout_bytes == b"OD-PYNCATA"
        assert server.stdout_bytes == b"OD-NETCATA"

    def test_nc_skanuje_pyncatowy_listener(self, runner) -> None:
        port = free_port()
        server = runner.spawn([*PYNCAT_CMD, "-l", str(port)], close_stdin=True)
        assert wait_until_bound(port)
        scan = runner.spawn(
            _nc("-z", "-v", "-w", "5", HOST, str(port)), close_stdin=True
        )
        assert scan.wait(20) == 0
        assert "succeeded" in scan.stderr_text
        server.kill()

    def test_pyncat_skanuje_netcatowy_listener(self, runner) -> None:
        port = free_port()
        server = runner.spawn(_nc("-l", "-p", str(port)), close_stdin=True)
        assert wait_until_bound(port)
        scan = runner.spawn(
            [*PYNCAT_CMD, "-z", "-v", "-w", "5", "-n", HOST, str(port)],
            close_stdin=True,
        )
        assert scan.wait(20) == 0
        assert "udane" in scan.stderr_text
        server.kill()

    def test_udp_pyncat_do_nc(self, runner) -> None:
        port = free_port(socket.SOCK_DGRAM)
        server = runner.spawn(_nc("-u", "-l", "-p", str(port)), close_stdin=True)
        assert wait_until_bound(port, socket.SOCK_DGRAM)
        client = runner.spawn(
            [*PYNCAT_CMD, "-u", "-w", "3", HOST, str(port)],
            stdin_data=b"UDP-OD-PYNCATA",
        )
        client.wait(15)
        time.sleep(0.6)
        server.kill()
        assert server.stdout_bytes == b"UDP-OD-PYNCATA"

    def test_udp_nc_do_pyncat(self, runner) -> None:
        port = free_port(socket.SOCK_DGRAM)
        server = runner.spawn(
            [*PYNCAT_CMD, "-u", "-l", str(port)], close_stdin=True
        )
        assert wait_until_bound(port, socket.SOCK_DGRAM)
        client = runner.spawn(
            _nc("-u", "-w", "3", HOST, str(port)), stdin_data=b"UDP-OD-NC"
        )
        client.wait(15)
        time.sleep(0.6)
        server.kill()
        assert server.stdout_bytes == b"UDP-OD-NC"

    def test_zgodnosc_kodow_wyjscia_na_zamknietym_porcie(self, runner) -> None:
        """Oba programy muszą zwrócić ten sam kod dla tej samej sytuacji."""
        closed = free_port()
        a = runner.spawn(
            [*PYNCAT_CMD, "-z", "-w", "3", "-n", HOST, str(closed)],
            close_stdin=True,
        )
        b = runner.spawn(
            _nc("-z", "-w", "3", HOST, str(closed)), close_stdin=True
        )
        assert a.wait(20) == b.wait(20) == 1

    def test_zgodnosc_kodow_wyjscia_zakres_mieszany(self, runner) -> None:
        """Kwirk „raz 0, już 0" musi zachowywać się identycznie w obu."""
        base = free_port()
        server = runner.spawn([*PYNCAT_CMD, "-l", str(base)], close_stdin=True)
        assert wait_until_bound(base)
        spec = f"{base}-{base + 2}"
        a = runner.spawn(
            [*PYNCAT_CMD, "-z", "-w", "3", "-n", HOST, spec], close_stdin=True
        )
        rc_pyncat = a.wait(30)
        server.kill()

        server2 = runner.spawn(_nc("-l", "-p", str(base)), close_stdin=True)
        assert wait_until_bound(base)
        b = runner.spawn(_nc("-z", "-w", "3", HOST, spec), close_stdin=True)
        rc_nc = b.wait(30)
        server2.kill()

        assert rc_pyncat == rc_nc == 0


@requires_c_ref
class TestPyncatZWersjaC:
    """Porównanie „jabłka do jabłek": ten sam algorytm, dwa języki."""

    def test_pyncat_klient_c_serwer(self, runner, tmp_path) -> None:
        port = free_port()
        _transfer(
            runner,
            tmp_path,
            _cref("-l", str(port)),
            lambda: [*PYNCAT_CMD, "-N", "-w", "20", HOST, str(port)],
            port,
        )

    def test_c_klient_pyncat_serwer(self, runner, tmp_path) -> None:
        port = free_port()
        _transfer(
            runner,
            tmp_path,
            [*PYNCAT_CMD, "-l", str(port)],
            lambda: _cref("-N", "-w", "20", HOST, str(port)),
            port,
        )

    def test_c_z_c(self, runner, tmp_path) -> None:
        """Wersja C sama ze sobą — kontrola, że ewentualny błąd nie jest
        po stronie Pythona."""
        port = free_port()
        _transfer(
            runner,
            tmp_path,
            _cref("-l", str(port)),
            lambda: _cref("-N", "-w", "20", HOST, str(port)),
            port,
        )

    def test_dwukierunkowo(self, runner) -> None:
        port = free_port()
        server = runner.spawn(_cref("-l", str(port)), stdin_data=b"OD-WERSJI-C")
        assert wait_until_bound(port)
        client = runner.spawn(
            [*PYNCAT_CMD, "-N", "-w", "15", HOST, str(port)],
            stdin_data=b"OD-PYNCATA",
        )
        assert client.wait(30) == 0
        server.wait(30)
        assert client.stdout_bytes == b"OD-WERSJI-C"
        assert server.stdout_bytes == b"OD-PYNCATA"

    def test_zgodnosc_kodow_wyjscia(self, runner) -> None:
        closed = free_port()
        a = runner.spawn(
            [*PYNCAT_CMD, "-z", "-w", "3", "-n", HOST, str(closed)],
            close_stdin=True,
        )
        b = runner.spawn(_cref("-z", "-w", "3", "-n", HOST, str(closed)), close_stdin=True)
        assert a.wait(20) == b.wait(20) == 1

    def test_zgodnosc_half_close(self, runner) -> None:
        """Bez ``-N`` obie implementacje muszą wisieć — to ta sama semantyka,
        nie przypadek."""
        for cmd in ([*PYNCAT_CMD], [str(C_REF_BIN)]):
            port = free_port()
            server = runner.spawn(
                [*PYNCAT_CMD, "-l", str(port)], close_stdin=True
            )
            assert wait_until_bound(port)
            client = runner.spawn([*cmd, HOST, str(port)], stdin_data=b"DANE")
            time.sleep(1.5)
            still_running = client.popen.poll() is None
            client.kill()
            server.kill()
            assert still_running, f"{cmd[0]} bez -N nie powinien zakończyć się"

    @requires_system_nc
    def test_c_z_netcatem(self, runner, tmp_path) -> None:
        port = free_port()
        _transfer(
            runner,
            tmp_path,
            _nc("-l", "-p", str(port)),
            lambda: _cref("-N", "-w", "20", HOST, str(port)),
            port,
        )


@requires_system_nc
class TestRozbieznosciUdokumentowane:
    """Miejsca, w których pyncat ŚWIADOMIE różni się od Debianowego nc.

    Te testy pilnują, żeby rozbieżność pozostała taka, jaką opisaliśmy
    w dokumentacji — a nie zmieniła się po cichu.
    """

    def test_l_z_p_pyncat_odrzuca_debian_przyjmuje(self, runner) -> None:
        """Upstream [netcat.c:428-429] zabrania ``-p`` razem z ``-l``.
        Debian to dopuszcza (``nc -l -p 1234`` jest tam idiomem).
        pyncat trzyma się upstreamu."""
        port = free_port()
        bad = runner.spawn(
            [*PYNCAT_CMD, "-l", "-p", str(port), str(port)], close_stdin=True
        )
        assert bad.wait(10) == 1
        assert "-p" in bad.stderr_text

        ok = runner.spawn(_nc("-l", "-p", str(port)), close_stdin=True)
        assert wait_until_bound(port), "Debianowy nc przyjmuje -l -p"
        ok.kill()

    def test_i_pyncat_opoznia_nc_nie(self, runner) -> None:
        """``-i`` między portami: nc(1) to obiecuje, Debian 1.226 tego nie
        robi (zmierzone), pyncat robi. Udokumentowane w app.py i docs."""
        base = free_port()
        spec = f"{base}-{base + 2}"

        start = time.monotonic()
        p = runner.spawn(
            [*PYNCAT_CMD, "-z", "-w", "2", "-i", "1", "-n", HOST, spec],
            close_stdin=True,
        )
        p.wait(30)
        pyncat_elapsed = time.monotonic() - start

        start = time.monotonic()
        n = runner.spawn(
            _nc("-z", "-w", "2", "-i", "1", HOST, spec), close_stdin=True
        )
        n.wait(30)
        nc_elapsed = time.monotonic() - start

        assert pyncat_elapsed >= 1.5, (
            f"pyncat -i 1 miał opóźnić skan, było {pyncat_elapsed:.2f} s"
        )
        assert nc_elapsed < 1.0, (
            f"Debianowy nc -i jest no-opem; jeśli ten assert padł, wersja nc "
            f"się zmieniła i dokumentację trzeba poprawić "
            f"(było {nc_elapsed:.2f} s)"
        )
