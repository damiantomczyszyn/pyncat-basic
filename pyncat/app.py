"""Ścieżka wykonania: ``main()``, pętla klienta, pętla nasłuchu.

Mapowanie: ``netcat.c`` 152–730 — ta część ``main()``, która *robi* coś po
sparsowaniu opcji. Podział na ``_run_client`` / ``_run_listen`` odpowiada
wielkiemu ``if (lflag) … else …`` z [netcat.c:561] i [netcat.c:672].
"""

from __future__ import annotations

import signal
import socket
import sys
import time

from .cli import (
    HELP,
    USAGE,
    FatalError,
    Options,
    UsageError,
    _HelpRequested,
    parse_args,
)
from .constants import EXIT_FAIL, EXIT_OK, UDP_PEEK_BUFSIZE
from .endpoints import local_listen, remote_connect, udptest
from .ports import build_ports
from .relay import RelayState, readwrite
from .report import connection_info, errx, report_sock, warnx

#: Kod wyjścia dla przerwania z klawiatury — konwencja powłoki (128 + SIGINT).
#: Oryginał nie ustawia handlera, więc proces ginie od sygnału; Python zamienia
#: to na wyjątek, a my odtwarzamy kod, który powłoka by zobaczyła.
EXIT_SIGINT = 130


def _run_client(opts: Options) -> int:
    """Tryb klienta: pętla po portach. [netcat.c:673-730]

    Kod wyjścia — pełna tablica w ``docs/tablice-decyzyjne.md`` §4:

    ``ret`` startuje z 1 [netcat.c:168] i jest zerowane po pierwszym udanym
    ``connect()`` [netcat.c:704]. Nieudane połączenie robi ``continue``
    **bez dotknięcia ``ret``** [netcat.c:701-702]. Stąd kwirk, który łatwo
    przegapić: w skanie zakresu wystarczy **jeden** otwarty port, by proces
    skończył się z 0, nawet jeśli wszystkie następne są zamknięte.
    Zweryfikowane na ``nc.openbsd 1.226`` (docs/tablice-decyzyjne.md §4).
    """
    assert opts.uport is not None
    ret = EXIT_FAIL

    # [netcat.c:677] ``build_ports(uport);``
    ports = build_ports(opts.uport, opts.uflag, opts.rflag)

    # Jeden obiekt stanu na cały proces — odtwarza globalny ``recvcount``
    # [netcat.c:114]. Przy wielu portach licznik -W akumuluje się, tak jak w C.
    state = RelayState()

    sock: socket.socket | None = None
    try:
        for index, port in enumerate(ports):
            # [netcat.c:681-682] ``if (s != -1) close(s);``
            if sock is not None:
                sock.close()
                sock = None

            # ODSTĘPSTWO OD KODU UPSTREAM (świadome, wymagane przez zadanie):
            # przerwa między portami. nc(1) linie 158-162 obiecuje „Also causes
            # a delay time between connections to multiple ports", ale w kodzie
            # upstream ``iflag`` występuje wyłącznie w pętli readwrite
            # [netcat.c:1153], a w Debianie 1.226 jest mierzalnym no-opem
            # (docs/tablice-decyzyjne.md §9.1). Implementujemy to, co obiecuje
            # dokumentacja.
            if opts.iflag and index > 0:
                time.sleep(opts.iflag)

            assert opts.host is not None
            sock, ipaddr = remote_connect(opts.host, port, opts)
            if sock is None:
                continue

            ret = EXIT_OK

            # [netcat.c:705-719]
            if opts.vflag or opts.zflag:
                print_info = 1
                if opts.uflag:
                    # [netcat.c:709-715] „For UDP, make sure we are connected."
                    print_info = udptest(sock, opts.zflag)
                    if print_info == -1:
                        # Jedyne miejsce, w którym ``ret`` wraca do 1 po
                        # wcześniejszym sukcesie.
                        ret = EXIT_FAIL
                        continue
                if print_info == 1:
                    connection_info(
                        opts.host,
                        port,
                        "udp" if opts.uflag else "tcp",
                        ipaddr,
                        opts.nflag,
                    )

            # [netcat.c:725-726] ``if (!zflag) readwrite(s, tls_ctx);``
            if not opts.zflag:
                readwrite(sock, opts, state)
    finally:
        # [netcat.c:733-734]
        if sock is not None:
            sock.close()

    return ret


def _run_listen(opts: Options) -> int:
    """Tryb nasłuchu: trzy warianty w jednej pętli. [netcat.c:561-657]

    Kod wyjścia to zawsze 0 [netcat.c:562] — niezależnie od tego, czy
    ktokolwiek się połączył.

    Trzy warianty, bo UDP nie ma połączeń:

    ============== ============================================================
    ``-u -k``      bez ``connect()``: socket odbiera od wielu nadawców, ale nie
                   ma do kogo pisać (``send()`` → ``ENOTCONN``). Komunikacja
                   jednokierunkowa. [netcat.c:588-598]
    ``-u`` bez -k  ``recvfrom(MSG_PEEK)`` + ``connect()`` do pierwszego nadawcy.
                   ``MSG_PEEK`` jest **kluczowe**: datagram zostaje w buforze
                   i zostanie normalnie wczytany przez ``readwrite()``, więc
                   pierwsze dane nie przepadają. [netcat.c:599-628]
    TCP            ``accept()``, nowy deskryptor na połączenie.
                   [netcat.c:629-651]
    ============== ============================================================
    """
    assert opts.uport is not None
    ret = EXIT_OK
    state = RelayState()

    listener: socket.socket | None = None
    try:
        while True:
            # [netcat.c:581-585] Listener jest zamykany i tworzony OD NOWA
            # w każdej iteracji ``-k``. To nie jest oczywiste i ma skutek:
            # między ``close()`` a ``bind()`` port jest wolny, więc trzeba
            # ``SO_REUSEADDR`` (zobacz komentarz w ``local_listen``).
            if listener is not None:
                listener.close()
            listener = local_listen(opts.host, opts.uport, opts)
            if listener is None:
                # [netcat.c:586-587] ``err(1, NULL)``
                raise errx(
                    f"nie udało się nasłuchiwać na porcie {opts.uport}"
                    + (f" (adres {opts.host})" if opts.host else "")
                )

            if opts.uflag and opts.kflag:
                readwrite(listener, opts, state)
            elif opts.uflag:
                data_len = UDP_PEEK_BUFSIZE
                _peeked, peer = listener.recvfrom(data_len, socket.MSG_PEEK)
                listener.connect(peer)
                if opts.vflag:
                    report_sock("Połączenie przychodzące od", peer)
                readwrite(listener, opts, state)
            else:
                conn, peer = listener.accept()
                # [netcat.c:633-635] ``accept4(…, SOCK_NONBLOCK)`` — nieblokujący
                # jest socket POŁĄCZENIA, nie listener.
                conn.setblocking(False)
                if opts.vflag:
                    report_sock("Połączenie przychodzące od", peer)
                try:
                    readwrite(conn, opts, state)
                finally:
                    conn.close()

            # [netcat.c:655-656]
            if not opts.kflag:
                break
    finally:
        if listener is not None:
            listener.close()

    return ret


def main(argv: list[str] | None = None) -> int:
    """Punkt wejścia. Zwraca kod wyjścia, nie wywołuje ``sys.exit()``.

    Dzięki temu całość da się przetestować wewnątrzprocesowo; podproces
    odpala ``__main__.py``.
    """
    if argv is None:
        argv = sys.argv[1:]

    # [netcat.c:174] ``signal(SIGPIPE, SIG_IGN);``
    #
    # CPython i tak ustawia SIGPIPE na SIG_IGN i zamienia EPIPE na
    # ``BrokenPipeError``, ale ustawiamy to jawnie: bez tego program ginąłby
    # po cichu, gdy odbiorca zamknie połączenie w trakcie zapisu, i
    # ``drainbuf()`` nigdy nie dostałby szansy zwrócić ``IO_ERROR``.
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    except (AttributeError, ValueError):  # pragma: no cover
        # AttributeError: platforma bez SIGPIPE. ValueError: nie główny wątek.
        pass

    try:
        opts = parse_args(argv)
    except _HelpRequested:
        # [netcat.c:219-221] ``help()`` kończy z kodem 0
        sys.stdout.write(HELP)
        return EXIT_OK
    except UsageError as exc:
        warnx(str(exc))
        sys.stderr.write(USAGE)
        return EXIT_FAIL
    except FatalError as exc:
        warnx(str(exc))
        return EXIT_FAIL

    try:
        if opts.lflag:
            return _run_listen(opts)
        return _run_client(opts)
    except FatalError as exc:
        warnx(str(exc))
        return EXIT_FAIL
    except KeyboardInterrupt:  # pragma: no cover
        return EXIT_SIGINT
    except BrokenPipeError:  # pragma: no cover
        # stdout zamknięty przez odbiorcę (``pyncat … | head``). To nie błąd.
        return EXIT_OK
