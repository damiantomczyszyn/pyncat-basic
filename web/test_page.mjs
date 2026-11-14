/* Test strony end-to-end.
 *
 * Wczytuje prawdziwy index.html do jsdom-a, wykonuje prawdziwe modules.js
 * i app.js, i podstawia prawdziwe Pyodide z node. Jedyne, czego brakuje, to
 * silnik renderujący — więc weryfikujemy logikę i strukturę DOM-u: czy
 * strona bootuje, czy slots pokazują właściwe fd i maski, czy stepper
 * przesuwa stan, czy scenariusz bez -N kończy się zawieszeniem, czy panel
 * portów liczy zakresy i zgłasza błędy.
 *
 * Sens tego testu: ``build_site.py --selftest`` sprawdza maszynę stanów po
 * stronie Pythona, ale nie tknie ani jednej linii app.js. Bez tego pliku
 * „demo działa" znaczyłoby tylko „plik się parsuje".
 *
 * Uruchomienie::
 *
 *     python3 web/build_site.py _preview
 *     cd web && npm ci && PYNCAT_SITE=../_preview node test_page.mjs
 */
import { JSDOM, VirtualConsole } from "jsdom";
import { loadPyodide } from "pyodide";
import { readFileSync } from "node:fs";

const SITE = process.env.PYNCAT_SITE || "_preview";

/* pyodide.js z CDN-u nie istnieje w teście — app.js i tak go nie wstrzyknie,
 * bo window.loadPyodide jest podstawione niżej. */
const html = readFileSync(`${SITE}/index.html`, "utf8");

const vc = new VirtualConsole();
const logs = [];
vc.on("jsdomError", (e) => logs.push(["jsdomError", e.message]));
vc.on("error", (...a) => logs.push(["error", a.join(" ")]));

const dom = new JSDOM(html, {
  runScripts: "outside-only",
  pretendToBeVisual: true,
  virtualConsole: vc,
  url: "https://example.test/",
});
const { window } = dom;

/* fetch dla data/latest-ci.json */
window.fetch = async (url) => {
  const path = String(url).replace(/^https?:\/\/[^/]+\//, "");
  try {
    const body = readFileSync(`${SITE}/${path}`, "utf8");
    return { ok: true, status: 200, text: async () => body, json: async () => JSON.parse(body) };
  } catch {
    return { ok: false, status: 404, text: async () => "", json: async () => ({}) };
  }
};

/* prawdziwe Pyodide z node; indexURL ignorujemy, bo pakiet npm ma własny */
window.loadPyodide = async () => await loadPyodide();

window.eval(readFileSync(`${SITE}/modules.js`, "utf8"));
window.eval(readFileSync(`${SITE}/app.js`, "utf8"));

/* app.js jest `"use strict"`, a deklaracje ze strict-owego eval-a nie trafiają
 * do zasięgu globalnego — dlatego punkt wejścia jest wystawiony jawnie jako
 * window.PYNCAT_DEMO. Bez tego nie dałoby się ani zawołać boot(), ani
 * zobaczyć błędu: odrzucony promise z wnętrza eval-a nigdzie nie trafia.
 *
 * boot() jest memoizowany, więc to nie jest drugie ładowanie Pyodide —
 * dostajemy promise tego, który strona odpaliła sama. */
if (!window.PYNCAT_DEMO || typeof window.PYNCAT_DEMO.boot !== "function") {
  console.log("BLAD app.js nie wystawił window.PYNCAT_DEMO.boot");
  process.exit(1);
}
await Promise.race([
  window.PYNCAT_DEMO.boot(),
  new Promise((_, rej) => setTimeout(() => rej(new Error("boot() nie skończył w 240 s")), 240000)),
]);
/* Dociągnięcie asynchronicznych dorysowań (panel CI leci przez fetch). */
await new Promise((r) => setTimeout(r, 500));

const $ = (id) => window.document.getElementById(id);
const fail = [];
const ok = (cond, msg) => {
  console.log(`  ${cond ? "OK  " : "BLAD"} ${msg}`);
  if (!cond) fail.push(msg);
};

console.log("\n== boot ==");
const bootErr = $("boot-err");
if (bootErr && !bootErr.hidden) {
  console.log("  tresc boot-err:\n" + bootErr.textContent);
}
ok(!$("app").hidden, "sekcja #app odsłonięta");
ok(bootErr.hidden, "brak komunikatu krytycznego");

console.log("\n== środowisko (mierzone w Pyodide, nie zakładane) ==");
const envText = $("env").textContent;
console.log("  " + envText.replace(/\s+/g, " ").trim());
ok(/emscripten/.test(envText), "env mówi, że to emscripten");
ok(/select\.poll: istnieje/.test(envText), "env raportuje, że select.poll istnieje");
ok(/socketpair.*Errno 28|socketpair.*OSError/.test(envText), "env raportuje brak socketpair");

console.log("\n== lista scenariuszy ==");
const btns = [...$("scen-list").querySelectorAll("button")];
ok(btns.length >= 9, `${btns.length} scenariuszy w menu`);
ok(btns.filter((b) => b.disabled).length === 0, "żaden scenariusz nie jest zepsuty");
ok(btns[0].getAttribute("aria-current") === "true", "pierwszy scenariusz zaznaczony");

console.log("\n== slots ==");
const slots = [...$("slots").querySelectorAll(".slot")];
ok(slots.length === 4, `${slots.length} slots (ma być 4)`);
const labels = slots.map((s) => s.querySelector("h4").textContent);
ok(
  JSON.stringify(labels) === JSON.stringify(["POLL_STDIN", "POLL_NETOUT", "POLL_NETIN", "POLL_STDOUT"]),
  `kolejność slotów: ${labels.join(", ")}`
);
const sameFd = slots.filter((s) => s.querySelector(".samefd"));
ok(sameFd.length === 2, "dokładnie dwa slots oznaczone tym samym fd (NETOUT + NETIN)");
const fdBadges = sameFd.map((s) => s.querySelector(".samefd").textContent);
ok(new Set(fdBadges).size === 1, `oba pokazują ten sam deskryptor: ${fdBadges.join(" / ")}`);

console.log("\n== stepper ==");
const posBefore = $("pos").textContent;
const codeBefore = $("code-src").textContent;
$("b-last").dispatchEvent(new window.Event("click"));
const posAfter = $("pos").textContent;
const codeAfter = $("code-src").textContent;
console.log(`  pozycja: "${posBefore.trim()}"  ->  "${posAfter.trim()}"`);
ok(posBefore !== posAfter, "przejście na ostatni krok zmienia pozycję");
ok(codeBefore !== codeAfter, "zmienia się pokazywana linia relay.py");
ok(/relay\.py:\d+/.test($("code-loc").textContent), `#code-loc to ${$("code-loc").textContent}`);
$("b-first").dispatchEvent(new window.Event("click"));
ok($("pos").textContent === posBefore, "powrót na pierwszy krok wraca do tej samej pozycji");

/* Krok wstecz z pierwszego kroku nie może nic zepsuć. */
$("b-prev").dispatchEvent(new window.Event("click"));
ok($("pos").textContent === posBefore, "krok wstecz na początku jest bezpieczny");

console.log("\n== odnośniki do netcat.c ==");
let refSeen = 0;
for (let i = 0; i < 40; i++) {
  $("b-next").dispatchEvent(new window.Event("click"));
  if (/netcat\.c:\d+/.test($("code-ncref").textContent)) refSeen++;
}
ok(refSeen > 10, `${refSeen}/40 kroków ma odnośnik do netcat.c`);

console.log("\n== scenariusz bez -N musi skończyć się zawieszeniem ==");
const noN = btns.find((b) => b.dataset.name === "halfclose-bez-N");
ok(Boolean(noN), "scenariusz halfclose-bez-N jest w menu");
noN.dispatchEvent(new window.Event("click"));
ok($("outcome").dataset.kind === "deadlock", `outcome = ${$("outcome").dataset.kind}`);
ok(/wisi/.test($("outcome").textContent), "komunikat mówi o zawieszeniu");

console.log("\n== scenariusz z -N musi skończyć się czysto ==");
const withN = btns.find((b) => b.dataset.name === "halfclose-z-N");
withN.dispatchEvent(new window.Event("click"));
ok($("outcome").dataset.kind === "return", `outcome = ${$("outcome").dataset.kind}`);

console.log("\n== -W 2 przepuszcza dokładnie AB (POMIAR: docs/tablice-decyzyjne.md §9.3) ==");
btns.find((b) => b.dataset.name === "W-limit").dispatchEvent(new window.Event("click"));
const wOut = $("outcome").textContent;
ok(/'AB'/.test(wOut), `w outcome widać stdout: ${/b'[^']*'/.exec(wOut)?.[0]}`);

console.log("\n== -q 0 gubi odpowiedź, -q 2 nie (POMIAR: docs/tablice-decyzyjne.md §9.4) ==");
btns.find((b) => b.dataset.name === "q-zero").dispatchEvent(new window.Event("click"));
ok(/0 B/.test($("outcome").textContent), "-q 0 → 0 B na stdout");
btns.find((b) => b.dataset.name === "q-timer").dispatchEvent(new window.Event("click"));
ok(/DELAYED/.test($("outcome").textContent), "-q 2 → DELAYED dolatuje");

console.log("\n== log wywołań systemowych ==");
btns.find((b) => b.dataset.name === "transfer").dispatchEvent(new window.Event("click"));
$("b-last").dispatchEvent(new window.Event("click"));
const logText = $("log").textContent;
ok(/read\(stdin/.test(logText), "log zawiera read(stdin…)");
ok(/write\(net/.test(logText), "log zawiera write(net…)");

console.log("\n== filtr „tylko zmiany stanu” ==");
const posFiltered = $("pos").textContent;
$("only-changes").checked = false;
$("only-changes").dispatchEvent(new window.Event("change"));
const posAll = $("pos").textContent;
console.log(`  z filtrem: "${posFiltered.trim()}"  bez: "${posAll.trim()}"`);
ok(posFiltered !== posAll, "wyłączenie filtra zmienia liczbę widocznych kroków");

console.log("\n== panel portów (prawdziwe build_ports) ==");
const setSpec = (v) => {
  $("ports-spec").value = v;
  $("ports-form").dispatchEvent(new window.Event("submit"));
};
setSpec("20-25");
let chips = [...$("ports-out").querySelectorAll(".chip")].map((c) => c.textContent);
ok(JSON.stringify(chips) === JSON.stringify(["20", "21", "22", "23", "24", "25"]), `20-25 → ${chips.join(",")}`);
setSpec("25-20");
chips = [...$("ports-out").querySelectorAll(".chip")].map((c) => c.textContent);
ok(JSON.stringify(chips) === JSON.stringify(["20", "21", "22", "23", "24", "25"]), `25-20 → swap → ${chips.join(",")}`);
setSpec("ssh");
chips = [...$("ports-out").querySelectorAll(".chip")].map((c) => c.textContent);
ok(JSON.stringify(chips) === JSON.stringify(["22"]), `ssh → ${chips.join(",")}`);
setSpec("80,443");
ok(Boolean($("ports-out").querySelector(".err")), "80,443 → błąd, nie lista portów");
console.log(`  komunikat: ${$("ports-out").querySelector(".err")?.textContent}`);
setSpec("99999");
ok(/too large/.test($("ports-out").textContent), "99999 → port number too large");

console.log("\n== tabela kodów wyjścia ==");
ok($("exit-rows").querySelectorAll("tr").length >= 7, `${$("exit-rows").querySelectorAll("tr").length} wierszy`);

console.log("\n== panel CI ==");
ok($("ci-out").textContent.trim().length > 0, "panel CI czymś wypełniony");

console.log("\n== konsola jsdom ==");
const real = logs.filter(([, m]) => !/Could not parse CSS|Error: Not implemented/.test(String(m)));
for (const [kind, m] of real.slice(0, 8)) console.log(`  ${kind}: ${String(m).slice(0, 220)}`);
ok(real.length === 0, `${real.length} błędów JS (poza ostrzeżeniami CSS jsdom-a)`);

console.log(
  fail.length
    ? `\nNIEPOWODZENIA (${fail.length}):\n` + fail.map((f) => "  - " + f).join("\n")
    : "\nWszystko przeszło."
);
process.exit(fail.length ? 1 : 0);
