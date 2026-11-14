"""Warstwa pośrednia między ``pyncat`` a demo w przeglądarce.

Zadanie tego modułu: **uruchomić prawdziwą funkcję** ``pyncat.relay.readwrite()``
w przeglądarce i wyprodukować z niej trace, który JavaScript potrafi odtworzyć
krok po kroku.

Dlaczego to w ogóle wymaga pośrednika
=====================================

``readwrite()`` opiera się na trzech rzeczach, których w WebAssembly nie ma
albo są zepsute. Wszystko poniżej to **POMIARY** na Pyodide 314.0.8
(CPython 3.14.2, ``sys.platform == "emscripten"``), node 22, ten sam
``pyodide.asm.wasm`` co w przeglądarce — szczegóły i surowe wyniki
w ``docs/demo-i-hosting.md`` §2.

1. **Socket.** ``socket.socketpair()`` → ``OSError: [Errno 28] Invalid
   argument``. Nie istnieje więc w Pyodide *żaden* dwukierunkowy deskryptor,
   a model netcata wymaga dokładnie tego: ``POLL_NETIN`` i ``POLL_NETOUT``
   to dwa slots na **tym samym** fd [netcat.c:1118-1133]. Para ``os.pipe()``
   tego nie zastąpi, bo to dwa różne, jednokierunkowe deskryptory —
   odwzorowanie straciłoby to, co jest w netcacie najciekawsze.

2. **``poll()`` nie blokuje.** ``select.poll`` w Pyodide *istnieje*
   (wbrew temu, co się zwykle zakłada), ale timeout jest ignorowany:
   ``poll(50)``, gdy nic nie jest gotowe, wraca po **0,0000 s** z pustą
   listą. Emscripten mówi to wprost w ``src/lib/libsyscall.js``:
   ``warnOnce('non-zero poll() timeout not supported')``. Relay oparty na
   takim ``poll()`` to busy-spin, który nigdy nie oddaje steru pętli zdarzeń
   przeglądarki — czyli zawieszona karta.

3. **Brak ``POLLHUP`` i brak backpressure na pipach.** Pipe z zamkniętym
   końcem zapisu i opróżniony daje w Emscriptenie ``poll()`` → ``[]``,
   a nie ``POLLHUP`` (na Linuksie: ``POLLHUP`` = 16). Do tego w MEMFS da się
   zapisać do pipe'a 2 002 944 B bez jednego ``EAGAIN`` — bufor jest
   praktycznie nieograniczony, więc częściowe zapisy i backpressure
   [netcat.c:1216-1234] nie wystąpią ani razu.

Wniosek: prawdziwe deskryptory **pogorszyłyby** wierność demo, nie poprawiły.
Dlatego podstawiamy atrapy — ale tylko na najniższym poziomie.

Co jest prawdziwe, a co podstawione
===================================

===============================  =========================================
prawdziwy kod z repo             atrapa w tym module
===============================  =========================================
``relay.readwrite()`` — całe     ``select.poll`` → ``FakePoll``
ciało pętli, wszystkie trzy      ``os.read``/``os.write`` → ``FakeOs``
warunki wyjścia, kolejność       ``time.monotonic``/``sleep`` → ``VClock``
obsługi ``revents``, backpressure ``socket.shutdown`` → ``FakeSock``
``netio.fillbuf``/``drainbuf``
``relay._handle_revents``
``relay._poll_once``
``relay._poll_timeout``
===============================  =========================================

Podstawione są **wyłącznie wywołania systemowe**. Logika decyzyjna — kto
czyta, kto pisze, który slot umiera i kiedy — wykonuje się linia po linii
z ``pyncat/relay.py``, a tracer rejestruje stan ``pfd[]`` przed każdą linią.
To nie jest reimplementacja maszyny stanów; to ta sama maszyna stanów
na symulowanym jądrze.

Atrapy odtwarzają semantykę **Linuksa**, nie Emscriptena — bo celem jest
pokazanie, jak netcat zachowuje się na prawdziwym systemie. W szczególności:

* pipe z danymi i zamkniętym pisarzem → ``POLLIN | POLLHUP`` (to ten niuans
  z [netcat.c:1166-1174], o który wywraca się naiwna implementacja),
* FIN od peera na TCP → ``POLLIN`` + ``read()`` zwracające 0, **nie**
  ``POLLHUP`` (Linux zapala ``POLLHUP`` dopiero przy zamknięciu obu
  kierunków) — to ścieżka [netcat.c:1247-1251],
* skończone okno zapisu → częściowe zapisy i ``EAGAIN``.

Jakiego API oczekujemy od ``pyncat``
====================================

Ten plik był pisany równolegle z ``pyncat/relay.py``, więc kontrakt jest tu
wypisany jawnie. ``_resolve_api()`` sprawdza każdy punkt i przy niezgodności
zwraca czytelny komunikat zamiast wysypać się stacktrace'em:

* ``pyncat.constants``: ``BUFSIZE``, ``FD_RETIRED``, ``NUM_SLOTS``,
  ``POLL_STDIN``, ``POLL_NETOUT``, ``POLL_NETIN``, ``POLL_STDOUT``
* ``pyncat.relay.readwrite(net_sock, opts, state=None, *, stdin_fd=0,
  stdout_fd=1)`` — ``net_sock`` musi być obiektem (potrzebny ``shutdown()``),
  a ``stdin_fd``/``stdout_fd`` muszą być parametrami, żeby dało się podstawić
  atrapy
* ``pyncat.relay.Slot`` z atrybutami ``fd``, ``events``, ``revents``
* ``pyncat.relay.RelayState`` z licznikiem ``recvcount`` (dla ``-W``)
* nazwy **modułowe** w ``pyncat.relay``, które podmieniamy: ``select``,
  ``time``, ``fillbuf``, ``drainbuf``
* nazwa modułowa ``os`` w ``pyncat.netio``
* ``opts`` jest kaczo-typowany (patrz ``Opts``): ``dflag``, ``iflag``,
  ``kflag``, ``lflag``, ``Nflag``, ``qflag``, ``recvlimit``, ``timeout_ms``

Jeśli ``relay.py`` zmieni któryś z tych punktów, trzeba poprawić wyłącznie
``_resolve_api()`` i ``Opts``.
"""

from __future__ import annotations

import json
import select as _real_select
import sys
import traceback

# ---------------------------------------------------------------------------
# Stałe poll() — bierzemy je z prawdziwego modułu ``select``, bo w Pyodide
# ten moduł istnieje i ma poprawne wartości (POMIAR: POLLIN=1, POLLOUT=4,
# POLLHUP=16). Atrapa podmienia tylko fabrykę ``poll()``, więc liczby muszą
# być te same, których relay.py użył do policzenia swojego ``_UNMASKABLE``
# w czasie importu.
# ---------------------------------------------------------------------------

POLLIN = _real_select.POLLIN
POLLOUT = _real_select.POLLOUT
POLLERR = _real_select.POLLERR
POLLHUP = _real_select.POLLHUP
POLLNVAL = _real_select.POLLNVAL

#: POSIX, ``poll(2)``: te flagi jądro ustawia niezależnie od ``events``.
UNMASKABLE = POLLERR | POLLHUP | POLLNVAL

#: Deskryptory atrap. Zgodne z konwencją POSIX dla 0/1; dla socketu bierzemy
#: 4, żeby w trace było widać, że to nie stdin ani stdout.
FD_STDIN = 0
FD_STDOUT = 1
FD_NET = 4

#: Limit kroków trace'u. Zabezpieczenie przed scenariuszem, który przez błąd
#: w atrapie kręciłby się bez końca — strona ma pokazać komunikat, nie zawisnąć.
MAX_STEPS = 6000

#: Limit iteracji pętli ``poll()`` w atrapie (ten sam powód).
MAX_POLL_ITERATIONS = 4000


class Deadlock(Exception):
    """``poll()`` bez timeoutu, a w symulacji nie ma już żadnego zdarzenia.

    To **nie jest** błąd demo — to dokładnie ta sytuacja, w której prawdziwy
    netcat wisi w nieskończoność. Scenariusz „half-close bez ``-N``" kończy
    się właśnie tak, i to jest jego puenta: POMIAR z docs/tablice-decyzyjne.md §1 mówi
    7,701 s do zabicia przez ``timeout``, czyli „wiecznie".
    """


# ---------------------------------------------------------------------------
# Zegar wirtualny
# ---------------------------------------------------------------------------


class VClock:
    """Podstawka pod ``relay.time``: ``monotonic()`` i ``sleep()``.

    Czas jest wirtualny i liczony w milisekundach, więc scenariusze z ``-w``,
    ``-q`` i ``-i`` są deterministyczne i wykonują się natychmiast. Bez tego
    demo z ``-q 2`` kazałoby użytkownikowi czekać dwie sekundy na nic.

    ``monotonic()`` zwraca sekundy (float), bo tego oczekuje ``relay.py``.
    """

    def __init__(self, world: FakeWorld) -> None:
        self.world = world

    def monotonic(self) -> float:
        return self.world.now_ms / 1000.0

    def sleep(self, seconds: float) -> None:
        # [netcat.c:1152] ``if (iflag) sleep(iflag);``
        self.world.log(f"sleep({seconds}) — -i na początku iteracji")
        self.world.advance_to(self.world.now_ms + int(seconds * 1000))


# ---------------------------------------------------------------------------
# Symulowane jądro
# ---------------------------------------------------------------------------


class FakeWorld:
    """Stan „jądra": kolejki bajtów, flagi zamknięcia, harmonogram zdarzeń.

    Trzy deskryptory:

    * ``FD_STDIN``  — kolejka ``stdin_rx`` + flaga ``stdin_eof``
      (odpowiednik pipe'a, którego pisarz zamknął końcówkę),
    * ``FD_STDOUT`` — zlew ``stdout_sink`` + okno zapisu,
    * ``FD_NET``    — **jeden** deskryptor z dwiema niezależnymi połówkami:
      ``net_rx``/``peer_fin``/``local_shut_rd`` po stronie odbioru
      i ``net_tx``/``local_shut_wr``/``peer_gone`` po stronie nadawania.
      Ta jedna cecha jest powodem, dla którego atrapa jest lepsza od pary
      ``os.pipe()``.
    """

    def __init__(self, events: list[tuple[int, dict]]) -> None:
        self.now_ms = 0

        # stdin
        self.stdin_rx = bytearray()
        self.stdin_eof = False

        # stdout
        self.stdout_sink = bytearray()
        self.stdout_window: int | None = None  # None = bez ograniczeń
        self.stdout_broken = False

        # net — odbiór
        self.net_rx = bytearray()
        self.peer_fin = False  # peer wysłał FIN (read() zwróci 0)
        self.local_shut_rd = False

        # net — nadawanie
        self.net_tx = bytearray()
        self.net_window: int | None = None
        self.local_shut_wr = False
        self.peer_gone = False  # połączenie zerwane w obie strony → POLLHUP

        #: Zdarzenia do zastosowania: ``(czas_ms, akcja)``, posortowane.
        self.schedule = sorted(events, key=lambda e: e[0])
        #: Reakcje na ``shutdown(SHUT_WR)`` — patrz ``FakeSock.shutdown``.
        self.on_shutdown_wr: list[dict] = []

        self.events_log: list[str] = []
        self._pending_log: list[str] = []

    # --- logowanie ---------------------------------------------------------

    def log(self, msg: str) -> None:
        stamp = f"t={self.now_ms}ms"
        self.events_log.append(f"{stamp} {msg}")
        self._pending_log.append(msg)

    def drain_log(self) -> list[str]:
        out = self._pending_log
        self._pending_log = []
        return out

    # --- harmonogram -------------------------------------------------------

    def next_event_ms(self) -> int | None:
        return self.schedule[0][0] if self.schedule else None

    def advance_to(self, t_ms: int) -> None:
        """Przesuń zegar do ``t_ms``, stosując po drodze zaplanowane zdarzenia."""
        while self.schedule and self.schedule[0][0] <= t_ms:
            at, action = self.schedule.pop(0)
            self.now_ms = max(self.now_ms, at)
            self.apply(action)
        self.now_ms = max(self.now_ms, t_ms)

    def apply(self, action: dict) -> None:
        kind = action["kind"]
        if kind == "stdin_data":
            data = action["data"]
            self.stdin_rx += data
            self.log(f"stdin: użytkownik wpisał {_short(data)}")
        elif kind == "stdin_eof":
            self.stdin_eof = True
            self.log("stdin: EOF (np. koniec pliku albo Ctrl-D)")
        elif kind == "net_data":
            data = action["data"]
            self.net_rx += data
            self.log(f"sieć: peer przysłał {_short(data)}")
        elif kind == "net_fin":
            self.peer_fin = True
            self.log("sieć: peer wysłał FIN → read() zwróci 0 (POLLIN, nie POLLHUP)")
        elif kind == "net_close":
            self.peer_fin = True
            self.peer_gone = True
            self.log("sieć: peer zamknął połączenie w obie strony → POLLHUP")
        elif kind == "stdout_broken":
            self.stdout_broken = True
            self.log("stdout: odbiorca zniknął (np. `| head` wyszedł) → POLLERR/EPIPE")
        elif kind == "net_window":
            # Peer odczytał dane → okno nadawcze socketu znów się otwiera.
            # To jest dokładnie to, co na prawdziwym TCP robi przychodzący ACK
            # ze zaktualizowanym oknem odbiorczym.
            self.net_window = action["size"]
            self.log(f"sieć: peer odczytał dane → okno nadawcze = {action['size']} B")
        else:  # pragma: no cover — scenariusze są w tym pliku
            raise ValueError(f"nieznane zdarzenie: {kind}")

    # --- gotowość (revents) ------------------------------------------------

    def revents_for(self, fd: int) -> int:
        """``revents`` dla deskryptora — tak, jak policzyłby to Linux.

        Zwracamy **surową** gotowość (bez maskowania przez ``events``);
        maskowaniem zajmuje się ``FakePoll`` i dalej ``relay._poll_once``,
        dokładnie jak w prawdziwym ``poll(2)``.
        """
        if fd == FD_STDIN:
            r = 0
            if self.stdin_rx:
                r |= POLLIN
            # Pipe z zamkniętym pisarzem: POLLHUP razem z POLLIN, dopóki są
            # dane. [netcat.c:1166-1170] opiera się właśnie na tym, że
            # „POLLHUP i NIE POLLIN" to dopiero prawdziwy koniec.
            if self.stdin_eof:
                r |= POLLHUP
            return r

        if fd == FD_STDOUT:
            if self.stdout_broken:
                return POLLERR
            if self.stdout_window is not None and self.stdout_window <= 0:
                return 0
            return POLLOUT

        if fd == FD_NET:
            r = 0
            # Odbiór: dane albo FIN (FIN daje POLLIN, bo read() zwróci 0).
            if not self.local_shut_rd and (self.net_rx or self.peer_fin):
                r |= POLLIN
            # Nadawanie: gniazdo gotowe, dopóki jest okno i nie zamknęliśmy
            # kierunku zapisu.
            if (
                not self.local_shut_wr
                and not self.peer_gone
                and (self.net_window is None or self.net_window > 0)
            ):
                r |= POLLOUT
            # POLLHUP na TCP zapala się, gdy połączenie padło w obie strony.
            if self.peer_gone:
                r |= POLLHUP
            return r

        return POLLNVAL

    # --- read()/write() ----------------------------------------------------

    def read(self, fd: int, n: int) -> bytes:
        if fd == FD_STDIN:
            if self.stdin_rx:
                chunk = bytes(self.stdin_rx[:n])
                del self.stdin_rx[:n]
                self.log(f"read(stdin, {n}) → {len(chunk)} B {_short(chunk)}")
                return chunk
            if self.stdin_eof:
                self.log(f"read(stdin, {n}) → 0 B = EOF")
                return b""
            raise BlockingIOError(11, "Resource temporarily unavailable")

        if fd == FD_NET:
            if self.local_shut_rd:
                raise OSError(9, "Bad file descriptor")
            if self.net_rx:
                chunk = bytes(self.net_rx[:n])
                del self.net_rx[:n]
                self.log(f"read(net, {n}) → {len(chunk)} B {_short(chunk)}")
                return chunk
            if self.peer_fin:
                self.log(f"read(net, {n}) → 0 B = EOF od peera")
                return b""
            raise BlockingIOError(11, "Resource temporarily unavailable")

        raise OSError(9, "Bad file descriptor")

    def write(self, fd: int, data: bytes) -> int:
        if fd == FD_STDOUT:
            if self.stdout_broken:
                raise BrokenPipeError(32, "Broken pipe")
            window = len(data) if self.stdout_window is None else self.stdout_window
            if window <= 0:
                raise BlockingIOError(11, "Resource temporarily unavailable")
            n = min(window, len(data))
            self.stdout_sink += data[:n]
            if self.stdout_window is not None:
                self.stdout_window -= n
            suffix = " (zapis CZĘŚCIOWY)" if n < len(data) else ""
            self.log(f"write(stdout, {len(data)}) → {n} B{suffix}")
            return n

        if fd == FD_NET:
            if self.local_shut_wr:
                raise BrokenPipeError(32, "Broken pipe")
            if self.peer_gone:
                raise ConnectionResetError(104, "Connection reset by peer")
            window = len(data) if self.net_window is None else self.net_window
            if window <= 0:
                raise BlockingIOError(11, "Resource temporarily unavailable")
            n = min(window, len(data))
            self.net_tx += data[:n]
            if self.net_window is not None:
                self.net_window -= n
            suffix = " (zapis CZĘŚCIOWY — okno nadawcze pełne)" if n < len(data) else ""
            self.log(f"write(net, {len(data)}) → {n} B{suffix}")
            return n

        raise OSError(9, "Bad file descriptor")


def _short(data: bytes, limit: int = 24) -> str:
    """Bajty w postaci czytelnej w logu demo."""
    if len(data) <= limit:
        return repr(bytes(data))
    return f"{bytes(data[:limit])!r}…(+{len(data) - limit} B)"


# ---------------------------------------------------------------------------
# Atrapa poll()
# ---------------------------------------------------------------------------


class FakePoll:
    """Obiekt zgodny z ``select.poll()``, liczący gotowość z ``FakeWorld``.

    Interfejs taki sam jak prawdziwy: ``register``, ``unregister``, ``poll``.
    ``relay._poll_once()`` używa dokładnie tych trzech metod i niczego więcej
    — w tym ``register()`` na już zarejestrowanym fd w roli ``modify()``.

    Zachowanie timeoutu jest **takie jak na Linuksie**, czyli blokujące:
    jeśli nic nie jest gotowe, przesuwamy zegar wirtualny do najbliższego
    zaplanowanego zdarzenia (albo do końca timeoutu). Dzięki temu widać
    różnicę między „``poll()`` wrócił, bo coś przyszło" i „``poll()`` wrócił,
    bo minął ``-w``" — a to drugie w netcacie znaczy koniec pętli
    [netcat.c:1162-1163].
    """

    def __init__(self, world: FakeWorld) -> None:
        self.world = world
        self.registered: dict[int, int] = {}

    def register(self, fd: int, mask: int) -> None:
        self.registered[fd] = mask

    def unregister(self, fd: int) -> None:
        if fd not in self.registered:
            raise KeyError(fd)
        del self.registered[fd]

    def poll(self, timeout_ms: int | None = None) -> list[tuple[int, int]]:
        deadline = None if timeout_ms is None else self.world.now_ms + timeout_ms

        for _ in range(MAX_POLL_ITERATIONS):
            ready = self._ready_now()
            if ready:
                return ready

            # Nic nie jest gotowe → trzeba przesunąć czas.
            nxt = self.world.next_event_ms()

            if deadline is None:
                if nxt is None:
                    # Brak timeoutu i brak zdarzeń: prawdziwy netcat wisi tu
                    # w nieskończoność. To jest puenta scenariusza bez -N.
                    raise Deadlock(
                        "poll() bez timeoutu, a w symulacji nie ma już "
                        "żadnego zdarzenia — tu netcat wisi w nieskończoność"
                    )
                self.world.advance_to(nxt)
                continue

            if nxt is not None and nxt <= deadline:
                self.world.advance_to(nxt)
                continue

            # Timeout wygasł przed jakimkolwiek zdarzeniem.
            self.world.advance_to(deadline)
            ready = self._ready_now()
            if ready:
                return ready
            self.world.log(f"poll() → timeout po {timeout_ms} ms (nic nie przyszło)")
            return []

        raise Deadlock(  # pragma: no cover — zabezpieczenie
            "przekroczono limit iteracji atrapy poll()"
        )

    def _ready_now(self) -> list[tuple[int, int]]:
        out = []
        for fd, mask in self.registered.items():
            revents = self.world.revents_for(fd) & (mask | UNMASKABLE)
            if revents:
                out.append((fd, revents))
        return out


class SelectShim:
    """Podstawka pod nazwę modułową ``select`` w ``pyncat.relay``.

    Wszystkie stałe (``POLLIN``, ``POLLHUP``, …) delegujemy do prawdziwego
    modułu ``select``, więc ``relay._UNMASKABLE``, policzone w czasie importu
    z prawdziwych wartości, dalej się zgadza. Podmieniona jest **tylko**
    fabryka ``poll()``.
    """

    def __init__(self, world: FakeWorld) -> None:
        self._world = world

    def poll(self) -> FakePoll:
        return FakePoll(self._world)

    def __getattr__(self, name: str):
        return getattr(_real_select, name)


class FakeOs:
    """Podstawka pod nazwę modułową ``os`` w ``pyncat.netio``.

    Dzięki temu ``fillbuf()``/``drainbuf()`` wykonują się **w całości**
    prawdziwym kodem z repo: razem z obsługą częściowego zapisu, ``del
    buf[:n]`` w roli ``memmove`` i rozróżnieniem ``EAGAIN`` od błędu trwałego.
    """

    def __init__(self, world: FakeWorld) -> None:
        self._world = world

    def read(self, fd: int, n: int) -> bytes:
        return self._world.read(fd, n)

    def write(self, fd: int, data) -> int:
        return self._world.write(fd, bytes(data))


class FakeSock:
    """Atrapa obiektu socketu: ``fileno()`` i ``shutdown()``.

    ``relay.readwrite()`` potrzebuje obiektu, nie liczby, bo w Pythonie
    ``shutdown`` jest metodą socketu. To jedyny powód — i jednocześnie jedyne
    miejsce, w którym demo musi udawać socket.
    """

    SHUT_RD = 0
    SHUT_WR = 1
    SHUT_RDWR = 2

    def __init__(self, world: FakeWorld) -> None:
        self.world = world

    def fileno(self) -> int:
        return FD_NET

    def shutdown(self, how: int) -> None:
        if how == self.SHUT_WR:
            self.world.local_shut_wr = True
            self.world.log("shutdown(SHUT_WR) → peer dostaje FIN (to robi -N)")
            # Reakcja drugiej strony na nasz FIN — np. `nc -l`, które po FIN
            # dochodzi do warunku wyjścia 3 i zamyka połączenie.
            for action in self.world.on_shutdown_wr:
                self.world.schedule.append(
                    (self.world.now_ms + action.get("after_ms", 1), action)
                )
            self.world.schedule.sort(key=lambda e: e[0])
            self.world.on_shutdown_wr = []
        elif how == self.SHUT_RD:
            self.world.local_shut_rd = True
            self.world.log("shutdown(SHUT_RD) → przestajemy odbierać")
        else:  # pragma: no cover
            self.world.local_shut_rd = True
            self.world.local_shut_wr = True


class Opts:
    """Kaczo-typowany odpowiednik ``pyncat.cli.Options``.

    Celowo **nie** importujemy ``pyncat.cli``: demo nie parsuje linii poleceń,
    a mniej zależności to mniejsza szansa, że zmiana w ``cli.py`` wywróci
    stronę. Lista pól to kontrakt wypisany w docstringu modułu.
    """

    def __init__(self, **kw) -> None:
        self.dflag = kw.get("dflag", False)
        self.iflag = kw.get("iflag", 0)
        self.kflag = kw.get("kflag", False)
        self.lflag = kw.get("lflag", False)
        self.Nflag = kw.get("Nflag", False)
        self.nflag = kw.get("nflag", False)
        self.qflag = kw.get("qflag")
        self.recvlimit = kw.get("recvlimit", 0)
        self.timeout_ms = kw.get("timeout_ms")
        self.uflag = kw.get("uflag", False)
        self.vflag = kw.get("vflag", False)
        self.zflag = kw.get("zflag", False)


# ---------------------------------------------------------------------------
# Rozpoznanie API pyncata
# ---------------------------------------------------------------------------


class ApiMismatch(Exception):
    """``pyncat`` nie ma tego, czego demo oczekuje — z czytelnym powodem."""


def _resolve_api() -> dict:
    """Sprawdź kontrakt z docstringu modułu i zwróć uchwyty.

    Każdy brak kończy się ``ApiMismatch`` z nazwą brakującego elementu, żeby
    strona pokazała „demo nie pasuje do tej wersji pyncata, brakuje X"
    zamiast stacktrace'u w konsoli.
    """
    try:
        from pyncat import constants, netio, relay
    except Exception as exc:
        raise ApiMismatch(f"nie da się zaimportować pyncat: {exc!r}") from exc

    need_consts = (
        "BUFSIZE",
        "FD_RETIRED",
        "NUM_SLOTS",
        "POLL_STDIN",
        "POLL_NETOUT",
        "POLL_NETIN",
        "POLL_STDOUT",
    )
    missing = [n for n in need_consts if not hasattr(constants, n)]
    if missing:
        raise ApiMismatch(f"pyncat.constants bez: {', '.join(missing)}")

    for name in ("readwrite", "Slot", "select", "time", "fillbuf", "drainbuf"):
        if not hasattr(relay, name):
            raise ApiMismatch(f"pyncat.relay bez nazwy modułowej `{name}`")

    if not hasattr(netio, "os"):
        raise ApiMismatch("pyncat.netio bez nazwy modułowej `os`")

    import inspect

    sig = inspect.signature(relay.readwrite)
    for param in ("stdin_fd", "stdout_fd"):
        if param not in sig.parameters:
            raise ApiMismatch(
                f"readwrite() bez parametru `{param}` — demo nie ma jak "
                f"podstawić atrap zamiast fd 0/1"
            )

    return {
        "relay": relay,
        "netio": netio,
        "constants": constants,
        "slot_names": (
            constants.POLL_STDIN,
            constants.POLL_NETOUT,
            constants.POLL_NETIN,
            constants.POLL_STDOUT,
        ),
    }


# ---------------------------------------------------------------------------
# Mapowanie linii relay.py → numery linii netcat.c
# ---------------------------------------------------------------------------


def _line_meta(source: str) -> dict[int, dict]:
    """Dla każdej linii kodu ``relay.py``: odnośnik do netcat.c + komentarz nad nią.

    Konwencja w ``pyncat/relay.py`` jest stała i daje się wykorzystać bez
    utrzymywania osobnej tablicy (która natychmiast by się rozjechała)::

        <pusta linia>
        # [netcat.c:1195-1215] stdin → stdinbuf
        # ...dalsze linie komentarza z uzasadnieniem...
        if pfd[POLL_STDIN].revents & select.POLLIN and ...:
            ...

    Czyli: blok komentarza przyklejony **bez pustej linii** do kodu dotyczy
    tego kodu. Skanujemy więc w przód, zbierając komentarz i odnośnik, i
    przypisujemy je wszystkim kolejnym liniom kodu aż do pustej linii.

    Przypisanie musi iść do *wszystkich* linii bloku, nie tylko do pierwszej:
    dla wieloliniowego ``if (`` CPython raportuje w zdarzeniu ``line`` numer
    **pierwszego operandu**, nie numer słowa ``if``. POMIAR: w scenariuszu
    ``halfclose-z-N`` tracer pokazał linie 328/338/347 (operandy), a nie
    327/337/346 (``if (``).

    Komentarz wraca jako ``doc`` i jest wyświetlany obok kodu — bo w tym
    projekcie komentarz jest materiałem dydaktycznym, nie ozdobą.
    """
    import re

    pat = re.compile(r"\[netcat\.c:([0-9]+(?:-[0-9]+)?)\]")
    lines = source.splitlines()
    out: dict[int, dict] = {}

    pending_ref: str | None = None
    pending_doc: list[str] = []

    for idx, raw in enumerate(lines):
        stripped = raw.strip()

        if stripped == "":
            # Pusta linia zamyka blok: komentarz nie przenosi się dalej.
            pending_ref = None
            pending_doc = []
            continue

        if stripped.startswith("#"):
            m = pat.search(raw)
            if m:
                pending_ref = m.group(1)
            text = stripped.lstrip("#").strip()
            # Linie-separatory (`# -----`) są ozdobą, nie treścią.
            if text and set(text) != {"-"}:
                pending_doc.append(text)
            continue

        out[idx + 1] = {
            "nc": pending_ref,
            "doc": pending_doc[:8],
        }

    return out


# ---------------------------------------------------------------------------
# Tracer
# ---------------------------------------------------------------------------

_SLOT_LABELS = ("STDIN", "NETOUT", "NETIN", "STDOUT")


class Tracer:
    """Rejestruje stan ``pfd[]`` przed każdą linią ``readwrite()``.

    Używamy ``sys.settrace`` — POMIAR potwierdził, że w Pyodide 314.0.8
    działa razem z czytaniem ``frame.f_locals`` (3 zdarzenia ``line``
    w teście kontrolnym). Zdarzenie ``line`` leci **przed** wykonaniem linii,
    więc każdy krok to „stan tuż przed tą linią" — dokładnie jak w debuggerze.
    """

    def __init__(self, world: FakeWorld, code, meta: dict[int, dict], consts) -> None:
        self.world = world
        self.code = code
        self.meta = meta
        self.c = consts
        self.steps: list[dict] = []
        self.truncated = False
        self._prev: tuple | None = None

    def install(self) -> None:
        sys.settrace(self._global)

    def remove(self) -> None:
        sys.settrace(None)

    def _global(self, frame, event, arg):
        if event == "call" and frame.f_code is self.code:
            return self._local
        return None

    def _local(self, frame, event, arg):
        if event == "line":
            self._record(frame)
        return self._local

    def _record(self, frame) -> None:
        if len(self.steps) >= MAX_STEPS:
            self.truncated = True
            self.remove()
            return

        loc = frame.f_locals
        pfd = loc.get("pfd")
        if pfd is None:
            # Linie przed utworzeniem pfd[] (ustawianie buforów) — pomijamy,
            # bo nie ma jeszcze czego pokazać.
            return

        slots = [[int(s.fd), int(s.events), int(s.revents)] for s in pfd]
        stdinbuf = loc.get("stdinbuf") or b""
        netinbuf = loc.get("netinbuf") or b""
        state = loc.get("state")
        qd = loc.get("quit_deadline")

        signature = (
            tuple(tuple(s) for s in slots),
            len(stdinbuf),
            len(netinbuf),
            getattr(state, "recvcount", 0),
        )
        changed = self._prev is not None and signature != self._prev
        self._prev = signature

        self.steps.append(
            {
                "i": len(self.steps),
                "line": frame.f_lineno,
                "nc": (self.meta.get(frame.f_lineno) or {}).get("nc"),
                "t": self.world.now_ms,
                "slots": slots,
                "si": len(stdinbuf),
                "ni": len(netinbuf),
                "rc": getattr(state, "recvcount", 0),
                "qd": None if qd is None else int(qd * 1000),
                "changed": changed,
                "log": self.world.drain_log(),
            }
        )


# ---------------------------------------------------------------------------
# Scenariusze
# ---------------------------------------------------------------------------
#
# Każdy scenariusz to: flagi (Opts), harmonogram zdarzeń na zegarze
# wirtualnym, opcjonalne okna zapisu (dla backpressure) i opcjonalna reakcja
# peera na nasz FIN. Wszystkie czasy i oczekiwane wyniki pochodzą
# z POMIARÓW w docs/tablice-decyzyjne.md — odnośniki w polu ``measured``.


def _d(kind: str, **kw) -> dict:
    return {"kind": kind, **kw}


SCENARIOS: dict[str, dict] = {
    "transfer": {
        "title": "Zwykły transfer w obie strony",
        "cmd": "pyncat 127.0.0.1 9000   # peer odpowiada",
        "opts": {},
        "lead": (
            "Punkt odniesienia. Widać podstawowy obieg: POLLIN na stdin → "
            "fillbuf do stdinbuf → uzbrojenie POLL_NETOUT na POLLOUT → "
            "drainbuf do sieci → wyzerowanie events, bo bufor pusty. "
            "I symetrycznie w drugą stronę."
        ),
        "events": [
            (0, _d("stdin_data", data=b"GET / HTTP/1.0\r\n\r\n")),
            (10, _d("net_data", data=b"HTTP/1.0 200 OK\r\n")),
            (20, _d("stdin_eof")),
            (30, _d("net_data", data=b"\r\nhello")),
            (40, _d("net_close")),
        ],
        "expect": "Pętla kończy się warunkiem 1 albo 2 po zamknięciu połączenia.",
    },
    "halfclose-bez-N": {
        "title": "Half-close BEZ -N → zawieszenie",
        "cmd": "printf 'DATA' | pyncat 127.0.0.1 9000",
        "opts": {},
        "lead": (
            "Najważniejszy scenariusz w całym projekcie. Po EOF na stdin "
            "[netcat.c:1291-1296] netcat **tylko** wycofuje POLL_NETOUT — "
            "FIN nie leci, bo nie ma -N. Peer milczy, bo nie wie, że "
            "skończyliśmy nadawać. POLL_NETIN żyje dalej z POLLIN, timeout "
            "jest None, więc poll() blokuje na zawsze."
        ),
        "events": [
            (0, _d("stdin_data", data=b"DATA")),
            (5, _d("stdin_eof")),
        ],
        "expect": (
            "Deadlock. POMIAR (docs/tablice-decyzyjne.md §1): klient żył 7,701 s i "
            "został zabity przez `timeout`, czyli wisiał w nieskończoność."
        ),
        "measured": "docs/tablice-decyzyjne.md §1",
    },
    "halfclose-z-N": {
        "title": "Half-close Z -N → czyste zakończenie",
        "cmd": "printf 'DATA' | pyncat -N 127.0.0.1 9000",
        "opts": {"Nflag": True},
        "lead": (
            "Ten sam harmonogram, jedna flaga różnicy. Po EOF na stdin "
            "leci shutdown(SHUT_WR), peer dostaje FIN, dochodzi do swojego "
            "warunku wyjścia 3 i zamyka połączenie. Wtedy POLL_NETIN dostaje "
            "POLLIN z read()==0, umiera, a za nim POLL_STDOUT — i warunek 1 "
            "kończy pętlę."
        ),
        "events": [
            (0, _d("stdin_data", data=b"DATA")),
            (5, _d("stdin_eof")),
        ],
        "on_shutdown_wr": [_d("net_close", after_ms=5)],
        "expect": (
            "Wyjście przez warunek 1. POMIAR (docs/tablice-decyzyjne.md §1): 0,008 s "
            "— natychmiast."
        ),
        "measured": "docs/tablice-decyzyjne.md §1",
    },
    "eof-z-sieci": {
        "title": "EOF z sieci (peer wysyła FIN pierwszy)",
        "cmd": "pyncat 127.0.0.1 9000   # peer kończy po odpowiedzi",
        "opts": {},
        "lead": (
            "Druga połowa symetrii. FIN od peera na TCP **nie** daje "
            "POLLHUP — daje POLLIN, a dopiero read() zwraca 0. Dlatego "
            "ścieżką wyjścia jest [netcat.c:1247-1251], nie "
            "[netcat.c:1171-1174]. Po niej netin umiera, netinbuf się "
            "opróżnia i [netcat.c:1297-1300] gasi stdout."
        ),
        "events": [
            (0, _d("net_data", data=b"banner\r\n")),
            (10, _d("net_fin")),
            (600, _d("stdin_eof")),
        ],
        "expect": (
            "netin umiera na read()==0, potem gaśnie stdout. Ale pętla "
            "NIE kończy się od razu: stdin i netout wciąż żyją, więc netcat "
            "dalej czeka na klawiaturę — mimo że nie ma już gdzie wypisać "
            "odpowiedzi. Dopiero Ctrl-D w 600 ms wycofuje netout i domyka "
            "warunek 2. To jest realne zachowanie, nie błąd."
        ),
    },
    "W-limit": {
        "title": "-W 2 — limit odczytów z sieci",
        "cmd": "pyncat -W 2 127.0.0.1 9000",
        "opts": {"recvlimit": 2},
        "lead": (
            "Peer wysyła A, B, C w trzech osobnych porcjach. -W liczy "
            "**udane odczyty**, nie pakiety [netcat.c:1252-1258], więc po "
            "drugim read() giną naraz POLL_NETIN i POLL_STDIN. Gdyby A, B i C "
            "przyszły w jednym read(), -W 2 przepuściłoby wszystkie trzy "
            "bajty — nazwa „packets” z man page jest dla TCP myląca."
        ),
        "events": [
            (0, _d("net_data", data=b"A")),
            (400, _d("net_data", data=b"B")),
            (800, _d("net_data", data=b"C")),
        ],
        "expect": (
            "Na stdout trafia dokładnie `AB`. POMIAR (docs/tablice-decyzyjne.md §9.3): "
            "klient -W 2 odebrał `AB`."
        ),
        "measured": "docs/tablice-decyzyjne.md §9.3",
    },
    "q-timer": {
        "title": "-q 2 — czekaj 2 s po EOF na stdin",
        "cmd": "printf 'X' | pyncat -q 2 127.0.0.1 9000",
        "opts": {"qflag": 2},
        "lead": (
            "Łatka Debiana, w upstream tego nie ma (docs/tablice-decyzyjne.md §9.4). Po EOF "
            "na stdin uzbraja się deadline; poll() dostaje min(-w, resztę -q). "
            "Peer odpowiada po 1,5 s, czyli zmieścił się w limicie — dane "
            "dolatują, a potem deadline kończy pętlę."
        ),
        "events": [
            (0, _d("stdin_data", data=b"X")),
            (5, _d("stdin_eof")),
            (1500, _d("net_data", data=b"DELAYED")),
        ],
        "expect": (
            "`DELAYED` dolatuje, wyjście po ~2 s wirtualnych. POMIAR "
            "(docs/tablice-decyzyjne.md §9.4): -q 2 → 2,007 s; -q 0 → 0,007 s i `DELAYED` "
            "przepada; bez -q → 8,602 s."
        ),
        "measured": "docs/tablice-decyzyjne.md §9.4",
    },
    "q-zero": {
        "title": "-q 0 — wyjdź natychmiast po EOF na stdin",
        "cmd": "printf 'X' | pyncat -q 0 127.0.0.1 9000",
        "opts": {"qflag": 0},
        "lead": (
            "Ten sam harmonogram co wyżej, ale -q 0. Wyjście następuje "
            "w tej samej iteracji, w której stdin dał EOF — odpowiedź peera "
            "z 1,5 s nie ma szansy dolecieć."
        ),
        "events": [
            (0, _d("stdin_data", data=b"X")),
            (5, _d("stdin_eof")),
            (1500, _d("net_data", data=b"DELAYED")),
        ],
        "expect": "Na stdout nic. POMIAR (docs/tablice-decyzyjne.md §9.4): -q 0 → 0,007 s, bez `DELAYED`.",
        "measured": "docs/tablice-decyzyjne.md §9.4",
    },
    "backpressure": {
        "title": "Backpressure — wąskie okno zapisu",
        "cmd": "yes | pyncat 127.0.0.1 9000   # peer czyta wolno",
        "opts": {},
        "lead": (
            "Okno nadawcze socketu ma tu 6 bajtów, a stdin dostaje 40. "
            "drainbuf robi zapis CZĘŚCIOWY, `del buf[:n]` przesuwa ogon "
            "(odpowiednik memmove z [netcat.c:1330-1333]), a kolejny zapis "
            "dostaje EAGAIN → WANT_POLLOUT. Widać wzorzec z "
            "[netcat.c:1216-1234]: pełny bufor gasi czytanie, pusty gasi "
            "pisanie."
        ),
        "events": [
            (0, _d("stdin_data", data=b"0123456789" * 4)),
            (5, _d("stdin_eof")),
            (20, _d("net_window", size=6)),
            (40, _d("net_window", size=6)),
            (60, _d("net_window", size=6)),
            (80, _d("net_window", size=6)),
            (100, _d("net_window", size=6)),
            (120, _d("net_window", size=64)),
            (200, _d("net_close")),
        ],
        "net_window": 6,
        "expect": (
            "Sześć częściowych zapisów po 6 B plus reszta, i co najmniej "
            "jedno EAGAIN → WANT_POLLOUT w logu. Do sieci trafia całe 40 B."
        ),
    },
    "stdout-znikl": {
        "title": "stdout zniknął (POLLERR) → shutdown(SHUT_RD)",
        "cmd": "pyncat 127.0.0.1 9000 | head -c 1",
        "opts": {},
        "lead": (
            "Odbiorca na stdout wychodzi. POLLERR gasi slot "
            "[netcat.c:1161-1165], a wtedy [netcat.c:1186-1192] robi "
            "shutdown(SHUT_RD) na sieci — żeby peer dostał sygnał zamiast "
            "pisać w próżnię — i wycofuje POLL_NETIN. Zostają dwa martwe "
            "wyjścia, czyli warunek 2."
        ),
        "events": [
            (0, _d("net_data", data=b"dane")),
            (10, _d("stdout_broken")),
            (20, _d("net_data", data=b"wiecej")),
            (300, _d("stdin_eof")),
        ],
        "expect": (
            "Na stdout trafia tylko to, co zmieściło się przed awarią. "
            "Wyjście przez warunek 2 (oba wyjścia martwe) po EOF na stdin, "
            "który wycofuje ostatni żywy slot wyjściowy."
        ),
    },
}


# ---------------------------------------------------------------------------
# Uruchomienie scenariusza
# ---------------------------------------------------------------------------


def run_scenario(name: str) -> dict:
    """Odpal scenariusz i zwróć trace gotowy do serializacji na JSON."""
    if name not in SCENARIOS:
        return {"ok": False, "error": f"nieznany scenariusz: {name}"}

    spec = SCENARIOS[name]

    try:
        api = _resolve_api()
    except ApiMismatch as exc:
        return {"ok": False, "error": str(exc), "kind": "api"}

    relay = api["relay"]
    netio = api["netio"]
    consts = api["constants"]

    world = FakeWorld(list(spec.get("events", [])))
    world.net_window = spec.get("net_window")
    world.stdout_window = spec.get("stdout_window")
    world.on_shutdown_wr = list(spec.get("on_shutdown_wr", []))

    opts = Opts(**spec.get("opts", {}))
    sock = FakeSock(world)

    try:
        import inspect

        source = inspect.getsource(relay)
    except Exception:
        source = ""
    meta = _line_meta(source) if source else {}
    src_lines = (
        {i + 1: line for i, line in enumerate(source.splitlines())} if source else {}
    )

    tracer = Tracer(world, relay.readwrite.__code__, meta, consts)

    # --- podstawienie atrap -------------------------------------------------
    saved = {
        "relay.select": relay.select,
        "relay.time": relay.time,
        "netio.os": netio.os,
    }
    relay.select = SelectShim(world)
    relay.time = VClock(world)
    netio.os = FakeOs(world)

    outcome = "return"
    error = None
    try:
        tracer.install()
        try:
            state = relay.RelayState() if hasattr(relay, "RelayState") else None
            if state is None:
                relay.readwrite(sock, opts, stdin_fd=FD_STDIN, stdout_fd=FD_STDOUT)
            else:
                relay.readwrite(
                    sock, opts, state, stdin_fd=FD_STDIN, stdout_fd=FD_STDOUT
                )
        finally:
            tracer.remove()
    except Deadlock as exc:
        outcome = "deadlock"
        error = str(exc)
        world.log(f"DEADLOCK: {exc}")
    except Exception as exc:
        outcome = "exception"
        error = f"{type(exc).__name__}: {exc}"
        world.log(f"WYJĄTEK: {error}")
        error += "\n" + "".join(traceback.format_exc(limit=8))
    finally:
        relay.select = saved["relay.select"]
        relay.time = saved["relay.time"]
        netio.os = saved["netio.os"]

    # Ostatni wpis: to, co się stało po ostatniej wykonanej linii.
    tail_log = world.drain_log()

    used_lines = sorted({s["line"] for s in tracer.steps})
    return {
        "ok": True,
        "name": name,
        "title": spec["title"],
        "cmd": spec["cmd"],
        "lead": spec["lead"],
        "expect": spec["expect"],
        "measured": spec.get("measured"),
        "outcome": outcome,
        "error": error,
        "truncated": tracer.truncated,
        "steps": tracer.steps,
        "tail_log": tail_log,
        "source": {str(n): src_lines.get(n, "") for n in used_lines},
        "nc_refs": {str(n): (meta.get(n) or {}).get("nc") for n in used_lines},
        "docs": {str(n): (meta.get(n) or {}).get("doc") or [] for n in used_lines},
        "stdout": _short(bytes(world.stdout_sink), 120),
        "net_tx": _short(bytes(world.net_tx), 120),
        "stdout_len": len(world.stdout_sink),
        "net_tx_len": len(world.net_tx),
        "events_log": world.events_log,
        "slot_labels": list(_SLOT_LABELS),
        "slot_index": {
            "STDIN": consts.POLL_STDIN,
            "NETOUT": consts.POLL_NETOUT,
            "NETIN": consts.POLL_NETIN,
            "STDOUT": consts.POLL_STDOUT,
        },
        "flags": {
            "POLLIN": POLLIN,
            "POLLOUT": POLLOUT,
            "POLLERR": POLLERR,
            "POLLHUP": POLLHUP,
            "POLLNVAL": POLLNVAL,
        },
        "fd": {"stdin": FD_STDIN, "stdout": FD_STDOUT, "net": FD_NET},
        "fd_retired": consts.FD_RETIRED,
        "bufsize": consts.BUFSIZE,
    }


def scenario_list() -> list[dict]:
    """Lista scenariuszy do menu na stronie."""
    return [
        {"name": k, "title": v["title"], "cmd": v["cmd"]} for k, v in SCENARIOS.items()
    ]


# ---------------------------------------------------------------------------
# Panel parsowania portów — build_ports() / strtoport()
# ---------------------------------------------------------------------------


def install_etc_services() -> str:
    """Wstrzyknij minimalne ``/etc/services`` do systemu plików Pyodide.

    POMIAR: bez tego pliku ``socket.getservbyname("ssh")`` w Pyodide zwraca
    ``OSError: service/proto not found``; po zapisaniu pliku do MEMFS zwraca
    ``22``. Czyli ścieżka „nazwa usługi" z ``strtoport()``
    [netcat.c:1470-1472] **da się** w przeglądarce pokazać uczciwie — na
    wycinku bazy, nie na pełnym ``/etc/services`` z systemu.

    Uwaga: ``getservbyport()`` nadal nie działa (``port/proto not found``),
    więc gwiazdka w ``[tcp/*]`` z ``connection_info()`` byłaby w przeglądarce
    zawsze — to osobne ograniczenie, nie do obejścia tym plikiem.

    **Zapis wykonujemy wyłącznie pod Emscriptenem.** System plików Pyodide
    jest wirtualny i żyje w karcie przeglądarki, więc nadpisanie tam
    ``/etc/services`` nie ma żadnych konsekwencji. Na zwykłym Linuksie —
    gdzie ten sam moduł jest importowany przez ``build_site.py --selftest``
    i przez testy — ``/etc/services`` to **prawdziwy plik systemowy** i nie
    wolno go dotknąć. Bez tej bramki selftest po cichu kasowałby bazę usług
    na maszynie deweloperskiej i w kontenerze CI.
    """
    if sys.platform != "emscripten":
        import socket as _socket

        try:
            _socket.getservbyname("ssh", "tcp")
            return "pominięte (nie Emscripten; systemowe /etc/services działa)"
        except OSError:
            return (
                "pominięte (nie Emscripten; brak systemowego /etc/services — "
                "nazwy usług nie będą się rozwiązywać)"
            )

    entries = """\
tcpmux\t1/tcp
echo\t7/tcp
echo\t7/udp
discard\t9/tcp
daytime\t13/tcp
ftp-data\t20/tcp
ftp\t21/tcp
ssh\t22/tcp
ssh\t22/udp
telnet\t23/tcp
smtp\t25/tcp
time\t37/tcp
domain\t53/tcp
domain\t53/udp
bootps\t67/udp
tftp\t69/udp
http\t80/tcp
pop3\t110/tcp
sunrpc\t111/tcp
imap2\t143/tcp
snmp\t161/udp
ldap\t389/tcp
https\t443/tcp
submission\t587/tcp
imaps\t993/tcp
openvpn\t1194/udp
mysql\t3306/tcp
postgresql\t5432/tcp
redis\t6379/tcp
http-alt\t8080/tcp
"""
    import os as _os

    try:
        _os.makedirs("/etc", exist_ok=True)
        with open("/etc/services", "w", encoding="ascii") as fh:
            fh.write(entries)
        import socket as _socket

        _socket.getservbyname("ssh", "tcp")
        return "ok"
    except Exception as exc:  # pragma: no cover
        return f"nie udało się: {type(exc).__name__}: {exc}"


def parse_ports(spec: str, udp: bool = False, randomize: bool = False) -> dict:
    """Odpal prawdziwe ``build_ports()`` na podanej specyfikacji.

    Ten panel nie wymaga **żadnych** atrap: ``build_ports()`` i ``strtoport()``
    to czyste parsowanie [netcat.c:1455-1532]. Jedyna zależność systemowa to
    ``getservbyname()``, załatwiona przez ``install_etc_services()``.
    """
    try:
        from pyncat import ports
    except Exception as exc:
        return {"ok": False, "error": f"import pyncat.ports: {exc!r}"}

    try:
        result = ports.build_ports(spec, udp, randomize)
    except Exception as exc:
        # ``errx()`` z report.py podnosi FatalError; pokazujemy komunikat
        # dokładnie tak, jak zobaczyłby go użytkownik na stderr.
        return {
            "ok": True,
            "spec": spec,
            "error": f"{type(exc).__name__}: {exc}",
            "ports": None,
        }

    return {
        "ok": True,
        "spec": spec,
        "error": None,
        "count": len(result),
        "ports": result[:512],
        "clipped": len(result) > 512,
    }


# ---------------------------------------------------------------------------
# Punkty wejścia wołane z JavaScriptu
# ---------------------------------------------------------------------------


def api_scenarios() -> str:
    return json.dumps(scenario_list(), ensure_ascii=False)


def api_run(name: str) -> str:
    return json.dumps(run_scenario(name), ensure_ascii=False, default=str)


def api_ports(spec: str, udp: bool = False, randomize: bool = False) -> str:
    return json.dumps(parse_ports(spec, udp, randomize), ensure_ascii=False)


def api_env() -> str:
    """Informacje o środowisku — strona pokazuje je, żeby nie udawać Linuksa."""
    import platform

    out = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "implementation": platform.python_implementation(),
        "select_has_poll": hasattr(_real_select, "poll"),
        "services": install_etc_services(),
    }
    try:
        out["pthreads"] = bool(sys._emscripten_info.pthreads)
    except Exception:
        out["pthreads"] = None

    # Czy socket w tym środowisku naprawdę nie działa? Nie zakładamy — mierzymy
    # na żywo i pokazujemy wynik na stronie.
    import socket as _socket

    try:
        _socket.socketpair()
        out["socketpair"] = "działa"
    except Exception as exc:
        out["socketpair"] = f"{type(exc).__name__}: {exc}"

    try:
        import os as _os
        import select as _s

        r, _w = _os.pipe()
        p = _s.poll()
        p.register(r, _s.POLLIN)
        t0 = _time_ns()
        res = p.poll(50)
        dt_ms = (_time_ns() - t0) / 1e6
        out["poll_50ms_blocks"] = f"wrócił po {dt_ms:.3f} ms, wynik {res}"
    except Exception as exc:
        out["poll_50ms_blocks"] = f"{type(exc).__name__}: {exc}"

    return json.dumps(out, ensure_ascii=False)


def _time_ns() -> int:
    import time as _t

    return _t.perf_counter_ns()
