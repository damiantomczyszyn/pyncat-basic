"""Tworzenie socketów: ``remote_connect()``, ``timeout_connect()``,
``local_listen()``, ``udptest()``.

Mapowanie:

* ``remote_connect()``  — ``netcat.c`` 930–1004
* ``timeout_connect()`` — ``netcat.c`` 1006–1037
* ``local_listen()``    — ``netcat.c`` 1039–1102
* ``udptest()``         — ``netcat.c`` 1534–1550

Wszystko, co dotyczy *nawiązania* połączenia. Pętla przekazywania danych jest
w ``relay.py``; ten moduł kończy pracę w momencie, w którym socket jest
połączony albo nasłuchuje.
"""

from __future__ import annotations

import errno
import os
import select
import socket
from typing import TYPE_CHECKING

from .constants import LISTEN_BACKLOG, UDPTEST_WRITES
from .report import errx, report_sock, warn

if TYPE_CHECKING:  # pragma: no cover
    from .cli import Options


def timeout_connect(
    sock: socket.socket, sockaddr: tuple, timeout_ms: int | None
) -> int:
    """Nieblokujący ``connect()`` z limitem czasu. [netcat.c:1006-1037]

    Zwraca ``0`` przy sukcesie, w przeciwnym razie numer ``errno``.

    Schemat jest klasyczny i wart zapamiętania, bo wraca w każdym kodzie
    sieciowym:

    1. socket jest nieblokujący, więc ``connect()`` zwraca ``EINPROGRESS``,
    2. czekamy na gotowość do **pisania** (``POLLOUT``) — tak jądro sygnalizuje
       zakończenie three-way handshake,
    3. ``poll()`` zwrócił gotowość ≠ sukces. Trzeba przeczytać ``SO_ERROR``,
       bo odrzucone połączenie (RST) też ustawia ``POLLOUT``. Pominięcie tego
       kroku to najczęstszy błąd w ręcznie pisanych skanerach portów —
       wszystkie porty wychodzą wtedy jako otwarte.

    ``connect_ex()`` jest tu dokładnym odpowiednikiem ``connect()`` z C:
    zwraca ``errno`` zamiast podnosić wyjątek.
    """
    err = sock.connect_ex(sockaddr)
    if err == 0:
        return 0
    # [netcat.c:1013] ``errno == EINPROGRESS``
    if err not in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EAGAIN):
        return err

    poller = select.poll()
    poller.register(sock.fileno(), select.POLLOUT)
    ready = poller.poll(timeout_ms)
    if not ready:
        # [netcat.c:1023-1025] ``errno = ETIMEDOUT``
        return errno.ETIMEDOUT
    # [netcat.c:1016-1021] ``getsockopt(s, SOL_SOCKET, SO_ERROR, …)``
    return sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)


def remote_connect(
    host: str, port: str, opts: Options
) -> tuple[socket.socket | None, str | None]:
    """Połącz się z ``host:port``. [netcat.c:930-1004]

    Zwraca ``(socket, ipaddr)`` albo ``(None, ipaddr)`` przy porażce.
    ``ipaddr`` to numeryczna postać adresu, potrzebna wyłącznie do komunikatów.

    Iteruje po **wszystkich** wynikach ``getaddrinfo()`` i zwraca pierwszy
    udany connect. Dzięki temu host z rekordami A i AAAA działa bez ``-4``/``-6``,
    a kolejność prób jest ta, którą ustalił resolver (RFC 6724).
    """
    try:
        res0 = socket.getaddrinfo(
            host, port, opts.family, opts.socktype, opts.proto, opts.ai_flags
        )
    except socket.gaierror as exc:
        # [netcat.c:937-939]
        raise errx(
            f'getaddrinfo for host "{host}" port {port}: {exc.strerror or exc}'
        ) from None

    ipaddr: str | None = None
    for af, socktype, proto, _canonname, sockaddr in res0:
        try:
            # [netcat.c:941-943] ``socket(…, ai_socktype | SOCK_NONBLOCK, …)``
            sock = socket.socket(af, socktype, proto)
        except OSError:
            continue
        sock.setblocking(False)

        # [netcat.c:947-965] -s / -p: bind przed connect
        if opts.sflag is not None or opts.pflag is not None:
            # Oryginał próbuje tu najpierw SO_BINDANY [netcat.c:949] i jawnie
            # „doesn't insist" — to opcja specyficzna dla OpenBSD (pozwala
            # bindować adres, którego host nie ma). Na Linuksie nie istnieje;
            # pomijamy, co nie zmienia zachowania w normalnym przypadku.
            try:
                ares = socket.getaddrinfo(
                    opts.sflag,
                    opts.pflag,
                    af,
                    opts.socktype,
                    opts.proto,
                    socket.AI_PASSIVE,
                )
            except socket.gaierror as exc:
                sock.close()
                raise errx(f"getaddrinfo: {exc.strerror or exc}") from None
            try:
                sock.bind(ares[0][4])
            except OSError as exc:
                sock.close()
                # [netcat.c:961] ``err(1, "bind failed")`` — błąd krytyczny,
                # nie przechodzimy do następnego adresu.
                raise errx(f"bind failed: {exc.strerror or exc}") from None

        # [netcat.c:967-979] numeryczny adres do komunikatów
        try:
            ipaddr = socket.getnameinfo(sockaddr, socket.NI_NUMERICHOST)[0]
        except socket.gaierror as exc:  # pragma: no cover
            sock.close()
            raise errx(f"getnameinfo: {exc.strerror or exc}") from None

        err = timeout_connect(sock, sockaddr, opts.timeout_ms)
        if err == 0:
            # [netcat.c:980] ``break`` — mamy połączenie
            return sock, ipaddr

        if opts.vflag:
            # [netcat.c:983-996] IP dopisujemy tylko, gdy wnosi informację
            proto_name = "udp" if opts.uflag else "tcp"
            if opts.nflag or ipaddr is None or host == ipaddr:
                warn(
                    f"nie udało się połączyć z {host} port {port} "
                    f"({proto_name})",
                    err,
                )
            else:
                warn(
                    f"nie udało się połączyć z {host} ({ipaddr}) port {port} "
                    f"({proto_name})",
                    err,
                )
        sock.close()

    return None, ipaddr


def local_listen(
    host: str | None, port: str, opts: Options
) -> socket.socket | None:
    """Socket nasłuchujący na ``host:port``. [netcat.c:1039-1102]

    Zwraca ``None``, gdy żaden adres z ``getaddrinfo()`` nie dał się zbindować.
    """
    family = opts.family

    # [netcat.c:1044-1051] „In the case of binding to a wildcard address
    # default to binding to an ipv4 address."
    #
    # Zaskakujące, ale celowe: ``pyncat -l 1234`` nasłuchuje na 0.0.0.0, nie
    # na ``::``. Bez tego ``getaddrinfo(NULL, port, AF_UNSPEC, AI_PASSIVE)``
    # zwróciłby najpierw ``::``, a na systemach z wyłączonym IPv4-mapped
    # klient IPv4 nie mógłby się połączyć.
    if host is None and family == socket.AF_UNSPEC:
        family = socket.AF_INET

    flags = opts.ai_flags | socket.AI_PASSIVE
    try:
        res0 = socket.getaddrinfo(
            host, port, family, opts.socktype, opts.proto, flags
        )
    except socket.gaierror as exc:
        # [netcat.c:1056]
        raise errx(f"getaddrinfo: {exc.strerror or exc}") from None

    sock: socket.socket | None = None
    for af, socktype, proto, _canonname, sockaddr in res0:
        try:
            sock = socket.socket(af, socktype, proto)
        except OSError:
            sock = None
            continue

        # [netcat.c:1063] oryginał ustawia ``SO_REUSEPORT``.
        #
        # ODSTĘPSTWO ŚWIADOME: używamy ``SO_REUSEADDR``.
        # Na BSD ``SO_REUSEPORT`` znaczy „pozwól przebindować port w TIME_WAIT",
        # i netcat potrzebuje tego, bo w trybie -k zamyka i tworzy listener
        # od nowa przy każdym połączeniu [netcat.c:581-585]. Na Linuksie
        # ``SO_REUSEPORT`` znaczy co innego: pozwala WIELU procesom bindować
        # ten sam port i rozdziela połączenia między nie. Dla narzędzia
        # edukacyjnego to pułapka (dwa uruchomione pyncaty cicho dzieliłyby
        # ruch), więc bierzemy ``SO_REUSEADDR``, które na Linuksie robi to,
        # czego netcat tu chce.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            # [netcat.c:1069-1071]
            sock.bind(sockaddr)
            break
        except OSError:
            sock.close()
            sock = None
            continue

    if sock is None:
        return None

    # [netcat.c:1079-1081] dla UDP NIE wołamy listen() — gniazdo datagramowe
    # nie ma kolejki połączeń.
    if not opts.uflag:
        sock.listen(LISTEN_BACKLOG)

    if opts.vflag:
        # [netcat.c:1082-1096]
        report_sock(
            "Przypisano gniazdo do" if opts.uflag else "Nasłuchuję na",
            sock.getsockname(),
        )

    return sock


def udptest(sock: socket.socket, zflag: bool) -> int:
    """Sprawdź, czy port UDP odpowiada. [netcat.c:1534-1550]

    Zwraca ``1`` (port wygląda na otwarty), ``-1`` (zamknięty) albo ``0``
    („nie testowano" — nie ma ``-z`` i stdin nie jest terminalem).

    **Dlaczego cztery zapisy, a nie jeden** [netcat.c:1542]: UDP nie ma
    handshake'u, więc pierwszy ``send()`` na zamknięty port **udaje się** —
    jądro nie wie jeszcze nic o stanie celu. Dopiero gdy wróci ICMP port
    unreachable, jądro zapamiętuje błąd w ``SO_ERROR`` i **następny** ``send()``
    zwraca ``ECONNREFUSED``. Jeden zapis nie wykryłby więc zamkniętego portu.

    Ograniczenie, którego oryginał też ma: to wyścig. Jeśli ICMP nie wróci
    przed czwartym zapisem (sieć z większym RTT, firewall tnący ICMP, pełna
    tablica stanów PF — o czym mówi komentarz w [netcat.c:1531]), port wyjdzie
    jako otwarty. Na loopbacku działa niezawodnie; przez sieć nie.

    **Uwaga:** wysyła 4 bajty ``X`` do celu, mimo że tryb nazywa się
    „Zero-I/O". Dla ``-u -z`` nazwa flagi kłamie.
    """
    # [netcat.c:1538-1539] „Only write to the socket in scan mode or
    # interactive mode." Zwraca 0, czyli „brak informacji", a NIE błąd —
    # dzięki temu ``ret`` w main() zostaje na 0 (sukces).
    if not zflag and not os.isatty(0):
        return 0

    ret = -1
    for _ in range(UDPTEST_WRITES):
        try:
            n = sock.send(b"X")
        except OSError:
            ret = -1
        else:
            # [netcat.c:1543-1546] ``ret`` odzwierciedla WYŁĄCZNIE ostatni
            # zapis — wcześniejsze sukcesy są nadpisywane.
            ret = 1 if n == 1 else -1
    return ret
