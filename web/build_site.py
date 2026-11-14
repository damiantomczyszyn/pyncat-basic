#!/usr/bin/env python3
"""Składa katalog strony demo do publikacji na GitHub Pages.

Użycie::

    python3 web/build_site.py site/
    python3 web/build_site.py site/ --ci-json ci-wynik.json
    python3 web/build_site.py --selftest          # bez budowania, tylko testy

Jak to działa
=============

Źródła Pythona **nie są kopiowane jako pliki ``.py``**, a wstrzykiwane do
``modules.js`` jako napisy, które ``app.js`` zapisuje do wirtualnego systemu
plików Pyodide. Trzy powody, te same co w projekcie bliźniaczym:

* niektóre hostingi statyczne nie serwują ``.py`` w ogóle (albo serwują
  z ``Content-Type: text/plain`` i mylącym cache'em),
* jedno żądanie zamiast sześciu,
* strona działa też z ``file://``, gdzie ``fetch()`` wywraca się na CORS.

Które moduły lecą do przeglądarki
=================================

Tylko te, które **naprawdę tam działają**. Lista jest krótka i to jest
sedno uczciwości tego demo:

============== ===================================================
``constants``  same ``#define`` — zero zależności systemowych
``report``     ``errx``/``warnx``; ``connection_info`` nieużywane
``netio``      ``fillbuf``/``drainbuf`` — ``os.read``/``os.write``
               podstawiane przez ``bridge.FakeOs``
``ports``      ``strtoport``/``build_ports`` — czyste parsowanie
``relay``      ``readwrite()`` — ``select.poll`` i ``time``
               podstawiane przez ``bridge``
``__init__``   tylko docstring + leniwy ``main()``; nic nie importuje
               na poziomie modułu, więc da się wciągnąć bez socketów
============== ===================================================

Celowo **nie ma** tu ``cli.py``, ``app.py`` ani ``endpoints.py``:
``endpoints`` to ``socket()``, ``connect()``, ``bind()``, ``listen()``,
``accept()`` — czyli dokładnie to, czego w przeglądarce nie ma i czego nie
da się uczciwie udawać. ``app.py`` i ``cli.py`` istnieją tylko po to, żeby
je zawołać. Gdyby trafiły na stronę, demo musiałoby kłamać.

Dlaczego build wczytuje źródła, a nie trzyma ich kopii
======================================================

``web/`` jest pisane niezależnie od ``pyncat/``. Gdyby demo miało własną
kopię maszyny stanów, rozjechałaby się przy pierwszej zmianie w ``relay.py``
i nikt by tego nie zauważył. Dlatego ``build_site.py`` czyta ``pyncat/*.py``
w czasie builda, a ``--selftest`` sprawdza, że scenariusze nadal dają
oczekiwane wyniki. CI odpala ``--selftest`` przed deployem, więc strona
nigdy nie publikuje maszyny stanów, która nie przeszła testów.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Moduły pakietu, które trafiają do przeglądarki. Kolejność bez znaczenia —
#: Pyodide importuje je normalnym mechanizmem importu z wirtualnego FS.
BROWSER_SAFE_MODULES = (
    "__init__",
    "constants",
    "report",
    "netio",
    "ports",
    "relay",
)

#: Moduły świadomie pominięte, razem z powodem. Lista jest wypisywana przez
#: build i pokazywana na stronie, żeby nie trzeba było zgadywać, czego nie ma.
EXCLUDED_MODULES = {
    "endpoints": "socket()/connect()/bind()/listen()/accept() — nie istnieją w WASM",
    "app": "ścieżka main(): nasłuch i pętla po portach, czyli same sockety",
    "cli": "parsowanie argv; demo nie ma linii poleceń",
}

STATIC_FILES = ("index.html", "app.js")

#: Oczekiwane wyniki scenariuszy — kontrakt sprawdzany przez ``--selftest``.
#: Wartości pochodzą z POMIARÓW na ``/usr/bin/nc`` (OpenBSD netcat, Debian
#: patchlevel 1.226), spisanych w GROUND_TRUTH; sekcje w komentarzach.
EXPECTED = {
    # GROUND_TRUTH §2.6: bez -N klient wisiał 7,701 s do zabicia
    "halfclose-bez-N": {"outcome": "deadlock", "stdout_len": 0, "net_tx_len": 4},
    # GROUND_TRUTH §2.6: z -N wyszedł po 0,008 s
    "halfclose-z-N": {"outcome": "return", "net_tx_len": 4},
    # GROUND_TRUTH §2.5: klient -W 2 odebrał dokładnie AB
    "W-limit": {"outcome": "return", "stdout": b"AB"},
    # GROUND_TRUTH §8: -q 2 → 2,007 s, DELAYED dolatuje
    "q-timer": {"outcome": "return", "stdout": b"DELAYED"},
    # GROUND_TRUTH §8: -q 0 → 0,007 s, DELAYED przepada
    "q-zero": {"outcome": "return", "stdout": b""},
    "transfer": {"outcome": "return", "net_tx_len": 18},
    "eof-z-sieci": {"outcome": "return", "stdout": b"banner\r\n"},
    "backpressure": {"outcome": "return", "net_tx_len": 40},
    "stdout-znikl": {"outcome": "return", "stdout": b"dane"},
}


def selftest() -> int:
    """Odpal wszystkie scenariusze na hostowym CPythonie i sprawdź wyniki.

    To nie jest test demo, to test **maszyny stanów**: scenariusze wołają
    prawdziwe ``relay.readwrite()``. Jeśli ktoś zmieni semantykę half-close
    albo ``-W``, ten test padnie — i słusznie.

    Uruchamiane na hoście, nie w Pyodide, bo wynik ma być ten sam: atrapy
    z ``bridge.py`` nie zależą od platformy. Zgodność samego Pyodide
    sprawdza osobny test węzłowy (``web/test_page.mjs``, jeśli istnieje).
    """
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "web"))
    try:
        import bridge
    except Exception as exc:
        print(
            f"selftest: nie da się zaimportować web/bridge.py: {exc!r}", file=sys.stderr
        )
        return 1

    names = [s["name"] for s in bridge.scenario_list()]
    failures = 0

    for name in names:
        result = bridge.run_scenario(name)
        if not result.get("ok"):
            print(f"  BŁĄD  {name}: {result.get('error')}")
            failures += 1
            continue

        want = EXPECTED.get(name)
        if want is None:
            print(f"  (bez kontraktu) {name}: outcome={result['outcome']}")
            continue

        problems = []
        if "outcome" in want and result["outcome"] != want["outcome"]:
            problems.append(
                f"outcome={result['outcome']!r}, oczekiwano {want['outcome']!r}"
            )
        if "stdout" in want:
            # ``run_scenario`` zwraca stdout skrócony do repr; do porównania
            # bierzemy długość i prefiks z pełnych danych.
            expected = want["stdout"]
            if result["stdout_len"] != len(expected):
                problems.append(
                    f"stdout_len={result['stdout_len']}, oczekiwano {len(expected)}"
                )
            elif repr(expected) not in result["stdout"]:
                problems.append(f"stdout={result['stdout']}, oczekiwano {expected!r}")
        for key in ("stdout_len", "net_tx_len"):
            if key in want and result[key] != want[key]:
                problems.append(f"{key}={result[key]}, oczekiwano {want[key]}")

        if problems:
            print(f"  BŁĄD  {name}: " + "; ".join(problems))
            failures += 1
        else:
            steps = len(result["steps"])
            refs = sum(1 for v in result["nc_refs"].values() if v)
            print(
                f"  ok    {name:18s} outcome={result['outcome']:8s} "
                f"kroków={steps:4d}  linii z odnośnikiem do netcat.c={refs}"
            )

    # Panel parsowania portów — bez atrap, więc warto sprawdzić osobno.
    bridge.install_etc_services()
    # UWAGA — rozjazd komunikatów między rewizjami, POMIAR na /usr/bin/nc
    # (OpenBSD netcat, Debian patchlevel 1.226-1ubuntu2):
    #
    #     $ nc -z -w 1 127.0.0.1 80,443      → nc: port number invalid: 80,443
    #     $ nc -z -w 1 127.0.0.1 nosuchsvc   → nc: port number invalid: nosuchsvc
    #     $ nc -z -w 1 127.0.0.1 99999       → nc: port number too large: 99999
    #     $ nc -z -w 1 127.0.0.1 0           → nc: port number too small: 0
    #     $ nc -z -w 1 127.0.0.1 20-25-30    → nc: port number invalid: 25-30
    #
    # Czyli 1.226 dla „nie liczba i nie znana usługa" mówi *port number
    # invalid*, a upstream 1.241 [netcat.c:1467-1470] rozdziela te przypadki:
    # ``errno != EINVAL`` → „port number %s: %s", a brak usługi → „service
    # \"%s\" unknown". ``pyncat/ports.py`` odwzorowuje 1.241, bo wszystkie
    # numery linii w projekcie odnoszą się do 1.241 — i dlatego poniżej
    # oczekujemy „unknown", nie „invalid". Rozjazd jest świadomy; jeśli
    # kiedyś ports.py pójdzie za binarium 1.226, ten test padnie i trzeba
    # będzie zmienić obie strony razem.
    port_cases = [
        ("22", 1, None),
        ("20-25", 6, None),
        ("25-20", 6, None),  # GROUND_TRUTH §6: swap lo/hi
        ("ssh", 1, None),  # ścieżka getservbyname
        ("http-alt", 1, None),  # minus w nazwie usługi ≠ zakres
        ("80,443", None, "unknown"),  # przecinki to błąd, nie lista portów
        ("20-25-30", None, "unknown"),  # split na PIERWSZYM minusie → hi="25-30"
        ("99999", None, "too large"),
        ("0", None, "too small"),
        ("nosuchsvc", None, "unknown"),
    ]
    for spec, count, err_substr in port_cases:
        res = bridge.parse_ports(spec)
        if err_substr is not None:
            if not res.get("error") or err_substr not in res["error"]:
                print(
                    f"  BŁĄD  ports({spec!r}): oczekiwano błędu z {err_substr!r}, dostano {res}"
                )
                failures += 1
            else:
                print(f"  ok    ports({spec!r:12s}) → {res['error']}")
        else:
            if res.get("error") or res.get("count") != count:
                print(
                    f"  BŁĄD  ports({spec!r}): oczekiwano {count} portów, dostano {res}"
                )
                failures += 1
            else:
                print(f"  ok    ports({spec!r:12s}) → {res['count']} portów")

    if failures:
        print(f"\nselftest: {failures} niepowodzeń", file=sys.stderr)
        return 1
    print(
        f"\nselftest: wszystko przeszło ({len(names)} scenariuszy, {len(port_cases)} testów portów)"
    )
    return 0


def build(out_dir: Path, ci_json: Path | None = None) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. statyczne pliki strony
    for name in STATIC_FILES:
        src = ROOT / "web" / name
        if not src.is_file():
            print(f"build_site: brak {src}", file=sys.stderr)
            return 1
        shutil.copy2(src, out_dir / name)

    # 2. źródła Pythona jako modules.js
    sources: dict[str, str] = {}
    for module in BROWSER_SAFE_MODULES:
        path = ROOT / "pyncat" / f"{module}.py"
        if not path.is_file():
            print(f"build_site: brak modułu {path}", file=sys.stderr)
            return 1
        sources[f"pyncat/{module}.py"] = path.read_text(encoding="utf-8")

    bridge_path = ROOT / "web" / "bridge.py"
    if not bridge_path.is_file():
        print(f"build_site: brak {bridge_path}", file=sys.stderr)
        return 1
    sources["bridge.py"] = bridge_path.read_text(encoding="utf-8")

    banner = (
        "/* Wygenerowane przez web/build_site.py — nie edytuj ręcznie.\n"
        " * Źródła Pythona wstrzyknięte jako napisy, żeby strona działała\n"
        " * też tam, gdzie hosting nie serwuje plików .py, i żeby demo\n"
        " * pokazywało TEN kod z repo, a nie jego kopię.\n"
        " */\n"
    )
    payload = {
        "sources": sources,
        "excluded": EXCLUDED_MODULES,
    }
    (out_dir / "modules.js").write_text(
        f"{banner}window.PYNCAT_SOURCES = {json.dumps(payload, ensure_ascii=False, indent=0)};\n",
        encoding="utf-8",
    )

    # 3. opcjonalny wynik realnego przebiegu z CI — odpowiednik „ostatniego
    #    realnego skanu" z projektu bliźniaczego. Dla netcata sensowną
    #    metryką jest transfer na loopbacku: suma kontrolna in == out.
    data_dir = out_dir / "data"
    data_dir.mkdir(exist_ok=True)
    if ci_json and ci_json.is_file():
        shutil.copy2(ci_json, data_dir / "latest-ci.json")
        print(f"build_site: dołączony wynik CI z {ci_json}")
    else:
        print("build_site: bez wyniku CI — strona pokaże odnośnik do Actions")

    # 4. Pages domyślnie przepuszcza treść przez Jekylla
    (out_dir / ".nojekyll").touch()

    total = sum(len(s) for s in sources.values())
    print(
        f"build_site: {len(sources)} plików źródłowych, "
        f"{total / 1024:.1f} KB (z czego relay.py "
        f"{len(sources['pyncat/relay.py']) / 1024:.1f} KB)"
    )
    for name, why in EXCLUDED_MODULES.items():
        print(f"  pominięty pyncat/{name}.py — {why}")
    for path in sorted(p for p in out_dir.rglob("*") if p.is_file()):
        print(f"  {path.relative_to(out_dir)}  ({path.stat().st_size} B)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Składa katalog strony demo pyncat-basic."
    )
    parser.add_argument(
        "out", type=Path, nargs="?", help="katalog wyjściowy (np. site/)"
    )
    parser.add_argument(
        "--ci-json", type=Path, help="JSON z wynikiem realnego przebiegu z CI"
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="odpal scenariusze i sprawdź kontrakt, nie buduj strony",
    )
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    if args.out is None:
        parser.error("podaj katalog wyjściowy albo --selftest")

    # Build zawsze przechodzi przez selftest: nie publikujemy strony, której
    # maszyna stanów nie daje oczekiwanych wyników.
    print("build_site: selftest…")
    rc = selftest()
    if rc != 0:
        print("build_site: selftest nie przeszedł — nie buduję", file=sys.stderr)
        return rc
    print()
    return build(args.out, args.ci_json)


if __name__ == "__main__":
    sys.exit(main())
