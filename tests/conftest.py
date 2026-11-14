"""Infrastruktura testów: alokacja portów, uruchamianie procesów, harness relay.

Zasady, które te testy starają się trzymać:

* **Żadnego ``sleep(1)`` jako synchronizacji.** Gotowość listenera sprawdzamy
  przez próbę zbindowania portu — jeśli ``bind()`` się nie udaje, ktoś już na
  nim siedzi. Próba *połączenia* nie nadaje się do tego celu, bo listener bez
  ``-k`` obsługuje dokładnie jedno połączenie i probe by je zużył.
* **Dane sprawdzamy bajt w bajt** (``sha256``), nie po długości.
* Każdy test dostaje własny port z puli, żeby testy dały się puszczać równolegle.
"""

from __future__ import annotations

import hashlib
import os
import select
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Wywołanie pyncata jako podprocesu. ``-u`` (unbuffered) nie jest potrzebne,
#: bo pyncat pisze przez ``os.write`` i nie buforuje stdout.
PYNCAT_CMD = [sys.executable, "-m", "pyncat"]

#: Binarka referencyjna w C. Testy jej wymagające są pomijane, gdy nie została
#: zbudowana (``make -C c_reference``).
C_REF_BIN = REPO_ROOT / "c_reference" / "pyncat_ref"

HOST = "127.0.0.1"


def find_system_nc() -> str | None:
    """Ścieżka do OpenBSD netcata, jeśli jest w systemie.

    Rozróżniamy warianty: tylko ``netcat-openbsd`` ma semantykę, którą
    odwzorowujemy. ``nc`` z busyboxa albo ``netcat-traditional`` mają inne
    flagi i inne zachowanie half-close, więc testy interop muszą je pominąć,
    a nie oblewać.
    """
    for candidate in ("/usr/bin/nc.openbsd", "/usr/bin/nc", "/bin/nc"):
        if not os.path.exists(candidate):
            continue
        try:
            out = subprocess.run(
                [candidate, "-h"], capture_output=True, timeout=5, text=True
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            continue
        combined = (out.stdout or "") + (out.stderr or "")
        if "OpenBSD netcat" in combined:
            return candidate
    return None


SYSTEM_NC = find_system_nc()

requires_system_nc = pytest.mark.skipif(
    SYSTEM_NC is None, reason="brak netcat-openbsd w systemie"
)
requires_c_ref = pytest.mark.skipif(
    not C_REF_BIN.exists(),
    reason="brak c_reference/pyncat_ref — zbuduj przez `make -C c_reference`",
)


# --------------------------------------------------------------------------
# Porty
# --------------------------------------------------------------------------


def free_port(kind: int = socket.SOCK_STREAM, host: str = HOST) -> int:
    """Port, który w tej chwili jest wolny.

    Klasyczny wyścig (ktoś może zająć port między ``close()`` a startem
    serwera) jest tu nieunikniony, bo netcat nie umie bindować portu 0 —
    nie miałby jak zgłosić, który port dostał. W praktyce wystarczające,
    bo testy używają portów efemerycznych, których nikt inny nie szuka.

    ``host`` wyznacza rodzinę adresów. IPv4 i IPv6 mają **osobne przestrzenie
    portów**, więc port wolny na ``127.0.0.1`` nie musi być wolny na ``::1``.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, kind) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def wait_until_bound(
    port: int,
    kind: int = socket.SOCK_STREAM,
    timeout: float = 15.0,
    host: str = HOST,
) -> bool:
    """Czeka, aż port zostanie zajęty przez listener.

    Celowo **nie** ustawiamy ``SO_REUSEADDR`` na sondzie: bez niej ``bind()``
    na zajętym porcie zwraca ``EADDRINUSE`` zarówno dla TCP w stanie LISTEN,
    jak i dla zbindowanego gniazda UDP. Z ``SO_REUSEADDR`` drugi ``bind()``
    na port UDP by się udał i sonda nic by nie wykryła.

    ``host`` musi być tym samym adresem, na którym nasłuchuje serwer, bo
    **rodzina adresów jest z niego wyprowadzana**. Pierwsza wersja tej funkcji
    miała ``AF_INET`` na stałe i przez to oblewała test IPv6: serwer siedział
    na ``::1``, sonda próbowała bindować ``127.0.0.1`` — a ten port w
    przestrzeni IPv4 był wolny, więc ``bind()`` się udawał i sonda raportowała
    „jeszcze nie gotowy" aż do timeoutu. Lokalnie tego nie było widać, bo bez
    loopbacku IPv6 test jest pomijany; wyszło dopiero na runnerze GitHuba,
    który IPv6 ma.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(family, kind) as probe:
            try:
                probe.bind((host, port))
            except OSError:
                return True
        time.sleep(0.02)
    return False


# --------------------------------------------------------------------------
# Uruchamianie procesów
# --------------------------------------------------------------------------


@dataclass
class Proc:
    """Uruchomiony proces z plikami na stdout/stderr."""

    popen: subprocess.Popen
    stdout_path: Path
    stderr_path: Path

    def wait(self, timeout: float = 20.0) -> int:
        try:
            return self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.popen.kill()
            self.popen.wait(timeout=5)
            raise

    def kill(self) -> None:
        if self.popen.poll() is None:
            self.popen.kill()
            try:
                self.popen.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass

    @property
    def stdout_bytes(self) -> bytes:
        return self.stdout_path.read_bytes()

    @property
    def stderr_text(self) -> str:
        return self.stderr_path.read_text(errors="replace")


@dataclass
class Runner:
    """Fabryka procesów zapisujących wyjście do plików w ``tmp_path``."""

    tmp_path: Path
    counter: list[int] = field(default_factory=lambda: [0])
    started: list[Proc] = field(default_factory=list)

    def spawn(
        self,
        argv: list[str],
        *,
        stdin_data: bytes | None = None,
        stdin_path: Path | None = None,
        close_stdin: bool = False,
    ) -> Proc:
        """Uruchom proces.

        ``stdin_data``   — bajty podane na stdin, potem EOF
        ``stdin_path``   — plik jako stdin (dla dużych transferów)
        ``close_stdin``  — stdin natychmiast na EOF (``/dev/null``)
        Domyślnie stdin jest **otwartą rurą, która nie dostaje EOF-u** —
        to odpowiednik terminala i jest konieczne do testów, w których klient
        nie ma prawa zakończyć się po EOF na stdin.
        """
        self.counter[0] += 1
        tag = self.counter[0]
        out_path = self.tmp_path / f"out{tag}.bin"
        err_path = self.tmp_path / f"err{tag}.txt"
        out_f = out_path.open("wb")
        err_f = err_path.open("wb")

        stdin_arg: object
        if stdin_path is not None:
            stdin_arg = stdin_path.open("rb")
        elif close_stdin:
            stdin_arg = subprocess.DEVNULL
        else:
            stdin_arg = subprocess.PIPE

        popen = subprocess.Popen(
            argv,
            stdin=stdin_arg,
            stdout=out_f,
            stderr=err_f,
            cwd=REPO_ROOT,
        )
        out_f.close()
        err_f.close()
        if hasattr(stdin_arg, "close") and stdin_path is not None:
            stdin_arg.close()  # type: ignore[union-attr]

        proc = Proc(popen=popen, stdout_path=out_path, stderr_path=err_path)
        self.started.append(proc)

        if stdin_data is not None and popen.stdin is not None:
            popen.stdin.write(stdin_data)
            popen.stdin.flush()
            popen.stdin.close()
        return proc

    def cleanup(self) -> None:
        for proc in self.started:
            proc.kill()
            if proc.popen.stdin is not None:
                try:
                    proc.popen.stdin.close()
                except OSError:  # pragma: no cover
                    pass


@pytest.fixture
def runner(tmp_path: Path):
    r = Runner(tmp_path=tmp_path)
    try:
        yield r
    finally:
        r.cleanup()


@pytest.fixture
def port() -> int:
    return free_port()


@pytest.fixture
def udp_port() -> int:
    return free_port(socket.SOCK_DGRAM)


# --------------------------------------------------------------------------
# Dane testowe
# --------------------------------------------------------------------------


def binary_blob(size: int = 1 << 20, seed: int = 1234) -> bytes:
    """Dane, na których naiwna implementacja się wysypie.

    Zawiera zera (koniec łańcucha w C), bajty > 0x7f (nie-UTF-8), ``\\n``,
    ``\\r`` i ``0x1a`` (EOF pod DOS). Deterministyczne, żeby błąd dał się
    odtworzyć.
    """
    import random

    rng = random.Random(seed)
    body = bytes(rng.getrandbits(8) for _ in range(size))
    tail = b"\x00\x00\x00\xff\xff\xff\n\r\x1a\x00\xff"
    return body + tail


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# Harness do testów jednostkowych pętli relay (bez podprocesów)
# --------------------------------------------------------------------------


@dataclass
class RelayHarness:
    """Uchwyty do ``readwrite()`` odpalonego w wątku.

    ``peer`` to drugi koniec ``socketpair()``, czyli „sieć" z punktu widzenia
    pętli. ``stdin_w`` to koniec, do którego piszemy, udając użytkownika.
    ``stdout_r`` to koniec, z którego czytamy to, co pętla wypisała.
    """

    peer: socket.socket
    stdin_w: int
    stdout_r: int
    thread: threading.Thread
    errors: list[BaseException]

    def send(self, data: bytes) -> None:
        """Wyślij dane „z sieci" do pętli."""
        self.peer.sendall(data)

    def try_send(self, data: bytes) -> bool:
        """Spróbuj wysłać; ``False``, gdy socket jest już zamknięty.

        Potrzebne tam, gdzie testujemy, że dane **nie** zostaną dostarczone:
        pętla, która zakończyła pracę, zamyka swój koniec ``socketpair()``,
        więc ``sendall()`` dostaje ``EPIPE``. Sam fakt porażki jest poprawnym
        wynikiem, ale test ma sprawdzać dostarczenie, nie to, które z dwóch
        poprawnych zachowań wystąpiło.
        """
        try:
            self.peer.sendall(data)
        except OSError:
            return False
        return True

    def feed_stdin(self, data: bytes) -> None:
        os.write(self.stdin_w, data)

    def close_stdin(self) -> None:
        """EOF na stdin — wyzwala propagację EOF w pętli."""
        os.close(self.stdin_w)
        self.stdin_w = -1

    def close_peer_write(self) -> None:
        """Druga strona przestaje nadawać (FIN) — EOF na netin."""
        self.peer.shutdown(socket.SHUT_WR)

    def read_stdout(self, nbytes: int, timeout: float = 5.0) -> bytes:
        """Czytaj ze stdout pętli do ``nbytes`` albo do timeoutu."""
        got = bytearray()
        deadline = time.monotonic() + timeout
        while len(got) < nbytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([self.stdout_r], [], [], remaining)
            if not ready:
                break
            chunk = os.read(self.stdout_r, nbytes - len(got))
            if not chunk:
                break
            got += chunk
        return bytes(got)

    def recv_peer(self, nbytes: int, timeout: float = 5.0) -> bytes:
        """Czytaj to, co pętla wysłała „do sieci"."""
        self.peer.settimeout(timeout)
        got = bytearray()
        try:
            while len(got) < nbytes:
                chunk = self.peer.recv(nbytes - len(got))
                if not chunk:
                    break
                got += chunk
        except (TimeoutError, OSError):
            pass
        return bytes(got)

    def peer_eof(self, timeout: float = 5.0) -> bool:
        """Czy pętla zamknęła swój kierunek nadawania (dostaliśmy FIN)?"""
        self.peer.settimeout(timeout)
        try:
            return self.peer.recv(1) == b""
        except (TimeoutError, OSError):
            return False

    def join(self, timeout: float = 5.0) -> bool:
        """Czy pętla zakończyła się w zadanym czasie?"""
        self.thread.join(timeout)
        if self.errors:
            raise self.errors[0]
        return not self.thread.is_alive()


def make_relay(opts) -> RelayHarness:
    """Odpal ``readwrite()`` w wątku na ``socketpair()`` + dwóch rurach.

    ``socketpair`` zamiast ``pipe`` dla strony sieciowej, bo pętla woła
    ``shutdown()`` — na rurze nie jest dostępne, a to ono realizuje half-close.
    """
    from pyncat.relay import RelayState, readwrite

    net, peer = socket.socketpair()
    net.setblocking(False)
    stdin_r, stdin_w = os.pipe()
    stdout_r, stdout_w = os.pipe()

    errors: list[BaseException] = []

    def target() -> None:
        try:
            readwrite(
                net,
                opts,
                RelayState(),
                stdin_fd=stdin_r,
                stdout_fd=stdout_w,
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            for fd in (stdin_r, stdout_w):
                try:
                    os.close(fd)
                except OSError:
                    pass
            net.close()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return RelayHarness(
        peer=peer,
        stdin_w=stdin_w,
        stdout_r=stdout_r,
        thread=thread,
        errors=errors,
    )
