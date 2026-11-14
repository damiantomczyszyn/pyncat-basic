"""Stałe odwzorowane z ``netcat.c``.

Mapowanie: ``netcat.c`` linie 60–123 (``#define`` + zmienne globalne).

Wszystkie numery linii w tym pakiecie odnoszą się do
``$OpenBSD: netcat.c,v 1.241 2026/10/31 03:42:10 tb Exp $`` — zobacz
``docs/netcat-architecture.md``.
"""

from __future__ import annotations

# --- rozmiary -------------------------------------------------------------

#: Rozmiar obu buforów relay. [netcat.c:69] ``#define BUFSIZE 16384``
#:
#: Ta wartość nie jest przypadkowa i nie należy jej „optymalizować": decyduje
#: o liczbie syscalli na megabajt, a tym samym o całym profilu wydajnościowym
#: pętli relay. Pomiary wpływu BUFSIZE na przepustowość w obu językach:
#: ``docs/c-vs-python.md``, sekcja „Gdzie idzie czas".
BUFSIZE = 16384

#: [netcat.c:62] ``#define PORT_MAX 65535``
PORT_MAX = 65535

#: Rozmiar bufora dla ``recvfrom(MSG_PEEK)`` w trybie ``-u -l``.
#: [netcat.c:606] ``char buf[2048];``
UDP_PEEK_BUFSIZE = 2048

#: Backlog ``listen()``. [netcat.c:1080] ``listen(s, 1)`` — netcat obsługuje
#: jedno połączenie naraz, drugi klient czeka w kolejce jądra.
LISTEN_BACKLOG = 1

#: Liczba zapisów w ``udptest()``. [netcat.c:1542] ``for (i = 0; i <= 3; i++)``
UDPTEST_WRITES = 4

# --- indeksy slotów w tablicy pollfd --------------------------------------
# [netcat.c:65-68]
#
# To nie są „po prostu" cztery deskryptory. POLL_NETOUT i POLL_NETIN wskazują
# na TEN SAM socket, ale mają osobne maski ``events`` i osobne „życie".
# Half-close jest w netcacie modelowany wyłącznie przez wycofanie jednego
# z tych dwóch slotów (``fd = -1``), podczas gdy drugi żyje dalej.
#
# Konsekwencja dla Pythona: modułu ``selectors`` nie da się tu użyć, bo
# ``SelectorKey`` jest indeksowany po deskryptorze — nie można zarejestrować
# tego samego fd dwa razy z różnymi maskami. Dlatego ``relay.py`` używa
# bezpośrednio ``select.poll()``. Uzasadnienie: docs/c-vs-python.md §3.1.

POLL_STDIN = 0
POLL_NETOUT = 1
POLL_NETIN = 2
POLL_STDOUT = 3

#: Liczba slotów w ``pfd[]``. [netcat.c:1106] ``struct pollfd pfd[4];``
NUM_SLOTS = 4

#: Sentinel „slot wycofany". W C to literalne ``-1`` wpisywane w ``pfd[i].fd``;
#: ``poll(2)`` ignoruje wpisy z ujemnym deskryptorem. Odtwarzamy dosłownie,
#: zamiast używać ``None``, żeby kod dał się czytać obok oryginału.
FD_RETIRED = -1

# --- wartości zwracane przez fillbuf()/drainbuf() -------------------------
# [netcat.c:1305-1367] + libtls
#
# Oryginał koduje w jednej wartości ``ssize_t`` cztery różne rzeczy:
#
#   n > 0                  liczba przeniesionych bajtów
#   0                      EOF (``read()`` zwrócił 0)
#   -1                     błąd trwały — wycofaj slot
#   TLS_WANT_POLLIN  (-2)  spróbuj ponownie, czekaj na gotowość do czytania
#   TLS_WANT_POLLOUT (-3)  spróbuj ponownie, czekaj na gotowość do pisania
#
# Bez TLS sentinele -2/-3 służą tylko do zakodowania „EAGAIN/EINTR", żeby
# odróżnić to od błędu trwałego. Nazywamy je, bo trzy różne liczby ujemne
# o trzech różnych znaczeniach to dokładnie ten rodzaj kodu, który w Pythonie
# należy nazwać.

IO_EOF = 0
IO_ERROR = -1
#: [libtls] ``TLS_WANT_POLLIN``
WANT_POLLIN = -2
#: [libtls] ``TLS_WANT_POLLOUT``
WANT_POLLOUT = -3

# --- kody wyjścia ---------------------------------------------------------
# [netcat.c:168] ``ret = 1;`` — pesymistyczna inicjalizacja; zerowane dopiero
# po udanym connect [netcat.c:704] albo od razu w trybie listen [netcat.c:562].
# Pełna tablica decyzyjna: docs/tablice-decyzyjne.md §4.

EXIT_OK = 0
EXIT_FAIL = 1

#: Nazwa programu w komunikatach diagnostycznych — odpowiednik
#: ``__progname`` używanego przez ``err(3)``/``warn(3)``.
PROGNAME = "pyncat"
