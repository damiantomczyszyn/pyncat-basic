"""``readwrite()`` — dwukierunkowa pętla przekazywania danych.

Mapowanie: ``netcat.c`` linie 1104–1303.

**To jest serce netcata.** Parsowanie flag jest tu nieistotne; cała trudność
siedzi w jednej pętli ``poll()``, która multipleksuje stdin i socket,
obsługuje half-close i propaguje EOF w obie strony.

Model czterech slotów
=====================

[netcat.c:1120-1134] Tablica ``pfd[4]`` z indeksami ``POLL_STDIN``,
``POLL_NETOUT``, ``POLL_NETIN``, ``POLL_STDOUT``::

    slot          fd           events na starcie   rola
    POLL_STDIN    0            POLLIN              czytamy wejście użytkownika
    POLL_NETOUT   net_fd       0                   piszemy do sieci
    POLL_NETIN    net_fd       POLLIN              czytamy z sieci
    POLL_STDOUT   1            0                   piszemy na wyjście

**``POLL_NETOUT`` i ``POLL_NETIN`` to dwa slots na TYM SAMYM deskryptorze.**
To nie jest niedopatrzenie, to cały mechanizm half-close: każdy z tych slotów
ma osobną maskę ``events`` i osobne „życie", a kierunek zamyka się przez
wpisanie ``fd = -1`` do jednego z nich, podczas gdy drugi pracuje dalej.
Nie ma żadnych dodatkowych zmiennych stanu — stan połączenia *jest* zawartością
tej czterolementowej tablicy.

Dlaczego ``select.poll``, a nie ``selectors``
---------------------------------------------

Pythonowy ``selectors`` byłby tu naturalnym wyborem i **nie da się go użyć**:
``SelectorKey`` jest indeksowany po deskryptorze, więc nie można zarejestrować
``net_fd`` dwa razy z dwiema różnymi maskami. Dodatkowo ``selectors`` ukrywa
``POLLHUP`` (zwraca ``EVENT_READ``), a netcat rozróżnia „HUP z danymi" od
„HUP bez danych" — zobacz ``_handle_revents``.

Cena: ``select.poll`` nie istnieje na Windows. Udokumentowane w README
(„Czego tu nie ma") i w ``docs/c-vs-python.md`` §3.1.

Emulacja dwóch wpisów na jednym fd
-----------------------------------

``poll(2)`` w C dostaje tablicę i wypełnia ``revents`` **osobno dla każdego
wpisu**, według maski tego wpisu. Pythonowy obiekt ``select.poll()`` jest
mapą ``fd → maska``, więc dwie rejestracje tego samego fd nadpisują się.
Odtwarzamy semantykę C w dwóch krokach (``_poll_once``):

1. rejestrujemy **sumę** masek dla każdego unikalnego fd,
2. rozdzielamy wynik: ``slot.revents = raw & (slot.events | POLLERR|POLLHUP|POLLNVAL)``.

Maskowanie przez ``slot.events`` plus flagi niemaskowalne to dokładnie to, co
jądro wpisuje w ``revents`` pojedynczego wpisu: POSIX wymaga, by ``POLLERR``,
``POLLHUP`` i ``POLLNVAL`` były raportowane **niezależnie** od ``events``
(i netcat na tym polega — ``POLL_NETOUT`` startuje z ``events = 0``, a mimo to
reaguje na ``POLLHUP``).
"""

from __future__ import annotations

import select
import socket
import time
from typing import TYPE_CHECKING

from .constants import (
    BUFSIZE,
    FD_RETIRED,
    IO_EOF,
    IO_ERROR,
    NUM_SLOTS,
    POLL_NETIN,
    POLL_NETOUT,
    POLL_STDIN,
    POLL_STDOUT,
    WANT_POLLIN,
    WANT_POLLOUT,
)
from .netio import drainbuf, fillbuf
from .report import FatalError

if TYPE_CHECKING:  # pragma: no cover
    from .cli import Options

#: Flagi, które jądro wpisuje w ``revents`` niezależnie od ``events``.
#: POSIX, ``poll(2)``: „POLLERR, POLLHUP and POLLNVAL shall be set in revents
#: regardless of events".
_UNMASKABLE = select.POLLERR | select.POLLHUP | select.POLLNVAL


class Slot:
    """Jeden wpis odpowiadający ``struct pollfd``. [netcat.c:1106]

    ``fd == FD_RETIRED`` (−1) oznacza slot wycofany — ``poll(2)`` pomija takie
    wpisy i zeruje ich ``revents``. Trzymamy dosłownie ``-1`` zamiast ``None``,
    żeby kod dał się czytać obok oryginału w C.
    """

    __slots__ = ("events", "fd", "revents")

    def __init__(self, fd: int, events: int) -> None:
        self.fd = fd
        self.events = events
        self.revents = 0

    def __repr__(self) -> str:  # pragma: no cover — tylko do debugowania
        return f"Slot(fd={self.fd}, events={self.events:#x}, revents={self.revents:#x})"


class RelayState:
    """Stan przeżywający pojedyncze wywołanie ``readwrite()``.

    W oryginale ``recvcount`` i ``recvlimit`` są **zmiennymi globalnymi**
    [netcat.c:114]. W trybie ``-k`` ``readwrite()`` jest wołane raz na
    połączenie, a ``recvcount`` **nigdy nie jest zerowany** — licznik ``-W``
    akumuluje się więc przez całe życie procesu. Przy ``-l -k -W 2`` pierwszy
    klient zużywa cały limit, a każdy następny zostaje rozłączony po
    pierwszym odczycie.

    Odtwarzamy ten kwirk dosłownie (dlatego obiekt stanu jest tworzony raz
    w ``app.py`` i przekazywany do wszystkich wywołań), bo to realne
    zachowanie oryginału, nie błąd naszej implementacji. Tablica decyzyjna:
    ``docs/tablice-decyzyjne.md`` §3.
    """

    __slots__ = ("recvcount",)

    def __init__(self) -> None:
        self.recvcount = 0


def _poll_timeout(timeout_ms: int | None, quit_deadline: float | None) -> int | None:
    """Timeout dla ``poll()``: ``min(-w, pozostały czas -q)``.

    ``None`` oznacza „bez limitu" (w C: ``timeout = -1``).
    """
    if quit_deadline is None:
        return timeout_ms
    remaining = int((quit_deadline - time.monotonic()) * 1000)
    if remaining < 0:
        remaining = 0
    if timeout_ms is None:
        return remaining
    return min(timeout_ms, remaining)


def _poll_once(
    poller: select.poll,
    registered: dict[int, int],
    pfd: list[Slot],
    timeout_ms: int | None,
) -> int:
    """Jedno ``poll()`` z rozdzieleniem ``revents`` na slots.

    Zwraca liczbę gotowych deskryptorów (jak ``num_fds`` w C) i wypełnia
    ``slot.revents``.
    """
    # Suma masek per unikalny fd — krok 1 emulacji opisanej w docstringu modułu.
    want: dict[int, int] = {}
    for slot in pfd:
        if slot.fd != FD_RETIRED:
            want[slot.fd] = want.get(slot.fd, 0) | slot.events

    for fd in [fd for fd in registered if fd not in want]:
        poller.unregister(fd)
        del registered[fd]
    for fd, mask in want.items():
        if registered.get(fd) != mask:
            # register() na już zarejestrowanym fd działa jak modify()
            poller.register(fd, mask)
            registered[fd] = mask

    if not want:
        # Wszystkie slots wycofane. W C ``poll()`` z samymi ujemnymi fd i
        # ``timeout = -1`` wisiałby w nieskończoność; warunki wyjścia na
        # początku pętli nie powinny do tego dopuścić. Zabezpieczenie
        # defensywne: traktujemy jak timeout.
        for slot in pfd:
            slot.revents = 0
        return 0

    try:
        ready = poller.poll(timeout_ms)
    except OSError as exc:  # pragma: no cover — PEP 475 pochłania EINTR
        # [netcat.c:1159-1160] ``err(1, "polling error")``
        raise FatalError(f"polling error: {exc.strerror or exc}") from exc

    raw = dict(ready)
    for slot in pfd:
        if slot.fd == FD_RETIRED:
            slot.revents = 0
        else:
            # Krok 2 emulacji: maska wpisu + flagi niemaskowalne.
            slot.revents = raw.get(slot.fd, 0) & (slot.events | _UNMASKABLE)
    return len(ready)


def _handle_revents(
    pfd: list[Slot], net_sock: socket.socket, nflag_shutdown: bool
) -> None:
    """Interpretacja ``revents``: błędy, HUP i kaskada zamknięć.

    Mapowanie: [netcat.c:1166-1199]. **Kolejność tych sprawdzeń ma znaczenie**
    i jest odtworzona 1:1.
    """
    # [netcat.c:1166-1171] błąd trwały na dowolnym slocie → wycofanie
    for slot in pfd:
        if slot.revents & (select.POLLERR | select.POLLNVAL):
            slot.fd = FD_RETIRED

    # [netcat.c:1172-1181] „reading is possible after HUP"
    #
    # Najsubtelniejsze miejsce w całej pętli. ``POLLHUP`` oznacza, że druga
    # strona zamknęła kanał — ale w buforze jądra mogą jeszcze leżeć
    # niewyczytane dane, i wtedy jądro ustawia RÓWNOCZEŚNIE ``POLLIN``.
    # Wycofanie slotu przy samym ``POLLHUP`` gubiłoby ogon strumienia.
    # Dlatego warunkiem jest „HUP i NIE IN".
    for idx in (POLL_STDIN, POLL_NETIN):
        slot = pfd[idx]
        if (
            slot.events & select.POLLIN
            and slot.revents & select.POLLHUP
            and not slot.revents & select.POLLIN
        ):
            slot.fd = FD_RETIRED

    # [netcat.c:1183-1187] HUP na kierunku wyjściowym do sieci
    netout = pfd[POLL_NETOUT]
    if netout.revents & select.POLLHUP:
        if netout.fd != FD_RETIRED and nflag_shutdown:
            _shutdown(net_sock, socket.SHUT_WR)
        netout.fd = FD_RETIRED

    # [netcat.c:1188-1190] „if HUP, stop watching stdout"
    if pfd[POLL_STDOUT].revents & select.POLLHUP:
        pfd[POLL_STDOUT].fd = FD_RETIRED

    # [netcat.c:1191-1193] „if no net out, stop watching stdin"
    # Nie ma gdzie wysyłać → nie ma po co czytać.
    if netout.fd == FD_RETIRED:
        pfd[POLL_STDIN].fd = FD_RETIRED

    # [netcat.c:1194-1199] „if no stdout, stop watching net in"
    # Nie ma gdzie wypisać → zamykamy odbiór, żeby druga strona dostała
    # sygnał (TCP wyśle RST na dalsze dane) zamiast pisać w próżnię.
    if pfd[POLL_STDOUT].fd == FD_RETIRED:
        netin = pfd[POLL_NETIN]
        if netin.fd != FD_RETIRED:
            _shutdown(net_sock, socket.SHUT_RD)
        netin.fd = FD_RETIRED


def _shutdown(sock: socket.socket, how: int) -> None:
    """``shutdown(2)`` z tolerancją na błędy.

    Oryginał ignoruje wynik ``shutdown()`` [netcat.c:1185, 1197, 1254, 1295].
    Jeśli druga strona już zniknęła, ``ENOTCONN`` jest normalną odpowiedzią
    i nie ma na nią sensownej reakcji.

    W C ``shutdown()`` bierze surowy deskryptor, więc wołane jest wprost na
    ``pfd[i].fd``. W Pythonie ``shutdown`` jest metodą obiektu ``socket``
    (``os`` nie ma odpowiednika), dlatego ``readwrite()`` dostaje obiekt
    socketu, a nie liczbę. Jedyna konsekwencja: w oryginale shutdown nigdy
    nie dotyczy stdin/stdout, i u nas też nie może — co jest raczej zaletą.
    """
    try:
        sock.shutdown(how)
    except OSError:
        pass


def readwrite(
    net_sock: socket.socket,
    opts: Options,
    state: RelayState | None = None,
    *,
    stdin_fd: int = 0,
    stdout_fd: int = 1,
) -> None:
    """Pętla przekazywania danych stdin↔socket. [netcat.c:1104-1303]

    ``net_sock`` to obiekt socketu (potrzebny dla ``shutdown()``); jego
    deskryptor trafia do dwóch slotów ``pfd``, dokładnie jak ``net_fd`` w C.

    ``stdin_fd``/``stdout_fd`` są parametrami (w C to stałe ``STDIN_FILENO``
    i ``STDOUT_FILENO``) wyłącznie po to, żeby testy mogły podstawić parę
    deskryptorów i sprawdzić maszynę stanów bez podprocesu. Semantyka się
    nie zmienia.
    """
    net_fd = net_sock.fileno()
    if state is None:
        state = RelayState()

    # [netcat.c:1116-1118] „don't read from stdin if requested" (-d)
    if opts.dflag:
        stdin_fd = FD_RETIRED

    # [netcat.c:1109-1112] dwa bufory po BUFSIZE. ``len(buf)`` zastępuje
    # ``bufpos``; ``bytearray`` nie rośnie powyżej BUFSIZE, bo fillbuf()
    # czyta najwyżej ``BUFSIZE - len(buf)``.
    stdinbuf = bytearray()
    netinbuf = bytearray()

    # [netcat.c:1120-1134]
    pfd = [Slot(FD_RETIRED, 0) for _ in range(NUM_SLOTS)]
    pfd[POLL_STDIN] = Slot(stdin_fd, select.POLLIN)
    pfd[POLL_NETOUT] = Slot(net_fd, 0)
    pfd[POLL_NETIN] = Slot(net_fd, select.POLLIN)
    pfd[POLL_STDOUT] = Slot(stdout_fd, 0)

    poller = select.poll()
    registered: dict[int, int] = {}

    #: Deadline dla ``-q`` (łatka Debiana, brak w upstream). Ustawiany
    #: dopiero po EOF na stdin — zobacz koniec pętli.
    quit_deadline: float | None = None

    try:
        while True:
            # ---------------------------------------------------------------
            # Warunki wyjścia. [netcat.c:1136-1147]
            # Sprawdzane na POCZĄTKU iteracji, przed poll().
            # ---------------------------------------------------------------

            # [netcat.c:1137-1140] „both inputs are gone, buffers are empty"
            # Bez tego: po EOF z obu stron pętla wisiałaby na poll() bez
            # żadnego uzbrojonego slotu.
            if (
                pfd[POLL_STDIN].fd == FD_RETIRED
                and pfd[POLL_NETIN].fd == FD_RETIRED
                and not stdinbuf
                and not netinbuf
            ):
                return

            # [netcat.c:1141-1143] „both outputs are gone, we can't continue"
            # Nie ma gdzie oddać danych — dalsze czytanie byłoby bezcelowe.
            if (
                pfd[POLL_NETOUT].fd == FD_RETIRED
                and pfd[POLL_STDOUT].fd == FD_RETIRED
            ):
                return

            # [netcat.c:1144-1147] „listen and net in gone, queues empty"
            # Bez tego serwer z -l po rozłączeniu klienta wisiałby, czekając
            # na stdin, zamiast wrócić do accept() (albo zakończyć).
            if (
                opts.lflag
                and pfd[POLL_NETIN].fd == FD_RETIRED
                and not stdinbuf
                and not netinbuf
            ):
                return

            # Czwarty warunek, nieobecny w upstream: deadline -q.
            # Umieszczony razem z pozostałymi warunkami wyjścia, bo jest
            # warunkiem terminacji. Pomiar potwierdza taką semantykę:
            # ``-q 2`` kończy się po 2,007 s (docs/tablice-decyzyjne.md §9.4).
            if quit_deadline is not None and time.monotonic() >= quit_deadline:
                return

            # [netcat.c:1149-1153] -i: sleep na początku iteracji.
            #
            # Komentarz autora w oryginale jest szczery: pomoc mówi, że -i to
            # „wait between lines sent", ale netcat czyta i pisze dowolne
            # porcje danych i nie chce skanować bufora w poszukiwaniu znaków
            # nowej linii — więc sleep w pętli „is as good as it gets".
            #
            # Uwaga: w Debianie 1.226 ta flaga jest mierzalnym NO-OPEM
            # (docs/tablice-decyzyjne.md §9.1). Implementujemy semantykę kodu upstream.
            if opts.iflag:
                time.sleep(opts.iflag)

            num_fds = _poll_once(
                poller, registered, pfd, _poll_timeout(opts.timeout_ms, quit_deadline)
            )

            # [netcat.c:1162-1164] „timeout happened"
            # Zarówno idle timeout z -w, jak i wygaśnięcie deadline'u -q.
            if num_fds == 0:
                return

            _handle_revents(pfd, net_sock, opts.Nflag)

            # ---------------------------------------------------------------
            # Cztery bloki I/O. [netcat.c:1201-1289]
            #
            # Każdy ma ten sam szkielet i ten sam wzorzec backpressure:
            #   po fillbuf:  bufor niepusty  → uzbrój przeciwny slot na POLLOUT
            #                bufor pełny     → events = 0 (przestań czytać)
            #   po drainbuf: bufor pusty     → events = 0 (nie ma co wysyłać)
            #                bufor niepełny  → uzbrój slot wejściowy na POLLIN
            #
            # Bez tego szybki producent (np. ``< /dev/zero``) zjadłby pamięć.
            # ---------------------------------------------------------------

            # [netcat.c:1201-1217] stdin → stdinbuf
            if pfd[POLL_STDIN].revents & select.POLLIN and len(stdinbuf) < BUFSIZE:
                ret = fillbuf(pfd[POLL_STDIN].fd, stdinbuf)
                if ret == WANT_POLLIN:
                    pfd[POLL_STDIN].events = select.POLLIN
                elif ret == WANT_POLLOUT:
                    pfd[POLL_STDIN].events = select.POLLOUT
                elif ret in (IO_EOF, IO_ERROR):
                    # [netcat.c:1212-1213] EOF i błąd traktowane identycznie
                    pfd[POLL_STDIN].fd = FD_RETIRED
                if stdinbuf:
                    pfd[POLL_NETOUT].events = select.POLLOUT
                if len(stdinbuf) == BUFSIZE:
                    pfd[POLL_STDIN].events = 0

            # [netcat.c:1218-1237] stdinbuf → sieć
            if pfd[POLL_NETOUT].revents & select.POLLOUT and stdinbuf:
                ret = drainbuf(pfd[POLL_NETOUT].fd, stdinbuf)
                if ret == WANT_POLLIN:
                    pfd[POLL_NETOUT].events = select.POLLIN
                elif ret == WANT_POLLOUT:
                    pfd[POLL_NETOUT].events = select.POLLOUT
                elif ret == IO_ERROR:
                    pfd[POLL_NETOUT].fd = FD_RETIRED
                if not stdinbuf:
                    pfd[POLL_NETOUT].events = 0
                if len(stdinbuf) < BUFSIZE:
                    pfd[POLL_STDIN].events = select.POLLIN

            # [netcat.c:1238-1272] sieć → netinbuf
            if pfd[POLL_NETIN].revents & select.POLLIN and len(netinbuf) < BUFSIZE:
                ret = fillbuf(pfd[POLL_NETIN].fd, netinbuf)
                if ret == WANT_POLLIN:
                    pfd[POLL_NETIN].events = select.POLLIN
                elif ret == WANT_POLLOUT:
                    pfd[POLL_NETIN].events = select.POLLOUT
                elif ret == IO_ERROR:
                    pfd[POLL_NETIN].fd = FD_RETIRED

                # [netcat.c:1251-1255] „eof on net in - remove from pfd"
                if ret == IO_EOF:
                    _shutdown(net_sock, socket.SHUT_RD)
                    pfd[POLL_NETIN].fd = FD_RETIRED

                # [netcat.c:1256-1261] -W: limit odczytów z sieci.
                #
                # Licznik rośnie przy KAŻDYM wejściu w ten blok, także gdy
                # fillbuf zwrócił EOF albo EAGAIN — to literalna semantyka
                # ``++recvcount`` z oryginału. Man page mówi „packets", ale
                # dla TCP granice read() nie mają nic wspólnego z granicami
                # segmentów: pomiar w docs/tablice-decyzyjne.md §9.3 pokazuje,
                # że ``-W 2``
                # przepuszcza dwa odczyty, nie dwa pakiety.
                if opts.recvlimit > 0:
                    state.recvcount += 1
                    if state.recvcount >= opts.recvlimit:
                        if pfd[POLL_NETIN].fd != FD_RETIRED:
                            _shutdown(net_sock, socket.SHUT_RD)
                        pfd[POLL_NETIN].fd = FD_RETIRED
                        pfd[POLL_STDIN].fd = FD_RETIRED

                if netinbuf:
                    pfd[POLL_STDOUT].events = select.POLLOUT
                if len(netinbuf) == BUFSIZE:
                    pfd[POLL_NETIN].events = 0
                # [netcat.c:1267-1271] tu oryginał woła atelnet() dla -t.
                # Nie implementujemy telnetu — zobacz README „Czego tu nie ma".

            # [netcat.c:1273-1289] netinbuf → stdout
            if pfd[POLL_STDOUT].revents & select.POLLOUT and netinbuf:
                ret = drainbuf(pfd[POLL_STDOUT].fd, netinbuf)
                if ret == WANT_POLLIN:
                    pfd[POLL_STDOUT].events = select.POLLIN
                elif ret == WANT_POLLOUT:
                    pfd[POLL_STDOUT].events = select.POLLOUT
                elif ret == IO_ERROR:
                    pfd[POLL_STDOUT].fd = FD_RETIRED
                if not netinbuf:
                    pfd[POLL_STDOUT].events = 0
                if len(netinbuf) < BUFSIZE:
                    pfd[POLL_NETIN].events = select.POLLIN

            # ---------------------------------------------------------------
            # Propagacja EOF. [netcat.c:1291-1300]
            #
            # NAJWAŻNIEJSZE MIEJSCE W CAŁYM PROGRAMIE.
            # ---------------------------------------------------------------

            # [netcat.c:1291-1296] „stdin gone and queue empty?"
            #
            # Bez -N netcat po EOF na stdin TYLKO przestaje pisać do socketu —
            # nie wysyła FIN. Druga strona nigdy się nie dowiaduje, że
            # nadawanie się skończyło, więc transfer pliku nigdy się nie
            # kończy. Pomiar (docs/tablice-decyzyjne.md §1): bez -N klient wisiał do
            # zabicia (7,701 s), z -N wyszedł po 0,008 s.
            #
            # Dlatego przykład „DATA TRANSFER" w nc(1) używa ``nc -N``.
            if pfd[POLL_STDIN].fd == FD_RETIRED and not stdinbuf:
                if pfd[POLL_NETOUT].fd != FD_RETIRED and opts.Nflag:
                    _shutdown(net_sock, socket.SHUT_WR)
                pfd[POLL_NETOUT].fd = FD_RETIRED

                # -q (łatka Debiana, brak w upstream — docs/tablice-decyzyjne.md §9.4).
                # Uzbrajamy dopiero tutaj, bo semantyka to „po EOF na stdin".
                #   -q 0  → wyjdź natychmiast (dane w locie przepadają)
                #   -q N>0→ czekaj N sekund, potem wyjdź bezwarunkowo
                #   -q N<0→ czekaj w nieskończoność (== brak flagi)
                if opts.qflag is not None and opts.qflag >= 0:
                    if opts.qflag == 0:
                        return
                    if quit_deadline is None:
                        quit_deadline = time.monotonic() + opts.qflag

            # [netcat.c:1297-1300] „net in gone and queue empty?"
            # Sieć zamknięta i wszystko wypisane → nie ma po co trzymać stdout.
            if pfd[POLL_NETIN].fd == FD_RETIRED and not netinbuf:
                pfd[POLL_STDOUT].fd = FD_RETIRED
    finally:
        for fd in list(registered):
            try:
                poller.unregister(fd)
            except (OSError, KeyError):  # pragma: no cover
                pass
