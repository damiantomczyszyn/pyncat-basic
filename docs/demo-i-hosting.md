# Demo w przeglądarce i darmowy hosting

Stan zweryfikowany w listopadzie 2026. Dokument odpowiada na jedno pytanie:
**czy da się zobaczyć efekt tego projektu bez instalowania czegokolwiek** —
i co dokładnie da się zobaczyć uczciwie.

Krótka odpowiedź: tak, ale nie netcata. Przeglądarka nie ma socketów, więc
pokazujemy **maszynę stanów** `readwrite()` — prawdziwy kod z tego repo,
wykonywany linia po linii na symulowanym jądrze. Prawdziwego netcata autor
dostaje w Codespaces, też bez instalowania czegokolwiek.

---

## 1. Rekomendacja

**Pages na maszynę stanów, Codespaces na prawdziwe sockety, Actions jako
źródło liczb. Żadnego PaaS-a.**

Konkretnie:

| Element | Gdzie | Dlaczego tam |
|---|---|---|
| Wizualizacja `readwrite()` krok po kroku | **GitHub Pages** + Pyodide | zero serwera, zero limitu czasu, link do wysłania; maszyna stanów nie potrzebuje socketów |
| Prawdziwy `pyncat -l` i `pyncat` po loopbacku | **Codespaces** | pełny Linux z terminalem w przeglądarce; sockety działają, bo to zwykły kontener |
| Transfer 1 MiB z weryfikacją sumy kontrolnej | **GitHub Actions** | efemeryczny kontener z prawdziwą siecią; wynik wstrzykiwany na stronę przez `--ci-json` |

Czego **nie** robić, z uzasadnieniem w §6.3:

* nie stawiać listenera netcata na darmowym PaaS — darmowe tiery routują
  tylko HTTP(S), a nie dowolny port TCP;
* nie budować mostka WebSocket → TCP, żeby przeglądarka „naprawdę" gadała
  z netcatem — to otwarty relay i jednocześnie nic nie wnosi dydaktycznie
  (§6.4);
* nie pokazywać w przeglądarce niczego, co wymaga `connect()` — Pyodide
  zgłasza **fałszywy sukces** na zamkniętym porcie (§3.2). To nie jest brak
  funkcji, to kłamstwo, i wszystko, co by na nim stało, byłoby fikcją.

---

## 2. Gdzie / co dostajesz / czego nie dostajesz

| Gdzie | Co dostajesz | Czego nie dostajesz |
|---|---|---|
| **Demo w przeglądarce** (GitHub Pages) | Pętlę `readwrite()` wykonywaną krok po kroku: stan `pfd[4]`, maski `events`/`revents`, oba bufory, log wywołań systemowych, linię `relay.py` z odnośnikiem do `netcat.c`. Dziewięć scenariuszy, w tym half-close z `-N` i bez. Plus prawdziwe `build_ports()`. | Jakiegokolwiek połączenia. Nic tu nigdzie nie wysyła bajtów — przeglądarka nie ma socketów TCP/UDP. |
| **Codespaces** (`Code → Codespaces`) | Prawdziwego `pyncat` i prawdziwego `nc` w terminalu w przeglądarce. `pyncat -l -p 9000` w jednej karcie, `pyncat 127.0.0.1 9000` w drugiej. Sockety, `poll()`, `shutdown()`, wszystko. | Nic — to zwykły Linux. Limit: 120 h/mies. na koncie Free, czyli ~60 h na maszynie 2-rdzeniowej (§6.1). |
| **GitHub Actions** | Realny transfer 1 MiB z weryfikacją `sha256` i pomiary C vs Python. Minuty bez limitu na repo publicznym. | Interaktywności — odpala się na `push`, nie na kliknięcie. |
| **Lokalnie** (`git clone`) | Wszystko, plus `pytest`. | — (to jedyny wariant wymagający instalacji) |

---

## 3. Dlaczego pętli relay na prawdziwych socketach nie da się pokazać w przeglądarce

Cztery niezależne przeszkody. **Każda z osobna wystarcza**, a razem układają
się we wniosek odwrotny do intuicyjnego: atrapy są *wierniejsze* niż
„prawdziwe" deskryptory z Pyodide.

Wszystkie liczby niżej to POMIARY na **Pyodide 314.0.8 (CPython 3.14.2,
`sys.platform == "emscripten"`)**, uruchomionym pod node 22 na **tym samym
`pyodide.asm.wasm`**, który ląduje w przeglądarce. Surowe wyniki: §5.

### 3.1 Przeglądarka nie ma socketów — i to nie jest kwestia uprawnień

Przeglądarka udostępnia `fetch`, WebSocket i WebRTC. Każdy z nich ma
**narzucony protokół**: `fetch` to HTTP, WebSocket zaczyna się od
handshake'u HTTP `Upgrade` i ramkuje dane, WebRTC to SDP/ICE/DTLS. Nie ma
API, które pozwala wysłać dowolny bajt na dowolny port TCP, i nie jest to
luka do obejścia — gdyby było, każda strona w internecie byłaby skanerem
portów sieci lokalnej odwiedzającego.

Netcat robi dokładnie jedną rzecz: przekazuje **surowe bajty** między
stdin/stdout a socketem. To jest ta jedna rzecz, której przeglądarka nie
umie.

### 3.2 Emscripten *udaje* sockety przez WebSocket — i kłamie

To najważniejszy punkt całego dokumentu, bo wygląda, jakby wszystko działało.

`socket.socket(AF_INET, SOCK_STREAM)` w Pyodide **się udaje** i zwraca
deskryptor. `bind()` i `listen()` też się udają. `connect()` też. Dlaczego?
Bo Emscripten implementuje warstwę socketów jako **tunel WebSocket** —
w `src/lib/libsockfs.js` jest `websocket_sock_ops` i stała `WEBSOCKET_URL`
(domyślnie `ws://`), z komunikatem błędu `WebSocket URL must be in the
format ws(s)://address:port`. „Socket TCP" w WASM to WebSocket z podprotokołem
`binary`.

Konsekwencje są zmierzone i są gorsze niż brak funkcji:

```
POMIAR — ten kontener, nic nie nasłuchuje na 127.0.0.1:80 ani :34999

  $ nc -z -v -w 2 127.0.0.1 80
  nc: connect to 127.0.0.1 port 80 (tcp) failed: Connection refused
  exit=1                                    <- prawdziwy netcat mówi prawdę

  Pyodide: socket(AF_INET, SOCK_STREAM)              -> OK, fd=13
  Pyodide: connect(("127.0.0.1", 80))                -> OK po 0,019 s
  Pyodide: connect(("127.0.0.1", 34999))             -> OK po 0,015 s
  Pyodide: send(b"HELLO") na tym sockecie            -> zwrócił 5
  Pyodide: bind(("127.0.0.1", 0)); listen(1)         -> OK,
           ale getsockname() -> ('127.0.0.1', 0)     <- nic nie zbindowane
  Pyodide: socketpair()                              -> OSError [Errno 28]
```

Czyli: **`connect()` na zamknięty port zgłasza sukces, a `send()` raportuje
wysłanie pięciu bajtów w próżnię.** Gdyby pyncat w przeglądarce używał tych
socketów, `pyncat -z` pokazywałby każdy port jako otwarty, a każdy transfer
jako udany. Tryb `-z` — którego cała wartość polega na odróżnieniu portu
otwartego od zamkniętego — produkowałby czystą fikcję.

Dlatego `web/build_site.py` **nie wysyła do przeglądarki**
`pyncat/endpoints.py`, `app.py` ani `cli.py`. To nie ostrożność, to warunek
uczciwości demo.

### 3.3 `poll()` istnieje, ale nie blokuje

Powszechne założenie jest takie, że `select.poll` w Pyodide nie istnieje.
**Nieprawda** — i to jest ważne, bo prawdziwy problem leży gdzie indziej.

```
POMIAR — Pyodide 314.0.8

  hasattr(select, "poll")    -> True
  hasattr(select, "select")  -> True
  hasattr(select, "epoll")   -> False
  stałe: POLLIN=1, POLLOUT=4, POLLHUP=16  (jak na Linuksie)

  poll(0) na pipie Z danymi          -> [(5, 1)]   = POLLIN, poprawnie
  poll(0) na pipie pustym            -> []         poprawnie
  poll(50) gdy nic nie jest gotowe   -> [] po 0,0000 s      <-- PROBLEM
```

Timeout jest **ignorowany**. Emscripten mówi to wprost w
`src/lib/libsyscall.js`:

```js
var count = doPollSync(fds, nfds);
if (!count && timeout != 0) warnOnce('non-zero poll() timeout not supported: ' + timeout)
```

Blokujący wariant (`doPollAsync`) istnieje, ale wymaga `PTHREADS` albo
`ASYNCIFY`/JSPI, a `sys._emscripten_info.pthreads` w Pyodide to **False**
(POMIAR; `threading.Thread().start()` → `RuntimeError: can't start new
thread`).

Dla `readwrite()` to jest śmiertelne z dwóch powodów:

1. Pętla relay **żyje z tego, że `poll()` blokuje**. Bez blokowania staje
   się busy-spinem, który nigdy nie oddaje steru pętli zdarzeń przeglądarki
   — czyli zawieszoną kartą, a nie demo.
2. `readwrite()` interpretuje `num_fds == 0` jako **wygaśnięcie `-w`**
   i kończy pętlę [netcat.c:1162-1163]. Przy nieblokującym `poll()` ten
   warunek trafia natychmiast, więc pętla wychodziłaby od razu — i jeszcze
   z błędnym uzasadnieniem.

Jedna ciekawostka na plus: linia 589 `libsyscall.js` robi
`flags & (events | POLLERR | POLLHUP | POLLNVAL)` — czyli Emscripten
odtwarza tę samą regułę POSIX o flagach niemaskowalnych, na której opiera
się `relay._poll_once()` przy rozdzielaniu `revents` na dwa slots jednego fd.

### 3.4 Pipe w Emscriptenie nie daje `POLLHUP`

Gdyby ktoś chciał obejść brak socketów parą `os.pipe()` (pipe'y w Pyodide
**działają** — POMIAR: `os.pipe()` → `r=3, w=4`, `os.write`/`os.read`
przenoszą bajty), rozbija się o detekcję EOF:

```
POMIAR — pipe, zamykamy koniec zapisu

  z danymi w buforze:   poll(0) -> [(3, 1)]     = POLLIN, bez POLLHUP
  po opróżnieniu:       poll(0) -> []           <-- PUSTO, nie POLLHUP
```

Na Linuksie pipe z zamkniętym pisarzem i bez danych daje `POLLHUP` (16).
W Emscriptenie nie daje nic. A `readwrite()` wykrywa koniec stdin właśnie
przez „`POLLHUP` bez `POLLIN`" [netcat.c:1166-1170] — to ten niuans, o który
wywraca się naiwna implementacja i który jest jednym z głównych tematów tego
projektu. Na WASM po prostu nigdy by nie wystąpił.

### 3.5 Brak backpressure: pipe się nie zapełnia

```
POMIAR — zapis do nieblokującego pipe'a w MEMFS, w pętli po 4096 B
  zapisano 2 002 944 B bez ani jednego EAGAIN
```

Bufor pipe'a w wirtualnym systemie plików jest praktycznie nieograniczony.
Czyli: żadnych zapisów częściowych, żadnego `EAGAIN` → `WANT_POLLOUT`,
żadnego przełączania `events` na zero przy pełnym buforze. Cały mechanizm
z [netcat.c:1216-1234] — pełny bufor gasi czytanie, pusty gasi pisanie — nie
zadziałałby ani raz. A to drugi najważniejszy temat po half-close.

### 3.6 Nie ma jednego deskryptora dwukierunkowego

`socket.socketpair()` → `OSError: [Errno 28] Invalid argument` (POMIAR).

To przesądza sprawę. Model netcata polega na tym, że `POLL_NETIN`
i `POLL_NETOUT` to **dwa wpisy na tym samym fd**, z osobnymi maskami
i osobnym „życiem" [netcat.c:1120-1134]. Half-close jest modelowany
wyłącznie przez wpisanie `fd = -1` do jednego z nich. Para `os.pipe()` to
dwa **różne**, jednokierunkowe deskryptory — odwzorowanie straciłoby dokładnie
to, co w netcacie jest najciekawsze. Plik otwarty `O_RDWR` jest dwukierunkowy,
ale nie ma EOF, nie ma FIN i dzieli jeden offset między czytanie i pisanie.

### 3.7 Wniosek: atrapa jest wierniejsza niż „prawdziwe" fd

Zestawmy to:

| Co trzeba pokazać | Prawdziwe fd w Pyodide | Atrapa z `web/bridge.py` |
|---|---|---|
| dwa slots na jednym fd | niemożliwe (`socketpair` → EINVAL) | tak, `FD_NET` z dwiema połówkami |
| `POLLHUP` bez `POLLIN` (koniec stdin) | nigdy nie wystąpi | tak, modelowane |
| FIN od peera jako `POLLIN` + `read()==0` | brak prawdziwego peera | tak |
| zapis częściowy, `EAGAIN`, backpressure | nigdy (bufor bez limitu) | tak, skończone okno nadawcze |
| `poll()` blokujący na `-w`/`-q` | timeout ignorowany | tak, na zegarze wirtualnym |
| `connect()` mówiący prawdę | **kłamie** (§3.2) | nie dotyczy — nie ma `connect()` |

Dlatego demo podstawia **tylko wywołania systemowe** i uruchamia prawdziwą
logikę. To nie jest kompromis z braku lepszego pomysłu; to jedyny wariant,
w którym na ekranie widać netcata, a nie Emscriptena.

---

## 4. Co demo pokazuje, a czego nie

### 4.1 Co jest prawdziwe, a co podstawione

Strona wczytuje do Pyodide **źródła z `pyncat/`**, wstrzyknięte w czasie
builda przez `web/build_site.py` (nie kopię — build czyta pliki z repo, a
`--selftest` pilnuje, że scenariusze nadal dają zmierzone wyniki).

| Wykonuje się prawdziwym kodem z repo | Podstawione przez `web/bridge.py` |
|---|---|
| całe ciało `relay.readwrite()` | `select.poll()` → `FakePoll` |
| `relay._handle_revents()` — kolejność obsługi `revents` | `os.read`/`os.write` → `FakeOs` |
| `relay._poll_once()` — rozdzielanie `revents` na slots | `time.monotonic`/`sleep` → `VClock` |
| `relay._poll_timeout()` — `min(-w, reszta -q)` | `socket.shutdown()` → `FakeSock` |
| `netio.fillbuf()` / `netio.drainbuf()` | |
| wszystkie trzy warunki wyjścia, propagacja EOF, backpressure | |
| `ports.strtoport()` / `ports.build_ports()` — **bez żadnych atrap** | |

Atrapy odtwarzają semantykę **Linuksa**, nie Emscriptena: pipe z zamkniętym
pisarzem daje `POLLIN|POLLHUP` dopóki są dane, FIN od peera daje `POLLIN`
i `read()==0` (a nie `POLLHUP`), okno nadawcze jest skończone.

Stan jest zbierany przez `sys.settrace` (POMIAR: działa w Pyodide, razem
z czytaniem `frame.f_locals`), więc każdy krok trace'u to **stan tuż przed
daną linią `relay.py`** — dokładnie jak w debuggerze. Odnośniki do
`netcat.c` są wyciągane z komentarzy w `relay.py` **w czasie przebiegu**,
więc nie rozjadą się po przenumerowaniu źródeł.

### 4.2 Co konkretnie widać

Dziewięć scenariuszy. Wyniki poniżej to faktyczne wyjście przebiegu
w Pyodide, nie zapowiedzi:

| Scenariusz | Wynik pętli | Na stdout | Do sieci | Kroków |
|---|---|---|---|---|
| zwykły transfer w obie strony | `return` | `HTTP/1.0 200 OK\r\n\r\nhello` | 18 B | 235 |
| **half-close BEZ `-N`** | **zawieszenie** | — | 4 B | 95 |
| **half-close Z `-N`** | `return` | — | 4 B | 124 |
| EOF z sieci (peer wysyła FIN) | `return` | `banner\r\n` | — | 121 |
| `-W 2` — limit odczytów | `return` | **`AB`** | — | 139 |
| `-q 2` — czekaj po EOF na stdin | `return` | **`DELAYED`** | 1 B | 165 |
| `-q 0` — wyjdź natychmiast | `return` | **nic** | 1 B | 88 |
| backpressure, okno 6 B | `return` | — | 40 B | 273 |
| stdout zniknął (`POLLERR`) | `return` | `dane` | — | 111 |

Trzy z tych wyników to odtworzenie POMIARÓW na prawdziwym `nc` z GROUND_TRUTH:

* `-W 2` → dokładnie `AB` (§2.5 — bo `-W` liczy **odczyty**, nie pakiety),
* `-q 2` → `DELAYED` dolatuje; `-q 0` → przepada (§8),
* bez `-N` → zawieszenie; z `-N` → natychmiastowe wyjście (§2.6: 7,701 s do
  zabicia kontra 0,008 s).

Scenariusz bez `-N` kończy się w demo komunikatem „ZAWIESZENIE — netcat wisi
tu w nieskończoność", bo atrapa `poll()` bez timeoutu i bez zaplanowanych
zdarzeń podnosi `Deadlock`. To nie jest błąd demo — to jego puenta, i jedyny
sposób, żeby pokazać zawieszenie, nie każąc nikomu na nie czekać.

Para scenariuszy „z `-N`" i „bez `-N`" ma **identyczny harmonogram zdarzeń**
i różni się jedną flagą. To jest cała lekcja tego projektu na jednym ekranie.

### 4.3 Czego demo NIE pokazuje

* **Żadnego połączenia.** Nie ma `socket()`, `connect()`, `bind()`,
  `listen()`, `accept()`. Strona nie wysyła ani nie odbiera bajtu z sieci.
* **Nie ma `-z`** ani niczego, co wymaga odróżnienia portu otwartego od
  zamkniętego — patrz §3.2.
* **Nie ma trybu `-l`/`-k`**, UDP ani `udptest()`: to `pyncat/endpoints.py`,
  którego build celowo nie wysyła.
* **Nie ma pomiarów wydajności.** Czas w Pyodide to czas WASM, nie czas
  natywnego CPythona — każda liczba byłaby myląca. Pomiary C vs Python są
  w `docs/c-vs-python.md`, robione na prawdziwym procesie.
* **`getservbyport()` nie działa** nawet po wstrzyknięciu `/etc/services`
  (POMIAR: `port/proto not found`), więc gwiazdka w `[tcp/*]`
  z `connection_info()` byłaby w przeglądarce zawsze.
* **Czas jest wirtualny.** `-q 2` kończy się „po 2 s" zegara symulowanego,
  natychmiast. Prawdziwe 2,007 s zmierzono na `/usr/bin/nc`, nie tutaj.

### 4.4 Jedno odstępstwo, które trzeba znać

`pyncat/ports.py` odtwarza **upstream 1.241**, a binarium testowe to
**Debian 1.226**, i w komunikatach błędów te wersje się różnią:

```
POMIAR — /usr/bin/nc (OpenBSD netcat, Debian patchlevel 1.226-1ubuntu2)
  nc -z -w 1 127.0.0.1 80,443     -> nc: port number invalid: 80,443
  nc -z -w 1 127.0.0.1 nosuchsvc  -> nc: port number invalid: nosuchsvc
  nc -z -w 1 127.0.0.1 99999      -> nc: port number too large: 99999
  nc -z -w 1 127.0.0.1 0          -> nc: port number too small: 0
  nc -z -w 1 127.0.0.1 20-25-30   -> nc: port number invalid: 25-30
  nc -z -w 1 127.0.0.1 ssh        -> (brak błędu, port zamknięty)
  nc -z -w 1 127.0.0.1 http-alt   -> (brak błędu, port zamknięty)
```

Upstream 1.241 [netcat.c:1467-1470] rozdziela te przypadki: `errno != EINVAL`
→ `port number %s: %s`, a nieznana usługa → `service "%s" unknown`. Debian
1.226 dla obu mówi `port number invalid`. Demo pokazuje więc
`service "80,443" unknown` tam, gdzie binarium powiedziałoby
`port number invalid: 80,443`. Odstępstwo jest świadome (cały projekt
odnosi numery linii do 1.241) i jest zapisane jako kontrakt w
`web/build_site.py`, więc zmiana którejkolwiek strony wywali selftest.

Dwa niuanse, które demo odtwarza wiernie i warto na nich kliknąć:
`25-20` → cichy swap na `20…25`, a `http-alt` → `8080`, bo zakres jest
rozpoznawany tylko gdy **pierwszy znak jest cyfrą**.

---

## 5. Pomiary w Pyodide — pełna tabela

Pyodide **314.0.8** = CPython **3.14.2**, `sys.platform == "emscripten"`,
`platform.python_implementation() == "CPython"`. Uruchomione pod node 22 na
tym samym `pyodide.asm.wasm`, który trafia do przeglądarki. Strona powtarza
część tych pomiarów **na żywo** i pokazuje je w nagłówku, zamiast polegać
na tej tabeli.

| Co sprawdzane | Wynik |
|---|---|
| `sys._emscripten_info.pthreads` | `False` |
| `threading` — import | OK |
| `threading.Thread().start()` | `RuntimeError: can't start new thread` |
| `subprocess`, `signal`, `selectors`, `fcntl`, `termios`, `asyncio` — import | OK (importowalne; `fcntl`/`termios` wbrew dokumentacji Pyodide) |
| `select` — import | OK |
| `hasattr(select, "poll")` | **`True`** |
| `hasattr(select, "select")` | `True` |
| `hasattr(select, "epoll")` | `False` |
| `select` — nazwy | `PIPE_BUF, POLLERR, POLLHUP, POLLIN, POLLMSG, POLLNVAL, POLLOUT, POLLPRI, POLLRDBAND, POLLRDHUP, POLLRDNORM, POLLWRBAND, POLLWRNORM, error, poll, select` |
| `POLLIN` / `POLLOUT` / `POLLHUP` | `1` / `4` / `16` |
| `poll(0)`, pipe z danymi | `[(5, 1)]` |
| `poll(0)`, pipe pusty, maska `POLLIN` | `[]` |
| `poll(0)`, pipe pusty, maska `POLLIN\|POLLOUT` | `[]` |
| **`poll(50)`, nic nie gotowe** | **`[]` po 0,0000 s** (w przeglądarkowym pomiarze na stronie: 0,064–0,090 ms) |
| `poll(0)`, pipe z zamkniętym pisarzem, z danymi | `[(3, 1)]` — bez `POLLHUP` |
| **`poll(0)`, pipe z zamkniętym pisarzem, opróżniony** | **`[]`** — zamiast `POLLHUP` |
| `os.pipe()` + `os.write`/`os.read` | działa (`r=3`, `w=4`, odczyt `b'abc'`) |
| zapis do nieblokującego pipe'a do pierwszego `EAGAIN` | 2 002 944 B **bez** `EAGAIN` |
| `socket.socket(AF_INET, SOCK_STREAM)` | OK, `fd=13` |
| `socket.connect(("127.0.0.1", 80))` — nic nie nasłuchuje | **OK po 0,019 s (fałszywy sukces)** |
| `socket.connect(("127.0.0.1", 34999))` — port zamknięty | **OK po 0,015 s (fałszywy sukces)** |
| `socket.send(b"HELLO")` na tym sockecie | zwrócił `5` |
| `socket.bind(("127.0.0.1", 0))` + `listen(1)` | „OK", ale `getsockname()` → `('127.0.0.1', 0)` |
| `socket.socketpair()` | `OSError: [Errno 28] Invalid argument` |
| `socket.getaddrinfo("127.0.0.1", 80)` | działa |
| `socket.getservbyname("ssh")` | `OSError: service/proto not found` |
| …po zapisaniu `/etc/services` do MEMFS | **`22`** |
| `socket.getservbyport(22)` | `OSError: port/proto not found` (również po zapisie) |
| `sys.settrace` + czytanie `frame.f_locals` | działa |
| 9 scenariuszy, 1351 kroków trace'u | **71 ms** (≈8 ms/scenariusz) |

Wydajność demo jest więc nieproblemem: całe obliczenie startuje raz, przy
wejściu na stronę, i trwa poniżej dziesiątej części sekundy. Koszt to
wyłącznie pobranie Pyodide.

### Koszt pobrania

| Plik | Rozmiar (nieskompresowany) |
|---|---|
| `pyodide.asm.wasm` | 9,15 MB |
| `python_stdlib.zip` | 2,43 MB |
| `pyodide.asm.mjs` | 1,19 MB |
| **razem Pyodide** | **≈12,8 MB** |
| cała strona bez Pyodide (`index.html` + `app.js` + `modules.js`) | **143 KB** |

Z tego `modules.js` to 93 KB — źródła Pythona wstrzyknięte jako napisy
(`relay.py` ≈20,6 KB). Powody wstrzykiwania zamiast kopiowania plików `.py`:
część hostingów statycznych nie serwuje `.py`, jedno żądanie zamiast sześciu,
i strona działa z `file://`, gdzie `fetch` wywraca się na CORS.

---

## 6. Darmowe tiery — mapa z limitami

### 6.0 Co udało się zweryfikować, a czego nie

Sieć w kontenerze, w którym pisano ten dokument, jest ograniczona. Mówiąc
wprost:

* **Zweryfikowane pobraniem treści:** wszystkie limity GitHuba — pobrane
  z **repozytorium źródeł dokumentacji** `github/docs` przez
  `raw.githubusercontent.com`, czyli z pierwszej ręki. Również
  `pyodide/pyodide` i `emscripten-core/emscripten`.
* **Niedostępne (403 z proxy na CONNECT):** `docs.github.com` (strony
  wyrenderowane), `render.com`, `fly.io`, `vercel.com`,
  **`cdn.jsdelivr.net`**.

Z tego wynikają dwie rzeczy, które trzeba powiedzieć zamiast zgadnąć:

1. **Liczby dla Rendera, Fly.io, Railwaya i Koyeba poniżej nie zostały
   zweryfikowane tutaj.** Pochodzą z `docs/darmowy-hosting.md` projektu
   bliźniaczego `pynmap-basic`, gdzie były sprawdzane w listopadzie 2026.
   Darmowe tiery zmieniają się szybciej niż dokumentacja — przed podjęciem
   decyzji trzeba je odświeżyć. Argument przeciw PaaS-om (§6.3) jest
   **strukturalny** i nie zależy od tych liczb.
2. **Adresu CDN dla Pyodide 314.0.8 nie dało się potwierdzić żądaniem
   HTTP.** `web/app.js` próbuje więc najpierw `./pyodide/` (gdyby ktoś
   zworkował dystrybucję do katalogu strony), potem CDN, ma twardy limit
   45 s na wczytanie skryptu i przyjmuje nadpisanie z paska adresu
   (`?pyodide=https://…/full/`). Przy nieudanym starcie pokazuje, co
   dokładnie zawiodło — zamiast wisieć na „pobieranie Pyodide".

### 6.1 GitHub — zweryfikowane z `github/docs`

**GitHub Pages** (`content/pages/getting-started-with-github-pages/github-pages-limits.md`):

| Limit | Wartość |
|---|---|
| rozmiar opublikowanej strony | **1 GB** |
| zalecany rozmiar repozytorium źródłowego | 1 GB |
| transfer | **100 GB/mies.** (limit *miękki*) |
| buildy | **10/godz.** (miękki; **nie dotyczy** własnego workflow Actions) |
| timeout deploya | **10 min** |
| rate limiting | przy przekroczeniu HTTP `429` |

Dwie uwagi praktyczne. Po pierwsze: Pages nie jest przeznaczone do
prowadzenia działalności komercyjnej ani SaaS-a — projekt edukacyjny jest
dokładnie tym, do czego służy. Po drugie: limit 10 buildów na godzinę
**nie obowiązuje**, gdy publikujesz własnym workflow Actions, a tak właśnie
robi to repo.

**GitHub Actions** (`content/billing/concepts/product-billing/github-actions.md`
i `content/actions/reference/limits.md`):

| Limit | Wartość |
|---|---|
| standardowe runnery GitHuba na repo **publicznym** | **bezpłatne** (dosłownie: „The use of standard GitHub-hosted runners is free: in public repositories, for GitHub Pages, for Dependabot") |
| czas jednego joba | **6 godz.** (przekroczenie = job zabity) |
| czas całego przebiegu | 35 dni |
| równoległe joby, plan Free | **20** (Pro 40, Team 60) |
| cache na repozytorium | 10 GB |

**GitHub Codespaces**
(`content/billing/concepts/product-billing/github-codespaces.md`):

| Plan | Storage/mies. | Czas obliczeń/mies. |
|---|---|---|
| **Free** (konto osobiste) | **15 GB-mies.** | **120 h** |
| Pro | 20 GB-mies. | 180 h |

Haczyk, który trzeba znać: te „120 h" to **jednostki zużycia**, a mnożnik
zależy od liczby rdzeni — dokumentacja podaje mnożnik **2 dla maszyny
2-rdzeniowej**, 4 dla 4-rdzeniowej itd. Czyli 120 jednostek to realnie
**≈60 godzin na maszynie 2-rdzeniowej** albo ≈30 na 4-rdzeniowej.
Zatrzymany codespace nie zużywa czasu obliczeń, ale **zużywa storage** —
nieużywane warto kasować. Plany organizacyjne i enterprise **nie mają**
darmowej puli na Codespaces.

### 6.2 Reszta rynku — liczby z projektu bliźniaczego, nie weryfikowane tutaj

| Platforma | Co dostajesz | Haczyk |
|---|---|---|
| **Render** | 512 MB RAM, 750 h/mies. (jedna usługa non-stop), Docker | usypia po 15 min bezczynności, budzi się ~minutę; od 23.04.2026 tylko 5 GB transferu |
| **Fly.io** | brak darmowego tieru dla nowych kont | trial: 2 h czasu maszyny albo 7 dni, potem karta |
| **Railway** | $5 kredytu jednorazowo na 30 dni, potem $1/mies. | nie utrzyma nic sensownego |
| **Koyeb** | wymaga karty z blokadą autoryzacyjną $29 (od lutego 2026) | — |
| **Heroku** | darmowego tieru nie ma od 2022 | — |

Render jest w praktyce jedynym sensownym always-free PaaS-em bez karty,
jaki został — i do tego projektu i tak się nie nadaje. Dlaczego, niżej.

### 6.3 Czy da się postawić prawdziwy listener netcata na darmowym PaaS?

**Nie, i powód jest strukturalny, nie regulaminowy.**

Darmowy PaaS daje jedno: **kontener za odwrotnym proxy HTTP**. Dostajesz
nazwę w domenie dostawcy, on terminuje TLS na 443 i routuje żądania HTTP(S)
do jednego portu w kontenerze, zwykle wskazanego przez `$PORT`. Co z tego
wynika:

1. **Nie ma trasy do dowolnego portu TCP.** Nawet jeśli `pyncat -l -p 9000`
   wstanie w kontenerze i poprawnie zbinduje port, nie istnieje ścieżka,
   którą pakiet z zewnątrz trafiłby na 9000. Routing dostawcy rozumie
   tylko HTTP.
2. **Nawet port HTTP nie jest surowym TCP.** Przed Twoim procesem stoi
   proxy, które parsuje HTTP i nie przepuszcza bajtów, które nie są żądaniem
   HTTP. Netcat nadający surowe bajty rozmawiałby z parserem HTTP, nie
   z drugim netcatem.
3. **Kontener jest efemeryczny i usypia.** Render budzi się ~minutę po
   15 min bezczynności. Listener, który musi być online w momencie, gdy ktoś
   się łączy, i tak by tego nie przeżył.
4. **Dopiero na czwartym miejscu regulamin.** Nasłuch na dowolnym porcie
   i ruch nie-HTTP to klasyczny wektor nadużyć (tunelowanie, relay,
   skanowanie), i dostawcy to ograniczają. Ale nawet gdyby pozwalali, punkty
   1–3 wystarczają.

Pojedynczy wyjątek, o którym warto wiedzieć: niektórzy dostawcy sprzedają
„TCP/UDP ingress" albo dedykowane adresy IP — **w planach płatnych**. To nie
jest darmowy tier i nie jest to droga dla projektu edukacyjnego.

Co z Codespaces? Codespace **potrafi** przekierować port, ale przez ten sam
rodzaj proxy HTTP(S) — przekierowany port dostaje adres `https://`, nie
surowe TCP. **I to nie ma znaczenia**, bo w codespace nie potrzebujesz
wychodzić na zewnątrz: odpalasz `pyncat -l -p 9000` w jednym terminalu
i `pyncat 127.0.0.1 9000` w drugim. Oba końce są Twoje, po loopbacku,
w prawdziwym jądrze Linuksa. To jest ten „prawdziwy netcat bez instalowania
czegokolwiek", którego szukamy — tylko nie trzeba go nikomu udostępniać.

### 6.4 Czemu nie mostek WebSocket → TCP

Byłoby to technicznie możliwe: Emscripten *już* tuneluje sockety przez
WebSocket (§3.2), a `websockify` albo 50 linii w Node potrafi rozpakować
ramki WS i przepisać bajty na prawdziwe gniazdo TCP. Odrzucam to z dwóch
niezależnych powodów.

**Bezpieczeństwo.** Publiczny mostek „otwórz TCP do hosta i portu, które
podam" to **otwarty relay**: dowolna osoba w internecie może przez niego
skanować porty, atakować usługi wewnętrzne dostawcy (klasyczny SSRF do
metadanych instancji) i ukrywać źródło ruchu. Żeby to było bezpieczne,
potrzeba autentykacji przed otwarciem WebSocketa, sztywnej listy dozwolonych
celów, limitów połączeń i odebranego dostępu do sieci wewnętrznej — czyli
więcej infrastruktury niż cały projekt.

**Dydaktyka.** I tak nic by to nie dało. Nawet z działającym mostkiem
`poll()` w przeglądarce **nadal nie blokuje** (§3.3), a `POLLHUP` nadal nie
przychodzi (§3.4). Pokazalibyśmy więc bajty lecące przez tunel, ale nie
maszynę stanów — czyli nie to, o co w tym projekcie chodzi.

---

## 7. Jak to zbudować i sprawdzić

```bash
# selftest maszyny stanów: 9 scenariuszy + 10 testów parsowania portów,
# bez budowania strony
python3 web/build_site.py --selftest

# build strony (robi selftest po drodze i bez niego nie publikuje)
python3 web/build_site.py site/
python3 web/build_site.py site/ --ci-json wynik-z-ci.json

# test strony end-to-end: prawdziwy index.html w jsdom
# z prawdziwym Pyodide z node
cd web && npm ci && PYNCAT_SITE=../site node test_page.mjs
```

Dwa poziomy testów, bo każdy łapie coś innego:

* `--selftest` sprawdza **maszynę stanów** po stronie Pythona: czy `-W 2`
  nadal przepuszcza `AB`, czy bez `-N` nadal jest zawieszenie, czy
  `build_ports("25-20")` nadal robi swap. Nie tknie ani jednej linii
  `app.js`.
* `test_page.mjs` wczytuje **prawdziwy `index.html`** do jsdom-a, wykonuje
  prawdziwe `modules.js` i `app.js`, podstawia prawdziwe Pyodide i sprawdza
  36 rzeczy: czy strona bootuje, czy slots mają właściwą kolejność, czy
  `POLL_NETIN` i `POLL_NETOUT` pokazują **ten sam deskryptor**, czy stepper
  przesuwa stan, czy scenariusz bez `-N` kończy się `deadlock`, czy panel
  portów liczy zakresy. Bez tego „demo działa" znaczyłoby tylko „plik się
  parsuje".

Build **nie opublikuje strony**, jeśli selftest nie przejdzie — a workflow
Pages powinien zależeć od testów, żeby opublikowana strona zawsze
odpowiadała commitowi, który przeszedł CI.

Przy pierwszym uruchomieniu `--selftest` warto spojrzeć na jedną linię:
`install_etc_services()` **nie zapisuje** `/etc/services` poza Emscriptenem.
Na Linuksie to prawdziwy plik systemowy; gdyby bramki nie było, selftest po
cichu kasowałby bazę usług na maszynie deweloperskiej i w CI.

---

## 8. Workflow, który z tego wychodzi

```
  ty: "dorób -q"
        │
        ▼
  Claude Code / lokalnie  ── pisze, uruchamia pytest
        │
        │  git push
        ▼
  GitHub ──┬─→ Actions: ruff, pytest, build C, potem REALNY transfer
           │            1 MiB po loopbacku z weryfikacją sha256
           │                    │
           │                    │  wynik jako JSON (--ci-json)
           │                    ▼
           └─→ Pages: maszyna stanów readwrite() w Pyodide + ten wynik
        │
        ▼
  ty: otwierasz URL (nic nie instalujesz)
      albo Codespaces, jeśli chcesz pogadać prawdziwym netcatem
```

Koszt: zero. Repo jest publiczne, więc standardowe runnery Actions są
bezpłatne, a Pages nie ma limitu czasu.

---

## Źródła

**Zweryfikowane pobraniem treści** (listopad 2026, przez
`raw.githubusercontent.com`):

- `github/docs` → `content/pages/getting-started-with-github-pages/github-pages-limits.md`
  — limity Pages
- `github/docs` → `content/billing/concepts/product-billing/github-actions.md`
  — bezpłatność standardowych runnerów na repo publicznym
- `github/docs` → `content/actions/reference/limits.md`
  — 6 h na job, 35 dni na przebieg, 20 równoległych jobów na planie Free
- `github/docs` → `content/billing/concepts/product-billing/github-codespaces.md`
  — 120 h + 15 GB na Free, mnożnik 2 dla maszyny 2-rdzeniowej
- `pyodide/pyodide` → `docs/usage/wasm-constraints.md`
  — lista modułów usuniętych i „included but not working" (tam m.in. `sockets`)
- `pyodide/pyodide` → `src/py/pyodide/__init__.py` — wersja
- `pyodide/pyodide` → `src/tests/python_tests.yaml`
  — `test_poll`, `test_select`, `test_selectors` **bez** `xfail`
- `emscripten-core/emscripten` → `src/lib/libsyscall.js`
  — `__syscall_poll`, `doPollSync`, `warnOnce('non-zero poll() timeout not supported')`
- `emscripten-core/emscripten` → `src/lib/libsockfs.js`
  — `websocket_sock_ops`, `WEBSOCKET_URL`, podprotokół `binary`

**Pomiary wykonane na miejscu:**

- Pyodide 314.0.8 z npm, uruchomiony pod node 22 — tabela w §5
- `/usr/bin/nc` (OpenBSD netcat, Debian patchlevel 1.226-1ubuntu2) —
  komunikaty parsowania portów w §4.4, „Connection refused" w §3.2
- `web/build_site.py --selftest` i `web/test_page.mjs` — wyniki w §4.2

**Nie zweryfikowane tutaj** (hosty niedostępne z tego kontenera):
`render.com`, `fly.io`, `vercel.com`, `docs.github.com` (wersje
wyrenderowane), `cdn.jsdelivr.net`. Liczby w §6.2 pochodzą
z `docs/darmowy-hosting.md` w `pynmap-basic`, sprawdzane w listopadzie 2026,
i przed decyzją trzeba je odświeżyć.
