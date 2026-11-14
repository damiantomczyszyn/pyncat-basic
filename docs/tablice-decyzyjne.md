# Tablice decyzyjne

Materiał referencyjny: co się dzieje, kiedy. Każda tablica odpowiada jednemu
miejscu w `netcat.c` (rev. 1.241) i jest potwierdzona pomiarem na
`nc.openbsd 1.226` albo testem w `tests/`.

Spis:

1. [Half-close: kto zamyka co i kiedy](#1-half-close-kto-zamyka-co-i-kiedy)
2. [Propagacja EOF — tablica przejść](#2-propagacja-eof--tablica-przejść)
3. [Kiedy pętla relay się kończy](#3-kiedy-pętla-relay-się-kończy)
4. [Kody wyjścia](#4-kody-wyjścia)
5. [Komunikaty: pyncat ↔ nc](#5-komunikaty-pyncat--nc)
6. [TCP kontra UDP](#6-tcp-kontra-udp)
7. [Dopuszczalne kombinacje flag](#7-dopuszczalne-kombinacje-flag)
8. [Parsowanie portów](#8-parsowanie-portów)
9. [Rozbieżności: man page ↔ kod ↔ Debian ↔ pyncat](#9-rozbieżności-man-page--kod--debian--pyncat)

---

## 1. Half-close: kto zamyka co i kiedy

Półzamknięcie (half-close) to sytuacja, w której jeden kierunek połączenia
TCP jest zamknięty, a drugi działa. W netcacie **nie ma na to osobnej
zmiennej** — stanem jest zawartość tablicy `pfd[4]`, a kierunek zamyka się
przez `pfd[i].fd = -1`.

Kluczowe: `POLL_NETOUT` i `POLL_NETIN` to **dwa slots na tym samym
deskryptorze**. Wycofanie jednego nie rusza drugiego.

| zdarzenie | slot, który umiera | czy leci FIN do peera? | linie |
|---|---|---|---|
| EOF na stdin, **bez `-N`** | `POLL_NETOUT` | **NIE** | 1291–1296 |
| EOF na stdin, **z `-N`** | `POLL_NETOUT` | **TAK** (`shutdown(SHUT_WR)`) | 1293–1294 |
| EOF z sieci (`read` → 0) | `POLL_NETIN` | — (`shutdown(SHUT_RD)` lokalnie) | 1247–1251 |
| `POLLHUP` na `POLL_NETOUT` | `POLL_NETOUT` | TAK, jeśli `-N` | 1176–1181 |
| `POLLHUP` na stdout | `POLL_STDOUT` | nie dotyczy | 1182–1183 |
| `POLL_NETOUT` martwy | `POLL_STDIN` (kaskada) | nie | 1184–1185 |
| `POLL_STDOUT` martwy | `POLL_NETIN` (kaskada) + `shutdown(SHUT_RD)` | nie | 1186–1192 |
| `POLL_NETIN` martwy i bufor pusty | `POLL_STDOUT` (kaskada) | nie | 1297–1300 |
| limit `-W` osiągnięty | `POLL_NETIN` **i** `POLL_STDIN` | nie | 1252–1258 |
| `POLLERR` / `POLLNVAL` na dowolnym | ten slot | nie | 1161–1165 |

### Dlaczego brak `-N` to nie błąd

Bez `-N` netcat po EOF na stdin **tylko przestaje pisać** do socketu. Druga
strona nigdy nie dowiaduje się, że nadawanie się skończyło, więc nie zamyka
połączenia, więc my nie dostajemy EOF-u z sieci, więc nie spełniamy warunku
wyjścia. Program wisi — i to jest poprawne zachowanie, bo netcat nie wie,
czy użytkownik nie zechce jeszcze poczytać odpowiedzi.

Pomiar (klient wysyła 4 bajty, serwer milczy):

| flaga | nc.openbsd 1.226 | pyncat |
|---|---|---|
| (brak) | 7,701 s — zabity przez `timeout` | 7,005 s — zabity |
| `-N` | **0,008 s** | **0,051 s** |

Dlatego przykład „DATA TRANSFER" w `nc(1)` (linia 458) używa `nc -N`.
Bez tego transfer pliku nigdy się nie kończy.

Pełny cykl z `-N`:

```
klient: EOF na stdin
     -> shutdown(SHUT_WR)              [1293]
serwer: read() -> 0 (EOF na netin)
     -> POLL_NETIN.fd = -1             [1248-1250]
     -> bufor pusty => POLL_STDOUT.fd = -1   [1297-1300]
     -> warunek 3 (lflag && netin==-1) => return    [1143-1147]
     -> close(connfd)                  [1646]
klient: read() -> 0 (EOF na netin)
     -> POLL_NETIN.fd = -1
     -> warunek 1 (stdin==-1 && netin==-1 && bufory puste) => return  [1137]
```

Testy: `tests/test_relay.py::TestHalfClose`, `tests/test_e2e.py::TestHalfClose`.

### Co mogą zrobić `-q 0` i `-N` zamiennie

`-q 0` też kończy klienta po EOF na stdin, ale **innym mechanizmem**: nie
wysyła `shutdown()`, tylko wychodzi, a FIN leci przy `close()` z jądra.
Różnica praktyczna: `-q 0` nie daje gwarancji, że bufor wyjściowy został
wysłany, i gubi dane, które przyszły w międzyczasie. `-N` jest właściwym
narzędziem; `-q 0` to młotek.

---

## 2. Propagacja EOF — tablica przejść

Stan to czwórka `(STDIN, NETOUT, NETIN, STDOUT)`, gdzie `+` = żywy,
`−` = wycofany. Start: `(+,+,+,+)`.

| stan | co się stało | następny stan | efekt |
|---|---|---|---|
| `(+,+,+,+)` | EOF na stdin, bez `-N` | `(−,−,+,+)` | nadal czytamy z sieci |
| `(+,+,+,+)` | EOF na stdin, z `-N` | `(−,−,+,+)` + FIN | peer zamknie, przejdziemy dalej |
| `(−,−,+,+)` | EOF z sieci | `(−,−,−,−)` | **koniec** (warunek 1) |
| `(+,+,+,+)` | EOF z sieci | `(+,+,−,−)` | bez `-l`: **wisimy** na stdin |
| `(+,+,−,−)` | — | — | warunek 3 kończy to tylko z `-l` |
| `(+,+,+,+)` | stdout zamknięty (`\| head`) | `(−,−,−,−)` | kaskada 1186→1184, **koniec** |
| `(+,+,+,+)` | limit `-W` | `(−,−,−,+)` → `(−,−,−,−)` | **koniec** |
| `(+,+,+,+)` | `-d` od startu | `(−,+,+,+)` | jednokierunkowy odbiór |
| `(−,+,+,+)` | EOF z sieci | `(−,−,−,−)` | **koniec** — `-d` wystarczy bez `-N` |

Dwa wnioski, które łatwo przegapić:

* **`(+,+,−,−)` to stan trwały bez `-l`.** Klient, któremu serwer zamknął
  połączenie, ale który ma otwarty stdin, **nie wychodzi**. To zachowanie
  oryginału, zweryfikowane. Test: `test_eof_z_sieci_nie_konczy_gdy_stdin_otwarty`.
* **`-d` zachowuje się jak `-N` dla zakończenia.** Z `-d` stdin jest martwy
  od startu, więc EOF z sieci od razu domyka warunek 1. Dlatego
  `pyncat -d -w 2 host port` to dobry sposób na „odbierz i wyjdź".

---

## 3. Kiedy pętla relay się kończy

Warunki sprawdzane **na początku** każdej iteracji, w tej kolejności
[netcat.c:1136-1147]:

| nr | warunek | po co istnieje |
|---|---|---|
| 1 | `STDIN==− && NETIN==− && oba bufory puste` | oba wejścia martwe, nie ma skąd wziąć danych |
| 2 | `NETOUT==− && STDOUT==−` | oba wyjścia martwe, nie ma gdzie oddać danych |
| 3 | `lflag && NETIN==− && oba bufory puste` | serwer nie ma po co czekać na swój stdin po rozłączeniu klienta |
| 4 | `poll()` zwróciło 0 | timeout `-w` albo wygaśnięcie `-q` |

Warunek 3 jest tym, bez którego `nc -l` wisiałby po każdym rozłączeniu.
Zwróć uwagę, że wymaga **pustych buforów** — dane w locie nie mogą przepaść.
Test: `test_lflag_oddaje_zbuforowane_dane_przed_wyjsciem`.

Warunek 4 obsługuje dwie różne rzeczy jednym mechanizmem, bo timeout `poll()`
to `min(-w, pozostały czas -q)`.

### Pułapka `POLLHUP` razem z `POLLIN`

[netcat.c:1172-1181] Wejście jest wycofywane **tylko** gdy `POLLHUP`
występuje **bez** `POLLIN`:

```
POLLHUP          -> wycofaj slot (peer zamknął, nic nie zostało)
POLLHUP | POLLIN -> NIE wycofuj (w buforze jądra są jeszcze dane)
```

Naiwna implementacja reagująca na sam `POLLHUP` gubi ogon strumienia —
i to jest błąd, który pojawia się tylko przy szybkich nadawcach, więc łatwo
go przepuścić. Test: `test_lflag_oddaje_zbuforowane_dane_przed_wyjsciem`
(peer wysyła dane i natychmiast zamyka, więc jądro zgłasza oba zdarzenia).

---

## 4. Kody wyjścia

`ret` startuje z **1** [netcat.c:168].

| sytuacja | kod | gdzie ustawiane |
|---|---|---|
| tryb `-l` (nasłuch), zawsze | **0** | 562, od razu |
| klient, co najmniej jeden port połączony | **0** | 701 |
| klient, żaden port nie połączony | **1** | nigdy nie zerowane |
| klient UDP, `udptest()` zwrócił −1 na **ostatnim** porcie | **1** | 709 |
| błąd użycia (`usage`) | 1 | 1885 |
| błędna wartość flagi / zła kombinacja | 1 | `errx` |
| `-h` | 0 | 216–218 |
| przerwanie z klawiatury (`SIGINT`) | 130 | konwencja powłoki |

### Kwirk „raz 0, już 0"

Nieudane połączenie robi `continue` **bez dotknięcia `ret`**
[netcat.c:701-702]. Stąd:

> **Wystarczy JEDEN otwarty port w zakresie, żeby `pyncat -z` skończył się
> z kodem 0** — nawet jeśli wszystkie pozostałe są zamknięte.

Pomiar (`nc -z -w 2 127.0.0.1 …`) i to samo dla `pyncat`:

| przypadek | nc | pyncat |
|---|---|---|
| port otwarty | 0 | 0 |
| port zamknięty | 1 | 1 |
| zakres: 1 otwarty + 2 zamknięte | **0** | **0** |
| zakres: wszystkie zamknięte | 1 | 1 |
| klient bez `-z`, port zamknięty | 1 | 1 |

**Konsekwencja dla skryptów:** `nc -z host 1-1000 && echo "wszystko otwarte"`
jest błędem. Kod wyjścia 0 znaczy „co najmniej jeden", nie „wszystkie".
Żeby sprawdzić jeden port, podaj jeden port.

Jedyny wyjątek od „raz 0, już 0" to UDP: `udptest() == -1` ustawia
`ret = 1` i pętla leci dalej, więc wynik odzwierciedla **ostatni** testowany
port, nie najlepszy.

Testy: `tests/test_e2e.py::TestKodyWyjscia`,
`tests/test_interop.py::test_zgodnosc_kodow_wyjscia_zakres_mieszany`.

---

## 5. Komunikaty: pyncat ↔ nc

`pyncat` pisze po polsku, zgodnie z konwencją tego projektu. **`stdout` jest
bajt w bajt zgodny z netcatem** (to kanał danych); różni się wyłącznie
`stderr`, czyli kanał dla człowieka.

| sytuacja | nc.openbsd 1.226 | pyncat |
|---|---|---|
| udane połączenie (`-v`/`-z`) | `Connection to 127.0.0.1 34829 port [tcp/*] succeeded!` | `Połączenie z 127.0.0.1 port 34829 [tcp/*] — udane` |
| nieudane połączenie (`-v`) | `nc: connect to 127.0.0.1 port 34828 (tcp) failed: Connection refused` | `pyncat: nie udało się połączyć z 127.0.0.1 port 34828 (tcp): Connection refused` |
| nasłuch (`-v`) | `Listening on 0.0.0.0 1234` | `Nasłuchuję na 0.0.0.0 1234` |
| bind UDP (`-v`) | `Bound on 0.0.0.0 1234` | `Przypisano gniazdo do 0.0.0.0 1234` |
| połączenie przyjęte (`-v`) | `Connection received on 127.0.0.1 55312` | `Połączenie przychodzące od 127.0.0.1 55312` |
| zły port | `nc: port number invalid: 80,443` | `pyncat: service "80,443" unknown` |
| port poza zakresem | `nc: port number too large: 99999` | `pyncat: port number too large: 99999` |
| nieznana usługa | *(Debian: `port number invalid`)* | `pyncat: service "xyz" unknown` |
| `-s` z `-l` | `nc: cannot use -s and -l` | `pyncat: nie można użyć -s razem z -l` |
| `-k` bez `-l` | `nc: must use -l with -k` | `pyncat: -k wymaga -l` |

`[tcp/*]` — gwiazdka, gdy `getservbyport()` nie zna portu albo podano `-n`.

Dwie uwagi o rozbieżnościach, które **nie** są kwestią języka:

* Przy nieznanej nazwie usługi upstream mówi `service "%s" unknown`
  [netcat.c:1470], a Debian 1.226 `port number invalid`. `pyncat` trzyma się
  **upstreamu**, bo to jest kanoniczne źródło tego projektu.
* `pyncat` zachowuje angielskie łańcuchy `strtonum(3)` (`too large`,
  `too small`, `invalid`), bo są wklejane w komunikat przez ten sam mechanizm
  co w C i stanowią część odwzorowanej semantyki.

---

## 6. TCP kontra UDP

UDP nie ma połączeń, więc netcat musi udawać. Robi to na **trzy różne
sposoby** w zależności od flag [netcat.c:586-648].

| | TCP | UDP bez `-k` | UDP z `-k` |
|---|---|---|---|
| `listen()` | `listen(s, 1)` | **nie** | **nie** |
| jak poznaje peera | `accept()` → nowy fd | `recvfrom(MSG_PEEK)` + `connect()` | nie poznaje |
| deskryptor dla relay | nowy (`connfd`) | ten sam, co nasłuchujący | ten sam |
| nieblokujący | tak (`accept4(SOCK_NONBLOCK)`) | nie | nie |
| od ilu nadawców odbiera | 1 (po `accept`) | **1** (po `connect`) | **wielu** |
| czy można odpisać | tak | tak | **nie** (`ENOTCONN`) |
| pierwszy datagram | — | **nie przepada** (`MSG_PEEK`) | nie przepada |
| linie | 626–648 | 594–625 | 586–593 |

### Dlaczego `MSG_PEEK`

`recvfrom(..., MSG_PEEK, ...)` czyta adres nadawcy, **zostawiając datagram
w buforze**. Potem `connect()` przypina socket do tego nadawcy, a pętla relay
czyta datagram normalnie. Bez `MSG_PEEK` pierwszy datagram zostałby zjedzony
przez `recvfrom` i przepadł.

Pomiar: `nc -u -l` odebrał `U1` od pierwszego nadawcy; `U2` wysłane z innego
portu źródłowego **nie dotarło** — potwierdza, że `connect()` przypina peera.

### `udptest()` — dlaczego cztery zapisy

[netcat.c:1542] `for (i = 0; i <= 3; i++) write(s, "X", 1);`

UDP nie ma handshake'u, więc **pierwszy** `send()` na zamknięty port się
udaje — jądro nic jeszcze nie wie. Dopiero gdy wróci ICMP port unreachable,
jądro zapamiętuje błąd w `SO_ERROR` i **następny** `send()` zwraca
`ECONNREFUSED`. Jeden zapis nie wykryłby zamkniętego portu.

| warunek | wynik `udptest()` | `ret` |
|---|---|---|
| ostatni `send()` udany | 1 | 0 |
| ostatni `send()` błąd | −1 | **1** |
| brak `-z` i stdin nie jest terminalem | 0 („nie testowano") | 0 |

Trzy ograniczenia, które ma też oryginał:

1. **To wyścig.** Jeśli ICMP nie wróci przed czwartym zapisem (większe RTT,
   firewall tnący ICMP, pełna tablica stanów PF — o czym mówi komentarz
   w [netcat.c:1531]), port wyjdzie jako otwarty. Na loopbacku niezawodne,
   przez sieć nie.
2. **Wysyła 4 bajty `X` do celu**, mimo nazwy „Zero-I/O mode". Dla `-u -z`
   nazwa flagi kłamie.
3. Zwraca 0 („brak informacji") gdy nie ma `-z` i stdin nie jest terminalem
   — wtedy `connection_info` się nie wywołuje, ale `ret` zostaje 0.

---

## 7. Dopuszczalne kombinacje flag

[netcat.c:452-459]. Walidacja następuje **po** ustaleniu argumentów
pozycyjnych, więc `pyncat -l -p 1234` (bez pozycyjnego portu) daje błąd
użycia, a nie „nie można użyć -p razem z -l".

| kombinacja | wynik | komunikat |
|---|---|---|
| `-l` + `-s` | **błąd** | `nie można użyć -s razem z -l` |
| `-l` + `-p` | **błąd** | `nie można użyć -p razem z -l` |
| `-l` + `-z` | **błąd** | `nie można użyć -z razem z -l` |
| `-k` bez `-l` | **błąd** | `-k wymaga -l` |
| `-l` + `-k` | OK | — |
| `-l` + `-u` | OK | inna semantyka, §6 |
| `-l` + `-w` | OK | `-w` **działa** jako idle timeout (§9) |
| `-l` + `-N` | OK | serwer też może zrobić half-close |
| `-z` + `-u` | OK | `udptest()`, wysyła 4 bajty |
| `-d` + `-N` | OK | `-N` bez skutku, stdin martwy od startu |
| `-4` + `-6` | OK | wygrywa **ostatnia** (getopt nie scala) |

Składnia pozycyjna [netcat.c:356-364]:

| argumenty | interpretacja |
|---|---|
| `host port` | klient |
| `port` razem z `-l` | serwer na wildcard |
| `host port` razem z `-l` | serwer na konkretnym adresie |
| `host` (bez `-l`, bez portu) | **błąd użycia** — nie ma portu domyślnego |
| nic | **błąd użycia** |
| trzy i więcej | **błąd użycia** |

---

## 8. Parsowanie portów

[netcat.c:1455-1532]

| wejście | wynik | uwaga |
|---|---|---|
| `80` | `[80]` | |
| `ssh` | `[22]` | `getservbyname` |
| `20-25` | `[20,21,22,23,24,25]` | |
| `25-20` | `[20,…,25]` | **swap** `lo`/`hi` [1495-1499] |
| `80-80` | `[80]` | |
| `80,443` | **błąd** | przecinki NIE są obsługiwane |
| `20-25-30` | **błąd** | dzielenie na **pierwszym** minusie → górna granica `"25-30"` |
| `http-alt` | `[8080]` lub błąd | zakres wymaga **cyfry** na pozycji 1, więc nazwa z minusem nie jest zakresem [1487] |
| `0` | **błąd** | `port number too small` |
| `99999` | **błąd** | `port number too large`, **nie** próbuje `getservbyname` [1468] |
| `1-65535` | 65 535 portów | |
| `-r` | permutacja | Fisher-Yates na CSPRNG |

To nie nmap: nie ma list po przecinku, nie ma `-p -` (wszystkie porty),
nie ma `--top-ports`. Jeden port albo jeden zakres.

Testy: `tests/test_ports.py` (30 przypadków).

---

## 9. Rozbieżności: man page ↔ kod ↔ Debian ↔ pyncat

Cztery miejsca, w których dokumentacja, kod upstream, pakiet Debiana
i `pyncat` **nie zgadzają się ze sobą**. Wszystkie zmierzone, nie wyczytane.

### 9.1 `-i` — opóźnienie między portami

| źródło | co twierdzi / robi |
|---|---|
| `nc(1)` linie 158–162 | „Sleep for *interval* seconds between lines of text sent and received. **Also causes a delay time between connections to multiple ports.**" |
| `nc -h` | „Delay interval for lines sent, ports scanned" |
| kod upstream | `iflag` występuje **wyłącznie** w `sleep(iflag)` w pętli relay [1152-1153]. **Zero** opóźnienia między portami. |
| Debian 1.226 | **mierzalny no-op** — żadnego opóźnienia w ogóle |
| **pyncat** | implementuje **oba**: sleep w pętli relay **i** przerwę między portami |

Pomiar na Debianie 1.226:

| scenariusz | bez `-i` | z `-i 1`/`-i 2` |
|---|---|---|
| `-z`, 3 zamknięte porty | 0,004 s | 0,005 s |
| 1 otwarty port, `-N`, 1 bajt | 0,006 s | 0,006 s / 0,005 s |
| 2 otwarte porty, bez `-z` | 0,008 s | 0,005 s |
| serwer `-l -i 2`, 3 porcje danych | 0,910 s | 0,910 s |

**Decyzja dla pyncat:** implementujemy to, co obiecuje dokumentacja, bo tak
brzmi specyfikacja i bo to jedyne zachowanie, które jest komukolwiek
użyteczne. Jest to **świadome odstępstwo** od kodu upstream, oznaczone
w `pyncat/app.py` i pilnowane testami
`test_i_opoznia_miedzy_portami` oraz `test_i_pyncat_opoznia_nc_nie`.

Ten drugi test padnie, jeśli Debian naprawi `-i` — i wtedy trzeba będzie
poprawić ten dokument, a nie test.

### 9.2 `-w` w trybie `-l`

| źródło | co twierdzi / robi |
|---|---|
| `nc(1)` linie 326–336 | „The `-w` flag **has no effect on the `-l` option**, i.e. nc will listen forever for a connection, with or without the `-w` flag." |
| `nc -h` | „Timeout for connects and **final net reads**" |
| kod | `timeout` jest globalny i trafia do `poll()` w `readwrite()` [1155]. Faza `accept()` go nie używa, **faza danych używa**. |
| **pyncat** | tak samo jak kod |

Pomiar: serwer `nc -l -p P -w 2` ze stdin na EOF, klient trzyma połączenie
i nic nie wysyła → **serwer wyszedł po 2,313 s**.

Zdanie z man page jest prawdziwe **tylko dla fazy nasłuchu**. `nc -h` jest
dokładniejsze niż `nc.1`.

### 9.3 `-W` — „packets"

| źródło | co twierdzi / robi |
|---|---|
| `nc(1)` linia 320 | „Terminate after receiving *recvlimit* **packets** from the network." |
| kod | `++recvcount` przy **każdym wejściu** w blok „read from network" [1252], czyli raz na iterację pętli, w której sieć była czytelna |
| **pyncat** | tak samo jak kod |

Pomiar: serwer wysyła `A`, pauza 0,4 s, `B`, pauza 0,4 s, `C`. Klient `-W 2`
odebrał dokładnie `AB` — tak samo `nc` i `pyncat`.

Dla TCP „packets" jest mylące: granice `read()` nie mają nic wspólnego
z granicami segmentów. Gdyby `ABC` przyszło w jednym `read()`, `-W 2`
przepuściłby wszystkie trzy bajty. **Właściwa nazwa to „odczyty z sieci".**

Dodatkowo: licznik jest **globalny** i nie jest zerowany między połączeniami
w trybie `-k`. Przy `-l -k -W 2` pierwszy klient zużywa cały limit.
Odwzorowane dosłownie przez `RelayState` w `pyncat` (zobacz komentarz tam).

### 9.4 `-q` — flaga, której nie ma w upstream

`grep -n qflag netcat.c` → **brak trafień**. `-q` pochodzi z netcata Hobbita
/ FreeBSD i w Debianie jest dodane patchem. Nie udało się pobrać tego patcha
(`sources.debian.org` i `salsa.debian.org` są zablokowane przez network
policy kontenera), więc semantykę ustalono **empirycznie na binarium** — co
jest mocniejszym dowodem niż lektura patcha.

Scenariusz: klient wysyła `X` i dostaje EOF na stdin; serwer odpowiada
`DELAYED` **po 1,5 s**.

| `-q` | nc: odebrane | nc: czas | pyncat: odebrane | pyncat: czas |
|---|---|---|---|---|
| (brak) | `DELAYED` | 8,602 s | `POZNO` | 11,511 s |
| `-q 0` | *(nic)* | **0,007 s** | *(nic)* | **0,068 s** |
| `-q 2` | — | **2,007 s** | `POZNO` | **2,065 s** |
| `-q -1` | jak brak flagi | — | jak brak flagi | — |

Semantyka:

| `-q` | zachowanie po EOF na stdin |
|---|---|
| nie podano | czekaj bez końca (≡ `-q -1`) |
| `0` | wyjdź **natychmiast**; dane w locie przepadają |
| `N > 0` | czekaj `N` sekund, potem wyjdź bezwarunkowo |
| `N < 0` | czekaj bez końca |

`pyncat` implementuje to przez deadline (`time.monotonic()`), a timeout
`poll()` to `min(-w, pozostały czas -q)`. Umiejscowienie warunku
(razem z pozostałymi warunkami wyjścia, na początku iteracji) wynika
z pomiaru: `-q 2` kończy się po 2,0 s, nie po 2 s + jednym pełnym `-w`.

### 9.5 `-l -p PORT`

| źródło | zachowanie |
|---|---|
| kod upstream [454-455] | **błąd**: `cannot use -p and -l` |
| Debian 1.226 | **przyjmuje** — `nc -l -p 1234` to w Debianie powszechny idiom |
| **pyncat** | **błąd**, jak upstream |

To najczęstsza praktyczna różnica między `pyncat` i `nc` na Linuksie, bo
połowa tutoriali w internecie używa debianowej składni. Właściwa składnia
to `pyncat -l 1234`.

Test: `tests/test_interop.py::test_l_z_p_pyncat_odrzuca_debian_przyjmuje`.

### 9.6 `SO_REUSEPORT` kontra `SO_REUSEADDR`

| źródło | zachowanie |
|---|---|
| kod upstream [1072] | `SO_REUSEPORT` |
| **pyncat** | `SO_REUSEADDR` |

Na BSD `SO_REUSEPORT` znaczy „pozwól przebindować port w `TIME_WAIT`",
i netcat tego potrzebuje, bo w trybie `-k` **zamyka i tworzy listener od
nowa** przy każdym połączeniu [573-577]. Na Linuksie `SO_REUSEPORT` znaczy
co innego: pozwala **wielu procesom** bindować ten sam port i rozdziela
między nie połączenia. Dla narzędzia edukacyjnego to pułapka — dwa
uruchomione `pyncaty` cicho dzieliłyby ruch. `SO_REUSEADDR` robi na Linuksie
to, czego netcat tu chce.

Świadome odstępstwo, oznaczone w `pyncat/endpoints.py`.
