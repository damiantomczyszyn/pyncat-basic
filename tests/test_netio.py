"""Testy ``fillbuf()``/``drainbuf()``. Odpowiada ``netcat.c`` 1305–1367.

Najważniejszy test w tym pliku to ``test_drainbuf_zapis_czesciowy``: zapis
częściowy jest normalną sytuacją, a nie błędem, i implementacja, która tego
nie obsłuży, cicho gubi dane przy większych transferach.
"""

from __future__ import annotations

import os

import pytest

from pyncat.constants import BUFSIZE, IO_EOF, IO_ERROR, WANT_POLLIN, WANT_POLLOUT
from pyncat.netio import drainbuf, fillbuf


@pytest.fixture
def pipe():
    r, w = os.pipe()
    try:
        yield r, w
    finally:
        for fd in (r, w):
            try:
                os.close(fd)
            except OSError:
                pass


class TestFillbuf:
    def test_czyta_dane(self, pipe) -> None:
        r, w = pipe
        os.write(w, b"abc")
        buf = bytearray()
        assert fillbuf(r, buf) == 3
        assert buf == b"abc"

    def test_dopisuje_do_istniejacych(self, pipe) -> None:
        r, w = pipe
        os.write(w, b"def")
        buf = bytearray(b"abc")
        assert fillbuf(r, buf) == 3
        assert buf == b"abcdef"

    def test_eof(self, pipe) -> None:
        r, w = pipe
        os.close(w)
        buf = bytearray()
        assert fillbuf(r, buf) == IO_EOF
        assert buf == b""

    def test_wycofany_slot(self) -> None:
        """[netcat.c:1342-1343] ``fd == -1`` → ``-1`` bez syscalla."""
        assert fillbuf(-1, bytearray()) == IO_ERROR

    def test_eagain(self, pipe) -> None:
        r, _w = pipe
        os.set_blocking(r, False)
        assert fillbuf(r, bytearray()) == WANT_POLLIN

    def test_zly_deskryptor(self) -> None:
        assert fillbuf(9999, bytearray()) == IO_ERROR

    def test_nie_przekracza_bufsize(self, pipe) -> None:
        """[netcat.c:1339] ``num = BUFSIZE - *bufpos``.

        Bufor nie ma prawa rosnąć powyżej BUFSIZE, bo to on realizuje
        backpressure — rosnący bufor oznaczałby zjadanie pamięci przy
        szybkim producencie i wolnym konsumencie.
        """
        r, w = pipe
        os.set_blocking(w, False)
        # Napełniamy rurę do oporu; jej pojemność (64 KiB) > BUFSIZE (16 KiB).
        try:
            while True:
                os.write(w, b"x" * 4096)
        except BlockingIOError:
            pass
        buf = bytearray(b"y" * (BUFSIZE - 10))
        n = fillbuf(r, buf)
        assert n == 10
        assert len(buf) == BUFSIZE

    def test_bajty_binarne_bez_zmian(self, pipe) -> None:
        r, w = pipe
        payload = bytes(range(256))
        os.write(w, payload)
        buf = bytearray()
        fillbuf(r, buf)
        assert bytes(buf) == payload


class TestDrainbuf:
    def test_pisze_wszystko(self, pipe) -> None:
        r, w = pipe
        buf = bytearray(b"abc")
        assert drainbuf(w, buf) == 3
        assert buf == b""
        assert os.read(r, 10) == b"abc"

    def test_wycofany_slot(self) -> None:
        assert drainbuf(-1, bytearray(b"x")) == IO_ERROR

    def test_zapis_czesciowy(self, pipe) -> None:
        """[netcat.c:1328-1332] ``memmove(buf, buf + n, adjust)``.

        Rura ma 64 KiB; wpychamy 1 MiB na nieblokujący deskryptor, więc
        ``write()`` przyjmie tylko część. Z bufora musi zniknąć dokładnie
        tyle, ile poszło, a reszta ma zostać na początku.
        """
        _r, w = pipe
        os.set_blocking(w, False)
        total = 1 << 20
        payload = bytes(range(256)) * (total // 256)
        buf = bytearray(payload)
        n = drainbuf(w, buf)
        assert 0 < n < total, f"oczekiwano zapisu częściowego, poszło {n}"
        assert len(buf) == total - n
        # Reszta musi być ogonem oryginału, nie przesuniętą o jeden bajt kopią.
        assert bytes(buf) == payload[n:]

    def test_eagain_gdy_rura_pelna(self, pipe) -> None:
        _r, w = pipe
        os.set_blocking(w, False)
        try:
            while True:
                os.write(w, b"x" * 4096)
        except BlockingIOError:
            pass
        buf = bytearray(b"y")
        assert drainbuf(w, buf) == WANT_POLLOUT
        assert buf == b"y", "bufor nie może się zmienić, gdy nic nie poszło"

    def test_zamknieta_rura_to_blad(self, pipe) -> None:
        """EPIPE → ``IO_ERROR``. Python zamienia SIGPIPE na ``BrokenPipeError``,
        więc gałąź jest osiągalna — w C to ``errno == EPIPE``."""
        r, w = pipe
        os.close(r)
        assert drainbuf(w, bytearray(b"x")) == IO_ERROR

    def test_zly_deskryptor(self) -> None:
        assert drainbuf(9999, bytearray(b"x")) == IO_ERROR


class TestRoundtrip:
    def test_pelny_transfer_przez_rure(self, pipe) -> None:
        """Pętla fillbuf/drainbuf przenosi 256 KiB bez zmiany ani jednego bajtu."""
        r, w = pipe
        os.set_blocking(r, False)
        os.set_blocking(w, False)

        source = bytes(range(256)) * 1024  # 256 KiB
        sink = bytearray()
        pending = bytearray()
        offset = 0

        for _ in range(100_000):
            if len(pending) < BUFSIZE and offset < len(source):
                take = min(BUFSIZE - len(pending), 4096)
                pending += source[offset : offset + take]
                offset += take
            if pending:
                drainbuf(w, pending)
            got = bytearray()
            if fillbuf(r, got) > 0:
                sink += got
            if offset >= len(source) and not pending and len(sink) == len(source):
                break

        assert bytes(sink) == source
