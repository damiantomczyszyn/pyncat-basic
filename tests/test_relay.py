"""Testy maszyny stanów ``readwrite()``. Odpowiada ``netcat.c`` 1104–1303.

Te testy odpalają pętlę relay **w wątku, na parze ``socketpair()``**, bez
podprocesów. Dzięki temu można sprawdzić rzeczy, których z zewnątrz nie widać:
czy po EOF na stdin poleciał FIN, czy pętla jeszcze żyje, i po ilu odczytach
zamknęła odbiór.

Każdy test odpowiada jednemu warunkowi albo jednej gałęzi z oryginału —
numery linii w docstringach.
"""

from __future__ import annotations

import socket
import time

import pytest
from conftest import make_relay

from pyncat.cli import Options
from pyncat.constants import BUFSIZE


@pytest.fixture
def harness_factory():
    created = []

    def make(**kwargs):
        opts = Options(host="h", uport="1", **kwargs)
        h = make_relay(opts)
        created.append(h)
        return h

    try:
        yield make
    finally:
        for h in created:
            try:
                h.peer.close()
            except OSError:
                pass
            if h.stdin_w != -1:
                try:
                    import os

                    os.close(h.stdin_w)
                except OSError:
                    pass
            h.thread.join(timeout=3)


class TestTransfer:
    """[netcat.c:1201-1289] Cztery bloki I/O."""

    def test_stdin_do_sieci(self, harness_factory) -> None:
        h = harness_factory()
        h.feed_stdin(b"ZAPYTANIE")
        assert h.recv_peer(9) == b"ZAPYTANIE"

    def test_siec_do_stdout(self, harness_factory) -> None:
        h = harness_factory()
        h.send(b"ODPOWIEDZ")
        assert h.read_stdout(9) == b"ODPOWIEDZ"

    def test_dwukierunkowo_jednoczesnie(self, harness_factory) -> None:
        h = harness_factory()
        h.feed_stdin(b"DO-SIECI")
        h.send(b"ZE-SIECI")
        assert h.recv_peer(8) == b"DO-SIECI"
        assert h.read_stdout(8) == b"ZE-SIECI"

    def test_dane_binarne_przechodza_bez_zmian(self, harness_factory) -> None:
        """Zera, bajty > 0x7f, CR, LF, 0x1a — wszystko musi przejść."""
        h = harness_factory()
        payload = bytes(range(256))
        h.send(payload)
        assert h.read_stdout(256) == payload

    def test_wiekszy_transfer_przez_bufor(self, harness_factory) -> None:
        """Więcej niż BUFSIZE: wymusza wiele iteracji pętli i opróżnianie
        bufora przy ``len(buf) == BUFSIZE`` (backpressure)."""
        h = harness_factory()
        payload = bytes(range(256)) * (4 * BUFSIZE // 256)  # 4x BUFSIZE
        h.peer.setblocking(True)

        import threading

        def feeder() -> None:
            h.peer.sendall(payload)

        t = threading.Thread(target=feeder, daemon=True)
        t.start()
        got = h.read_stdout(len(payload), timeout=20)
        t.join(timeout=5)
        assert got == payload


class TestHalfClose:
    """[netcat.c:1290-1296] Propagacja EOF — najważniejsza część pętli."""

    def test_bez_N_nie_wysyla_fin(self, harness_factory) -> None:
        """Bez ``-N`` netcat po EOF na stdin tylko przestaje pisać.

        Druga strona NIE dostaje FIN-u, więc nie wie, że nadawanie się
        skończyło. To jest powód, dla którego ``nc host port < plik`` bez
        ``-N`` nigdy się nie kończy — pomiar: 7,701 s do zabicia
        (GROUND_TRUTH §2.6).
        """
        h = harness_factory()
        h.feed_stdin(b"DANE")
        assert h.recv_peer(4) == b"DANE"
        h.close_stdin()
        # Peer nie powinien zobaczyć EOF-u.
        h.peer.settimeout(1.0)
        with pytest.raises((TimeoutError, socket.timeout)):
            h.peer.recv(1)
        # ...a pętla ma dalej żyć, bo czeka na dane z sieci.
        assert not h.join(timeout=0.5), "pętla nie powinna się zakończyć"

    def test_z_N_wysyla_fin(self, harness_factory) -> None:
        """[netcat.c:1293-1294] ``-N`` → ``shutdown(SHUT_WR)``.

        To ten shutdown kończy transfer pliku: druga strona dostaje EOF,
        zamyka połączenie, a my dostajemy EOF z sieci i wychodzimy.
        """
        h = harness_factory(Nflag=True)
        h.feed_stdin(b"DANE")
        assert h.recv_peer(4) == b"DANE"
        h.close_stdin()
        assert h.peer_eof(timeout=5), "peer powinien dostać FIN"

    def test_z_N_petla_konczy_po_zamknieciu_przez_peera(
        self, harness_factory
    ) -> None:
        """Pełny cykl: EOF na stdin → FIN → peer zamyka → warunek 1."""
        h = harness_factory(Nflag=True)
        h.close_stdin()
        assert h.peer_eof(timeout=5)
        h.peer.close()
        assert h.join(timeout=5), "pętla powinna się zakończyć"

    def test_eof_z_sieci_nie_konczy_gdy_stdin_otwarty(
        self, harness_factory
    ) -> None:
        """[netcat.c:1137-1140] Warunek 1 wymaga obu wejść martwych.

        Klient, któremu serwer zamknął połączenie, ale który ma jeszcze
        otwarty stdin, **nie wychodzi**. Zaskakujące, ale to zachowanie
        oryginału — zweryfikowane na nc.openbsd 1.226.
        """
        h = harness_factory()
        h.close_peer_write()
        assert not h.join(timeout=1.0), "bez -l pętla ma czekać na stdin"

    def test_lflag_konczy_po_eof_z_sieci(self, harness_factory) -> None:
        """[netcat.c:1144-1147] Warunek 3 — i po co istnieje.

        Z ``-l`` ta sama sytuacja kończy pętlę: serwer nie ma po co czekać
        na swój stdin, gdy klient się rozłączył. Bez tego warunku ``nc -l``
        wisiałby po każdym rozłączeniu, zamiast wrócić do ``accept()``.
        """
        h = harness_factory(lflag=True)
        h.close_peer_write()
        assert h.join(timeout=5), "z -l pętla powinna się zakończyć"

    def test_lflag_oddaje_zbuforowane_dane_przed_wyjsciem(
        self, harness_factory
    ) -> None:
        """Warunek 3 wymaga pustych buforów — dane w locie nie mogą przepaść.

        Jednocześnie testuje „POLLHUP razem z POLLIN nie zamyka wejścia"
        [netcat.c:1172-1181]: peer wysyła dane i od razu zamyka, więc jądro
        zgłasza oba zdarzenia naraz.
        """
        h = harness_factory(lflag=True)
        h.send(b"OSTATNIE-DANE")
        h.close_peer_write()
        assert h.read_stdout(13, timeout=5) == b"OSTATNIE-DANE"
        assert h.join(timeout=5)


class TestDetach:
    """[netcat.c:1116-1118] ``-d``"""

    def test_d_ignoruje_stdin(self, harness_factory) -> None:
        h = harness_factory(dflag=True)
        h.feed_stdin(b"TO-NIE-POWINNO-POJSC")
        h.peer.settimeout(1.0)
        with pytest.raises((TimeoutError, socket.timeout)):
            h.peer.recv(1)

    def test_d_nadal_odbiera_z_sieci(self, harness_factory) -> None:
        h = harness_factory(dflag=True)
        h.send(b"ODBIOR-DZIALA")
        assert h.read_stdout(13) == b"ODBIOR-DZIALA"

    def test_d_konczy_po_zamknieciu_przez_peera(self, harness_factory) -> None:
        """Z ``-d`` stdin jest martwy od startu, więc EOF z sieci wystarczy
        do spełnienia warunku 1 — bez ``-l`` i bez ``-N``."""
        h = harness_factory(dflag=True)
        h.close_peer_write()
        assert h.join(timeout=5)


class TestRecvLimit:
    """[netcat.c:1256-1261] ``-W``"""

    def test_W_konczy_po_dwoch_odczytach(self, harness_factory) -> None:
        """Licznik zwiększa się raz na iterację pętli, w której sieć była
        czytelna — a nie raz na pakiet. Każde ``send`` jest tu odczytywane
        osobno, bo czekamy na stdout między wysyłkami."""
        h = harness_factory(recvlimit=2)
        h.send(b"A")
        assert h.read_stdout(1) == b"A"
        h.send(b"B")
        assert h.read_stdout(1) == b"B"
        # Po drugim odczycie pętla zamyka odbiór i stdin → wychodzi.
        assert h.join(timeout=5), "pętla powinna wyjść po limicie -W"
        h.try_send(b"C")
        assert h.read_stdout(1, timeout=0.5) == b"", "C nie miało dojść"

    def test_bez_W_ta_sama_sekwencja_nie_konczy_petli(
        self, harness_factory
    ) -> None:
        """Kontrola dla testu wyżej: to ``-W``, a nie cokolwiek innego,
        kończy pętlę po dwóch odczytach."""
        h = harness_factory()
        h.send(b"A")
        assert h.read_stdout(1) == b"A"
        h.send(b"B")
        assert h.read_stdout(1) == b"B"
        assert not h.join(timeout=0.7), "bez -W pętla ma dalej działać"
        h.send(b"C")
        assert h.read_stdout(1, timeout=3) == b"C"

    def test_W_jeden(self, harness_factory) -> None:
        h = harness_factory(recvlimit=1)
        h.send(b"PIERWSZE")
        assert h.read_stdout(8) == b"PIERWSZE"
        assert h.join(timeout=5)


class TestQuitTimer:
    """Łatka Debiana ``-q``. Brak w upstream — GROUND_TRUTH §8."""

    def test_q_zero_konczy_natychmiast_po_eof(self, harness_factory) -> None:
        h = harness_factory(qflag=0)
        h.close_stdin()
        start = time.monotonic()
        assert h.join(timeout=5), "-q 0 ma zakończyć pętlę od razu"
        assert time.monotonic() - start < 1.0

    def test_q_zero_gubi_dane_wyslane_pozniej(self, harness_factory) -> None:
        """Cena ``-q 0``: odpowiedź, która przyjdzie po EOF, przepada.
        Zmierzone na nc.openbsd: ``-q 0`` nie odebrał ``DELAYED``."""
        h = harness_factory(qflag=0)
        h.close_stdin()
        assert h.join(timeout=5)
        h.try_send(b"POZNO")
        assert h.read_stdout(5, timeout=0.5) == b""

    def test_q_dodatni_czeka_zadany_czas(self, harness_factory) -> None:
        h = harness_factory(qflag=1)
        h.close_stdin()
        start = time.monotonic()
        assert h.join(timeout=6)
        elapsed = time.monotonic() - start
        assert 0.8 <= elapsed <= 3.0, f"oczekiwano ~1 s, było {elapsed:.3f} s"

    def test_q_dodatni_przepuszcza_dane_w_okienku(self, harness_factory) -> None:
        h = harness_factory(qflag=2)
        h.close_stdin()
        time.sleep(0.4)
        h.send(b"W-OKIENKU")
        assert h.read_stdout(9, timeout=3) == b"W-OKIENKU"
        assert h.join(timeout=6)

    def test_q_ujemny_czeka_bez_konca(self, harness_factory) -> None:
        """``-q -1`` ≡ brak flagi. Zmierzone: oba czekały do zabicia."""
        h = harness_factory(qflag=-1)
        h.close_stdin()
        assert not h.join(timeout=1.5), "-q -1 nie ma kończyć po EOF na stdin"


class TestTimeout:
    """[netcat.c:1155-1163] ``-w`` jako idle timeout."""

    def test_w_konczy_bezczynna_petle(self, harness_factory) -> None:
        h = harness_factory(timeout_ms=700, dflag=True)
        start = time.monotonic()
        assert h.join(timeout=5), "-w powinno zakończyć bezczynną pętlę"
        elapsed = time.monotonic() - start
        assert 0.4 <= elapsed <= 3.0, f"oczekiwano ~0,7 s, było {elapsed:.3f} s"

    def test_w_nie_przerywa_aktywnego_transferu(self, harness_factory) -> None:
        """Timeout jest liczony od ostatniego zdarzenia, nie od startu."""
        h = harness_factory(timeout_ms=1500, dflag=True)
        for i in range(4):
            h.send(bytes([65 + i]))
            assert h.read_stdout(1, timeout=3) == bytes([65 + i])
            time.sleep(0.5)
        assert not h.join(timeout=0.2), "pętla nie powinna jeszcze wyjść"
