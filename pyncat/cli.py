"""Parsowanie opcji i walidacja. [netcat.c:152-490]

Mapowanie: pętla ``getopt`` [netcat.c:176-347], ustalenie pozycyjnych
argumentów [netcat.c:356-364], walidacja wzajemnej wykluczalności flag
[netcat.c:425-456], ``help()``/``usage()`` [netcat.c:1837-1898].

Dlaczego ``getopt``, a nie ``argparse``
=======================================

``argparse`` jest w Pythonie domyślnym wyborem i **tutaj jest zły**:

* ``-q -1`` — ``argparse`` traktuje łańcuch zaczynający się od ``-`` jako
  kolejną opcję, więc ujemna wartość ``-q`` wymagałaby obejścia (``-q=-1``
  albo ``--``). A ``-q -1`` to udokumentowana, działająca składnia netcata.
* ``argparse`` dopuszcza skróty długich opcji i przestawia argumenty —
  netcat tego nie robi.
* ``getopt`` z biblioteki standardowej jest **tym samym** ``getopt(3)``:
  obsługuje klastrowanie (``-ul``) i wartości zbite z flagą (``-w5``)
  dokładnie tak jak C, co zweryfikowano porównawczo.

Cena: brak automatycznego ``--help`` i trzeba ręcznie napisać ``usage``.
Przy 17 flagach to kilkanaście linii — opłaca się.

Język komunikatów
=================

Diagnostyka jest **po polsku**, zgodnie z preferencją autora projektu. Oznacza
to, że format ``stderr`` różni się od ``nc(1)``. Rozmyślnie:

* ``stdout`` to kanał **danych** i jest bajt w bajt zgodny z netcatem — to
  jedyna zgodność, która ma znaczenie dla interoperacyjności (potwierdzone
  testami ``tests/test_interop.py``),
* ``stderr`` to kanał **dla człowieka**, a człowiekiem jest tu polskojęzyczny
  czytelnik kodu.

Tabela odwzorowania „komunikat pyncat ↔ komunikat nc" jest w
``docs/tablice-decyzyjne.md`` §5.
"""

from __future__ import annotations

import getopt
import socket
from dataclasses import dataclass, field

from .constants import PORT_MAX, PROGNAME
from .ports import strtonum
from .report import FatalError, UsageError, errx

#: Flagi, które netcat ma, a ``pyncat`` świadomie pomija. Parsujemy je mimo to
#: (z poprawną informacją o argumencie), żeby zamiast „nierozpoznana opcja"
#: dać komunikat, który mówi *dlaczego* jej nie ma.
#: Uzasadnienia zbiorczo: README „Czego tu nie ma" + docs/netcat-architecture.md §7.
_OMITTED: dict[str, str] = {
    "c": "TLS (-c) pominięte: wymaga libtls, a ssl z Pythona ma inny model "
    "niż tls_*(3), więc porównanie 1:1 przestałoby być uczciwe",
    "C": "TLS (-C) pominięte — zobacz -c",
    "K": "TLS (-K) pominięte — zobacz -c",
    "R": "TLS (-R) pominięte — zobacz -c",
    "H": "TLS (-H) pominięte — zobacz -c",
    "e": "TLS (-e, oczekiwana nazwa w certyfikacie) pominięte — zobacz -c. "
    "UWAGA: to NIE jest -e z netcata 1.10 (uruchom program); tego nie ma "
    "ani tutaj, ani w OpenBSD",
    "o": "TLS (-o) pominięte — zobacz -c",
    "Z": "TLS (-Z) pominięte — zobacz -c",
    "x": "proxy (-x) pominięte: w netcacie siedzi w osobnym pliku socks.c, "
    "poza zakresem 'jednego pliku netcat.c'",
    "X": "proxy (-X) pominięte — zobacz -x",
    "P": "proxy (-P) pominięte — zobacz -x",
    "U": "gniazda uniksowe (-U) pominięte: inna rodzina adresów, nie TCP/UDP",
    "F": "-F (przekazanie deskryptora przez SCM_RIGHTS) pominięte: ciekawostka "
    "bez związku z pętlą relay",
    "t": "emulacja telnetu (-t) pominięta: negocjacja IAC z lat 80.",
    "D": "-D (SO_DEBUG) pominięte: opcja socketu poza zakresem",
    "T": "-T (ToS/TLS) pominięte: opcja socketu poza zakresem",
    "I": "-I (SO_RCVBUF) pominięte: opcja socketu poza zakresem",
    "O": "-O (SO_SNDBUF) pominięte: opcja socketu poza zakresem",
    "M": "-M (TTL) pominięte: opcja socketu poza zakresem",
    "m": "-m (min TTL) pominięte: opcja socketu poza zakresem",
    "S": "-S (TCP MD5SIG) pominięte: wymaga konfiguracji klucza w jądrze",
    "V": "-V (rtable) pominięte: specyficzne dla OpenBSD",
    "b": "-b (broadcast) pominięte: poza zakresem",
}

#: Łańcuch opcji dla ``getopt`` — zaimplementowane plus pominięte (żeby móc
#: dać im sensowny komunikat). Odpowiada [netcat.c:177-178].
_OPTSTRING = (
    # zaimplementowane
    "46dhi:klNnp:q:rs:uvW:w:z"
    # pominięte, ale rozpoznawane
    "bcDFSUt"
    "C:e:H:I:K:M:m:O:o:P:R:T:V:X:x:Z:"
)


@dataclass
class Options:
    """Stan z linii poleceń — odpowiednik zmiennych globalnych [netcat.c:77-117].

    W C to dwadzieścia kilka globali. W Pythonie jeden obiekt przekazywany
    jawnie: ta sama informacja, ale widać, kto jej używa. Nazwy pól są
    utrzymane 1:1 z nazwami globali w C (``dflag``, ``Nflag``, …), łamiąc
    PEP 8 dla ``Nflag`` i ``W``-podobnych — celowo, żeby kod dał się czytać
    obok oryginału.
    """

    # --- pozycyjne ---
    host: str | None = None
    uport: str | None = None

    # --- zaimplementowane flagi ---
    dflag: bool = False  #: -d: nie czytaj ze stdin [netcat.c:77]
    iflag: int = 0  #: -i: przerwa w sekundach [netcat.c:79]
    kflag: bool = False  #: -k: nie kończ po rozłączeniu [netcat.c:80]
    lflag: bool = False  #: -l: nasłuchuj [netcat.c:81]
    Nflag: bool = False  #: -N: shutdown(SHUT_WR) po EOF [netcat.c:82]
    nflag: bool = False  #: -n: bez DNS [netcat.c:83]
    pflag: str | None = None  #: -p: port źródłowy [netcat.c:85]
    qflag: int | None = None  #: -q: łatka Debiana, zobacz docs
    rflag: bool = False  #: -r: losowa kolejność portów [netcat.c:86]
    sflag: str | None = None  #: -s: adres źródłowy [netcat.c:87]
    uflag: bool = False  #: -u: UDP [netcat.c:89]
    vflag: bool = False  #: -v: verbose [netcat.c:90]
    zflag: bool = False  #: -z: zero-I/O [netcat.c:92]
    recvlimit: int = 0  #: -W [netcat.c:114]
    timeout_ms: int | None = None  #: -w w milisekundach; None = bez limitu

    # --- pochodne ---
    family: int = socket.AF_UNSPEC  #: -4 / -6 [netcat.c:116]
    ai_flags: int = field(default=0)

    @property
    def socktype(self) -> int:
        """[netcat.c:475] ``hints.ai_socktype = uflag ? SOCK_DGRAM : SOCK_STREAM``"""
        return socket.SOCK_DGRAM if self.uflag else socket.SOCK_STREAM

    @property
    def proto(self) -> int:
        """[netcat.c:476] ``hints.ai_protocol = uflag ? IPPROTO_UDP : IPPROTO_TCP``"""
        return socket.IPPROTO_UDP if self.uflag else socket.IPPROTO_TCP


USAGE = (
    f"użycie: {PROGNAME} [-46dhklNnruvz] [-i sek] [-p port] [-q sek] "
    f"[-s adres]\n"
    f"{' ' * (len('użycie: ') + len(PROGNAME))} [-W liczba] [-w sek] "
    f"[cel] [port]\n"
)

HELP = f"""\
{PROGNAME} — netcat przepisany z C na Pythona (projekt edukacyjny)

{USAGE}
    -4            wymuś IPv4
    -6            wymuś IPv6
    -d            nie czytaj ze stdin (detach)
    -h            ta pomoc
    -i sek        przerwa: w pętli relay oraz między kolejnymi portami
    -k            nie kończ po rozłączeniu klienta (wymaga -l)
    -l            nasłuchuj zamiast łączyć się
    -N            shutdown(SHUT_WR) po EOF na stdin
    -n            bez DNS (adresy i porty tylko numerycznie)
    -p port       port źródłowy
    -q sek        po EOF na stdin: 0 = wyjdź od razu, >0 = czekaj sek,
                  <0 = czekaj bez końca (domyślne)
    -r            losowa kolejność portów w zakresie
    -s adres      adres źródłowy
    -u            UDP zamiast TCP
    -v            więcej komunikatów
    -W liczba     zakończ po tylu odczytach z sieci
    -w sek        timeout na connect oraz idle timeout w pętli relay
    -z            tryb zero-I/O: sprawdź port, nie przesyłaj danych

    cel i port mogą być nazwami. Port może być zakresem: 20-25.
    Listy po przecinku NIE są obsługiwane — tak jak w oryginale.

OSTRZEŻENIE: narzędzie służy do nauki. Łączyć się i nasłuchiwać wolno
wyłącznie na własnej infrastrukturze albo za pisemną zgodą właściciela.

Czego tu nie ma (i dlaczego): TLS, proxy, gniazda uniksowe, SCTP oraz —
rozmyślnie — jakiekolwiek wykonywanie poleceń. Nie ma flagi -e i nie
będzie; OpenBSD też jej nie przepisało. Szczegóły: docs/netcat-architecture.md
"""


def parse_args(argv: list[str]) -> Options:
    """Sparsuj ``argv`` (bez nazwy programu) i zwróć ``Options``.

    Podnosi ``UsageError`` dla błędu składni i ``FatalError`` dla błędnej
    wartości albo niedozwolonej kombinacji flag.
    """
    opts = Options()

    try:
        parsed, rest = getopt.getopt(argv, _OPTSTRING)
    except getopt.GetoptError as exc:
        raise UsageError(str(exc)) from None

    for flag, arg in parsed:
        name = flag.lstrip("-")

        if name in _OMITTED:
            raise errx(_OMITTED[name])

        # [netcat.c:180-185]
        if name == "4":
            opts.family = socket.AF_INET
        elif name == "6":
            opts.family = socket.AF_INET6
        elif name == "d":
            opts.dflag = True
        elif name == "h":
            # [netcat.c:219-221] help() kończy proces z kodem 0
            raise _HelpRequested
        elif name == "i":
            # [netcat.c:222-226] strtonum(optarg, 0, UINT_MAX)
            value, err = strtonum(arg, 0, 2**32 - 1)
            if err:
                raise errx(f"błędna wartość -i ({err}): {arg}")
            assert value is not None
            opts.iflag = value
        elif name == "k":
            opts.kflag = True
        elif name == "l":
            opts.lflag = True
        elif name == "N":
            opts.Nflag = True
        elif name == "n":
            opts.nflag = True
        elif name == "p":
            opts.pflag = arg
        elif name == "q":
            # Łatka Debiana; brak w upstream. Zakres jak dla int w C.
            value, err = strtonum(arg, -(2**31), 2**31 - 1)
            if err:
                raise errx(f"błędna wartość -q ({err}): {arg}")
            assert value is not None
            opts.qflag = value
        elif name == "r":
            opts.rflag = True
        elif name == "s":
            opts.sflag = arg
        elif name == "u":
            opts.uflag = True
        elif name == "v":
            opts.vflag = True
        elif name == "W":
            # [netcat.c:283-287] strtonum(optarg, 1, INT_MAX)
            value, err = strtonum(arg, 1, 2**31 - 1)
            if err:
                raise errx(f"błędny limit odbioru -W ({err}): {arg}")
            assert value is not None
            opts.recvlimit = value
        elif name == "w":
            # [netcat.c:288-293] strtonum(optarg, 0, INT_MAX/1000), potem *1000
            value, err = strtonum(arg, 0, (2**31 - 1) // 1000)
            if err:
                raise errx(f"błędny timeout -w ({err}): {arg}")
            assert value is not None
            # -w 0 oznacza timeout 0 ms, czyli poll() bez blokowania —
            # a NIE „bez limitu". Brak flagi to timeout_ms = None.
            opts.timeout_ms = value * 1000
        elif name == "z":
            opts.zflag = True
        else:  # pragma: no cover — _OPTSTRING i ta gałąź są spójne
            raise UsageError(f"nieobsłużona opcja {flag}")

    # [netcat.c:477-479] AI_NUMERICHOST przy -n
    if opts.nflag:
        opts.ai_flags |= socket.AI_NUMERICHOST

    # --- argumenty pozycyjne [netcat.c:356-364] ---
    if len(rest) == 1 and opts.lflag:
        opts.uport = rest[0]
    elif len(rest) == 2:
        opts.host, opts.uport = rest[0], rest[1]
    else:
        # Jeden komunikat dla wszystkich złych układów argumentów. Wariant
        # z liczbą argumentów był bardziej rozmowny, ale rozchodził się
        # z wersją w C — a parzystość obu implementacji jest tu ważniejsza
        # niż odcień komunikatu (pilnuje tego tests/test_parity.py).
        raise UsageError("oczekiwano 'cel port' albo 'port' razem z -l")

    _validate(opts)
    return opts


def _validate(opts: Options) -> None:
    """Wzajemna wykluczalność flag. [netcat.c:425-456]

    Oryginał sprawdza to po ``pledge()``, więc już po ustaleniu pozycyjnych
    argumentów — kolejność komunikatów jest odtworzona.
    """
    # [netcat.c:426-427]
    if opts.lflag and opts.sflag is not None:
        raise errx("nie można użyć -s razem z -l")
    # [netcat.c:428-429]
    if opts.lflag and opts.pflag is not None:
        raise errx("nie można użyć -p razem z -l")
    # [netcat.c:430-431]
    if opts.lflag and opts.zflag:
        raise errx("nie można użyć -z razem z -l")
    # [netcat.c:432-433]
    if not opts.lflag and opts.kflag:
        raise errx("-k wymaga -l")

    # Poza oryginałem: sanity check portu źródłowego, żeby błąd wyszedł tu,
    # a nie dopiero przy getaddrinfo z mało czytelnym komunikatem.
    if opts.pflag is not None:
        value, err = strtonum(opts.pflag, 1, PORT_MAX)
        if err == "invalid":
            try:
                socket.getservbyname(
                    opts.pflag, "udp" if opts.uflag else "tcp"
                )
            except OSError:
                raise errx(f'nieznana usługa portu źródłowego "{opts.pflag}"') from None
        elif err is not None:
            raise errx(f"błędny port źródłowy -p ({err}): {opts.pflag}")
        else:
            assert value is not None


class _HelpRequested(Exception):
    """Sygnał „użytkownik poprosił o -h"; obsługiwany w ``app.main()``."""


__all__ = [
    "HELP",
    "USAGE",
    "FatalError",
    "Options",
    "UsageError",
    "_HelpRequested",
    "parse_args",
]
