/* pyncat-basic — wizualizacja maszyny stanów readwrite().
 *
 * Ładuje CPythona skompilowanego do WebAssembly (Pyodide), wrzuca do jego
 * wirtualnego systemu plików moduły pyncat z tego repo i woła warstwę
 * pośrednią z web/bridge.py.
 *
 * Granica JS <-> Python jest celowo wąska: przekazujemy tylko JSON-a
 * (pyCall niżej), więc nie ma zarządzania proxy ani wyciekających uchwytów.
 *
 * Podział pracy:
 *
 *   Python  — wykonuje PRAWDZIWE readwrite() na atrapach poll/read/write
 *             i produkuje trace: listę snapshotów pfd[4] przed każdą linią.
 *   JS      — tylko odtwarza ten trace. Nie ma tu ani jednej linii logiki
 *             netcata. To jest cała różnica między „demo" i „animacja".
 *
 * Czego tu nie ma: łączenia się z czymkolwiek. Przeglądarka nie daje
 * socketów, więc moduły endpoints/app/cli nie są nawet wczytywane —
 * build_site.py ich nie wysyła.
 */

"use strict";

/* Wersja Pyodide, na której demo było MIERZONE: 314.0.8 (CPython 3.14.2).
 * Sprawdzone uruchomieniem wszystkich scenariuszy pod node 22 na tym samym
 * pyodide.asm.wasm, który ląduje w przeglądarce.
 *
 * UWAGA — adresu CDN nie dało się zweryfikować z kontenera, w którym ten
 * plik powstał: cdn.jsdelivr.net jest tam zablokowany (proxy zwraca 403 na
 * CONNECT). Ścieżka poniżej jest zgodna ze schematem, którego Pyodide używa
 * dla wszystkich wydań, ale NIE została potwierdzona żądaniem HTTP.
 *
 * Dlatego kandydaci są listą i próbujemy ich po kolei:
 *   1. ./pyodide/  — jeśli ktoś zworkuje Pyodide do katalogu strony
 *      (`npm pack pyodide` i rozpakować do site/pyodide/), działa bez CDN-u
 *      i bez ryzyka, że wersja zniknie;
 *   2. CDN z przypiętą wersją.
 *
 * Można też nadpisać z paska adresu: `?pyodide=https://…/full/`.
 */
const PYODIDE_VERSION = "314.0.8";
const PYODIDE_CANDIDATES = [
  "./pyodide/",
  `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`,
];

/* Katalog w wirtualnym FS, do którego wrzucamy źródła. */
const PY_ROOT = "/lib/pyncat-site";

/* Pomiary na /usr/bin/nc (OpenBSD netcat, Debian patchlevel 1.226-1ubuntu2),
 * `nc -z -w 2 127.0.0.1 …`. Tabela jest statyczna, bo wymaga prawdziwych
 * połączeń — i strona mówi to wprost, zamiast udawać, że liczy. */
const EXIT_TABLE = [
  ["-z, port otwarty", 0, "ret = 0 po udanym connect"],
  ["-z, port zamknięty", 1, "connect zawiódł; ret zostaje 1 z inicjalizacji"],
  ["-z, zakres: 1 otwarty + 2 zamknięte", 0, "raz ustawione ret = 0 nie wraca do 1"],
  ["-z, zakres: wszystkie zamknięte", 1, "ret nigdy nie został wyzerowany"],
  ["klient bez -z, port zamknięty", 1, "ta sama ścieżka co wyżej"],
  ["tryb -l (nasłuch)", 0, "ret = 0 od razu, niezależnie od tego, czy ktoś się połączył"],
  ["-u -z, udptest() zawiódł", 1, "jedyny przypadek, w którym ret wraca do 1"],
];

/* Gotowe specyfikacje portów do panelu parsowania. Każda ilustruje jeden
 * niuans z [netcat.c:1455-1532]. */
const PORT_EXAMPLES = [
  ["22", "pojedynczy port"],
  ["20-25", "zakres"],
  ["25-20", "zakres odwrotny → cichy swap"],
  ["ssh", "nazwa usługi przez getservbyname"],
  ["http-alt", "minus w nazwie usługi, a nie zakres"],
  ["80,443", "przecinki to BŁĄD, nie lista"],
  ["20-25-30", "split na pierwszym minusie → hi = „25-30”"],
  ["99999", "poza zakresem → bez próby getservbyname"],
  ["0", "port 0 jest za mały"],
];

let pyodide = null;
let scenarios = [];
let traces = {};          /* name -> trace z bridge.run_scenario */
let current = null;       /* aktualny trace */
let stepIdx = 0;
let visible = [];         /* indeksy kroków po filtrze „tylko zmiany" */
let playing = false;
let playTimer = null;

/* ------------------------------------------------------------------ */
/* Pomocnicze DOM                                                      */
/* ------------------------------------------------------------------ */

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  }
  for (const child of [].concat(children)) {
    if (child == null) continue;
    node.append(child);
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function bootStep(n, state) {
  const node = $(`boot-${n}`);
  if (node) node.dataset.done = state;
}

function fatal(msg, detail) {
  const box = $("boot-err");
  clear(box);
  box.append(el("strong", { text: msg }));
  if (detail) box.append(el("pre", { text: String(detail) }));
  box.hidden = false;
}

/* ------------------------------------------------------------------ */
/* Granica JS <-> Python                                               */
/* ------------------------------------------------------------------ */

/* Woła funkcję z bridge.py i parsuje JSON-a. Wszystkie api_* z bridge.py
 * zwracają napis, więc po stronie JS nie ma ani jednego PyProxy do
 * zwolnienia. */
function pyCall(fn, ...args) {
  pyodide.globals.set("_nc_args", JSON.stringify(args));
  const out = pyodide.runPython(
    `import json as _json; bridge.${fn}(*_json.loads(_nc_args))`
  );
  return JSON.parse(out);
}

/* ------------------------------------------------------------------ */
/* Start                                                               */
/* ------------------------------------------------------------------ */

async function loadPyodideFrom(candidates) {
  /* Jeśli `loadPyodide` już istnieje, nie wstrzykujemy niczego. Dwa
   * przypadki, oba realne: ktoś zworkował Pyodide i wczytał pyodide.js
   * zwykłym <script> w index.html, albo stronę uruchamia test węzłowy
   * (web/test_page.mjs), który podstawia loadPyodide z npm-owego pakietu. */
  if (typeof loadPyodide === "function") {
    return await loadPyodide({ indexURL: candidates[0] });
  }

  const errors = [];
  for (const base of candidates) {
    try {
      /* loadPyodide pochodzi ze skryptu pyodide.js, który dociągamy
       * dynamicznie — dzięki temu nieudany kandydat nie blokuje kolejnego. */
      await injectScript(`${base}pyodide.js`);
      if (typeof loadPyodide !== "function") {
        throw new Error("pyodide.js wczytany, ale brak loadPyodide");
      }
      return await loadPyodide({ indexURL: base });
    } catch (err) {
      errors.push(`${base} → ${err && err.message ? err.message : err}`);
    }
  }
  throw new Error(
    "nie udało się wczytać Pyodide z żadnego źródła:\n  " + errors.join("\n  ")
  );
}

/* Wstrzyknięcie <script src>. Z TWARDYM limitem czasu: w normalnej
 * przeglądarce nieudane pobranie odpala `onerror`, ale są konfiguracje
 * (CSP, rozszerzenia blokujące, pośrednik, który trzyma połączenie), gdzie
 * nie odpala się ani `onload`, ani `onerror`. Bez limitu strona wisiałaby
 * wtedy na „pobieranie Pyodide" bez żadnego komunikatu — a to najgorszy
 * możliwy tryb awarii, bo wygląda jak wolny internet. */
const SCRIPT_TIMEOUT_MS = 45000;

function injectScript(url) {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[data-nc-src="${url}"]`);
    if (existing) return resolve();
    const s = document.createElement("script");
    let settled = false;
    const done = (fn, arg) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fn(arg);
    };
    const timer = setTimeout(
      () => done(reject, new Error(`brak odpowiedzi po ${SCRIPT_TIMEOUT_MS} ms`)),
      SCRIPT_TIMEOUT_MS
    );
    s.src = url;
    s.dataset.ncSrc = url;
    s.onload = () => done(resolve);
    s.onerror = () => done(reject, new Error("nie wczytał się"));
    document.head.append(s);
  });
}

/* Start jest idempotentny i zwraca zawsze ten sam promise: strona woła go
 * sama, a test strony woła go jeszcze raz, żeby mieć na czym `await`-ować.
 * Bez memoizacji Pyodide ładowałoby się dwa razy. */
let bootPromise = null;

function boot() {
  if (bootPromise) return bootPromise;
  bootPromise = (async () => {
    try {
      await bootInner();
    } catch (err) {
      /* Siatka bezpieczeństwa. Każdy etap bootInner() ma własny try/catch
       * z sensownym komunikatem, ale gdyby coś przeciekło między nimi,
       * strona musi pokazać błąd, a nie zostać na zawsze na ekranie
       * „wczytywanie". */
      bootStep(1, "err");
      fatal("Nieoczekiwany błąd przy starcie demo.", err && err.stack ? err.stack : err);
    }
  })();
  return bootPromise;
}

async function bootInner() {
  const override = new URLSearchParams(location.search).get("pyodide");
  const candidates = override ? [override] : PYODIDE_CANDIDATES;

  try {
    pyodide = await loadPyodideFrom(candidates);
    bootStep(1, "1");
  } catch (err) {
    bootStep(1, "err");
    fatal(
      "Nie udało się wczytać Pyodide.",
      `${err.message}\n\nWersja, na której demo było mierzone: ${PYODIDE_VERSION}.\n` +
        "Można podać inne źródło w adresie: ?pyodide=https://…/full/\n" +
        "albo zworkować Pyodide do katalogu strony jako ./pyodide/."
    );
    return;
  }

  /* --- źródła Pythona do wirtualnego FS --- */
  try {
    const payload = window.PYNCAT_SOURCES;
    if (!payload || !payload.sources) {
      throw new Error("brak window.PYNCAT_SOURCES — czy modules.js się wczytał?");
    }
    pyodide.FS.mkdirTree(`${PY_ROOT}/pyncat`);
    for (const [path, text] of Object.entries(payload.sources)) {
      pyodide.FS.writeFile(`${PY_ROOT}/${path}`, text);
    }
    pyodide.runPython(`
import sys
if ${JSON.stringify(PY_ROOT)} not in sys.path:
    sys.path.insert(0, ${JSON.stringify(PY_ROOT)})
`);
    bootStep(2, "1");
  } catch (err) {
    bootStep(2, "err");
    fatal("Nie udało się zapisać modułów pyncat do wirtualnego FS.", err);
    return;
  }

  /* --- import --- */
  try {
    pyodide.runPython("import bridge");
    bootStep(3, "1");
  } catch (err) {
    bootStep(3, "err");
    fatal(
      "Import pyncat/bridge nie przeszedł. To zwykle znaczy, że API pyncata " +
        "się zmieniło — kontrakt jest wypisany w docstringu web/bridge.py.",
      err
    );
    return;
  }

  renderEnv(pyCall("api_env"));

  /* --- przebieg scenariuszy --- */
  try {
    scenarios = pyCall("api_scenarios");
    for (const s of scenarios) {
      traces[s.name] = pyCall("api_run", s.name);
    }
    bootStep(4, "1");
  } catch (err) {
    bootStep(4, "err");
    fatal("Przebieg scenariuszy się wywrócił.", err);
    return;
  }

  const broken = scenarios.filter((s) => !traces[s.name] || !traces[s.name].ok);
  if (broken.length === scenarios.length) {
    fatal(
      "Żaden scenariusz nie dał trace'u.",
      broken.map((s) => `${s.name}: ${traces[s.name] && traces[s.name].error}`).join("\n")
    );
    return;
  }

  $("boot").hidden = true;
  $("app").hidden = false;

  renderScenarioList();
  renderExitTable();
  renderPortExamples();
  wireControls();
  wirePorts();
  selectScenario(scenarios[0].name);
  loadCi();
  runPorts();
}

/* ------------------------------------------------------------------ */
/* Środowisko — mierzone na żywo, nie zakładane                         */
/* ------------------------------------------------------------------ */

function renderEnv(env) {
  const box = $("env");
  clear(box);
  const rows = [
    ["python", `${env.python} (${env.platform})`],
    ["pyodide", PYODIDE_VERSION],
    ["select.poll", env.select_has_poll ? "istnieje" : "BRAK"],
    ["poll(50 ms)", env.poll_50ms_blocks],
    ["socketpair()", env.socketpair],
    ["wątki", env.pthreads ? "są" : "brak"],
  ];
  for (const [k, v] of rows) {
    box.append(el("div", {}, [el("b", { text: `${k}: ` }), document.createTextNode(String(v))]));
  }
}

/* ------------------------------------------------------------------ */
/* Lista scenariuszy                                                   */
/* ------------------------------------------------------------------ */

function renderScenarioList() {
  const list = $("scen-list");
  clear(list);
  for (const s of scenarios) {
    const t = traces[s.name];
    const btn = el("button", { type: "button", "data-name": s.name }, [
      document.createTextNode(s.title),
      el("span", { class: "cmd", text: s.cmd }),
    ]);
    if (!t || !t.ok) {
      btn.disabled = true;
      btn.append(el("span", { class: "cmd err", text: "trace się nie udał" }));
    }
    btn.addEventListener("click", () => selectScenario(s.name));
    list.append(el("li", {}, [btn]));
  }
}

function selectScenario(name) {
  stopPlay();
  current = traces[name];
  if (!current || !current.ok) return;
  for (const btn of $("scen-list").querySelectorAll("button")) {
    btn.setAttribute("aria-current", btn.dataset.name === name ? "true" : "false");
  }
  renderLead();
  recomputeVisible();
  stepIdx = visible.length ? visible[0] : 0;
  renderStep();
  renderOutcome();
}

function renderLead() {
  const box = $("lead");
  clear(box);
  box.append(el("p", { class: "cmdline", text: `$ ${current.cmd}` }));
  box.append(el("p", { text: current.lead }));
  const exp = el("p", { class: "expect" }, [
    el("strong", { text: "Czego się spodziewać: " }),
    document.createTextNode(current.expect),
  ]);
  box.append(exp);
  if (current.measured) {
    box.append(el("p", { class: "measured", text: `odniesienie: ${current.measured}` }));
  }
}

/* ------------------------------------------------------------------ */
/* Rysowanie kroku                                                     */
/* ------------------------------------------------------------------ */

function flagNames(mask, flags) {
  if (!mask) return "0";
  const out = [];
  for (const [name, bit] of Object.entries(flags)) {
    if (mask & bit) out.push(name.replace("POLL", ""));
  }
  return out.length ? out.join("|") : `0x${mask.toString(16)}`;
}

function recomputeVisible() {
  const onlyChanges = $("only-changes").checked;
  const steps = current.steps;
  if (!onlyChanges) {
    visible = steps.map((_, i) => i);
  } else {
    visible = steps.map((s, i) => (s.changed || s.log.length ? i : -1)).filter((i) => i >= 0);
    /* Zawsze zostawiamy pierwszy i ostatni krok, żeby suwak miał końce. */
    if (!visible.length) visible = steps.map((_, i) => i);
    else {
      if (visible[0] !== 0) visible.unshift(0);
      const last = steps.length - 1;
      if (visible[visible.length - 1] !== last) visible.push(last);
    }
  }
  const scrub = $("scrub");
  scrub.max = String(Math.max(0, visible.length - 1));
  scrub.value = String(Math.max(0, visible.indexOf(stepIdx)));
}

function renderStep() {
  if (!current) return;
  const step = current.steps[stepIdx];
  if (!step) return;

  renderSlots(step);
  renderMeters(step);
  renderCode(step);
  renderLog(step);

  const vpos = visible.indexOf(stepIdx);
  $("pos").textContent =
    `krok ${vpos >= 0 ? vpos + 1 : "?"}/${visible.length}` +
    `  (linia ${stepIdx + 1}/${current.steps.length} trace'u)`;
  $("scrub").value = String(Math.max(0, vpos));

  $("b-prev").disabled = vpos <= 0;
  $("b-first").disabled = vpos <= 0;
  $("b-next").disabled = vpos >= visible.length - 1;
  $("b-last").disabled = vpos >= visible.length - 1;
}

function renderSlots(step) {
  const box = $("slots");
  clear(box);
  const flags = current.flags;
  const retired = current.fd_retired;
  const netFd = current.fd.net;

  current.slot_labels.forEach((label, i) => {
    const [fd, events, revents] = step.slots[i];
    const dead = fd === retired;
    const state = dead ? "dead" : events ? "armed" : "idle";
    const isNet = fd === netFd || label === "NETIN" || label === "NETOUT";

    const dl = el("dl", {}, [
      el("dt", { text: "fd" }),
      el("dd", { class: dead ? "zero" : "", text: dead ? "-1 (wycofany)" : String(fd) }),
      el("dt", { text: "events" }),
      el("dd", { class: events ? "flags" : "zero", text: flagNames(events, flags) }),
      el("dt", { text: "revents" }),
      el("dd", { class: revents ? "rev" : "zero", text: flagNames(revents, flags) }),
    ]);

    const slot = el("div", { class: "slot", "data-state": state }, [
      el("h4", { text: `POLL_${label}` }),
      dl,
    ]);
    if (isNet && !dead) slot.dataset.net = "1";
    if (label === "NETOUT" || label === "NETIN") {
      slot.append(el("span", { class: "samefd", text: `fd ${netFd}` }));
    }
    box.append(slot);
  });
}

function renderMeters(step) {
  const box = $("meters");
  clear(box);
  const bufsize = current.bufsize;

  const mk = (title, val, frac, extra) => {
    const node = el("div", { class: "meter" }, [
      el("h5", { text: title }),
      el("div", { class: "val", text: val }),
    ]);
    if (frac !== null) {
      node.append(el("div", { class: "bar" }, [
        el("i", { style: `width:${Math.min(100, frac * 100).toFixed(2)}%` }),
      ]));
    }
    if (extra) node.append(el("div", { class: "meta mono", text: extra }));
    box.append(node);
  };

  mk("stdinbuf (stdin → sieć)", `${step.si} B`, step.si / bufsize, `z ${bufsize} B`);
  mk("netinbuf (sieć → stdout)", `${step.ni} B`, step.ni / bufsize, `z ${bufsize} B`);
  mk("zegar wirtualny", `${step.t} ms`, null, "czas symulowany, nie realny");
  mk("recvcount (-W)", String(step.rc), null, step.qd === null ? "" : `deadline -q: ${step.qd} ms`);
}

function renderCode(step) {
  const srcMap = current.source;
  const line = String(step.line);
  const text = srcMap[line] !== undefined ? srcMap[line] : "(źródło niedostępne)";

  $("code-loc").textContent = `pyncat/relay.py:${step.line}`;
  $("code-ncref").textContent = step.nc ? `netcat.c:${step.nc}` : "brak odpowiednika w upstream";

  const pre = $("code-src");
  clear(pre);
  pre.append(el("code", { text: text.replace(/\s+$/, "") || "(pusta linia)" }));

  const doc = $("code-doc");
  clear(doc);
  const lines = (current.docs && current.docs[line]) || [];
  if (!lines.length) {
    doc.append(el("p", { class: "meta", text: "— bez komentarza nad tą linią —" }));
  } else {
    for (const l of lines) doc.append(el("p", { text: l }));
  }
}

function renderLog(step) {
  const box = $("log");
  clear(box);
  /* Pokazujemy log narastająco do aktualnego kroku: to, co jądro zrobiło
   * do tej pory. */
  const entries = [];
  for (let i = 0; i <= stepIdx; i++) {
    for (const line of current.steps[i].log) {
      entries.push({ text: line, t: current.steps[i].t, now: i === stepIdx });
    }
  }
  if (!entries.length) {
    box.append(el("p", { class: "empty", text: "— jeszcze żadnego wywołania systemowego —" }));
    return;
  }
  const ol = el("ol");
  for (const e of entries.slice(-80)) {
    ol.append(el("li", { "data-now": e.now ? "1" : "0", text: `t=${e.t}ms  ${e.text}` }));
  }
  box.append(ol);
  box.scrollTop = box.scrollHeight;
}

function renderOutcome() {
  const box = $("outcome");
  clear(box);
  box.dataset.kind = current.outcome;

  const titles = {
    return: "readwrite() wróciło — pętla zakończona czysto",
    deadlock: "ZAWIESZENIE — netcat wisi tu w nieskończoność",
    exception: "Wyjątek w trakcie przebiegu",
  };
  box.append(el("h4", { text: titles[current.outcome] || current.outcome }));

  if (current.outcome === "deadlock") {
    box.append(el("p", {
      text:
        "poll() został zawołany bez timeoutu, a w symulacji nie ma już " +
        "żadnego zdarzenia, które mogłoby go obudzić. To nie jest błąd " +
        "demo — to realne zachowanie: bez -N druga strona nigdy nie " +
        "dowiaduje się, że skończyliśmy nadawać.",
    }));
  }
  if (current.error && current.outcome === "exception") {
    box.append(el("pre", { text: current.error }));
  }
  if (current.truncated) {
    box.append(el("p", { class: "meta", text: "trace obcięty na limicie kroków" }));
  }

  box.append(el("dl", {}, [
    el("dt", { text: "na stdout" }),
    el("dd", { text: `${current.stdout}  (${current.stdout_len} B)` }),
    el("dt", { text: "do sieci" }),
    el("dd", { text: `${current.net_tx}  (${current.net_tx_len} B)` }),
    el("dt", { text: "kroków trace'u" }),
    el("dd", { text: String(current.steps.length) }),
  ]));

  if (current.tail_log && current.tail_log.length) {
    box.append(el("p", { class: "meta", text: `po ostatniej linii: ${current.tail_log.join(" | ")}` }));
  }
}

/* ------------------------------------------------------------------ */
/* Sterowanie                                                          */
/* ------------------------------------------------------------------ */

function goToVisible(vpos) {
  if (!visible.length) return;
  const clamped = Math.max(0, Math.min(visible.length - 1, vpos));
  stepIdx = visible[clamped];
  renderStep();
}

function currentVisiblePos() {
  const p = visible.indexOf(stepIdx);
  if (p >= 0) return p;
  /* Po przełączeniu filtra bieżący krok mógł wypaść — szukamy najbliższego. */
  let best = 0;
  for (let i = 0; i < visible.length; i++) {
    if (visible[i] <= stepIdx) best = i;
  }
  return best;
}

function stopPlay() {
  playing = false;
  if (playTimer) { clearInterval(playTimer); playTimer = null; }
  const b = $("b-play");
  if (b) b.textContent = "▶ odtwarzaj";
}

function startPlay() {
  stopPlay();
  playing = true;
  $("b-play").textContent = "⏸ pauza";
  const fps = Number($("speed").value) || 18;
  playTimer = setInterval(() => {
    const pos = currentVisiblePos();
    if (pos >= visible.length - 1) { stopPlay(); return; }
    goToVisible(pos + 1);
  }, Math.max(16, 1000 / fps));
}

function wireControls() {
  $("b-first").addEventListener("click", () => { stopPlay(); goToVisible(0); });
  $("b-prev").addEventListener("click", () => { stopPlay(); goToVisible(currentVisiblePos() - 1); });
  $("b-next").addEventListener("click", () => { stopPlay(); goToVisible(currentVisiblePos() + 1); });
  $("b-last").addEventListener("click", () => { stopPlay(); goToVisible(visible.length - 1); });
  $("b-play").addEventListener("click", () => { playing ? stopPlay() : startPlay(); });

  $("scrub").addEventListener("input", (ev) => {
    stopPlay();
    goToVisible(Number(ev.target.value));
  });

  $("only-changes").addEventListener("change", () => {
    stopPlay();
    const keep = stepIdx;
    recomputeVisible();
    stepIdx = keep;
    goToVisible(currentVisiblePos());
  });

  $("speed").addEventListener("change", () => { if (playing) startPlay(); });

  document.addEventListener("keydown", (ev) => {
    if (ev.target && /^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName)) return;
    if (ev.key === "ArrowRight") { stopPlay(); goToVisible(currentVisiblePos() + 1); ev.preventDefault(); }
    else if (ev.key === "ArrowLeft") { stopPlay(); goToVisible(currentVisiblePos() - 1); ev.preventDefault(); }
    else if (ev.key === " ") { playing ? stopPlay() : startPlay(); ev.preventDefault(); }
  });
}

/* ------------------------------------------------------------------ */
/* Tabela kodów wyjścia                                                */
/* ------------------------------------------------------------------ */

function renderExitTable() {
  const tb = $("exit-rows");
  clear(tb);
  for (const [what, code, why] of EXIT_TABLE) {
    tb.append(el("tr", {}, [
      el("td", { text: what }),
      el("td", { class: "mono", text: String(code) }),
      el("td", { text: why }),
    ]));
  }
}

/* ------------------------------------------------------------------ */
/* Panel portów                                                        */
/* ------------------------------------------------------------------ */

function renderPortExamples() {
  const box = $("ports-quick");
  clear(box);
  for (const [spec, why] of PORT_EXAMPLES) {
    const b = el("button", { class: "ghost", type: "button", title: why, text: spec });
    b.addEventListener("click", () => {
      $("ports-spec").value = spec;
      runPorts();
    });
    box.append(b);
  }
}

function wirePorts() {
  $("ports-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    runPorts();
  });
  for (const id of ["ports-udp", "ports-rand"]) {
    $(id).addEventListener("change", runPorts);
  }
}

function runPorts() {
  const spec = $("ports-spec").value;
  const udp = $("ports-udp").checked;
  const rand = $("ports-rand").checked;
  const out = $("ports-out");
  clear(out);

  let res;
  try {
    res = pyCall("api_ports", spec, udp, rand);
  } catch (err) {
    out.append(el("p", { class: "err", text: `błąd wywołania: ${err}` }));
    return;
  }

  if (!res.ok) {
    out.append(el("p", { class: "err", text: res.error }));
    return;
  }
  if (res.error) {
    out.append(el("p", { class: "err", text: `pyncat: ${res.error}` }));
    out.append(el("p", {
      class: "meta",
      text: "Taki komunikat wychodzi na stderr i kończy program z kodem 1.",
    }));
    return;
  }

  out.append(el("p", {
    text: `${res.count} ${res.count === 1 ? "port" : "portów"}` +
      (rand ? " — kolejność losowa (-r)" : " — kolejność rosnąca"),
  }));
  const chips = el("div", { class: "chips" });
  for (const p of res.ports) chips.append(el("span", { class: "chip", text: p }));
  out.append(chips);
  if (res.clipped) {
    out.append(el("p", { class: "meta", text: "lista obcięta do 512 pozycji" }));
  }
}

/* ------------------------------------------------------------------ */
/* Wynik z CI                                                          */
/* ------------------------------------------------------------------ */

async function loadCi() {
  const box = $("ci-out");
  let data = null;
  try {
    const res = await fetch("data/latest-ci.json", { cache: "no-store" });
    if (res.ok) data = await res.json();
  } catch (err) {
    /* file:// albo brak pliku — obsługa niżej. */
  }
  clear(box);
  if (!data) {
    box.append(el("p", {
      text: "Nie ma dołączonego wyniku z CI. Build strony przyjmuje go " +
        "przez --ci-json; bez niego strona nie udaje, że coś zmierzyła.",
    }));
    box.append(el("p", { class: "meta" }, [
      document.createTextNode("Przebiegi: "),
      el("a", {
        href: "https://github.com/damiantomczyszyn/pyncat-basic/actions",
        text: "GitHub Actions",
      }),
    ]));
    return;
  }

  const wrap = el("div", { class: "tbl-wrap" });
  const table = el("table");
  table.append(el("thead", {}, [el("tr", {}, [
    el("th", { text: "Metryka" }),
    el("th", { text: "Wartość" }),
  ])]));
  const tb = el("tbody");
  for (const [k, v] of Object.entries(data)) {
    tb.append(el("tr", {}, [
      el("td", { class: "mono", text: k }),
      el("td", { class: "mono", text: typeof v === "object" ? JSON.stringify(v) : String(v) }),
    ]));
  }
  table.append(tb);
  wrap.append(table);
  box.append(wrap);
}

/* ------------------------------------------------------------------ */
/* Start                                                               */
/* ------------------------------------------------------------------ */

/* Wystawiamy punkt wejścia jawnie na `window`.
 *
 * Powód nie jest kosmetyczny: ten plik jest `"use strict"`, a w trybie
 * strict deklaracje wykonane przez `eval()` **nie trafiają do zasięgu
 * globalnego**. Test strony (web/test_page.mjs) wczytuje app.js przez
 * `window.eval`, więc bez tej linii nie miałby jak zawołać boot() ani
 * sprawdzić, czy start się udał — a przy okazji nie zobaczyłby żadnego
 * błędu, bo odrzucony promise z wnętrza eval-a nigdzie nie trafia.
 *
 * Przy okazji daje to wygodny uchwyt do debugowania z konsoli:
 * `PYNCAT_DEMO.pyCall("api_env")`. */
window.PYNCAT_DEMO = {
  boot,
  pyCall: (...a) => pyCall(...a),
  get pyodide() { return pyodide; },
  get traces() { return traces; },
  get current() { return current; },
};

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot, { once: true });
} else {
  boot();
}
