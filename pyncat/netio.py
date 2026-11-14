"""``fillbuf()`` i ``drainbuf()`` — przenoszenie bajtów między fd a buforem.

Mapowanie: ``netcat.c`` linie 1305–1367.

Te dwie funkcje są celowo trywialne i celowo *nie* próbują być sprytne.
Cała inteligencja — kto czyta, kto pisze, kiedy przestać — siedzi w
``relay.py``. Tutaj jest tylko jeden ``read()``/``write()`` i obsługa trzech
rzeczy, które w kodzie sieciowym psują się najczęściej:

1. **Zapis częściowy.** ``write()`` ma prawo przyjąć mniej, niż mu dano, i to
   jest normalna sytuacja, nie błąd. Oryginał przesuwa wtedy ogon bufora na
   początek przez ``memmove`` [netcat.c:1328-1332].
2. **``EAGAIN``** na nieblokującym sockecie — to nie błąd, to „spróbuj potem".
3. **EOF kontra błąd** — ``read()`` zwracające 0 i −1 to dwie różne historie.

Odpowiednik ``memmove`` w Pythonie to ``del buf[:n]`` na ``bytearray``:
CPython realizuje to jednym ``memmove`` na buforze obiektu, więc złożoność
jest ta sama co w C (O(pozostałych bajtów)). Pomiar kosztu tej operacji —
``docs/c-vs-python.md`` §3.3.
"""

from __future__ import annotations

import os

from .constants import (
    BUFSIZE,
    FD_RETIRED,
    IO_EOF,
    IO_ERROR,
    WANT_POLLIN,
    WANT_POLLOUT,
)


def fillbuf(fd: int, buf: bytearray) -> int:
    """Doczytaj do ``buf`` z deskryptora ``fd``. [netcat.c:1337-1367]

    ``buf`` pełni rolę pary ``(buf, bufpos)`` z oryginału — ``len(buf)``
    to ``bufpos``, więc wolne miejsce to ``BUFSIZE - len(buf)``.
    [netcat.c:1339] ``size_t num = BUFSIZE - *bufpos;``

    Zwraca: liczbę bajtów > 0, ``IO_EOF`` (0), ``IO_ERROR`` (−1) albo
    ``WANT_POLLIN`` (−2) przy ``EAGAIN``/``EINTR``.
    """
    # [netcat.c:1342-1343] slot już wycofany — nie ma czego czytać
    if fd == FD_RETIRED:
        return IO_ERROR

    num = BUFSIZE - len(buf)
    if num <= 0:  # pragma: no cover — relay nie woła fillbuf przy pełnym buforze
        return WANT_POLLIN

    try:
        chunk = os.read(fd, num)
    except (BlockingIOError, InterruptedError):
        # [netcat.c:1354-1356] „don't treat EAGAIN, EINTR as error"
        #
        # InterruptedError jest w CPythonie praktycznie martwą gałęzią: od
        # PEP 475 (Python 3.5) interpreter sam ponawia syscalle przerwane
        # przez sygnał, jeśli handler nie podniósł wyjątku. Zostawiamy ją
        # dla symetrii z oryginałem i dla egzotycznych implementacji.
        return WANT_POLLIN
    except OSError:
        # ECONNRESET, EPIPE, EBADF… — błąd trwały, slot do wycofania.
        return IO_ERROR

    if not chunk:
        # [netcat.c:1358] ``if (n <= 0) return n;`` — read() == 0 to EOF
        return IO_EOF

    buf += chunk
    return len(chunk)


def drainbuf(fd: int, buf: bytearray) -> int:
    """Wypchnij zawartość ``buf`` do deskryptora ``fd``. [netcat.c:1305-1335]

    Zwraca: liczbę bajtów > 0, ``IO_ERROR`` (−1) albo ``WANT_POLLOUT`` (−3).
    Po udanym zapisie usuwa z ``buf`` to, co poszło — również przy zapisie
    częściowym.
    """
    # [netcat.c:1310-1311]
    if fd == FD_RETIRED:
        return IO_ERROR

    if not buf:  # pragma: no cover — relay nie woła drainbuf przy pustym buforze
        return IO_EOF

    try:
        n = os.write(fd, buf)
    except (BlockingIOError, InterruptedError):
        # [netcat.c:1322-1324]
        return WANT_POLLOUT
    except OSError:
        # Tu wpada m.in. BrokenPipeError (EPIPE). Python domyślnie ustawia
        # SIGPIPE na SIG_IGN i zamienia go na wyjątek — to ten sam efekt, co
        # ``signal(SIGPIPE, SIG_IGN)`` w [netcat.c:174]; ``app.py`` ustawia to
        # jawnie, żeby nie zależeć od domyślnej konfiguracji interpretera.
        return IO_ERROR

    if n <= 0:  # pragma: no cover — os.write nie zwraca 0 dla niepustego buf
        return n

    # [netcat.c:1328-1332] ``memmove(buf, buf + n, adjust); *bufpos -= n;``
    # ``del buf[:n]`` to w CPythonie jeden memmove — identyczna złożoność.
    del buf[:n]
    return n
