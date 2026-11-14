"""Komunikaty diagnostyczne i raportowanie — odpowiednik ``err(3)``/``warn(3)``.

Mapowanie:

* ``errx()`` / ``err()`` / ``warnx()`` / ``warn()`` — biblioteka ``err(3)``
  z BSD, używana w ``netcat.c`` wszędzie zamiast ręcznego ``fprintf(stderr)``.
* ``connection_info()`` — ``netcat.c`` linie 1552–1581.
* ``report_sock()`` — ``netcat.c`` linie 1807–1835.

**Wszystko leci na stderr**, nigdy na stdout. To nie jest kosmetyka: stdout
jest w netcacie kanałem danych. Gdyby komunikat „Connection to … succeeded!"
poszedł na stdout, zatruwałby transfer — ``nc -v host 80 > plik`` miałby
komunikat wklejony w środek pliku.

Z tego samego powodu ``relay.py`` pisze dane przez ``os.write(1, …)``, a nie
przez ``sys.stdout``: bufor Pythona nie miesza się wtedy z niczym.
"""

from __future__ import annotations

import os
import socket
import sys

from .constants import PORT_MAX, PROGNAME


class UsageError(Exception):
    """Błąd użycia — odpowiednik ``usage(1)`` z ``netcat.c:1885``."""


class FatalError(Exception):
    """Błąd krytyczny — odpowiednik ``errx(1, …)`` / ``err(1, …)``.

    Nie wywołujemy ``sys.exit()`` w głębi kodu, bo pakiet ma być testowalny
    bez uruchamiania podprocesu. Wyjątek jest przechwytywany w ``cli.main()``
    i tam zamieniany na kod wyjścia.
    """


def warnx(msg: str) -> None:
    """``warnx(3)``: komunikat bez ``errno``."""
    sys.stderr.write(f"{PROGNAME}: {msg}\n")
    sys.stderr.flush()


def warn(msg: str, err: OSError | int | None = None) -> None:
    """``warn(3)``: komunikat z dopisanym ``strerror(errno)``.

    Format dokładnie jak w C: ``"nc: <msg>: <strerror>"``.
    """
    if isinstance(err, OSError):
        reason = err.strerror or str(err)
    elif isinstance(err, int):
        reason = os.strerror(err)
    else:
        reason = None
    if reason is None:
        warnx(msg)
    else:
        sys.stderr.write(f"{PROGNAME}: {msg}: {reason}\n")
        sys.stderr.flush()


def errx(msg: str) -> FatalError:
    """``errx(1, …)``: zwraca wyjątek do podniesienia (``raise errx(...)``)."""
    return FatalError(msg)


def connection_info(
    host: str,
    port: str,
    proto: str,
    ipaddr: str | None,
    nflag: bool,
) -> None:
    """Komunikat o udanym połączeniu. [netcat.c:1552-1581]

    Format oryginału (linie 1572–1580), zweryfikowany na ``nc.openbsd 1.226``::

        Connection to 127.0.0.1 34829 port [tcp/*] succeeded!

    Nasz odpowiednik, po polsku (uzasadnienie wyboru języka: ``cli.py``,
    sekcja „Język komunikatów"; tabela odwzorowania:
    ``docs/tablice-decyzyjne.md`` §5)::

        Połączenie z 127.0.0.1 port 34829 [tcp/*] — udane

    ``service`` to ``"*"``, gdy ``getservbyport()`` nie zna portu — albo gdy
    podano ``-n`` i lookupu w ogóle nie robimy. [netcat.c:1556]
    """
    service = "*"
    if not nflag:
        # [netcat.c:1558-1562] port jest tu już zwalidowany przez build_ports(),
        # ale oryginał waliduje go ponownie — bo connection_info dostaje string.
        try:
            p = int(port)
        except ValueError:  # pragma: no cover — build_ports gwarantuje liczbę
            raise errx(f"port number invalid: {port}") from None
        if not 1 <= p <= PORT_MAX:  # pragma: no cover
            raise errx(f"port number too large: {port}")
        try:
            service = socket.getservbyport(p, proto)
        except OSError:
            service = "*"

    out = f"Połączenie z {host}"
    # [netcat.c:1574-1576] IP dopisujemy tylko wtedy, gdy wnosi informację:
    # bez -n i gdy różni się od tego, co podał użytkownik.
    if not nflag and ipaddr is not None and host != ipaddr:
        out += f" ({ipaddr})"
    out += f" port {port} [{proto}/{service}] — udane\n"
    sys.stderr.write(out)
    sys.stderr.flush()


def report_sock(msg: str, sockaddr: tuple | None) -> None:
    """Odpowiednik „Listening on …" / „Connection received on …".
    [netcat.c:1807-1835]

    ``msg`` to pełna polska fraza podawana przez wołającego („Nasłuchuję na",
    „Połączenie przychodzące od"), bo polski wymaga tu różnych przyimków,
    a oryginał sklejał wszystko z angielskim „on".

    Oryginał używa ``getnameinfo(NI_NUMERICHOST|NI_NUMERICSERV)``, więc zawsze
    pokazuje adres numerycznie, niezależnie od ``-n``. ``sockaddr`` z Pythona
    jest już numeryczny, więc nie ma tu nic do konwertowania — dla IPv6 krotka
    ma cztery elementy (``addr``, ``port``, ``flowinfo``, ``scope_id``),
    bierzemy dwa pierwsze.
    """
    if sockaddr is None:  # pragma: no cover
        sys.stderr.write(f"{msg}\n")
    else:
        sys.stderr.write(f"{msg} {sockaddr[0]} {sockaddr[1]}\n")
    sys.stderr.flush()
