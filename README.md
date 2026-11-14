# pyncat-basic

**OpenBSD netcat przepisany z C na Pythona** — projekt edukacyjny
z referencyjną implementacją w C11 do porównania.

Sercem netcata nie jest parsowanie flag, a **dwukierunkowa pętla
przekazywania danych** — w oryginale `readwrite()`. Multipleksuje stdin
i socket, obsługuje half-close i propaguje EOF. Na tym skupiony jest cały
wysiłek tego repozytorium.

```
$ python3 -m pyncat -z -v -w 3 -n 127.0.0.1 39498-39501
pyncat: nie udało się połączyć z 127.0.0.1 port 39498 (tcp): Connection refused
pyncat: nie udało się połączyć z 127.0.0.1 port 39499 (tcp): Connection refused
Połączenie z 127.0.0.1 port 39500 [tcp/*] — udane
pyncat: nie udało się połączyć z 127.0.0.1 port 39501 (tcp): Connection refused
$ echo $?
0
```

Tak, `0` — mimo trzech zamkniętych portów. To nie błąd, to odwzorowany kwirk
oryginału: **wystarczy jeden otwarty port w zakresie.** Dlaczego tak jest
i co z tego wynika dla skryptów: [`docs/tablice-decyzyjne.md`](docs/tablice-decyzyjne.md#4-kody-wyjścia).

---

## ⚠️ Zanim uruchomisz

**To narzędzie służy do nauki. Łączyć się i nasłuchiwać wolno wyłącznie na
własnej infrastrukturze albo za pisemną zgodą właściciela.**

Nawiązywanie połączeń z obcą infrastrukturą bez zgody może naruszać
art. 267 k.k. (nieuprawniony dostęp do informacji) oraz regulaminy
dostawców. Do nauki użyj własnego homelabu, loopbacku albo celowo podatnych
maszyn w izolowanej sieci.

Do pracy używaj `nc` z pakietu `netcat-openbsd` — jest szybszy, przetestowany
na milionach hostów i ma wszystko, czego tu nie ma.

### Czego tu nie ma i nie będzie: wykonywania poleceń

**Nie ma flagi `-e`** („uruchom program i podłącz go do socketu") ani niczego
równoważnego. To nie przeoczenie — OpenBSD **świadomie** nie przepisało tej
flagi z netcata 1.10 Hobbita, i to jest granica tego projektu. Narzędzie
przekazuje bajty między stdin/stdout a socketem; nie uruchamia procesów.

Pilnują tego dwa testy: `test_nie_ma_zadnej_flagi_uruchamiajacej_program`
grepuje moduły Pythona pod kątem `subprocess`/`os.exec`/`os.fork`/`pty`,
a `test_zadna_implementacja_nie_uruchamia_procesow` sprawdza przez `nm -u`,
że binarka C nie importuje `exec*`/`system`/`popen`/`fork`.

Pełne omówienie — co robiło `nc -e`, dlaczego było prymitywnym backdoorem
i czym to zastąpić: [`docs/netcat-architecture.md`](docs/netcat-architecture.md) §7.

---

## Wypróbuj bez instalacji

| Gdzie | Co dostajesz | Czego nie dostajesz |
|---|---|---|
| **[Demo w przeglądarce](https://damiantomczyszyn.github.io/pyncat-basic/)** (wymaga włączenia Pages — zobacz niżej) | Maszynę stanów `readwrite()` krok po kroku — **prawdziwy `pyncat/relay.py`** uruchomiony przez Pyodide na atrapach deskryptorów. Dziewięć scenariuszy: half-close z `-N` i bez, EOF z sieci, limit `-W`, timer `-q`, backpressure. Plus parsowanie portów i tablica kodów wyjścia. | Transferu danych — przeglądarka **nie ma socketów** TCP/UDP. Pyodide też nie. Szczegóły i uzasadnienie: [`docs/demo-i-hosting.md`](docs/demo-i-hosting.md). |
| **[GitHub Actions](https://github.com/damiantomczyszyn/pyncat-basic/actions)** | Realny test sieciowy: nginx, redis i echo UDP w kontenerach, odpytywane po adresie na mostku `docker0` — przez prawdziwy ARP, bridge i routing. | Interaktywności — odpala się na push. |
| **Lokalnie** | Wszystko. Zero zależności poza biblioteką standardową. | — |

Dlaczego demo nie przesyła danych: netcat to narzędzie sieciowe, a przeglądarka
ma tylko `fetch`, WebSocket i WebRTC — wszystko z narzuconym protokołem.
`socket.socket(AF_INET, SOCK_STREAM)` w Pyodide **nie działa**, bo WASM nie ma
backendu socketowego. Gorzej: `connect()` na zamknięty port tam *udaje sukces*
(zmierzone — [`docs/demo-i-hosting.md`](docs/demo-i-hosting.md) §5), więc demo
„na prawdziwych socketach" pokazywałoby wyniki, które są nieprawdą. Zamiast
tego demo pokazuje tę część, która jest tu najciekawsza i **nie wymaga
socketów** — maszynę stanów.

> **Hosting wymaga jednego kliknięcia po Twojej stronie.** W pierwszym
> przebiegu CI job `pages` oblał się komunikatem *„Get Pages site failed.
> Please verify that the repository has Pages enabled"*, bo GitHub Pages nie
> jest w tym repo włączone. Dołożyłem `enablement: true` do
> `configure-pages`, żeby workflow włączył je sam — i **to nie zadziałało**:
>
> ```
> Create Pages site failed.
> Error: Resource not accessible by integration
> ```
>
> `GITHUB_TOKEN` nie ma prawa utworzyć witryny Pages. Trzeba to zrobić
> ręcznie, raz: **Settings → Pages → Source: GitHub Actions**. Po włączeniu
> job zadziała bez żadnej zmiany w kodzie.
>
> Job ma `continue-on-error`, więc brak hostingu **nie oblewa CI** — cały
> przebieg jest zielony mimo czerwonego `pages`. To ustawienie repozytorium,
> nie regresja.

#### Czego w demo NIE zweryfikowałem — i to jest istotne

Demo można zbudować lokalnie:

```bash
python3 web/build_site.py _preview
```

i otworzyć `_preview/index.html` w przeglądarce. **Ale start wymaga
ściągnięcia Pyodide** (CPython skompilowany do WebAssembly, ~10 MB z
`cdn.jsdelivr.net`), a tego **nie udało mi się sprawdzić**: w środowisku,
w którym to powstało, proxy blokuje ten CDN (`curl` zwraca kod 000).

Co **jest** sprawdzone:

* **Logika demo** — `web/build_site.py --selftest` przepuszcza 9 scenariuszy
  przez prawdziwy `pyncat/relay.py`, a `web/test_page.mjs` wczytuje prawdziwy
  `index.html` do jsdom-a, wykonuje prawdziwe `app.js` i podstawia **Pyodide
  z pakietu npm** (czyli bez CDN-u). Oba przechodzą, także w CI.
* **Renderowanie i obsługa porażki** — stronę otwarto w prawdziwym Chromium
  z `file://`. Renderuje się poprawnie, a gdy Pyodide nie da się wczytać,
  pokazuje dokładny komunikat z listą obu prób (`./pyodide/` i CDN) zamiast
  wisieć na „pobieranie Pyodide".

Czego **nie jest** sprawdzone: że Pyodide faktycznie dociąga się z CDN-u
w przeglądarce z normalnym dostępem do internetu. Powinno — ale tego nie
widziałem i nie będę udawać, że widziałem. Jeśli start zawiedzie, strona
przyjmuje inne źródło w adresie (`?pyodide=https://…/full/`) albo lokalną
kopię zworkowaną do `_preview/pyodide/`.

### Co weryfikuje CI

Na każdy push:

- **`ruff`** na całym repo,
- **`pytest`** na Pythonie **3.10, 3.11 i 3.13** (188 testów),
- **build wersji C** pod `-Wall -Wextra -Wpedantic -Werror` z **zerową
  tolerancją na ostrzeżenia** (liczone osobno, żeby usunięcie `-Werror`
  nie przeszło niezauważone),
- **AddressSanitizer + UBSan + LeakSanitizer** — transfer 1 MiB, wszystkie
  ścieżki błędów, zakres 30 portów, pętla `-k`, UDP,
- **weryfikacja, że binarka C nie importuje `exec*`/`fork`/`system`**,
- **realny test sieciowy** w kontenerach — build **pada**, gdy którykolwiek
  wynik różni się od oczekiwanego (skrypt kończy się kodem ≠ 0, nie loguje
  ostrzeżeń „do przejrzenia"),
- **test strony demo** end-to-end: prawdziwy `index.html` w jsdom-ie
  z prawdziwym Pyodide — bo bez tego „demo działa" znaczyłoby tylko
  „plik się parsuje",
- **benchmark** C vs Python jako artefakt (nie jako bramka — na współdzielonym
  runnerze wariancja CPU oblewałaby build bez sensu).

---

## Instalacja

Python 3.10+, **zero zależności** poza biblioteką standardową.

```bash
git clone https://github.com/damiantomczyszyn/pyncat-basic.git
cd pyncat-basic
```

```bash
python3 -m pyncat --help
```

```bash
pip install -e .
pyncat --help
```

### Wersja w C

```bash
make -C c_reference          # pyncat_ref       (-O2, -Werror)
make -C c_reference debug    # pyncat_ref_debug (-O0 -g3)
make -C c_reference asan     # pyncat_ref_asan  (ASan + UBSan)
```

---

## Użycie

Flagi nazwane jak w `nc(1)`, więc polecenia przekładają się 1:1.

```bash
# klient TCP
python3 -m pyncat example.com 80

# transfer pliku — UWAGA: -N jest KONIECZNE, inaczej nigdy się nie skończy
python3 -m pyncat -N 192.168.1.10 1234 < plik.bin
python3 -m pyncat -l 1234 > plik.bin

# rozmowa z serwerem HTTP
printf 'GET / HTTP/1.0\r\n\r\n' | python3 -m pyncat -N -w 5 example.com 80

# skan portów bez przesyłania danych
python3 -m pyncat -z -v -w 2 192.168.1.10 20-25

# serwer, który nie kończy po rozłączeniu klienta
python3 -m pyncat -l -k 1234

# UDP
printf 'ping' | python3 -m pyncat -u -w 3 192.168.1.10 9999
python3 -m pyncat -u -l 9999

# wymuszony adres i port źródłowy (obejście reguł typu "allow from :53")
python3 -m pyncat -s 10.0.0.5 -p 53 192.168.1.10 1234
```

### Opcje

| Opcja | Znaczenie |
|---|---|
| `-4` / `-6` | wymuszenie `AF_INET` / `AF_INET6` |
| `-d` | nie czytaj ze stdin (detach) |
| `-h` | pomoc |
| `-i SEK` | przerwa w pętli relay **oraz między portami** |
| `-k` | nie kończ po rozłączeniu klienta (wymaga `-l`) |
| `-l` | nasłuchuj zamiast łączyć się |
| `-N` | `shutdown(SHUT_WR)` po EOF na stdin |
| `-n` | bez DNS (`AI_NUMERICHOST`) |
| `-p PORT` | port źródłowy |
| `-q SEK` | po EOF na stdin: `0` = wyjdź od razu, `>0` = czekaj, `<0` = bez końca |
| `-r` | losowa kolejność portów w zakresie |
| `-s ADRES` | adres źródłowy |
| `-u` | UDP zamiast TCP |
| `-v` | więcej komunikatów |
| `-W N` | zakończ po `N` odczytach z sieci |
| `-w SEK` | timeout na connect **oraz** idle timeout w pętli relay |
| `-z` | tryb zero-I/O: sprawdź port, nie przesyłaj danych |

**Porty:** pojedynczy (`80`), nazwa usługi (`ssh`) albo jeden zakres
(`20-25`, odwrotny `25-20` jest zamieniany). **Listy po przecinku nie są
obsługiwane** — to nie nmap, i oryginał też ich nie ma.

### Trzy pułapki, na które warto uważać

1. **Bez `-N` transfer pliku nigdy się nie kończy.** Po EOF na stdin netcat
   tylko przestaje pisać do socketu — *nie wysyła FIN*. Druga strona nie wie,
   że to koniec. Zmierzone: bez `-N` klient wisiał do zabicia (7,0 s), z `-N`
   wyszedł po 0,05 s.
2. **`-l -p PORT` to błąd**, mimo że połowa tutoriali w internecie tak pisze.
   Debianowy `nc` to przyjmuje, upstream (i my) nie. Poprawnie: `pyncat -l PORT`.
3. **Kod wyjścia 0 nie znaczy „wszystkie porty otwarte"**, a „co najmniej
   jeden". Żeby sprawdzić jeden port — podaj jeden port.

---

## Struktura projektu

Każdy moduł odpowiada konkretnej części `netcat.c`
(`$OpenBSD: netcat.c,v 1.241`, 1900 linii). Numery linii w komentarzach
odnoszą się do tej rewizji; jak je sprawdzić, opisuje
[`docs/netcat-architecture.md`](docs/netcat-architecture.md) §2.4.

```
pyncat/
  constants.py   stałe i stan globalny                 <- netcat.c:62-117
  cli.py         getopt, argumenty, walidacja, pomoc    <- 176-347, 356-364, 425-456
  ports.py       strtoport(), build_ports()             <- 1455-1532
  netio.py       fillbuf(), drainbuf()                  <- 1305-1367
  relay.py       readwrite() — SERCE PROGRAMU           <- 1104-1303
  endpoints.py   remote_connect(), timeout_connect(),
                 local_listen(), udptest()              <- 930-1102, 1534-1550
  report.py      connection_info(), report_sock(), err  <- 1552-1581, 1807-1835
  app.py         ścieżka wykonania main()               <- 152-174, 561-730

c_reference/
  pyncat_ref.c   ten sam algorytm w C11, od zera (nie kopia netcat.c)
  Makefile       all | debug | asan | clean

docs/
  netcat-architecture.md  jak zbudowany jest netcat.c, z numerami linii
  c-vs-python.md          porównanie: zmierzona wydajność, konstrukcje, wnioski
  tablice-decyzyjne.md    half-close, EOF, kody wyjścia, TCP vs UDP, rozbieżności
  demo-i-hosting.md       co da się pokazać w przeglądarce i dlaczego tyle

ci/
  bench.py         benchmark C vs Python (metodologia w docstringu)
  network_check.py realny test sieciowy w kontenerach
  asan_check.sh    przebieg pod ASan/UBSan/LeakSanitizer

tests/            188 testów (pytest)
web/              demo: maszyna stanów readwrite() przez Pyodide
```

---

## Porównanie z C

Repo zawiera **drugą implementację tego samego algorytmu w C11**
(`c_reference/pyncat_ref.c`) — napisaną od zera, nie skopiowaną z netcata.
Te same nazwy funkcji, te same stałe, ta sama semantyka half-close, te same
kody wyjścia. Różni się tylko językiem.

```bash
make -C c_reference
./c_reference/pyncat_ref -z -v -w 2 -n 127.0.0.1 20-25
python3 -m pyncat        -z -v -w 2 -n 127.0.0.1 20-25
```

Pomiary (Intel Xeon @ 2,10 GHz, CPython 3.13.18, loopback; metodologia
i odtworzenie: [`docs/c-vs-python.md`](docs/c-vs-python.md) §1):

| Metryka | Python | C | `nc` 1.226 | Stosunek |
|---|---|---|---|---|
| Przepustowość (256 MiB, `BUFSIZE=16384`) | 616 MiB/s | 1554 MiB/s | 1554 MiB/s | **2,5×** |
| Narzut startu (`-h`) | 61,8 ms | 2,2 ms | 1,4 ms | **28×** |
| Skan `-z` (2000 portów) | 63,8 µs/port | 8,3 µs/port | 8,1 µs/port | **7,7×** |
| RSS (`VmHWM`) | 16 152 KiB | 1 648 KiB | 1 824 KiB | **9,8×** |
| Linie kodu (bez komentarzy) | **855** | **859** | — | **1,0×** |

Dwa wyniki, które były dla mnie najciekawsze:

**Liczba syscalli jest identyczna.** `strace -c` na transferze 256 MiB:
`poll` 32 771 w obu, `write` 16 384 w obu (= 256 MiB / 16 KiB, co do jednego).
To dowód, że algorytm został odwzorowany wiernie — i jednocześnie dowód, że
pętla relay **nie jest syscall-bound**, bo identyczna liczba syscalli daje
2,5× różnicy w czasie. Cała różnica to stały koszt iteracji interpretera,
około **5 µs**. Dlatego stosunek C/Python spada z **6,8×** przy buforze 512 B
do **2,0×** przy 64 KiB: przy dużym buforze dominuje kopiowanie bajtów przez
jądro, które kosztuje tyle samo w obu językach.

**Kod jest tej samej długości** — 855 wobec 859 linii. Spodziewałem się, że
Python będzie wyraźnie krótszy; nie jest, bo ten program składa się z maszyny
stanów i obsługi syscalli, a nie z przetwarzania struktur danych. W dwóch
miejscach Python potrzebuje **więcej** kodu niż C: emulacji tablicy `pollfd`
(bo `select.poll` to mapa `fd → maska`, więc nie da się zarejestrować tego
samego deskryptora dwa razy z różnymi maskami — a netcat właśnie na tym stoi)
i ręcznej obsługi `getopt`.

---

## Testy

```bash
pip install pytest
make -C c_reference          # testy interop i parzystości wymagają binarki
python3 -m pytest tests/ -v
```

**188 testów.** To nie smoke testy — klient i serwer są uruchamiane przeciwko
sobie jako procesy i sprawdzamy **faktycznie przesłane bajty** (`sha256`), nie
długości.

| Plik | Testów | Co pokrywa |
|---|---|---|
| `test_relay.py` | 24 | maszyna stanów `readwrite()` odpalona **w wątku na `socketpair()`** — sprawdza rzeczy niewidoczne z zewnątrz: czy poleciał FIN, czy pętla jeszcze żyje, po ilu odczytach zamknęła odbiór |
| `test_e2e.py` | 34 | prawdziwe procesy na loopbacku: 1 MiB danych binarnych w obie strony, same zera, same `0xFF`, transfer zerobajtowy, zakresy, `-z`, timeouty, UDP, `-k`, `-4`/`-6`, `-s`/`-p` |
| `test_ports.py` | 32 | parsowanie portów z przypadkami błędnymi |
| `test_cli.py` | 38 | opcje, walidacja, komunikaty pominiętych flag, audyt „brak uruchamiania procesów" |
| `test_parity.py` | 26 | **Python i C muszą zachowywać się identycznie** — ten sam komunikat i ten sam kod wyjścia dla 16 przypadków błędnych, ta sama liczba `write()` przy tym samym transferze |
| `test_interop.py` | 19 | pyncat ↔ prawdziwy `nc` ↔ wersja C, w obie strony, 1 MiB binarnie |
| `test_netio.py` | 15 | `fillbuf`/`drainbuf`, w tym **zapis częściowy** i limit `BUFSIZE` |

`test_parity.py` istnieje, bo pierwsza wersja wersji w C **rozchodziła się**
z Pythonem w trzech miejscach naraz (brak walidacji `-s`/`-p` z `-l`, inna
semantyka `strtoport()`, angielskie komunikaty), a żaden test interop tego nie
złapał — bo one porównują przesłane bajty, nie diagnostykę.

---

## Czego tu nie ma

Poza `-e` (wyżej) pominięte **świadomie**, z uzasadnieniem
w [`docs/c-vs-python.md`](docs/c-vs-python.md) §6:

- **TLS** (`-c -H -R -e -o -Z -K -C`) — wymaga `libtls`; `ssl` z Pythona ma
  inny model niż `tls_*(3)`, więc porównanie 1:1 przestałoby być uczciwe
- **proxy SOCKS/HTTP** (`-x -X -P`) — w netcacie siedzi w osobnym pliku
  `socks.c`, poza zakresem „jednego pliku `netcat.c`"
- **gniazda uniksowe** (`-U`), **SCTP**
- `-F` (przekazanie deskryptora przez `SCM_RIGHTS`), `-t` (telnet)
- opcje socketu `-D -T -I -O -M -m -S`, `-V` (rtable), `pledge`/`unveil`
- **Windows** — `select.poll` tam nie istnieje. `select.select` by się
  uruchomił, ale nie rozróżnia `POLLHUP` od gotowości do czytania, więc
  propagacja EOF przestałaby działać. Lepiej nie wspierać, niż wspierać błędnie.

Każda pominięta flaga jest **rozpoznawana** i daje komunikat mówiący
*dlaczego* jej nie ma:

```
$ python3 -m pyncat -c example.com 443
pyncat: TLS (-c) pominięte: wymaga libtls, a ssl z Pythona ma inny model
niż tls_*(3), więc porównanie 1:1 przestałoby być uczciwe
```

### Czego nie udało się zweryfikować

Uczciwie, bo to ogranicza wnioski:

- **IPv6** — kod obsługuje `-6` i `AF_INET6`, ale w środowisku, w którym to
  powstało, nie ma działającego loopbacku IPv6 (`AF_INET6` →
  `Address family not supported by protocol`). Test `test_6_wymusza_ipv6` jest
  **pomijany**. CI raportuje osobnym krokiem, czy IPv6 był dostępny.
- **Realna sieć poza loopbackiem** — testy lokalne chodzą po `127.0.0.1`.
  Sandbox, w którym to powstało, przechwytuje ruch wychodzący (połączenie do
  dowolnego adresu zewnętrznego „udaje się"), więc test sieciowy przez mostek
  **pierwszy raz uruchomił się w CI, nie lokalnie**. Logikę skryptu
  zweryfikowano wcześniej backendem `--backend loopback`.
- **`udptest()` przez prawdziwą sieć** — mechanizm opiera się na ICMP port
  unreachable, co jest wyścigiem i pada za firewallem tnącym ICMP. To
  ograniczenie oryginału, nie nasze; na loopbacku działa niezawodnie.
- **PyPy** — luka z §3.3 `c-vs-python.md` to czysty narzut interpretera,
  czyli idealny materiał dla JIT-a. Nie sprawdzone.
- **Dociąganie Pyodide z CDN-u w demo** — proxy w środowisku builda blokuje
  `cdn.jsdelivr.net`. Logika demo i obsługa porażki są sprawdzone (wyżej),
  samo dostarczenie ~10 MB WASM-a przez CDN — nie.
- **Publikacja na GitHub Pages** — niemożliwa bez działania właściciela repo.
  `enablement: true` zostało sprawdzone i **odrzucone** przez GitHuba
  (`Resource not accessible by integration`), więc Pages trzeba włączyć
  ręcznie w Settings. Dopóki tego nie zrobisz, job `pages` jest czerwony,
  a cały przebieg CI i tak zielony.

---

## Licencja

Kod w tym repozytorium: **MIT** (patrz [`LICENSE`](LICENSE)).

Napisany od zera. Z `netcat.c` pochodzą **algorytmy i semantyka** (model
czterech slotów `pollfd`, propagacja EOF, tablice decyzyjne kodów wyjścia,
semantyka `udptest()`), odtworzone na podstawie analizy publicznie dostępnych
źródeł i udokumentowane odwołaniami do nazw funkcji i numerów linii.
**Żaden fragment kodu źródłowego netcata nie został skopiowany do tego
repozytorium**, a sam plik `netcat.c` nie jest tu redystrybuowany.

`netcat.c` jest objęty licencją BSD 3-clause (Copyright © 2001 Eric Jackson,
© 2015 Bob Beck). Oryginalne `nc(1)` napisał *Hobbit* (netcat 1.10, 1996).

## Źródła

- `https://raw.githubusercontent.com/openbsd/src/master/usr.bin/nc/netcat.c` —
  źródło kanoniczne (rev. **1.241**, 2026-10-31, 1900 linii)
- `https://raw.githubusercontent.com/openbsd/src/master/usr.bin/nc/nc.1` —
  man page
- `https://www.openssh.com/cgi-bin/cvsweb/src/usr.bin/nc/` — historia CVS
- `netcat-openbsd` 1.226-1ubuntu2 — binarium użyte jako punkt odniesienia
  w pomiarach i testach interop
- POSIX.1-2017, `poll(2)` — semantyka `POLLERR`/`POLLHUP`/`POLLNVAL`
- PEP 475 — ponawianie syscalli przerwanych sygnałem w CPythonie
