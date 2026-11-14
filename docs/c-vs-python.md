# C kontra Python: pomiar, nie przekonania

Dokument porównuje dwie implementacje **tego samego algorytmu**: pętli relay
netcata. `pyncat/` w Pythonie i `c_reference/pyncat_ref.c` w C11. Ta sama
struktura funkcji, te same stałe, ta sama semantyka — więc różnice w czasie
i w kodzie są różnicami **językowymi**, a nie projektowymi.

Każda liczba w tym dokumencie pochodzi z przebiegu `ci/bench.py` na maszynie
opisanej niżej. Nie ma tu liczb przepisanych z internetu ani oszacowań.

> **Wniosek w jednym zdaniu, dla niecierpliwych:** przy domyślnym
> `BUFSIZE = 16384` wersja w Pythonie przepuszcza **616 MiB/s**, wersja w C
> **1554 MiB/s** (2,5×) — ale pętla relay **nie jest** syscall-bound, bo
> liczba syscalli jest w obu **identyczna**; cała różnica to stały koszt
> iteracji interpretera, około **5 µs**, i dlatego stosunek spada z 6,8× przy
> buforze 512 B do 2,0× przy 64 KiB.

---

## 1. Metodologia

### 1.1 Środowisko

| | |
|---|---|
| CPU | Intel(R) Xeon(R) Processor @ 2.10 GHz, 4 rdzenie |
| Jądro | Linux 6.18.44-fc-v470, x86_64 |
| Python | CPython 3.13.18 |
| Kompilator | `cc` (Ubuntu 13.3.0-6ubuntu2~24.04.1), `-std=c11 -O2` |
| Punkt odniesienia | OpenBSD netcat, Debian patchlevel 1.226-1ubuntu2 |

Maszyna jest **współdzielona i zwirtualizowana**, więc wartości bezwzględne
nie przeniosą się na inny sprzęt. Stosunki między implementacjami — tak,
bo wszystkie trzy mierzone są w tych samych warunkach, przeplatane w czasie.

### 1.2 Jak mierzone

* **Transfer idzie przez loopback**, między dwoma procesami tej samej
  implementacji. Czas liczony od uruchomienia klienta do zakończenia obu.
* **Źródło danych w tmpfs** (`/dev/shm`), ujście `/dev/null`. Dysku nie ma
  w pomiarze; mierzymy pętlę relay, nie I/O blokowe.
* **Mediana z N powtórzeń**, po odrzuconym przebiegu rozgrzewającym. Podajemy
  min i max, bo na współdzielonym CPU rozrzut jest informacją.
* **Rozmiar transferu dobrany tak, żeby narzut startu był marginalny.**
  To nie detal: przy 8 MiB cały transfer w Pythonie trwa 64 ms, czyli
  **mniej niż sam start interpretera** (62 ms). Taka liczba nie mówi nic
  o pętli relay. Pomiary główne robimy na 256 MiB i 1 GiB.
* **Narzut startu mierzony osobno** (`-h`) i odejmowany tam, gdzie interesuje
  nas sama pętla.
* **RSS** z `VmHWM` w `/proc/<pid>/status`, odpytywanego z zewnątrz podczas
  transferu.

#### Pułapka, w którą wpadłem i którą warto opisać

Pierwsza wersja pomiaru RSS używała `os.wait4()` i `ru_maxrss`. Dawała dla
wersji w C **11 712 KiB** — dokładnie tyle samo, co dla `nc`. Podejrzanie
równo. Powód: `ru_maxrss` to maksimum w **całym życiu procesu**, a proces
powstaje przez `fork()` z mierzącego interpretera i do `exec()` ma (przez
copy-on-write) cały jego obraz pamięci. Mierzyłem więc RSS Pythona, nie
netcata. `VmHWM` czytany z zewnątrz tej wady nie ma i daje **1 648 KiB**,
czyli siedmiokrotnie mniej.

Morał, który jest w tym dokumencie ważniejszy od samej liczby: **pomiar,
w którym dwie różne rzeczy wychodzą identycznie, jest prawdopodobnie
pomiarem czegoś trzeciego.**

### 1.3 Odtworzenie

```bash
make -C c_reference
python3 ci/bench.py                 # pełny przebieg (256 MiB, 5 powtórzeń)
python3 ci/bench.py --quick         # skrócony, do CI
python3 ci/bench.py --json out.json # wyniki maszynowo
```

---

## 2. Wyniki

### 2.1 Przepustowość (256 MiB, `BUFSIZE = 16384`, mediana z 5)

| Implementacja | czas | MiB/s | po odjęciu startu | min–max |
|---|---|---|---|---|
| Python (`pyncat`) | 0,416 s | 616,0 | **723,6** | 0,365–0,466 |
| C (`pyncat_ref`) | 0,165 s | 1554,3 | **1575,4** | 0,165–0,165 |
| `nc` (OpenBSD 1.226) | 0,165 s | 1554,3 | 1567,7 | 0,165–0,166 |

**Stosunek C/Python: 2,5× surowo, 2,2× po odjęciu startu.**

Najważniejsza liczba w tej tabeli nie dotyczy Pythona: moja wersja w C i
prawdziwy `nc` dają **ten sam czas z dokładnością do milisekundy**
(0,165 s). To znaczy, że implementacja referencyjna jest faktycznie
odwzorowaniem netcata, a nie czymś przypadkiem podobnym — i że porównanie
„jabłka do jabłek" ma sens.

Rozrzut jest asymetryczny: C ma 0,165–0,165 (zero zmienności), Python
0,365–0,466 (±14%). To nie przypadek — garbage collector i alokacje
obiektów `bytes` w `os.read()` wprowadzają zmienność, której C nie ma.

### 2.2 Narzut startu (`-h`, mediana z 15)

| Implementacja | czas |
|---|---|
| Python | **61,8 ms** |
| C | 2,2 ms |
| `nc` | 1,4 ms |

**28× (wobec mojego C) / 44× (wobec `nc`).** Prawie całość to import
`socket`, `select`, `getopt`, `dataclasses` i inicjalizacja interpretera.

Konsekwencja praktyczna: dla netcata uruchamianego z pętli powłoki
(`for p in $(seq 1 1000); do pyncat -z host $p; done`) narzut startu
**dominuje nad wszystkim innym** i wersja w Pythonie jest 28× wolniejsza
niezależnie od jakości pętli relay. Dla jednego długiego transferu jest
nieistotny.

### 2.3 Skan `-z` (2000 zamkniętych portów, loopback)

| Implementacja | czas | µs/port |
|---|---|---|
| Python | 0,128 s | **63,8** |
| C | 0,017 s | 8,3 |
| `nc` | 0,016 s | 8,1 |

**7,7×.** Tu jest wyraźnie gorzej niż przy transferze (2,5×), bo na port
przypada stała liczba operacji (`getaddrinfo`, `socket`, `connect`, `poll`,
`getnameinfo`, `close`) i **żadnych bajtów do amortyzacji**. Narzut Pythona
na port to 63,8 − 8,3 = **55,5 µs**, czyli jedenaście razy więcej niż stały
koszt iteracji w pętli relay (§3.3) — bo dochodzą tu konstrukcja obiektu
`socket`, wywołania `getaddrinfo`/`getnameinfo` przez warstwę `socket`
i rozpakowywanie krotek `sockaddr`.

### 2.4 Pamięć (`VmHWM` serwera, transfer 256 MiB)

| Implementacja | RSS |
|---|---|
| Python | **16 152 KiB** |
| C | 1 648 KiB |
| `nc` | 1 824 KiB |

**9,8×.** Dwa bufory po 16 KiB to w obu implementacjach 32 KiB; cała
różnica to interpreter. Zwróć uwagę, że RSS **nie rośnie** z rozmiarem
transferu — backpressure w pętli relay działa, bufor nie ma prawa przekroczyć
`BUFSIZE` (pilnuje tego `tests/test_netio.py::test_nie_przekracza_bufsize`).

---

## 3. Gdzie idzie czas

To jest właściwa treść tego dokumentu. „Python jest wolniejszy" nie jest
odpowiedzią na nic.

### 3.1 Liczba syscalli jest identyczna

`strace -c -f` na kliencie, transfer 256 MiB, `BUFSIZE = 16384`:

| syscall | Python | C | `nc` |
|---|---|---|---|
| `poll` | 32 771 | 32 771 | 32 770 |
| `write` | 16 384 | 16 384 | 16 384 |
| `read` | 16 520 | 16 387 | 16 397 |

`write` = 16 384 = 256 MiB / 16 KiB — dokładnie co do jednego. `poll` i
`write` zgadzają się **bit w bit** między Pythonem i C. Nadwyżka 133
odczytów w Pythonie to import modułów przy starcie (`.pyc` z dysku), nie
praca pętli.

Ta tabela jest dowodem dwóch rzeczy:

1. **Algorytm został odwzorowany wiernie.** Nie da się mieć identycznego
   profilu syscalli z innym algorytmem.
2. **Pętla relay NIE jest syscall-bound.** Gdyby była, identyczna liczba
   syscalli dawałaby identyczny czas. Daje 2,5× różnicy.

To wniosek odwrotny niż w projekcie bliźniaczym `pynmap-basic`, gdzie pętla
skanowania okazała się syscall-bound, a przewaga C sprowadzała się do narzutu
startu. Tu jest inaczej, i poniżej jest dlaczego.

### 3.2 Liczba syscalli przy różnych BUFSIZE — nadal identyczna

Transfer 64 MiB, `strace -c -f`, rekompilacja C z `-DBUFSIZE=`:

| BUFSIZE | `poll` Py | `poll` C | `write` Py | `write` C |
|---|---|---|---|---|
| 512 | 262 147 | 262 147 | 131 072 | 131 072 |
| 2 048 | 65 539 | 65 539 | 32 768 | 32 768 |
| 8 192 | 16 387 | 16 387 | 8 192 | 8 192 |
| 16 384 | 8 195 | 8 195 | 4 096 | 4 096 |
| 65 536 | 2 051 | 2 051 | 1 024 | 1 024 |
| 262 144 | 515 | 515 | 256 | 256 |

Zgodność jest doskonała w całym zakresie. Warto zauważyć regularność:

```
poll ≈ 2 × write + 3
```

To nie jest nieefektywność, to **struktura maszyny stanów**. Maska `POLLOUT`
na slocie wyjściowym jest uzbrajana *po* `poll()` [netcat.c:1212-1213], więc
zapis następuje w **następnej** iteracji. Jeden bufor danych = jedna iteracja
na odczyt + jedna na zapis. Pomiar potwierdza opis z
`docs/netcat-architecture.md` §4 — i jest to najtańszy sposób sprawdzenia,
czy implementacja naprawdę działa tak, jak myślimy, że działa.

### 3.3 Koszt jednej iteracji pętli

Skoro iteracji jest tyle samo, dzielimy czas przez liczbę `poll()`:

| bajtów na iterację | Python | C | różnica |
|---|---|---|---|
| 256 | 6,01 µs | 1,01 µs | 5,00 µs |
| 1 024 | 7,87 µs | 1,75 µs | 6,12 µs |
| 4 095 | 13,11 µs | 3,93 µs | 9,18 µs |
| 8 189 | 20,12 µs | 7,86 µs | 12,26 µs |

W reżimie małych buforów (256–1024 B na iterację), gdzie praca na bajtach
jest zaniedbywalna, różnica stabilizuje się na **około 5–6 µs na iterację**.
To jest cena interpretera za jedno przejście pętli `readwrite()`: zbudowanie
słownika masek w `_poll_once`, rejestracja w obiekcie `select.poll`,
rozdzielenie `revents` na cztery slots, cztery bloki warunków, alokacja
obiektu `bytes` w `os.read()` i `memmove` przy `del buf[:n]`.

5 µs to na tym CPU około 10 000 cykli. Dla porównania: cały `poll()` +
`read()` + `write()` w C kosztuje 1,01 µs, czyli ~2 100 cykli. **Interpreter
dokłada pięć razy więcej, niż kosztuje praca, którą organizuje.**

### 3.4 BUFSIZE decyduje, co dominuje

Transfer 1 GiB, mediana z 3, po odjęciu narzutu startu:

| BUFSIZE | Python MiB/s | C MiB/s | C/Python |
|---|---|---|---|
| 8 192 | 433,8 | 1183,4 | 2,73× |
| 16 384 *(domyślny)* | 701,2 | 1667,3 | **2,38×** |
| 65 536 | 1015,5 | 1991,9 | 1,96× |
| 262 144 | 754,8 | 1814,9 | 2,40× |

Oraz mały koniec skali (256 MiB, bo przy 512 B transfer 1 GiB trwałby pół minuty):

| BUFSIZE | Python MiB/s | C MiB/s | C/Python |
|---|---|---|---|
| 512 | 38,8 | 264,5 | **6,82×** |
| 2 048 | 142,6 | 700,8 | 4,91× |

**To jest centralny wynik tego dokumentu.** Stosunek C/Python spada
monotonicznie z 6,8× do 2,0×, bo stały koszt iteracji (5 µs w Pythonie,
1 µs w C) amortyzuje się na coraz większej liczbie bajtów. Przy 512 B na
iterację interpreter jest całym kosztem; przy 64 KiB większość czasu zajmuje
kopiowanie bajtów przez jądro, które w obu językach kosztuje tyle samo.

Dwie uwagi, żeby nie przeczytać tej tabeli zbyt optymistycznie:

* **Spadek przy 262 144 B jest realny i występuje w OBU językach**
  (Python 1015 → 755 MiB/s, C 1992 → 1815 MiB/s). To nie efekt językowy,
  a sprzętowy: bufor 256 KiB przestaje się mieścić w cache L2, więc
  `memmove` i kopiowanie w jądrze zwalniają. Dlatego `BUFSIZE = 16384`
  w netcacie nie jest przypadkową wartością — siedzi w okolicy optimum
  i jest bezpieczny pamięciowo.
* 616–1015 MiB/s w Pythonie to **4,9–8,1 Gbit/s**. Dla rzeczywistego
  zastosowania netcata (łącze 1 GbE, a nawet 10 GbE) wersja w Pythonie
  **nie jest wąskim gardłem**. Różnica 2,5× jest widoczna wyłącznie na
  loopbacku, gdzie nie ma sieci.

### 3.5 Czego NIE zmierzyłem

Uczciwie, bo to ogranicza wnioski:

* **Wielu równoczesnych połączeń.** Netcat obsługuje jedno naraz
  (`listen(s, 1)`), więc nie ma tu czego mierzyć — ale to znaczy, że nic
  nie wiem o zachowaniu obu wersji pod obciążeniem współbieżnym.
* **Prawdziwej sieci.** Sandbox, w którym to powstało, przechwytuje ruch
  wychodzący: połączenie do dowolnego adresu zewnętrznego „udaje się"
  niezależnie od tego, czy coś tam nasłuchuje (sprawdzone na `198.51.100.1`
  i `10.255.255.1`). Wszystkie pomiary są więc z loopbacku. Realny test
  sieciowy przez bridge `docker0` jest w CI (`.github/workflows/ci.yml`),
  ale bez pomiarów czasu.
* **Innych implementacji Pythona.** PyPy prawdopodobnie zamknąłby
  większość luki w §3.3 (to czysty narzut interpretera, idealny materiał
  dla JIT-a), ale tego nie sprawdziłem.
* **IPv6.** W tym kontenerze nie ma działającego loopbacku IPv6
  (`AF_INET6` → `Address family not supported by protocol`), więc ścieżka
  IPv6 nie została zweryfikowana ani w pomiarach, ani w testach — test
  `test_6_wymusza_ipv6` jest pomijany.

---

## 4. Różnice w konstrukcjach języka

### 4.1 `fillbuf()`/`drainbuf()` — gdzie Python nie jest krótszy

C (`c_reference/pyncat_ref.c:1234`):

```c
n = write(fd, buf, *bufpos);
if (n == -1 && (errno == EAGAIN || errno == EINTR))
	n = WANT_POLLOUT;
if (n <= 0)
	return n;
adjust = (ssize_t)*bufpos - n;
if (adjust > 0)
	memmove(buf, buf + n, (size_t)adjust);
*bufpos -= (size_t)n;
return n;
```

Python (`pyncat/netio.py`):

```python
try:
    n = os.write(fd, buf)
except (BlockingIOError, InterruptedError):
    return WANT_POLLOUT
except OSError:
    return IO_ERROR
if n <= 0:
    return n
del buf[:n]
return n
```

Trzy rzeczy warte uwagi:

1. **`del buf[:n]` zastępuje `memmove` + aktualizację `bufpos`.** W CPythonie
   to dosłownie jeden `memmove` na buforze obiektu, więc złożoność jest ta
   sama. Python oszczędza tu dwie linie, nie klasę złożoności.
2. **Para `(buf, bufpos)` z C staje się jednym `bytearray`.** W C trzeba
   przekazywać wskaźnik i pozycję osobno (`size_t *bufpos`), bo tablica nie
   zna swojej długości. To jedyne miejsce, gdzie Python realnie upraszcza
   sygnaturę.
3. **Wyjątki kontra `errno`.** Python wymusza rozdzielenie „ponów"
   (`BlockingIOError`) od „błąd trwały" (`OSError`) na poziomie typów, co
   jest czytelniejsze niż `if (n == -1 && errno == ...)`. Ale uwaga: od
   **PEP 475** (Python 3.5) interpreter sam ponawia syscalle przerwane
   sygnałem, więc gałąź `InterruptedError` jest w CPythonie praktycznie
   martwa. Zostawiliśmy ją dla symetrii z oryginałem — i to jest przykład
   sytuacji, w której wierne odwzorowanie C daje w Pythonie kod
   nieosiągalny.

### 4.2 `poll()` — jedyne miejsce, gdzie Python przeszkadza

To najciekawsza różnica w całym projekcie.

W C `poll()` dostaje **tablicę**, a jądro wypełnia `revents` **osobno dla
każdego wpisu**. Netcat tego wprost używa: `POLL_NETOUT` i `POLL_NETIN` to
dwa wpisy na **tym samym deskryptorze**, z różnymi maskami i osobnym
„życiem". Half-close jest modelowany wyłącznie przez `pfd[i].fd = -1` na
jednym z nich.

```c
struct pollfd pfd[4];
pfd[POLL_NETOUT].fd = net_fd;  pfd[POLL_NETOUT].events = 0;
pfd[POLL_NETIN ].fd = net_fd;  pfd[POLL_NETIN ].events = POLLIN;
poll(pfd, 4, timeout);           /* jedno wywolanie, cztery niezalezne wyniki */
```

W Pythonie:

* **`selectors` nie da się użyć.** `SelectorKey` jest indeksowany po
  deskryptorze, więc `register(net_fd, EVENT_READ)` i
  `register(net_fd, EVENT_WRITE)` kolidują. Do tego `selectors` ukrywa
  `POLLHUP` (zwraca `EVENT_READ`), a netcat rozróżnia „HUP z danymi" od
  „HUP bez danych" — bez tego rozróżnienia gubi się ogon strumienia.
* **`select.poll()` to mapa `fd → maska`**, nie tablica. Druga rejestracja
  tego samego fd nadpisuje pierwszą.

Rozwiązanie w `pyncat/relay.py` (`_poll_once`) to emulacja w dwóch krokach:

```python
# 1. rejestrujemy SUMĘ masek dla każdego unikalnego fd
want: dict[int, int] = {}
for slot in pfd:
    if slot.fd != FD_RETIRED:
        want[slot.fd] = want.get(slot.fd, 0) | slot.events

# 2. rozdzielamy wynik z powrotem na slots
for slot in pfd:
    slot.revents = raw.get(slot.fd, 0) & (slot.events | _UNMASKABLE)
```

Maskowanie przez `slot.events | POLLERR|POLLHUP|POLLNVAL` odtwarza dokładnie
to, co jądro wpisuje w `revents` pojedynczego wpisu — POSIX wymaga, by te
trzy flagi były raportowane niezależnie od `events`, i netcat na tym polega
(`POLL_NETOUT` startuje z `events = 0`, a reaguje na `POLLHUP`).

**To jest ta nadwyżka kodu, która zjada oszczędności z §4.1.** Dwadzieścia
linii i słownik na iterację, żeby odtworzyć coś, co w C jest darmowe, bo
tablica jest tablicą. Jednocześnie to główne źródło 5 µs z §3.3.

### 4.3 Flagi globalne kontra obiekt opcji

C ma dwadzieścia kilka zmiennych globalnych [netcat.c:77-117]:

```c
int	dflag;		/* detached, no stdin */
int	lflag;		/* Bind to local port */
int	Nflag;		/* shutdown() network socket */
int recvcount, recvlimit;
```

Python ma jeden `@dataclass Options` przekazywany jawnie. Ta sama informacja,
ale widać, kto jej używa — i da się przetestować parsowanie bez uruchamiania
procesu (`tests/test_cli.py`, 40 przypadków w procesie).

Nazwy pól zachowaliśmy 1:1 z nazwami globali (`Nflag`, nie `shutdown_after_eof`),
łamiąc PEP 8 — celowo, żeby kod dał się czytać obok oryginału. To decyzja,
którą w zwykłym projekcie uznałbym za błąd; tutaj czytelność *względem źródła*
jest ważniejsza niż zgodność z konwencją.

Jeden kwirk wymagał dosłownego odwzorowania globalnego stanu:
`recvcount` dla `-W` **nie jest zerowany** między połączeniami w trybie `-k`,
więc licznik akumuluje się przez całe życie procesu. Stąd `RelayState`
tworzony raz w `app.py` i przekazywany do wszystkich wywołań `readwrite()`.
Gdybyśmy użyli zmiennej lokalnej, zachowanie `-l -k -W 2` różniłoby się od
oryginału.

### 4.4 `getopt` kontra `argparse`

`argparse` jest w Pythonie domyślnym wyborem i tutaj jest **zły**:

* `-q -1` — `argparse` traktuje łańcuch zaczynający się od `-` jako kolejną
  opcję. A `-q -1` (czekaj bez końca) to udokumentowana składnia netcata.
* `argparse` dopuszcza skróty długich opcji i przestawia argumenty; netcat nie.

`getopt` z biblioteki standardowej to **ten sam** `getopt(3)`: klastrowanie
(`-ul`), wartości zbite z flagą (`-w5`), zatrzymanie na pierwszym
nie-argumencie. Sprawdzone porównawczo. Cena: trzeba ręcznie napisać `usage`
i obsłużyć każdą flagę w łańcuchu `elif` — co przy pierwszym podejściu
kosztowało mnie przeoczenie `-z` (łapie to teraz
`tests/test_cli.py` przez audyt kompletności łańcucha).

### 4.5 `shutdown()` — jedyne wymuszone odstępstwo API

W C `shutdown()` bierze surowy deskryptor, więc netcat woła je wprost na
`pfd[i].fd`. W Pythonie `shutdown` jest **metodą obiektu `socket`** i nie ma
odpowiednika w `os`. Dlatego `readwrite()` w `pyncat` przyjmuje obiekt
socketu, a nie liczbę:

```python
def readwrite(net_sock, opts, state=None, *, stdin_fd=0, stdout_fd=1):
    net_fd = net_sock.fileno()
```

Skutek uboczny jest korzystny: w oryginale `shutdown()` można by przez pomyłkę
zawołać na stdin/stdout, u nas nie można.

### 4.6 Rozmiar kodu — wynik, którego nie przewidywałem

Linie kodu bez komentarzy, docstringów i pustych:

| | linie kodu | linie wszystkich |
|---|---|---|
| `pyncat/` (10 modułów) | **855** | 1 876 |
| `c_reference/pyncat_ref.c` | **859** | 1 545 |

**Praktycznie tyle samo.** Spodziewałem się, że Python będzie wyraźnie
krótszy — zwykle jest. Nie jest, i to jest pouczające: ten kod składa się
z **maszyny stanów i obsługi syscalli**, a nie z przetwarzania struktur
danych. Python wygrywa tam, gdzie można zastąpić dwadzieścia linii pętli
jednym `dict`/comprehension. Tutaj nie ma czego zastąpić: każda gałąź
`if (pfd[n].revents & POLLHUP)` musi istnieć w obu językach, bo odpowiada
osobnemu stanowi protokołu.

Co więcej, w dwóch miejscach Python potrzebuje **więcej** kodu niż C:
emulacja tablicy `pollfd` (§4.2) i ręczna obsługa `getopt` (§4.4).

Różnica jest w liczbie linii **wszystkich**: 1 876 wobec 1 545, bo
komentarzy i docstringów jest więcej — ale to cecha tego projektu
(dokumentujemy odwzorowanie), nie języka.

---

## 5. Wnioski: kiedy który język

Konkretnie, bo neutralne „to zależy" nie jest wnioskiem.

**C — gdy którekolwiek z tych jest prawdą:**

* program jest uruchamiany **często i krótko** (narzut startu 28×; skan
  z pętli powłoki, hook, wrapper w `inetd`);
* liczy się **RSS** (10×; router, kontener z limitem, system wbudowany —
  w kontekście ESP32 czy OpenWrt 16 MB na proces to nie jest opcja);
* pracujesz z **małymi porcjami danych** (2,5× rośnie do 6,8× przy
  buforze 512 B — protokół pakietowy, interaktywny terminal, przekazywanie
  linii);
* potrzebujesz **determinizmu czasowego** (C miał 0,165–0,165 s, Python
  0,365–0,466 s; GC nie pyta o pozwolenie);
* narzędzie ma być **jedną statyczną binarką** bez interpretera.

**Python — gdy:**

* przepustowość 600 MiB/s wystarcza (czyli: zawsze, gdy po drugiej stronie
  jest prawdziwa sieć, a nie loopback);
* liczy się **czas wprowadzania zmian** — dodanie flagi i testu to minuty,
  a nie cykl kompilacji i polowanie na UB;
* chcesz **testowalności wewnątrzprocesowej**: `tests/test_relay.py` odpala
  pętlę relay w wątku na `socketpair()` i sprawdza, czy poleciał FIN. Tego
  samego w C nie da się zrobić bez wystawienia wewnętrznych funkcji i
  osobnego harnessa — moja wersja w C jest testowana wyłącznie jako proces,
  od zewnątrz;
* **bezpieczeństwo pamięciowe** ma wartość. Wersja w C przechodzi
  AddressSanitizer bez wycieków, ale ta gwarancja kosztowała osobny cel
  w Makefile, osobny przebieg w CI i świadome pilnowanie każdego
  `freeaddrinfo()`. W Pythonie ta klasa błędów nie istnieje.

**Dla DevSecOps, czyli po co to komu:** netcat jest narzędziem, które
uruchamia się tysiące razy w skryptach — i tam narzut startu 62 ms wygrywa
nad wszystkim innym, więc produkcyjnie bierz `nc`. Ale rozumienie, **dlaczego**
`-N` jest konieczne do zakończenia transferu, dlaczego `-z` na UDP wysyła
cztery bajty, i dlaczego exit code 0 nie znaczy „wszystkie porty otwarte" —
to wiedza, którą zdobywa się przez przepisanie, nie przez przeczytanie
man page. Tabele w `docs/tablice-decyzyjne.md` są tym, co z tego zostaje.

---

## 6. Czego wersja w Pythonie nie ma

Pominięte świadomie, z uzasadnieniem. Pełna analiza w
`docs/netcat-architecture.md` §7.

| Pominięte | Powód |
|---|---|
| **wykonywanie poleceń (`-e`)** | OpenBSD **świadomie** tego nie przepisało z netcata 1.10. To granica projektu, nie przeoczenie. Narzędzie przekazuje bajty, nie uruchamia procesów. Pilnuje tego test `test_nie_ma_zadnej_flagi_uruchamiajacej_program`, który grepuje moduły pod kątem `subprocess`, `os.exec`, `os.fork`, `pty`. |
| TLS (`-c -H -R -e -o -Z -K -C`) | Wymaga `libtls`; `ssl` z Pythona ma inny model niż `tls_*(3)`, więc porównanie 1:1 przestałoby być uczciwe |
| proxy SOCKS/HTTP (`-x -X -P`) | W netcacie siedzi w osobnym pliku `socks.c`, poza zakresem „jednego pliku `netcat.c`" |
| gniazda uniksowe (`-U`) | Inna rodzina adresów, nie TCP/UDP |
| SCTP | Nie ma tego w OpenBSD nc |
| `-F` (przekazanie fd) | `SCM_RIGHTS`; ciekawostka bez związku z pętlą relay |
| `-t` (telnet) | Negocjacja IAC z lat 80. |
| opcje socketu (`-D -T -I -O -M -m -S`) | Poza zakresem; `-S` (TCP MD5SIG) wymaga konfiguracji klucza w jądrze |
| `-V` (rtable), `pledge`/`unveil` | Specyficzne dla OpenBSD, brak odpowiednika w Pythonie |
| Windows | `select.poll` nie istnieje na Windows. `select.select` by się skompilował, ale nie rozróżnia `POLLHUP` od gotowości do czytania, więc propagacja EOF przestałaby działać — a to jest sedno tego programu. Lepiej nie wspierać, niż wspierać błędnie. |
| IPv6 — **niezweryfikowane** | Kod obsługuje `-6` i `AF_INET6`, ale w tym kontenerze nie ma loopbacku IPv6, więc ścieżka nie została przetestowana. Jedyna funkcja w repo z takim statusem. |

Każda pominięta flaga jest **rozpoznawana** przez parser i daje komunikat
mówiący *dlaczego* jej nie ma, zamiast „nieznana opcja":

```
$ python3 -m pyncat -c example.com 443
pyncat: TLS (-c) pominięte: wymaga libtls, a ssl z Pythona ma inny model
niż tls_*(3), więc porównanie 1:1 przestałoby być uczciwe
```
