"""Parsowanie specyfikacji portu — ``strtoport()`` i ``build_ports()``.

Mapowanie: ``netcat.c`` linie 1455–1532.

To najmniejszy moduł w pakiecie i jednocześnie ten, w którym najłatwiej
odejść od oryginału bez zauważenia. Netcat **nie jest nmapem**:

* obsługuje pojedynczy port (``80``), nazwę usługi (``ssh``) i jeden zakres
  (``20-25``);
* **nie obsługuje list rozdzielonych przecinkami.** ``nc host 80,443`` to błąd,
  nie dwa porty. Zweryfikowane na ``nc.openbsd 1.226``::

      $ nc -z 127.0.0.1 34701,40010
      nc: port number invalid: 34701,40010

* zakres odwrotny jest cicho zamieniany: ``25-20`` → skan 20…25.
"""

from __future__ import annotations

import random
import re
import socket

from .constants import PORT_MAX
from .report import errx

#: ``strtonum(3)`` z OpenBSD opiera się na ``strtoll(3)``, który pomija wiodące
#: białe znaki, przyjmuje opcjonalny znak, a potem wymaga, by łańcuch skończył
#: się zaraz po cyfrach (``*ep == '\0'``). Odtwarzamy to regexem, bo ``int()``
#: z Pythona jest bardziej liberalny: akceptuje podkreślenia (``1_0``) i
#: białe znaki po obu stronach, czego ``strtonum`` nie robi.
_STRTONUM_RE = re.compile(r"\A\s*[+-]?[0-9]+\Z")


def strtonum(s: str, lo: int, hi: int) -> tuple[int | None, str | None]:
    """Odpowiednik ``strtonum(3)``: zwraca ``(wartość, errstr)``.

    ``errstr`` to dokładnie jeden z trzech łańcuchów używanych przez OpenBSD:
    ``"invalid"``, ``"too small"``, ``"too large"``. Netcat wkleja go wprost
    w komunikat błędu, więc treść ma znaczenie dla zgodności wyjścia.
    """
    if not _STRTONUM_RE.match(s):
        return None, "invalid"
    value = int(s, 10)
    if value < lo:
        return None, "too small"
    if value > hi:
        return None, "too large"
    return value, None


def strtoport(portstr: str, udp: bool) -> int:
    """Port jako liczba albo nazwa usługi. [netcat.c:1455-1478]

    Kolejność jest istotna: najpierw próba liczbowa, a dopiero przy wyniku
    ``"invalid"`` zapytanie o nazwę usługi. Gdy liczba jest poza zakresem
    (``"too small"``/``"too large"``), oryginał **nie** próbuje
    ``getservbyname`` — od razu kończy błędem [netcat.c:1467-1468]. Dzięki
    temu ``nc host 99999`` daje „port number too large", a nie „service
    unknown".
    """
    proto = "udp" if udp else "tcp"

    port, errstr = strtonum(portstr, 1, PORT_MAX)
    if errstr is None:
        assert port is not None
        return port

    # [netcat.c:1467-1468] ``if (errno != EINVAL) errx(...)``
    # strtonum ustawia EINVAL dla "invalid" i ERANGE dla przekroczeń zakresu.
    if errstr != "invalid":
        raise errx(f"port number {errstr}: {portstr}")

    # [netcat.c:1469-1471]
    try:
        return socket.getservbyname(portstr, proto)
    except OSError:
        raise errx(f'service "{portstr}" unknown') from None


def build_ports(spec: str, udp: bool, randomize: bool) -> list[str]:
    """Rozwiń specyfikację portu w listę portów. [netcat.c:1480-1532]

    Zwraca listę **łańcuchów**, nie liczb — tak jak oryginalne
    ``char *portlist[]`` [netcat.c:117]. Nie jest to kaprys: lista trafia
    potem wprost do ``getaddrinfo()`` i do komunikatów, a oryginał nigdzie
    nie konwertuje jej z powrotem na liczby.

    Warunek rozpoznania zakresu [netcat.c:1486]::

        isdigit((unsigned char)*p) && (n = strchr(p, '-')) != NULL

    Pierwszy znak musi być cyfrą — dzięki temu nazwa usługi zawierająca
    minus (``http-alt``) nie jest brana za zakres. Dzielimy na **pierwszym**
    minusie (``strchr``, nie ``strrchr``), więc ``20-25-30`` daje górną
    granicę ``"25-30"``, co kończy się błędem — identycznie jak w C.
    """
    if spec[:1].isdigit() and "-" in spec:
        lo_str, _, hi_str = spec.partition("-")

        # [netcat.c:1488-1489] kolejność wywołań zachowana: najpierw górna
        # granica, potem dolna. Ma znaczenie tylko dla tego, który błąd
        # zostanie zgłoszony pierwszy przy dwóch złych wartościach.
        hi = strtoport(hi_str, udp)
        lo = strtoport(lo_str, udp)

        # [netcat.c:1490-1494] „Make sure the ports are in order: lowest->highest."
        if lo > hi:
            lo, hi = hi, lo

        ports = [str(p) for p in range(lo, hi + 1)]

        if randomize:
            # [netcat.c:1500-1509] oryginał robi permutację w stylu Knutha na
            # ``arc4random_uniform``. ``SystemRandom`` czerpie z tego samego
            # źródła entropii (``getrandom(2)``/``/dev/urandom``), a
            # ``shuffle`` to ten sam algorytm Fishera-Yatesa — więc jest to
            # odwzorowanie, nie uproszczenie.
            random.SystemRandom().shuffle(ports)

        return ports

    # [netcat.c:1511-1520] pojedynczy port albo nazwa usługi
    return [str(strtoport(spec, udp))]
