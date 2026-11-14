# Architektura `netcat.c` — materiał referencyjny

Ten dokument opisuje, **jak zbudowany jest OpenBSD netcat**. Celem jest, żeby po
lekturze dało się rozumieć i dyskutować kod źródłowy netcata bez otwierania go —
i żeby każdy komentarz w `pyncat/` odwołujący się do numeru linii dał się tu
sprawdzić.

| Co | Wersja |
|---|---|
| Kanoniczne źródło | `$OpenBSD: netcat.c,v 1.241 2026/10/31 03:42:10 tb Exp $`, 1900 linii |
| Man page | `$OpenBSD: nc.1,v 1.101 2025/06/24 13:37:39 tb Exp $`, 599 linii roff |
| Binarium do pomiarów | `OpenBSD netcat (Debian patchlevel 1.226-1ubuntu2)`, pakiet `netcat-openbsd 1.226-1ubuntu2 amd64` |

**Numery linii `[netcat.c:NNNN]` odnoszą się do rev. 1.241; `[nc.1:NNNN]` do
roff-owego źródła man page rev. 1.101.** Zostały zweryfikowane `grep`-em na kopii
rev. 1.241 i sprawdzone `grep`-em po treści, a nie przez odejmowanie numerów
z indeksu funkcji. Jak zweryfikować je samemu — §2.4.

> **Licencja.** `netcat.c` jest na licencji BSD 3-clause (Eric Jackson 2001,
> Bob Beck 2015). W tym repo **nie ma ani jednej linii kodu skopiowanej
> z netcata**. Odtwarzamy algorytmy i semantykę; warunki są podane jako
> przeformułowany pseudokod z własnymi nazwami (`STDIN.fd`, `sbuf`) i odwołaniem
> do numeru linii. Wyjątkiem są **krótkie, oznaczone cytaty blokowe**: jeden
> komentarz autora przy `-i` (§4.4) i zdania z `nc.1` w §6 — cytowane jako
> cytaty, z atrybucją, bo w tych miejscach przedmiotem analizy jest właśnie
> brzmienie tekstu. Kodu C nie cytujemy nigdzie.
>
> **Metoda.** Każda liczba pochodzi z pomiaru wykonanego na `/usr/bin/nc` w tym
> kontenerze — `strace`, `date +%s.%N`, sumy kontrolne. Zestawienie: §8.

---

## 1. Skąd się wziął netcat

### 1.1 Trzy warstwy historii

| Kiedy | Kto | Co |
|---|---|---|
| 1995–1996 | *Hobbit* `<hobbit@avian.org>` | Oryginalny `nc`, kulminacja w **netcat 1.10** (1996). Jeden plik C, przenośny przez `#ifdef`-y, „TCP/IP swiss army knife". Zawierał flagę `-e`. |
| 2001 | **Eric Jackson** `<ericj@monkey.org>` | Przepisanie **od zera** dla OpenBSD, z obsługą IPv6. To jest przodek dzisiejszego pliku; copyright 2001 w nagłówku `[netcat.c:3]`. |
| 2015 | **Bob Beck** | Warstwa TLS przez `libtls` — `-c`, `-C`, `-K`, `-R`, `-H`, `-T`, `-Z`, `-o`, i przedefiniowanie `-e`. Copyright 2015 `[netcat.c:4]`. |

Atrybucja jest zapisana w dwóch miejscach: komentarz `[netcat.c:30-33]`
(„Re-written nc(1) for OpenBSD. Original implementation by *Hobbit*") oraz sekcja
AUTHORS w man page `[nc.1:581-586]` („Original implementation by *Hobbit*" /
„Rewritten with IPv6 support by Eric Jackson").

### 1.2 Dlaczego przepisano to od zera

Netcat 1.10 był kodem z połowy lat 90., zbudowanym wokół `select()` i obudowanym
warstwą `#ifdef`-ów na kilkanaście uniksów. OpenBSD chciał narzędzia, które ma
**jeden plik i zero zależności** poza `libc` (później `libtls`), obsługuje IPv6
przez `getaddrinfo()`/`getnameinfo()` zamiast ręcznego składania `sockaddr_in`,
daje się audytować w całości w jedno posiedzenie — i **nie ma żadnej funkcji
uruchamiającej procesy**.

Ostatni punkt to nie przeoczenie (§7). Najmocniejszy dowód intencji: w OpenBSD
litera `-e` została **zajęta na coś innego** — `-e name` znaczy „przyjmij
certyfikat TLS tylko jeśli zawiera podaną nazwę" `[nc.1:116-123]`,
`[netcat.c:210-212]`. Gdyby traktowali `-e` jako „do zrobienia kiedyś", nie
oddaliby tej litery warstwie TLS.

### 1.3 Co z tego wynika dla kształtu pliku

| Cecha | Konsekwencja |
|---|---|
| Jeden plik, 1900 linii | Brak abstrakcji „na zapas". Stan programu to ~30 globalnych `int`-ów `[netcat.c:77-120]`, nie struktura konfiguracyjna. |
| Zero zależności | Własny `build_ports()`, własna permutacja Knutha, `strtonum()` z `libc` OpenBSD. Nie ma parsera opcji poza `getopt()`. |
| Jedna pętla I/O | `readwrite()` to **jedyne** miejsce, gdzie przenoszone są dane. Nie ma wątków, nie ma `fork()`, nie ma kolejek zadań. |
| `pledge()`/`unveil()` | `[netcat.c:367-423]` — po sparsowaniu opcji proces zrzeka się prawa do czegokolwiek poza `stdio inet dns`. W wariancie bez plików: `unveil("/", "")` `[netcat.c:399]`, czyli **zerowa widoczność systemu plików**. To dodatkowy argument, dlaczego `-e` nie ma sensu w tym modelu: proces, który nie widzi `/bin/sh`, nie może go uruchomić. |

---

## 2. Mapa pliku

### 2.1 Stałe i stan globalny

| Linie | Element | Rola |
|---|---|---|
| 1–28 | nagłówek licencyjny | BSD 3-clause; Eric Jackson 2001, Bob Beck 2015 |
| 30–33 | komentarz atrybucyjny | „Re-written nc(1) for OpenBSD" |
| 35–60 | `#include` | `libc` + `<poll.h>` + `<tls.h>` + lokalny `atomicio.h` |
| 62 | `#define PORT_MAX 65535` | górna granica dla `strtoport()` |
| 63 | `#define UNIX_DG_TMP_SOCKET_SIZE 19` | tylko dla `-U -u` (pomijamy) |
| **65–68** | `POLL_STDIN 0`, `POLL_NETOUT 1`, `POLL_NETIN 2`, `POLL_STDOUT 3` | indeksy w `pfd[4]` — fundament §4 |
| **69** | `#define BUFSIZE 16384` | rozmiar **obu** buforów relay |
| 71–74 | `TLS_NOVERIFY`/`TLS_NONAME`/`TLS_CCERT`/`TLS_MUSTSTAPLE` | bitmaska dla `-T` |
| 77–120 | globalne flagi (`dflag`, `Fflag`, `iflag`, `kflag`, `lflag`, `Nflag`, `nflag`, `rflag`, `uflag`, `vflag`, `zflag`, `Dflag`, `Iflag`, `Oflag`, `Sflag`, `Tflag`, `ttl`, `minttl`, …) | cały stan z `getopt` jako zmienne globalne |
| 114 | `int recvcount, recvlimit;` | licznik i limit dla `-W` |
| 115 | `int timeout = -1;` | `-w` **w milisekundach**; −1 = `poll()` bez limitu |
| 116 | `int family = AF_UNSPEC;` | `-4`/`-6`/`-U` |
| 117 | `char *portlist[PORT_MAX+1];` | lista portów jako stringi, zakończona `NULL` |
| 122–149 | prototypy | |

`BUFSIZE = 16384` potwierdzone empirycznie — `strace` klienta pokazuje
`read(0, "hi", 16384)`, a `strace` serwera `read(4, "hello", 16384)` na
deskryptorze z `accept4()` (pełne przebiegi w §4.1 i §4.6).

### 2.2 Funkcje — role

| Linie | Funkcja | Rola | pyncat |
|---|---|---|---|
| 152–739 | `main()` | parsowanie opcji, walidacja, wybór ścieżki klient/serwer, zwrot `ret` | tak |
| 746–776 | `unix_bind()` | `AF_UNIX` bind + opcjonalny komunikat „Bound on" | **nie** (`-U`) |
| 779–803 | `timeout_tls()` | pętla `poll()` wokół `tls_handshake`/`tls_close` z limitem `timeout` | **nie** (TLS) |
| 806–830 | `tls_setup_client()` | `tls_connect_socket` + handshake + weryfikacja hash/nazwy | **nie** |
| 833–865 | `tls_setup_server()` | `tls_accept_socket` + handshake + polityka `clientcert` | **nie** |
| 872–901 | `unix_connect()` | connect do gniazda uniksowego | **nie** |
| 908–922 | `unix_listen()` | `unix_bind()` + `listen(s, 5)` | **nie** |
| **930–1003** | `remote_connect()` | `getaddrinfo()` → pętla po `res`: `socket(SOCK_NONBLOCK)`, opcjonalny `bind` dla `-s`/`-p`, `set_common_sockopts()`, `timeout_connect()`. Zwraca pierwszy udany fd albo −1 | tak |
| **1006–1031** | `timeout_connect()` | nieblokujący `connect()` → `EINPROGRESS` → `poll(POLLOUT, timeout)` → `getsockopt(SO_ERROR)` | tak |
| **1039–1097** | `local_listen()` | `AI_PASSIVE`, wymuszenie `AF_INET` dla wildcardu, `SO_REUSEPORT`, `bind`, `listen(s,1)` (tylko TCP), komunikat „Listening on"/„Bound on" | tak |
| **1104–1302** | **`readwrite()`** | **serce programu** — pętla `poll()` na czterech slotach. Rozdział §4 | tak |
| **1305–1334** | `drainbuf()` | `write()` + `memmove` ogona na początek bufora | tak |
| **1337–1362** | `fillbuf()` | `read()` do `buf + *bufpos`, maks. `BUFSIZE - *bufpos` bajtów | tak |
| 1369–1421 | `fdpass()` | przekazanie fd przez `SCM_RIGHTS` i `exit(0)` | **nie** (`-F`) |
| 1425–1452 | `atelnet()` | odpowiedzi DONT/WONT na DO/WILL (RFC 854) | **nie** (`-t`) |
| 1455–1472 | `strtoport()` | `strtonum(1, 65535)`, przy `EINVAL` fallback na `getservbyname()` | tak |
| 1480–1526 | `build_ports()` | pojedynczy port albo zakres `lo-hi`; `-r` = permutacja Knutha | tak |
| 1534–1549 | `udptest()` | 4× `write("X", 1)` dla wymuszenia ICMP port unreachable | tak |
| 1552–1580 | `connection_info()` | „Connection to … port [proto/service] succeeded!" | tak |
| 1583–1636 | `set_common_sockopts()` | `-S`, `-D`, `-T`, `-I`, `-O`, `-M`, `-m` | częściowo |
| 1639–1685 | `process_tos_opt()` | tablica słów kluczowych DSCP → wartość TOS | częściowo |
| 1688–1730 | `process_tls_opt()` | parser `-T key=value` dla opcji TLS | **nie** |
| 1733–1744 | `save_peer_cert()` | zapis PEM peera do pliku (`-Z`) | **nie** |
| 1747–1804 | `report_tls()` | dump parametrów sesji TLS i OCSP | **nie** |
| 1807–1834 | `report_sock()` | „%s on %s %s" — „Listening on …", „Connection received on …" | tak |
| 1837–1882 | `help()` | `usage(0)` + lista flag, potem `exit(1)` | tak |
| 1885–1900 | `usage()` | sam blok `usage:`; `exit(1)` tylko gdy `ret != 0` | tak |

### 2.3 Rozkład `main()`

| Linie | Co |
|---|---|
| 168–174 | `ret = 1`, `socksv = 5`, domyślny CA, **`signal(SIGPIPE, SIG_IGN)`** `[netcat.c:174]` |
| 176–348 | pętla `getopt`; string opcji w `[netcat.c:177]` |
| 349–350 | `argc -= optind; argv += optind;` |
| 352–354 | `setrtable()` dla `-V` (OpenBSD) |
| **356–365** | „Cruft to make sure options are clean" — ustalenie `host`/`uport` z `argc`: 1 arg + `-l` → to port; 2 argi → host i port; inaczej `usage(1)` |
| 367–423 | `unveil()` + `pledge()` |
| **425–456** | walidacja wzajemnej wykluczalności: `-s`+`-l` `[426]`, `-p`+`-l` `[428]`, `-z`+`-l` `[430]`, `-k` bez `-l` `[432]`, oraz zależności TLS |
| 459–469 | nazwa tymczasowego gniazda dla `-U -u` |
| **471–479** | `hints` dla `getaddrinfo`: `ai_socktype`/`ai_protocol` z `uflag` `[475-476]`, `AI_NUMERICHOST` z `nflag` `[478]` |
| 481–521 | parsowanie adresu proxy (`-x`) |
| 523–560 | konfiguracja `tls_config` |
| **561–657** | **ścieżka listen** (`lflag`) — §3.4 i §3.5 |
| 658–672 | ścieżka klienta `AF_UNIX` |
| **673–730** | **ścieżka klienta** TCP/UDP — §3.2 i §3.3 |
| 733–738 | `close(s)`, zwolnienie TLS, `return ret` |

### 2.4 Jak sprawdzić te numery samemu

Wszystkie odwołania w tym repozytorium (w dokumentacji, w komentarzach
`pyncat/*.py` i w `c_reference/pyncat_ref.c`) wskazują na **`netcat.c`
rev. 1.241**. Pliku nie ma w repo — jest na licencji BSD i świadomie go nie
kopiujemy. Żeby zweryfikować którekolwiek odwołanie:

```bash
curl -sO https://raw.githubusercontent.com/openbsd/src/master/usr.bin/nc/netcat.c
sed -n '1137,1147p' netcat.c      # trzy warunki wyjścia readwrite()
sed -n '1291,1300p' netcat.c      # propagacja EOF
grep -n 'define BUFSIZE' netcat.c # 69
```

Uwaga: `master` w OpenBSD **idzie dalej**. Jeśli nagłówek pobranego pliku nie
mówi `netcat.c,v 1.241`, numery będą przesunięte — i wtedy rozbieżność jest
w pliku, nie w tej dokumentacji. Historia: `https://www.openssh.com/cgi-bin/cvsweb/src/usr.bin/nc/`

#### Kotwice, od których warto zacząć

Te wartości zostały sprawdzone maszynowo (`grep` po charakterystycznym
fragmencie, nie liczeniem linii w zrzucie ekranu — co jest typowym źródłem
przesunięć o kilka wierszy):

| Element | Linia |
|---|---|
| `PORT_MAX` / `POLL_STDIN`…`POLL_STDOUT` / `BUFSIZE` | 62 / 65–68 / 69 |
| `recvcount,recvlimit` / `timeout = -1` / `portlist[]` | 114 / 115 / 117 |
| `ret = 1` (inicjalizacja) / `signal(SIGPIPE, SIG_IGN)` | 168 / 174 |
| `getopt()` / łańcuch opcji | 176 / 177 |
| walidacja wzajemnej wykluczalności flag | 425–456 |
| `hints.ai_socktype` / `AI_NUMERICHOST` | 475 / 477–478 |
| `if (lflag)` / `ret = 0` (listen) | 561 / 562 |
| trzy tryby pętli listen: UDP+`-k` / UDP / TCP | 588–598 / 599–628 / 629–653 |
| `build_ports(uport)` / pętla po portach / `ret = 0` (klient) | 677 / 680 / 704 |
| `remote_connect()` / `timeout_connect()` / `local_listen()` | 930 / 1006 / 1039 |
| `SO_REUSEPORT` / `listen(s, 1)` | 1063 / 1080 |
| **`readwrite()`** / `pfd[4]` | **1104** / 1106 |
| trzy warunki wyjścia | 1137–1140 / 1141–1143 / 1144–1147 |
| `sleep(iflag)` / `poll(pfd, 4, timeout)` | 1153 / 1156 |
| pętla `POLLERR\|POLLNVAL` / blok `revents` | 1167–1171 / 1167–1199 |
| cztery bloki I/O | 1202–1289 |
| EOF na net in / limit `-W` | 1252–1255 / 1256–1261 |
| **propagacja EOF** | **1292–1300** |
| `drainbuf()` / `memmove` / `fillbuf()` | 1305 / 1331 / 1337 |
| `strtoport()` / `build_ports()` / warunek zakresu | 1455 / 1480 / 1486 |
| `udptest()` / cztery zapisy | 1534 / 1542 |
| `connection_info()` / `report_sock()` | 1552 / 1807 |

#### Dwie rzeczy, które łatwo policzyć źle

Nie są to numery linii, ale trafiają wprost do implementacji:

* **Kolejność w `set_common_sockopts()`** `[1583-1636]` to `-S`, `-D`, `-T`,
  `-I`, `-O`, `-M`, `-m` — czyli NIE taka, jak sugeruje kolejność flag
  w `usage`. Ma to znaczenie, bo kolejność `setsockopt()` jest widoczna
  w `strace`, a testy porównawcze obu implementacji ją sprawdzają.
* **Granice funkcji liczone „od nagłówka do nagłówka" są o kilka wierszy za
  szerokie**, bo między funkcjami stoją komentarze blokowe. `readwrite()` to
  1104–**1302**, a nie 1104–1303 (czyli **199** linii, nie 200); `fillbuf()`
  1337–**1362**; `build_ports()` 1480–**1526**; `timeout_connect()`
  1006–**1031**. Wyjątek w drugą stronę: `main()` kończy się w **739**,
  tam gdzie jest `return ret` — miejsce cytowane w §3.3 jako `[738]`.

Praktyczny wniosek dla kogoś, kto chce zweryfikować te odwołania: licz
`grep`-em po treści, nie przez odejmowanie numerów z indeksu funkcji.
Przesunięcia nie są stałe ani jednokierunkowe — w tym pliku sięgają od
2 wierszy w jedną stronę do 27 w drugą, więc żadna pojedyncza poprawka
nie uzgodni wszystkiego naraz.

---

## 3. Ścieżki wykonania

### 3.1 Szkielet i rozwidlenie

```text
  main() [152]: getopt [176-348] -> host/uport [356-365] -> pledge [367-423]
                -> walidacja flag [425-456] -> hints [471-479]
    |
    +-- lflag ? TAK -> SCIEZKA LISTEN  [561-657]   ret = 0 OD RAZU  [562]
    |           NIE -> SCIEZKA KLIENTA [673-730]   ret = 1 na starcie [168]
    |
    +-- close(s) [733-734] -> return ret [738]
```

### 3.2 Klient TCP, jeden port

```text
  build_ports("1234") [677]  ->  portlist[0]="1234", portlist[1]=NULL
  petla po portlist [680]
    |
    +-- remote_connect(host, port, hints, ipaddr) [698]
    |     getaddrinfo [936]; dla kazdego res:
    |       socket(SOCK_NONBLOCK) [941] -> bind gdy -s/-p [959]
    |       -> set_common_sockopts [965] -> getnameinfo->ipaddr [968]
    |       -> timeout_connect [980]: connect->EINPROGRESS [1013]
    |                                 poll(POLLOUT, timeout) [1016]
    |                                 getsockopt(SO_ERROR) [1018]
    |       sukces -> break;  porazka -> warn gdy -v [983-992], close, s=-1
    |
    +-- s == -1 ? TAK -> continue [701-702]    <<< ret NIE jest ruszane
    +-- ret = 0                                <<< [704] TU i tylko tu (TCP)
    +-- -v lub -z ? -> connection_info [716-718]
    +-- -z ? TAK -> nic wiecej;  NIE -> readwrite(s, tls_ctx) [726]
    |
    +-- koniec listy -> return ret [738]
```

Kluczowy szczegół: **`ret = 0` ustawia się natychmiast po udanym connect
`[netcat.c:704]`, przed jakimkolwiek transferem.** Kod wyjścia 0 znaczy „dało się
połączyć", nie „transfer się udał".

### 3.3 Klient z zakresem portów

`build_ports("20-30")` `[netcat.c:1486]` rozpoznaje zakres tylko wtedy, gdy
**pierwszy znak jest cyfrą** i w stringu jest `-`. Dzięki temu nazwa usługi
z minusem nie jest brana za zakres.

| Zachowanie | Linie | POMIAR |
|---|---|---|
| `lo > hi` → swap | 1493–1497 | `nc -z -n -v 127.0.0.1 35101-35099` skanuje **35099, 35100, 35101** — rosnąco |
| `-r` → permutacja Knutha z `arc4random_uniform` | 1503–1509 | — |
| bez `-r` → kolejność rosnąca | 1511–1515 | j.w. |
| przecinki **nie** są obsługiwane | 1486 (brak gałęzi) | `35101,35099` → `nc: port number invalid: 35101,35099` |
| nazwa usługi | 1469–1471 | `ssh` → port **22** |
| nieznana usługa | 1469–1470 | `nosuchsvc` → `nc: port number invalid: nosuchsvc` |
| poza zakresem | 1464, komunikat 1467–1468 | `0` → `nc: port number too small: 0`; `65536` → `nc: port number too large: 65536` |

> **Uwaga: dwa dolne-środkowe wiersze mieszają źródła.** Komunikaty w kolumnie
> POMIAR są **debianowe**. Upstream rev. 1.241 w `[1469-1470]` drukuje
> `service "%s" unknown`, więc `nosuchsvc` dałby tam `nc: service "nosuchsvc"
> unknown`, a nie `port number invalid`. Ścieżka kodu jest ta sama (`strtonum`
> z `errno == EINVAL` → `getservbyname` → `NULL`), różni się tylko tekst. Dla
> pyncat przyjmujemy brzmienie **debianowe**, bo testy porównawcze uruchamiają
> binarium z kontenera. Komunikaty „too small"/„too large" `[1467-1468]` są
> w obu wersjach identyczne.

Pętla `[netcat.c:680-730]` leci po całej liście; **po każdym porcie poprzedni
socket jest zamykany** `[681-682]`. Nie ma równoległości — netcat to nie nmap.

#### Gdzie ustawia się `ret`

| Linia | Przypisanie | Kiedy |
|---|---|---|
| 168 | `ret = 1` | start `main()` |
| 562 | `ret = 0` | wejście w ścieżkę listen — **bezwarunkowo** |
| 659 / 667 | `ret = 0` / `ret = 1` | klient `AF_UNIX` (pomijamy) |
| **704** | `ret = 0` | udany connect do **któregokolwiek** portu |
| **712** | `ret = 1` | `udptest() == -1`, czyli zamknięty port UDP |
| 738 | `return ret` | koniec |

Nieudane połączenia robią `continue` **bez dotknięcia `ret`** `[701-702]`, więc raz
ustawione `ret = 0` nie wraca do 1. Jedyny wyjątek to UDP: `[712]` może nadpisać
wcześniejszy sukces.

**POMIAR** (`nc -z -n -w 2`):

| Przypadek | exit |
|---|---|
| TCP, port otwarty | 0 |
| TCP, port zamknięty | 1 |
| TCP, zakres: otwarty **pierwszy** + zamknięty | **0** |
| TCP, zakres: zamknięty + otwarty **ostatni** | **0** |
| TCP, zakres: wszystkie zamknięte | 1 |
| TCP, klient bez `-z`, port zamknięty | 1 |
| **UDP, zakres: otwarty pierwszy + zamknięty** | **1** ← `[712]` nadpisuje sukces |
| **UDP, zakres: zamknięty + otwarty ostatni** | **0** |

Dwa ostatnie wiersze to zmierzona asymetria między `[704]` i `[712]`: w obu
przypadkach UDP na stderr pojawiła się linia `succeeded!` dla otwartego portu,
a kod wyjścia był inny — zależnie od tego, czy porażka wypadła **po** sukcesie.

### 3.4 Serwer TCP

```text
  ret = 0 [562]                      <<< serwer konczy z 0 nawet bez polaczen
  for (;;) [580]
    |
    +-- if (s != -1) close(s) [582-583]
    +-- s = local_listen(host, uport, hints) [584]
    |     AI_PASSIVE [1046]; host==NULL && AF_UNSPEC -> AF_INET [1052-1053]
    |     socket [1059] -> setsockopt(SO_REUSEPORT) [1063]
    |       -> set_common_sockopts [1067] -> bind [1069]
    |       -> listen(s, 1) tylko gdy !uflag [1079-1082]
    |       -> report_sock("Listening"/"Bound") gdy -v [1083-1092]
    |
    +-- connfd = accept4(s, ..., SOCK_NONBLOCK) [634-635]
    |     blad accept -> err(1) BEZWARUNKOWO [636-639] ("all errnos are fatal")
    +-- report_sock("Connection received") gdy -v [640-643]
    +-- readwrite(connfd, NULL) [648] -> close(connfd) [651]
    |
    +-- if (!kflag) break [655-656]
```

| Niuans | Linie | POMIAR |
|---|---|---|
| `listen()` z **backlogiem 1** | 1080 | `strace`: `listen(3, 1) = 0` |
| `accept4` z `SOCK_NONBLOCK` | 634–635 | `strace`: `accept4(3, …, SOCK_NONBLOCK) = 4` |
| wildcard → wymuszone **`AF_INET`** | 1052–1053 | `nc -l -v -p P` wypisuje `Listening on 0.0.0.0 P` — nie `::`. Czyli `nc -l 1234` **nie** słucha na IPv6 |
| `-w` nie dotyczy `accept()` | — | `nc -l -p P -w 1` bez klienta: ubite przez `timeout 5`, exit 124 |
| bez `-k` dokładnie jedno połączenie | 655–656 | trzy kolejne połączenia: serwer odebrał tylko `C1;`, połączenia 2 i 3 → exit 1 u klienta |
| z `-k` połączenia **sekwencyjnie** | 580–657 | serwer odebrał `C1;C2;C3;` |
| komunikat UDP | 1090 | `nc -u -l -v`: **`Bound on 0.0.0.0 P`**, nie „Listening on" |

**Upstream odtwarza socket nasłuchujący w każdej iteracji `-k`** — `close(s)`
`[582-583]` plus nowy `local_listen()` `[584]`. Dlatego `local_listen` ustawia
`SO_REUSEPORT` `[1063]`: bez tego kolejny `bind()` na ten sam port mógłby się nie
udać.

**Debian 1.226 robi to inaczej — zmierzone.** `strace` serwera `nc -l -k` przy
trzech kolejnych połączeniach daje `socket`/`bind`/`listen(3,1)` **po jednym
razie** i `accept4(3, …)` **cztery razy**, z `close(4)` po każdym połączeniu.
Listener powstaje **raz** i zamykany jest tylko fd połączenia. To różnica wobec
rev. 1.241, nie detal implementacyjny: w upstream między iteracjami istnieje
okno, w którym nikt nie trzyma portu.

Dodatkowo Debian ustawia **oba** opcje — `setsockopt(SO_REUSEADDR)` **i**
`setsockopt(SO_REUSEPORT)` — podczas gdy rev. 1.241 ustawia tylko `SO_REUSEPORT`
`[1063]`. Dla pyncat właściwy jest `SO_REUSEADDR`: na Linuksie `SO_REUSEPORT`
pozwala wielu procesom bindować ten sam port, co jest zupełnie inną semantyką niż
na BSD. To świadome odstępstwo.

### 3.5 Serwer UDP — trzy tryby w jednej pętli

Pętla `[netcat.c:580-657]` ma trzy gałęzie i **UDP obsadza dwie z nich**:

```text
  for (;;) [580]
    |
    +-- uflag && kflag   -> TRYB A [588-598]
    |     BRAK connect(). Socket odbiera datagramy od wielu nadawcow.
    |     readwrite(s, NULL) [598]. Nie ma do kogo pisac -> write()
    |     daje ENOTCONN, wiec komunikacja jest JEDNOKIERUNKOWA.
    |
    +-- uflag && !kflag  -> TRYB B [599-628]
    |     recvfrom(s, buf, 2048, MSG_PEEK, &z, &len) [610-611]
    |       MSG_PEEK: datagram ZOSTAJE w buforze jadra
    |     connect(s, &z, len) [615]
    |       socket przyjmuje ruch TYLKO od tego jednego nadawcy
    |     report_sock("Connection received") gdy -v [623-626]
    |     readwrite(s, NULL) [628]
    |
    +-- else             -> TRYB C [629-653]
    |     accept4 + readwrite(connfd) + close(connfd)
    |
    +-- if (!kflag) break [655-656]
```

`MSG_PEEK` `[610]` jest tu kluczowe i łatwo je przeoczyć: pierwszy datagram
**nie jest konsumowany** przez `recvfrom()`, tylko podejrzany, żeby poznać adres
nadawcy. Potem `readwrite()` czyta go normalnie, więc **pierwsze dane nie
przepadają**. Implementacja ze zwykłym `recvfrom()` gubiłaby pierwszy datagram.

**POMIAR.** Trzy datagramy z trzech różnych portów źródłowych:

| Tryb | Nadawcy | Odebrano | Dlaczego |
|---|---|---|---|
| B (`nc -u -l`) | `U1` z :41001, `U2` z :41002, `U3` z :41001 | **`U1U3`** | po `connect()` `[615]` socket jest powiązany z `:41001`, więc jądro odrzuca `U2` |
| A (`nc -u -l -k`) | `K1` z :41011, `K2` z :41012, `K3` z :41013 | **`K1K2K3`** | brak `connect()` → wszyscy przechodzą, jak obiecuje `[nc.1:173-176]` |

| Tryb | `connect()` | Wielu nadawców | Dwukierunkowo | `listen()` |
|---|---|---|---|---|
| A: `-u -l -k` | nie | **tak** | **nie** (`ENOTCONN`) | nie `[1079]` |
| B: `-u -l` | tak `[615]` | nie | tak | nie `[1079]` |
| C: `-l` (TCP) | — (`accept4`) | sekwencyjnie z `-k` | tak | `listen(s,1)` `[1080]` |

---

## 4. `readwrite()` — anatomia

`[netcat.c:1104-1302]`, 199 linii. To cały transfer danych w netcacie.

### 4.1 Model czterech slotów

```text
  stdin (fd 0) ---> [0] POLL_STDIN   events=POLLIN  --+
                                                      |  fillbuf()
                                                      v
                                             stdinbuf[16384]
                                                      |  drainbuf()
                    [1] POLL_NETOUT  events=0    <----+
                             |
                        net_fd (ten SAM deskryptor)
                             |
                    [2] POLL_NETIN   events=POLLIN ---+
                                                      |  fillbuf()
                                                      v
                                             netinbuf[16384]
                                                      |  drainbuf()
  stdout (fd 1) <-- [3] POLL_STDOUT  events=0    <----+
```

**`POLL_NETOUT` i `POLL_NETIN` to dwa sloty na tym samym deskryptorze**, z osobnymi
maskami `events` i osobnym „życiem". Half-close modelowany jest wyłącznie przez
wpisanie `fd = -1` w jeden z nich, podczas gdy drugi żyje dalej. Nie ma żadnych
dodatkowych zmiennych stanu — tablica `pfd[4]` **jest** maszyną stanową.

**POMIAR** — `strace` klienta, pierwsze wywołanie `poll()`:

```text
  poll([{fd=0, events=POLLIN}, {fd=3, events=0},
        {fd=3, events=POLLIN}, {fd=1, events=0}], 4, -1)
```

`fd=3` występuje **dwukrotnie, z różnymi maskami**. To nie jest interpretacja
kodu — tak wygląda realne wywołanie syscalla.

Konsekwencja dla Pythona: moduł `selectors` jest tu **nieużywalny**, bo
`SelectorKey` jest indeksowany po deskryptorze — tego samego fd nie da się
zarejestrować dwa razy z różnymi maskami. Trzeba `select.poll()` bezpośrednio.

### 4.2 Stan startowy `[netcat.c:1116-1134]`

| Slot | Indeks | `fd` | `events` na starcie | Linie |
|---|---|---|---|---|
| `POLL_STDIN` | 0 | `STDIN_FILENO` (0); **−1 gdy `-d`** | `POLLIN` | 1117–1122 |
| `POLL_NETOUT` | 1 | `net_fd` | `0` | 1125–1126 |
| `POLL_NETIN` | 2 | `net_fd` | `POLLIN` | 1129–1130 |
| `POLL_STDOUT` | 3 | `STDOUT_FILENO` (1) | `0` | 1133–1134 |

Sloty wyjściowe startują z `events = 0`, bo **nie ma jeszcze czego wysyłać**.
`POLLOUT` zostanie uzbrojone dopiero, gdy odpowiedni bufor przestanie być pusty.

**POMIAR `-d`** — `strace` klienta `nc -d`, pierwszy `poll()`:

```text
  poll([{fd=-1}, {fd=3, events=0}, {fd=3, events=POLLIN}, {fd=1, events=0}], 4, -1)
```

Slot 0 jest `-1` od pierwszej iteracji — `poll(2)` ignoruje wpisy z ujemnym
deskryptorem. Dokładnie to robi `[netcat.c:1117-1118]`.

### 4.3 Trzy warunki wyjścia — na POCZĄTKU pętli

Oznaczenia: `STDIN`/`NETOUT`/`NETIN`/`STDOUT` = sloty, `sbuf`/`nbuf` =
`stdinbufpos`/`netinbufpos`.

| # | Linia | Warunek | Co by się stało bez niego |
|---|---|---|---|
| 1 | **1138** | `STDIN.fd == -1 && NETIN.fd == -1 && sbuf == 0 && nbuf == 0` | Oba wejścia martwe, nic w buforach — `poll()` zostałby wywołany z czterema slotami, z których żaden nie może nic zgłosić. Przy `timeout == -1` **zawis na zawsze**. |
| 2 | **1142** | `NETOUT.fd == -1 && STDOUT.fd == -1` | Nie ma gdzie pisać. Bez tego netcat dalej **czytałby** z obu wejść i wyrzucał dane do pełnych buforów, a po ich zapełnieniu przestałby cokolwiek robić — znowu zawis, tylko po zmarnowaniu 32 KiB. |
| 3 | **1145** | `lflag && NETIN.fd == -1 && sbuf == 0 && nbuf == 0` | Serwer z `-l`, którego klient się rozłączył, ale którego stdin wciąż żyje (terminal albo otwarty pipe). Warunek 1 nie zadziała, bo `STDIN.fd != -1`. **Serwer wisiałby po rozłączeniu klienta, czekając na stdin.** |

Warunek 3 jest tym, który pozwala pętli `-k` `[580-657]` przejść do następnego
`accept()`. Bez niego `-k` byłoby bezużyteczne.

### 4.4 `poll()` i timeout `[netcat.c:1152-1164]`

| Linie | Co |
|---|---|
| 1152–1153 | `if (iflag) sleep(iflag);` — **jedyne** użycie `iflag` w całym pliku (`grep`: 79, 223, 1152, 1153). Szczegóły w §6.1 |
| **1156** | `num_fds = poll(pfd, 4, timeout);` — zawsze 4 sloty, `timeout` globalny w ms |
| 1159–1160 | `poll() == -1` → `err(1, "polling error")`, **bez retry** |
| **1163–1164** | `poll() == 0` → `return` — **timeout to normalne wyjście, nie błąd**; kod wyjścia pozostaje taki, jak ustawiono wcześniej |

Komentarz autora przy `-i` `[1149-1151]` jest szczery i wart zacytowania w całości,
bo wyjaśnia rozbieżność z man page:

> help says -i is for "wait between lines sent". We read and write arbitrary
> amounts of data, and we don't want to start scanning for newlines, so this is
> as good as it gets

### 4.5 Obsługa `revents` — kolejność ma znaczenie `[netcat.c:1167-1199]`

| Krok | Linie | Co robi |
|---|---|---|
| 1 | 1167–1171 | pętla po wszystkich 4 slotach: `revents & (POLLERR \| POLLNVAL)` → `fd = -1`. Błąd twardy ubija slot niezależnie od tego, czego dotyczył |
| 2 | 1173–1176 | `STDIN`: `events & POLLIN` **i** `revents & POLLHUP` **i** `!(revents & POLLIN)` → `fd = -1` |
| 3 | 1178–1181 | `NETIN`: ten sam wzorzec |
| 4 | 1183–1187 | `NETOUT`: `revents & POLLHUP` → gdy `fd != -1` i `-N`: `shutdown(fd, SHUT_WR)`; potem `fd = -1` |
| 5 | 1189–1190 | `STDOUT`: `revents & POLLHUP` → `fd = -1` |
| 6 | **1192–1193** | **kaskada:** `NETOUT.fd == -1` → `STDIN.fd = -1` („nie ma gdzie pisać, przestań czytać") |
| 7 | **1195–1199** | **kaskada:** `STDOUT.fd == -1` → `shutdown(NETIN.fd, SHUT_RD)` i `NETIN.fd = -1` |

Kroki 6 i 7 muszą być **po** 2–5, bo konsumują ich wynik; odwrócenie kolejności
zgubiłoby jedną iterację propagacji. `POLLERR`, `POLLHUP` i `POLLNVAL` są
w `poll(2)` **niemaskowalne** — przychodzą w `revents`, nawet jeśli nie ma ich
w `events`. Dlatego kroki 4 i 5 nie sprawdzają `events`, a kroki 2 i 3 **muszą**
(`events & POLLIN`), żeby nie ubić slotu, którego netcat w tej iteracji wcale nie
obserwował.

### 4.6 Dlaczego `POLLHUP` razem z `POLLIN` nie zamyka wejścia

Po `POLLHUP` w buforze jądra mogą **nadal być dane**; dopiero `POLLHUP` bez
`POLLIN` oznacza prawdziwy koniec strumienia. Naiwna implementacja, która ubija
slot na samo `POLLHUP`, gubi ogon strumienia.

**POMIAR** — `strace` klienta `printf 'hi' | nc -N`, stdin to pipe z natychmiastowym EOF:

```text
  poll([{fd=0, events=POLLIN}, ...], 4, -1) = 1 ([{fd=0, revents=POLLIN|POLLHUP}])
  read(0, "hi", 16384)                    = 2          <<< slot ZYJE, dane odczytane
  poll([{fd=0, events=POLLIN}, ...], 4, -1) = 2 ([{fd=0, revents=POLLHUP}, ...])
                                                       <<< POLLHUP bez POLLIN -> slot ubity
  poll([{fd=-1}, {fd=-1}, {fd=3, events=POLLIN}, {fd=1, events=0}], 4, -1)
```

Pierwsze `poll()` zwróciło `POLLIN|POLLHUP` **jednocześnie**. Gdyby `[1173-1176]`
nie zawierało członu `!(revents & POLLIN)`, bajty `hi` nigdy nie zostałyby
przeczytane. Trzecie `poll()` pokazuje `{fd=-1}` w slotach 0 i 1 — to literalny
sentinel z kodu, widoczny w syscallu.

### 4.7 Cztery bloki I/O i backpressure `[netcat.c:1202-1289]`

Każdy blok ma identyczny szkielet: warunek wejścia, wywołanie `fillbuf`/`drainbuf`,
reakcja na wartość zwróconą, przełączenie zainteresowania.

| Blok | Linie | Warunek wejścia | Operacja |
|---|---|---|---|
| stdin → `stdinbuf` | 1202–1217 | `STDIN.revents & POLLIN && sbuf < BUFSIZE` | `fillbuf(STDIN.fd, stdinbuf, &sbuf, NULL)` |
| `stdinbuf` → net | 1219–1237 | `NETOUT.revents & POLLOUT && sbuf > 0` | `drainbuf(NETOUT.fd, stdinbuf, &sbuf, tls)` |
| net → `netinbuf` | 1239–1272 | `NETIN.revents & POLLIN && nbuf < BUFSIZE` | `fillbuf(NETIN.fd, netinbuf, &nbuf, tls)` |
| `netinbuf` → stdout | 1274–1289 | `STDOUT.revents & POLLOUT && nbuf > 0` | `drainbuf(STDOUT.fd, netinbuf, &nbuf, NULL)` |

Szkielety nie są w pełni symetryczne: **tylko** dwa bloki sieciowe mają po
`ret == -1` dodatkowe `return` z całego `readwrite()`, gdy `tls_ctx != NULL &&
kflag` `[1228-1229]` i `[1248-1249]`. Bez TLS ta gałąź jest martwa, ale to ona
tłumaczy, dlaczego bloki 2 i 3 wyglądają inaczej niż 1 i 4. W pyncat jej nie
odwzorowujemy — nie ma `tls_ctx`.

Wzorzec przełączania zainteresowania — **to jest backpressure**:

| Po | Warunek | Akcja | Linie (blok stdin) |
|---|---|---|---|
| `fillbuf` | `bufpos > 0` | uzbrój **przeciwny** slot na `POLLOUT` | 1212–1213 |
| `fillbuf` | `bufpos == BUFSIZE` | **`events = 0` na sobie** — przestań czytać | 1215–1216 |
| `drainbuf` | `bufpos == 0` | `events = 0` na sobie — nic do wysłania | 1232–1233 |
| `drainbuf` | `bufpos < BUFSIZE` | uzbrój slot **wejściowy** na `POLLIN` | 1235–1236 |

Pełny bufor wyłącza czytanie, opróżniony wyłącza pisanie. Bez tego szybki
producent przy wolnym konsumencie zjadałby pamięć — a tu pamięć jest stała:
2 × 16384 B na stos.

**POMIAR — backpressure w akcji.** Transfer 1 048 585 B (1 MiB + 9 bajtów ogona
binarnego) z wymuszonym małym buforem (`-O 2048` u klienta, `-I 2048` u serwera)
i odbiorcą, który nie czyta stdout przez 8 s:

| Metryka | Wartość |
|---|---|
| `write(3, …)` ogółem | **343** |
| z tego **częściowych** (`zapisano < żądano`) | **342** |
| dominujący wzorzec | `write(3, …, 16384) = 3072` — **336 razy** |
| pozostałe | `16384→2048`, `16384→1024`, `13321→3072`, `10249→3072`, `7177→3072`, `4105→3072`, `1033→1033` |
| suma zapisanych bajtów | 1 048 585 — zgadza się z rozmiarem wejścia |

Ogon domyka się arytmetycznie: `13321 − 4 × 3072 = 1033`, a
`336 × 3072 + 2048 + 1024 + 4 × 3072 + 1033 = 1 048 585`. **Dokładna liczba
zapisów zależy od przebiegu** — ile jądro przyjmie w pierwszych zapisach, zanim
bufor odbiorcy się zapcha. Powtórzenie tego samego scenariusza dało **342**
zapisy, **341** częściowych, ten sam dominujący wzorzec `16384 → 3072` **336
razy** i inny ogon (`14345 → 3072 … 2057 → 2057`, bo `14345 − 4 × 3072 = 2057`).
Reprodukowalne jest: liczba 3072-bajtowych zapisów, udział częściowych bliski
100 % i suma bajtów — nie całkowita liczba `write()`.

Fragment `strace` z widocznym przełączaniem zainteresowania:

```text
  poll([{fd=0, events=0},      {fd=3, events=POLLOUT}, ...])   <<< stdin WYLACZONY: bufor pelny
  write(3, ..., 16384)                    = 3072               <<< czesciowy zapis
  poll([{fd=0, events=POLLIN}, {fd=3, events=POLLOUT}, ...])   <<< stdin WLACZONY: zwolnilo sie 3072 B
  read(0, ..., 3072)                      = 3072               <<< dokladnie BUFSIZE - bufpos
  write(3, ..., 16384)                    = 3072
  poll([{fd=0, events=POLLIN}, {fd=3, events=POLLOUT}, ...])
  read(0, ..., 3072)                      = 3072
```

`{fd=0, events=0}` → `{fd=0, events=POLLIN}` to linie 1215–1216 i 1235–1236
uchwycone na gorąco. `read(0, …, 3072)` po zapisaniu 3072 z 16384 to dowód na
`num = BUFSIZE - *bufpos` z `fillbuf` `[1339]` **oraz** na `memmove` z `drainbuf`
`[1331]`: bufor pozostaje wypełniony do 13312 B i jest dopełniany o 3072 B.

Dla kontrastu, ten sam 1 MiB **bez** wymuszania małych buforów — pełny rozkład
odczytów i zapisów klienta (66 odczytów, 65 zapisów):

| Wywołanie | Razy |
|---|---|
| `read(0, …, 16384) = 16384` | **64** |
| `read(0, …, 16384) = 9` | 1 — ogon 9 B |
| `read(0, …, 16375) = 0` | 1 — **EOF**, nie dane; 16375 = 16384 − 9, bo `bufpos` wynosił jeszcze 9 |
| `write(3, …, 16384) = 16384` | **64** |
| `write(3, …, 9) = 9` | 1 |

Zapisów częściowych: **0 na 65**. Ostatni `read` jest tu drugim, niezależnym
dowodem na `num = BUFSIZE - *bufpos` `[1339]`: żądanie ma rozmiar 16375, bo
9 bajtów ogona wciąż siedziało w buforze, gdy jądro zgłosiło EOF.

Integralność: `sha256` wejścia == wyjścia, 1 048 585 B == 1 048 585 B. Samej
sumy nie podajemy jako stałej — payload to `/dev/urandom`, więc każdy przebieg
ma inny digest (docs/c-vs-python.md §1 ma digest z innego przebiegu tego samego
testu). Reprodukowalne jest **równanie**, nie liczba.

Czyli na loopbacku z domyślnymi buforami częściowych zapisów **nie ma wcale** —
test, który ich nie wymusza, nie testuje `memmove`.

#### Dodatki w bloku „read from network" `[netcat.c:1239-1272]`

| Linie | Co |
|---|---|
| 1252–1255 | `ret == 0` (EOF) → `shutdown(NETIN.fd, SHUT_RD)`, `NETIN.fd = -1` |
| **1256–1261** | `recvlimit > 0 && ++recvcount >= recvlimit` → `shutdown(SHUT_RD)`, `NETIN.fd = -1`, **`STDIN.fd = -1`**. To `-W`: ubija **oba** wejścia |
| 1263–1264 | `nbuf > 0` → `STDOUT.events = POLLOUT` |
| 1266–1267 | `nbuf == BUFSIZE` → `NETIN.events = 0` |
| 1269–1271 | `-t` → `atelnet()` (pomijamy) |

Zwrot `shutdown()` jest **ignorowany** `[1253]`. **POMIAR:** gdy peer już zamknął
połączenie, `strace` pokazuje `shutdown(3, SHUT_RD) = -1 ENOTCONN` — i netcat
spokojnie idzie dalej.

### 4.8 `-W` liczy odczyty, nie pakiety

`[netcat.c:1256]` inkrementuje `recvcount` **raz na wejście w blok**, czyli raz na
udany `fillbuf`. To liczba `read()`, nie liczba segmentów TCP.

**POMIAR.** Serwer wysyła `A`, `B`, `C`; generator startuje po podłączeniu
klienta, więc nie ma race'a z `accept()`.

| Flaga | przerwy 0,5 s → trzy `read()` | jeden `write()` → jeden `read()` |
|---|---|---|
| `-W 1` | `A` (1 B) | **`ABC`** (3 B) |
| `-W 2` | `AB` (2 B) | `ABC` (3 B) |
| `-W 3` | `ABC` (3 B) | — |

Identyczny strumień bajtów, inny wynik — zależnie wyłącznie od tego, jak dane
rozłożyły się na wywołania `read()`. Nazwa „packets" z `[nc.1:320-323]` jest dla
TCP myląca: granice `read()` nie odpowiadają granicom segmentów.

### 4.9 Propagacja EOF — na KOŃCU pętli `[netcat.c:1292-1300]`

**To najważniejsze 9 linii w całym programie.**

| Linie | Pseudokod | Znaczenie |
|---|---|---|
| 1292–1296 | `STDIN.fd == -1 && sbuf == 0` → (gdy `NETOUT.fd != -1` **i `-N`**) `shutdown(NETOUT.fd, SHUT_WR)`; potem `NETOUT.fd = -1` | stdin skończony i kolejka pusta → zamknij kierunek wyjścia |
| 1298–1300 | `NETIN.fd == -1 && nbuf == 0` → `STDOUT.fd = -1` | sieć skończona i kolejka pusta → zamknij stdout |

Warunek `&& sbuf == 0` / `&& nbuf == 0` jest krytyczny: **najpierw dokończ
wysyłanie tego, co już przeczytałeś.** Bez tego ostatnie 16 KiB przepadałoby.

**Bez `-N` netcat po EOF na stdin NIE wysyła FIN** — tylko przestaje pisać do
socketu `[1295]`. Druga strona nigdy nie dowiaduje się, że nadawanie się
skończyło.

**POMIAR.** `printf 'DATA' | nc …`, serwer przyjmuje połączenie i milczy:

| Flaga | Czas życia klienta | exit | `shutdown()` w `strace` |
|---|---|---|---|
| (brak) | **8,004 s** i **8,005 s** — ubity przez `timeout 8` | 124 | **0** — proces zostaje w `poll()` do zabicia |
| `-N` | **0,006 s** (dwa niezależne przebiegi) | 0 | **2** — `SHUT_WR` po `write`, potem `SHUT_RD` po EOF |

Łańcuch z `-N`, krok po kroku:

```text
  klient: write(3,"hi",2); shutdown(3, SHUT_WR)       [1294]
     |
     v
  serwer: read() na netin zwraca 0 -> EOF            [1252]
          shutdown(SHUT_RD); NETIN.fd = -1           [1253-1254]
          nbuf == 0 -> STDOUT.fd = -1                [1298-1299]
          warunek 3 (lflag && NETIN == -1) -> return [1145]
          close(connfd)                              [651]
     |
     v
  klient: read() na netin zwraca 0 -> NETIN.fd = -1  [1252-1254]
          STDIN.fd == -1, NETIN.fd == -1, bufory puste
          warunek 1 -> return                        [1138]
```

Dlatego przykład „DATA TRANSFER" w man page `[nc.1:462]` używa `nc -N`, a sekcja
CLIENT/SERVER MODEL `[nc.1:428]` też. Bez `-N` transfer pliku nigdy się nie kończy
— i to jest najczęstsza pułapka netcata.

### 4.10 Diagram przejść stanu

```text
                        START [1116-1134]
          STDIN=0  NETOUT=net  NETIN=net  STDOUT=1
                            |
   +------------------------+------------------------+
   |                                                 |
   v  EOF/HUP na stdin                               v  EOF/HUP na net in
 [1176] lub [1210] lub [1260](-W)               [1181] lub [1254] lub [1259](-W)
   STDIN.fd = -1                                   NETIN.fd = -1
   |                                                 |
   | sbuf == 0 ? [1292]                              | nbuf == 0 ? [1298]
   v                                                 v
 -N ? -> shutdown(SHUT_WR) [1294]                  STDOUT.fd = -1 [1299]
 NETOUT.fd = -1 [1295]                               |
   |                                                 | [1195-1199] kaskada w DRUGA strone:
   | [1192-1193] kaskada:                            | STDOUT martwy => shutdown(SHUT_RD)
   | NETOUT martwy => STDIN.fd = -1                  |              => NETIN.fd = -1
   v                                                 v
 +-------------------------------------------------------------+
 |  WARUNEK 1 [1138]: STDIN=-1 & NETIN=-1 & oba bufory puste   |
 |  WARUNEK 2 [1142]: NETOUT=-1 & STDOUT=-1                    |
 |  WARUNEK 3 [1145]: lflag & NETIN=-1 & oba bufory puste      |
 +-------------------------------------------------------------+
                            |
                            v
                        return [1140/1143/1147]
            albo poll()==0 -> return [1163-1164]  (timeout -w)
```

### 4.11 Który slot umiera kiedy i co to pociąga

| Umiera | Powód | Linie | Pociąga za sobą |
|---|---|---|---|
| `STDIN` | `POLLERR`/`POLLNVAL` | 1168–1169 | przez 1292: `NETOUT` (gdy `sbuf == 0`) |
| `STDIN` | `POLLHUP` bez `POLLIN` | 1173–1176 | j.w. |
| `STDIN` | `fillbuf` zwrócił 0 lub −1 | 1209–1210 | j.w. |
| `STDIN` | `NETOUT` już martwy | 1192–1193 | — (to już skutek) |
| `STDIN` | limit `-W` osiągnięty | 1260 | j.w. |
| `NETOUT` | `POLLHUP` | 1183–1187 | przez 1192: `STDIN` |
| `NETOUT` | `drainbuf` zwrócił −1 | 1226–1227 | j.w. |
| `NETOUT` | `STDIN` martwy i `sbuf == 0` | 1292–1295 | `-N` → `shutdown(SHUT_WR)` |
| `NETIN` | `POLLHUP` bez `POLLIN` | 1178–1181 | przez 1298: `STDOUT` (gdy `nbuf == 0`) |
| `NETIN` | `fillbuf` zwrócił 0 (EOF) | 1252–1255 | `shutdown(SHUT_RD)`, potem j.w. |
| `NETIN` | `fillbuf` zwrócił −1 | 1246–1247 | j.w. |
| `NETIN` | limit `-W` | 1256–1261 | **także `STDIN`** |
| `NETIN` | `STDOUT` martwy | 1195–1199 | `shutdown(SHUT_RD)` |
| `STDOUT` | `POLLHUP` | 1189–1190 | przez 1195: `NETIN` |
| `STDOUT` | `drainbuf` zwrócił −1 | 1281–1282 | j.w. |
| `STDOUT` | `NETIN` martwy i `nbuf == 0` | 1298–1300 | — |

---

## 5. `fillbuf()` i `drainbuf()`

### 5.1 `drainbuf()` `[netcat.c:1305-1334]`

```text
  fd == -1                 -> return -1                        [1310-1311]
  n = write(fd, buf, *bufpos)                                   [1321]
  n == -1 && (EAGAIN || EINTR) -> n = TLS_WANT_POLLOUT          [1323-1324]
  n <= 0                   -> return n                          [1326-1327]
  adjust = *bufpos - n
  adjust > 0               -> memmove(buf, buf + n, adjust)     [1330-1331]
  *bufpos -= n
  return n
```

### 5.2 `fillbuf()` `[netcat.c:1337-1362]`

```text
  num = BUFSIZE - *bufpos                                       [1339]
  fd == -1                 -> return -1                        [1342-1343]
  n = read(fd, buf + *bufpos, num)                              [1353]
  n == -1 && (EAGAIN || EINTR) -> n = TLS_WANT_POLLIN           [1355-1356]
  n <= 0                   -> return n                          [1358-1359]
  *bufpos += n
  return n
```

Oba **zaczynają** od gardy `fd == -1` → `return -1` (w `fillbuf` zaraz po
wyliczeniu `num`, `[1342-1343]`; w `drainbuf` jako pierwsza instrukcja,
`[1310-1311]`). Slot może zostać wycofany
w kroku kaskadowym `[1192-1199]` **w tej samej iteracji**, w której `poll()`
zgłosił na nim gotowość; bez tej gardy netcat wołałby `read()`/`write()` na `-1`.

### 5.3 Wartości zwracane — trzy różne znaczenia „liczby ujemnej"

| Wartość | Nazwa | Skąd | Znaczenie | Reakcja w `readwrite()` |
|---|---|---|---|---|
| `0` | — | `read()` | **EOF** | ubij slot `[1210]`, `[1252-1255]` |
| `-1` | — | `read()`/`write()`, albo garda `fd == -1` | **błąd trwały** | ubij slot `[1210]`, `[1227]`, `[1247]`, `[1282]` |
| `-2` | `TLS_WANT_POLLIN` | `<tls.h>`; tu także z `EAGAIN`/`EINTR` w `fillbuf` | „ponów, czekaj na czytelność" | `events = POLLIN` `[1206]`, `[1223]`, `[1243]`, `[1278]` |
| `-3` | `TLS_WANT_POLLOUT` | `<tls.h>`; tu także z `EAGAIN`/`EINTR` w `drainbuf` | „ponów, czekaj na zapisywalność" | `events = POLLOUT` `[1208]`, `[1225]`, `[1245]`, `[1280]` |

> Uwaga o proweniencji: `netcat.c` tych stałych **nie definiuje** — używa tylko
> nazw z `<tls.h>` (`grep TLS_WANT_POLL` → 785, 787, 1205, 1207, 1222, 1224,
> 1242, 1244, 1277, 1279, oraz przypisania w 1324 i 1356).
> Wartości `-2`/`-3` pochodzą z nagłówka `libtls`,
> którego w tym kontenerze nie ma, więc **nie są przez nas zweryfikowane** —
> w przeciwieństwie do wszystkiego, co w tym dokumencie oznaczono jako POMIAR.
> Dla pyncat liczy się tylko to, że są to dwie **różne, ujemne i różne od −1**
> wartości; konkretne liczby są nieistotne, bo nigdy nie wychodzą poza moduł.

Bez TLS sentinele `-2`/`-3` służą **tylko** do zakodowania „przerwane, ponów",
żeby odróżnić to od `-1` (błąd trwały) i `0` (EOF). Trzy różne znaczenia
przeciążone na jeden `ssize_t` — w Pythonie warto to nazwać enumem, bo inaczej
`if ret == -2` jest nieczytelne.

Zauważ **sprytną konsekwencję**: `TLS_WANT_POLLIN` w bloku „write to network"
`[1222-1223]` przestawia `NETOUT.events` na `POLLIN`. Bez TLS to się nie zdarza,
ale kod jest na to przygotowany, bo w TLS zapis może wymagać odczytu (renegocjacja).

### 5.4 Częściowy zapis jest normą

`memmove(buf, buf + n, *bufpos - n)` `[1331]` przesuwa niewysłany ogon na początek
bufora — O(n) przy każdym częściowym zapisie. Pomiar z §4.7 pokazuje **342
częściowe zapisy na 343** przy wymuszonym małym buforze, czyli `memmove` jest na
gorącej ścieżce, nie w obsłudze przypadku brzegowego. W Pythonie odpowiednikiem
jest `del buf[:n]` na `bytearray` — też `memmove` pod spodem, ta sama złożoność.

### 5.5 `EINTR` w Pythonie

Od PEP 475 (Python 3.5) interpreter **sam ponawia** syscalle przerwane przez
`EINTR`, więc gałąź `EINTR` z `[1323]`/`[1355]` jest w Pythonie martwa. Gałąź
`EAGAIN` **nie**: socket jest nieblokujący (`SOCK_NONBLOCK` `[netcat.c:941-942]`,
`accept4(…, SOCK_NONBLOCK)` `[634-635]`), więc `BlockingIOError` jest realny
i trzeba go mapować na `TLS_WANT_POLL*`.

---

## 6. Rozbieżności między dokumentacją a kodem

Cztery flagi, przy których man page, `nc -h`, kod upstream i zachowanie binarium
nie są zgodne. Przy `-i`, `-w` i `-W` są to trzy różne twierdzenia o tym samym
kodzie; przy `-q` kodu upstream **nie ma wcale** (§6.4).
Wszystkie pomiary na `Debian patchlevel 1.226-1ubuntu2`.

### 6.1 `-i` — obiecane w dwóch miejscach, jest w jednym, działa w zerowym

| Źródło | Twierdzenie | Odniesienie |
|---|---|---|
| `nc.1` | „Sleep for *interval* seconds between lines of text sent and received." | **`[nc.1:158-161]`** |
| `nc.1` | „Also causes a delay time between connections to multiple ports." | **`[nc.1:162]`** |
| `nc -h` | „`-i interval` Delay interval for lines sent, ports scanned" | `[netcat.c:1852]` |
| kod rev. 1.241 | `sleep(iflag)` na początku **każdej iteracji** `readwrite()` | **`[netcat.c:1152-1153]`** |
| kod rev. 1.241 | **nic** między portami — `grep -n iflag` daje tylko linie 79, 223, 1152, 1153 | — |

Czyli: man page obiecuje „między liniami" i „między portami"; upstream
implementuje „na iterację pętli relay", co nie jest ani jednym, ani drugim (pętla
nie wie nic o liniach); między portami nie ma **nic**.

**POMIAR — w Debianie 1.226 `-i` jest no-opem we wszystkich scenariuszach:**

| Scenariusz | bez `-i` | z `-i` |
|---|---|---|
| `-z`, 3 zamknięte porty | 0,005 s | 0,006 s (`-i 1`), 0,005 s (`-i 2`) |
| 1 otwarty port, 1 B, `-N` | 0,006 s | 0,005 s (`-i 1`), 0,006 s (`-i 3`) |
| `-z`, 2 **otwarte** porty w zakresie | 0,005 s | 0,006 s (`-i 2`) |
| serwer `-l` odbierający 3 porcje, nadawca `A`·0,3 s·`B`·0,3 s·`C`·0,3 s | 0,910 s | 0,910 s (`-i 1`), 0,911 s (`-i 2`) |
| klient `-N` wysyłający 3 porcje, `A`·0,3 s·`B`·0,3 s·`C` | 0,607 s | 0,607 s (`-i 2`) |

Dwa dolne wiersze to czysty koszt harmonogramu nadawcy: 3 × 0,3 s = 0,9 s i
2 × 0,3 s = 0,6 s. Gdyby `sleep(iflag)` z `[1152-1153]` działał, serwer z `-i 2`
odbierający trzy porcje potrzebowałby ≥ 6 s (sleep przed **każdym** `poll()`);
potrzebował 0,910 s, tyle samo co bez flagi. **Trzy różne zachowania w trzech
miejscach.**

**Decyzja dla pyncat:** implementujemy **obie** semantyki obiecane przez man page
— `sleep` w pętli relay ORAZ przerwę między portami. Świadome odstępstwo od
upstream, do odnotowania w dokumentacji CLI.

### 6.2 `-w` — „no effect on `-l`" jest nieprawdą dla fazy danych

| Źródło | Twierdzenie | Odniesienie |
|---|---|---|
| `nc.1` | „Connections which cannot be established or are idle timeout after *timeout* seconds." | `[nc.1:325-327]` |
| `nc.1` | „The `-w` flag has **no effect on the `-l` option**, i.e. nc will listen forever for a connection, with or without the `-w` flag." | **`[nc.1:328-336]`** |
| `nc.1` | pod `-l`: „Additionally, any timeouts specified with the `-w` option **are ignored**." | **`[nc.1:182-184]`** |
| `nc -h` | „`-w timeout` Timeout for connects **and final net reads**" | `[netcat.c:1875]` |
| kod | `timeout` jest **globalny** `[netcat.c:115]` i trafia wprost do `poll()` w `readwrite()` | **`[netcat.c:1156]`** |

`accept4()` `[634]` nie dostaje żadnego limitu — tu man page ma rację. Ale
`readwrite()` dostaje, więc `-w` **jest** idle timeoutem fazy danych również
w trybie `-l`.

**POMIAR.** Serwer `nc -l -p P -w W` ze stdin na EOF (`< /dev/null`); klient
podłącza się i **nic nie wysyła**. Czas liczony od chwili podłączenia klienta:

| `-w` | Czas do wyjścia serwera | exit |
|---|---|---|
| `-w 1` | **1,009 s** | 0 |
| `-w 2` | **2,009 s** (trzy niezależne przebiegi: 2,009 / 2,009 / 2,009) | 0 |
| `-w 3` | **3,010 s** | 0 |
| (brak) | **19,408 s** — czyli dopóki klient nie odpadł | 0 |

Liniowo i dokładnie `-w` sekund. Zdanie z `[nc.1:182-184]` („are ignored") jest
mocniejsze i **bardziej błędne** niż to z `[nc.1:328-336]`; `nc -h`
`[netcat.c:1875]` jest najdokładniejsze z trzech źródeł. Uwaga na wyjście:
timeout `poll()` daje `return` `[1163-1164]`, **nie** błąd — serwer kończy z 0,
bo `ret = 0` ustawiono w `[562]`.

### 6.3 `-W` — „packets" to w rzeczywistości odczyty

| Źródło | Twierdzenie | Odniesienie |
|---|---|---|
| `nc.1` | „Terminate after receiving *recvlimit* **packets** from the network." | **`[nc.1:320-323]`** |
| `nc -h` | „`-W recvlimit` Terminate after receiving a number of **packets**" | `[netcat.c:1874]` |
| kod | `++recvcount` raz na udany `fillbuf`, czyli raz na `read()` | **`[netcat.c:1256]`** |

Pomiar w §4.8: te same trzy bajty dają `-W 1` → `A` albo `-W 1` → `ABC`, zależnie
od tego, czy przyszły trzema `read()` czy jednym. Dla TCP pojęcie „pakiet" nie
istnieje na poziomie API — `-W` to **limit wywołań `read()`**. Dla UDP „packets"
jest przypadkowo trafne, bo tam jeden `read()` to jeden datagram.

### 6.4 `-q` — nie ma tego w upstream

| Źródło | Stan |
|---|---|
| `netcat.c` rev. 1.241 | `grep -n qflag` → **zero trafień**. Flagi nie ma w stringu `getopt` `[netcat.c:177]` |
| `nc.1` rev. 1.101 | `grep -n 'Fl q'` → **zero trafień** |
| `nc -h` w Debianie | „`-q secs` quit after EOF on stdin and delay of secs" |
| `usage` w Debianie | `[-q seconds]` obecne w bloku `usage:` |

`-q` pochodzi z linii netcata Hobbita / FreeBSD i w Debianie jest dodane patchem.
Semantykę ustalono **empirycznie na binarium** (`sources.debian.org` i
`salsa.debian.org` są zablokowane przez network policy kontenera) — co dla
semantyki jest dowodem mocniejszym niż lektura patcha.

**POMIAR 1.** Klient wysyła `X` i dostaje EOF na stdin; serwer odpowiada
`DELAYED` po 1,5 s i trzyma socket:

| `-q` | Odebrano | Czas życia klienta | exit |
|---|---|---|---|
| (brak) | `DELAYED` | 9,005 s — ubity przez `timeout 9` | 124 |
| `-q 0` | *(nic)* | **0,006 s** | 0 |
| `-q 2` | *(nic)* | **2,006 s** | 0 |
| `-q 3` | *(nic)* | **3,008 s** | 0 |
| `-q -1` | `DELAYED` | 9,005 s — ubity | 124 |
| `-N` (dla porównania) | *(nic)* | 0,007 s | 0 |

**POMIAR 2 — kiedy startuje zegar i czy w jego trakcie trwa relay.** Sterowane
osobno: moment EOF na stdin klienta i moment wysłania przez serwer.

| EOF na stdin | Serwer wysyła | Flaga | Odebrano | Czas |
|---|---|---|---|---|
| po 2,0 s | na 1,0 s (**przed** EOF) | `-q 3` | `REPLY` | **5,005 s** = 2,0 + 3,0 |
| natychmiast | na 1,0 s (**po** EOF) | `-q 3` | *(nic)* | 3,006 s |
| natychmiast | na 1,0 s (**po** EOF) | `-q 6` | *(nic)* | 6,008 s |
| natychmiast | na 1,0 s (**po** EOF) | (brak) | `REPLY` | 11,006 s — ubity |

Wnioski — i to jest więcej, niż mówi `nc -h`:

1. **Zegar startuje przy EOF na stdin**, nie przy starcie procesu (2,0 + 3,0 = 5,005 s).
2. **W trakcie odliczania relay net → stdout NIE działa.** Dane przychodzące po
   EOF są tracone — `-q N` to „poczekaj N sekund i wyjdź", a **nie** „dociągnij
   jeszcze N sekund". Mechanizm odpowiada `sleep(N)` z następnym `return`, bez
   powrotu do `poll()`.
3. `-q 0` → wyjdź natychmiast po EOF; dane w locie przepadają.
4. `-q N`, N < 0 → czekaj w nieskończoność. **Brak `-q` ≡ `-q -1`.**

**Praktyczna pułapka:** `-q 0` bez `-N` **nie wysyła FIN** w ramach
`readwrite()` — po prostu wychodzi, a FIN leci przy `close()` z jądra. Skutek
bywa podobny do `-N`, ale mechanizm inny i bez gwarancji, że bufor wyjściowy
został opróżniony. Do kończenia transferu właściwe jest `-N`, nie `-q 0`.

### 6.5 Dodatkowe rozbieżności — zmierzone, poza listą z zadania

| # | Twierdzenie | Rzeczywistość (POMIAR) |
|---|---|---|
| 1 | `[nc.1:587-591]`: „UDP port scans using the `-uz` combination will **always report success** irrespective of the target machine's state" | **Nieprawda na Linuksie (loopback).** Zamknięty port UDP: stderr pusty, **exit 1** (powtórzone na 4 portach). Otwarty: `Connection to 127.0.0.1 35211 port [udp/*] succeeded!`, exit 0. Powód: ICMP port unreachable wraca do connected socketu i ustawia `SO_ERROR`, więc drugi `write()` z `udptest()` zawodzi. Zdanie z man page jest prawdziwe tam, gdzie ICMP jest filtrowane albo gubione — nie na loopbacku. |
| 2 | Upstream `timeout_connect()` używa `poll()` `[netcat.c:1016]` | Debian 1.226 używa **`select()`**: `strace` pokazuje `pselect6(4, NULL, [3], NULL, {tv_sec=3, tv_nsec=0}, NULL) = 1`, a zaraz po nim `getsockopt(3, SOL_SOCKET, SO_ERROR, [ECONNREFUSED], [4])`. W całym przebiegu: 0 wywołań `poll`/`ppoll`, 1 `pselect6`. `readwrite()` nadal używa `poll()` z czterema slotami. |
| 3 | Upstream `udptest()` robi **4** zapisy bezwarunkowo `[netcat.c:1542-1547]` | **Otwarty** port UDP: dokładnie **4** × `write(3, "X", 1) = 1` ✔. **Zamknięty**: `write = 1`, potem `write = -1 ECONNREFUSED` i proces **wychodzi** — tylko **2** zapisy. Debian przerywa pętlę po porażce. |
| 4 | Upstream odtwarza listener w każdej iteracji `-k` `[netcat.c:582-584]` | Debian 1.226: **1** × `socket`/`bind`/`listen(3,1)`, **4** × `accept4` na 3 połączenia, `close(4)` po każdym. Listener nie jest odtwarzany. |
| 5 | Upstream ustawia tylko `SO_REUSEPORT` `[netcat.c:1063]` | Debian ustawia **oba**: `setsockopt(SO_REUSEADDR)` i `setsockopt(SO_REUSEPORT)`. |
| 6 | Debian `nc -h` wymienia `-b`, `-C`, `-q`, `-Z` (DCCP) | Flag `-b` (broadcast), `-C` (CRLF), `-q`, oraz `-Z` w znaczeniu DCCP **nie ma** w rev. 1.241; `-Z` w upstream to „peer certificate file" `[netcat.c:299-304]`. `-T` w Debianie przyjmuje tylko TOS, bez opcji TLS. **POMIAR:** `nc -h` w kontenerze wypisuje `-b Allow broadcast`, `-C Send CRLF as line-ending`, `-q secs quit after EOF on stdin and delay of secs`, `-Z DCCP mode`, `-T keyword TOS value`. Pełna lista 35 flag Command Summary: `-4 -6 -b -C -D -d -F -h -I -i -k -l -M -m -N -n -O -P -p -q -r -S -s -T -t -U -u -V -v -W -w -X -x -Z -z` — brakuje całej rodziny TLS upstream (`-c`, `-e`, `-H`, `-K`, `-o`, `-R`). |
| 7 | Upstream na nieznaną usługę drukuje `service "%s" unknown` `[netcat.c:1469-1470]` | Debian 1.226 drukuje `nc: port number invalid: %s`. **POMIAR:** `nosuchsvc` i `35101,35099` → `nc: port number invalid: …` w obu przypadkach. Ta sama ścieżka kodu, inny tekst (§3.3). |

### 6.6 Podsumowanie — komu wierzyć

| Priorytet | Źródło | Dlaczego |
|---|---|---|
| 1 | **Pomiar na binarium** | Rozstrzyga, co faktycznie robi *ta* wersja |
| 2 | **Kod** `netcat.c` rev. 1.241 | Rozstrzyga, co robi upstream |
| 3 | `nc -h` `[netcat.c:1840-1880]` | Zwykle bliższy prawdy niż man page (`-w`!), bo żyje w tym samym pliku |
| 4 | `nc.1` | Opisuje intencję sprzed lat; w 4 miejscach rozchodzi się z kodem |

---

## 7. Czego netcat nie robi i dlaczego — flaga `-e`

### 7.1 Co robiło `nc -e` w netcacie 1.10

W netcacie Hobbita `-e <program>` powodowało, że po nawiązaniu połączenia netcat
**podmieniał swoje deskryptory 0, 1 i 2 na socket** i robił `execve()` na podanym
programie. Wszystko, co przyszło z sieci, było wejściem tego programu, a wszystko,
co wypisał, leciało w sieć.

W źródłach 1.10 było to obudowane warunkową kompilacją pod nazwą
`GAPING_SECURITY_HOLE` — autor sam nazwał tę funkcję ziejącą dziurą
bezpieczeństwa i miał rację.

### 7.2 Dlaczego to jest prymitywny backdoor

| Właściwość | Konsekwencja |
|---|---|
| **Brak uwierzytelniania** | Kto pierwszy dojdzie do portu, ten dostaje shell. Netcat nie ma pojęcia tożsamości. |
| **Brak szyfrowania** | Cała sesja — hasła, klucze, zawartość plików — leci plaintextem. |
| **Brak autoryzacji** | Program dziedziczy **pełne uprawnienia** procesu netcata. `nc -e` jako root to root dla każdego, kto się połączy. |
| **Brak logowania** | Żadnego śladu kto, kiedy, co. |
| **Brak terminala** | Program nie ma kontrolującego tty, więc kontrola zadań i sygnały zachowują się nietypowo — co zresztą sprawia, że jako narzędzie administracyjne to jest **złe**, a nie tylko niebezpieczne. |
| **Trywialność nadużycia** | Jedna flaga zamienia diagnostyczne narzędzie w zdalny dostęp do powłoki. |

Suma: `-e` to usługa zdalnego dostępu bez żadnej z własności, których wymaga się
od usługi zdalnego dostępu. Dlatego `nc -e` jest klasycznym elementem
po-eksploatacyjnego zestawu narzędzi i dlatego sama **obecność** netcata z `-e` na
hoście jest traktowana przez narzędzia detekcyjne jako sygnał kompromitacji.

### 7.3 Dlaczego OpenBSD tego nie przepisało

To nie było przeoczenie. Cztery argumenty, wszystkie widoczne w samym pliku:

1. **Rola narzędzia.** Netcat przekazuje bajty między stdin/stdout a socketem.
   Kropka. Uruchamianie procesów to rola `inetd`, `sshd` albo systemd socket
   activation — **narzędzi, które mają uwierzytelnianie, autoryzację i logi**.
2. **`pledge()`/`unveil()`** `[netcat.c:367-423]`. Netcat po sparsowaniu opcji
   zrzeka się praw do wszystkiego poza `stdio inet dns`, a w wariancie bez plików
   woła `unveil("/", "")` `[netcat.c:399]` — **zeruje widoczność systemu plików**.
   `-e` wymagałoby `pledge("exec")` i widoczności `/bin`, czyli zdjęcia całej tej
   ochrony dla jednej flagi. Te dwie rzeczy wykluczają się z definicji.
3. **Powierzchnia audytu.** Jeden plik, 1900 linii, bez `fork()`/`execve()` da
   się przeczytać w całości. Netcat z `exec` to inny program i inny model
   zagrożeń.
4. **Litera `-e` jest zajęta.** W OpenBSD `-e name` to opcja TLS — „przyjmij
   certyfikat peera tylko jeśli zawiera nazwę" `[nc.1:116-123]`,
   `[netcat.c:210-212]`, walidacja `[netcat.c:455-456]`.

Czego brak `-e` **nie** znaczy: netcat nadal nadaje się do testowania usług,
skanowania, transferu plików i jako `ProxyCommand` dla `ssh(1)`
`[nc.1:561-571]`. Usunięcie `-e` nie odebrało mu żadnego legalnego zastosowania.

### 7.4 Czym to zastąpić

| Potrzeba | Właściwe narzędzie | Dlaczego |
|---|---|---|
| Zdalna powłoka | **`ssh`** | Uwierzytelnianie kluczem, szyfrowanie, logi, pty, kontrola dostępu |
| Usługa uruchamiana na żądanie | **systemd socket activation**, `inetd`/`xinetd` | Nadzór procesu, limity zasobów, logi, restart |
| Przekierowanie portu / tunel | **`ssh -L` / `ssh -W`**, `nc` jako `ProxyCommand` | Tunel szyfrowany i uwierzytelniony |
| Testowanie protokołu „ręcznie" | **`nc`** bez `-e` | To dokładnie to, do czego netcat jest |

Jeśli realnym celem jest „mój proces ma obsługiwać połączenia", to jest cała
potrzeba, a nie potrzeba flagi w netcacie. Rozwiązuje ją **socket activation**:
nadzorca (systemd, `inetd`) trzyma listener, przyjmuje połączenie i uruchamia
proces z własnym uwierzytelnianiem, limitami zasobów, logiem i polityką
restartu. Alternatywa równie uczciwa: **napisać serwer**. Pętla relay z §4 nie
jest dużym kodem — `netcat.c:readwrite()` to 199 linii, `pyncat/relay.py` w tym
repo 517 (327 bez komentarzy i pustych wierszy) — a proces, który sam trzyma
socket, decyduje też, *kto* i *co* może przez niego zrobić.

Istnieją narzędzia, które potrafią przypiąć dowolny program do socketu i robią
to świadomie (`socat` jest najbardziej znanym). Nie opisujemy ich konfiguracji,
bo ryzyko nie zależy od narzędzia: **listener, który uruchamia program, bez
uwierzytelniania i szyfrowania jest backdoorem niezależnie od tego, czym go
zbudowano.** To ta sama klasa ryzyka co `nc -e`, tylko pod inną nazwą.

**Rekomendacja:** gdy ktoś sięga po `-e`, w przeważającej większości przypadków
poprawną odpowiedzią jest `ssh`, a w pozostałych socket activation. Trzeciej
odpowiedzi w tym dokumencie nie ma i nie będzie — §7.5.

### 7.5 Granica tego projektu

`pyncat` **nie implementuje** uruchamiania poleceń — ani `-e`, ani `-c`
w znaczeniu „uruchom program i podłącz go do socketu", ani niczego, co składa się
na reverse shell. Nie jest to kwestia braku czasu ani etapu rozwoju: to granica
projektu, dokładnie ta sama, którą postawił OpenBSD. Narzędzie przekazuje bajty
między stdin/stdout a socketem.

Dodatkowo pomijamy, z powodów technicznych (nie etycznych): TLS (`-c` i rodzina),
proxy SOCKS/HTTP (`-x`, `-X`, `-P` — w upstream żyją w osobnym pliku `socks.c`),
gniazda uniksowe (`-U`), `fdpass` (`-F`), emulację telnetu (`-t`), SCTP, oraz
`pledge`/`unveil`, które nie mają odpowiednika w Pythonie.

---

## 8. Co zostało zmierzone

Wszystko na `/usr/bin/nc` → `/usr/bin/nc.openbsd` (`readlink -f`),
`OpenBSD netcat (Debian patchlevel 1.226-1ubuntu2)`, Linux 6.18.44, loopback.
Każda liczba w tym dokumencie jest podana w sekcji, która jej używa, i oznaczona
słowem **POMIAR**. Zmierzone grupy: półzamknięcie `-N` (§4.9), `-w` jako idle
timeout w `-l` (§6.2), `-W` przy odczytach rozdzielonych i scalonych (§4.8),
`-q` z łatki Debiana wraz ze startem zegara (§6.4), `-i` w pięciu scenariuszach
(§6.1), kody wyjścia TCP i UDP (§3.3), parsowanie portów (§3.3), dokładne stringi
stderr (§3.4), `strace` modelu czterech slotów i `BUFSIZE` (§4.1, §4.2), `POLLHUP`
z `POLLIN` (§4.6), backpressure wymuszony i domyślny (§4.7), integralność 1 MiB
(§4.7), tryby serwera UDP i TCP `-k` (§3.4, §3.5), liczba zapisów `udptest()`
(§6.5) oraz **siedem** rozbieżności Debian ↔ upstream (§6.5).

Pomiary czasowe są powtarzalne z dokładnością do kilku milisekund; tam gdzie
liczba zależy od przebiegu (łączna liczba `write()` w §4.7, czas życia klienta
bez `-N`/bez `-q` — ograniczony przez `timeout` harnessu, nie przez netcata),
jest to w tekście powiedziane wprost. `sha256` losowego payloadu świadomie nie
jest podawany jako stała (§4.7).

---

## 9. Gdzie znaleźć oryginał

| Co | Gdzie |
|---|---|
| `netcat.c` rev. 1.241, `nc.1` rev. 1.101 | `cvsweb.openbsd.org` → `src/usr.bin/nc/`, albo `raw.githubusercontent.com/openbsd/src/master/usr.bin/nc/` |
| `atomicio.c` | ten sam katalog — używane tylko przez `atelnet()` `[netcat.c:1449]` |
| `socks.c` | ten sam katalog — `-x`/`-X`/`-P`, poza „jednym plikiem" |
| łatki Debiana | `sources.debian.org/src/netcat-openbsd/` → `debian/patches/` |

**Powiązane dokumenty w tym repo:**
`docs/c-vs-python.md` — odwzorowanie pętli relay na `select.poll()` i pomiary
wydajności. `pyncat/constants.py` — stałe z odniesieniami do linii (patrz §2.4:
numery wymagają korekty). `pyncat/relay.py` — odpowiednik `readwrite()`.
`pyncat/netio.py` — odpowiednik `fillbuf()`/`drainbuf()`.
