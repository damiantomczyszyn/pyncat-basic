#!/usr/bin/env python3
"""Benchmark: pyncat (Python) kontra pyncat_ref (C) kontra nc(1).

Metodologia — żeby liczby z ``docs/c-vs-python.md`` dały się odtworzyć:

* **Transfer idzie przez loopback**, między dwoma procesami tej samej
  implementacji. Mierzymy czas od uruchomienia klienta do zakończenia obu
  procesów.
* **Źródło danych leży w tmpfs** (``/dev/shm``), a ujście to ``/dev/null``.
  Dysku nie ma w pomiarze; mierzymy pętlę relay, nie I/O blokowe.
* **Mediana z N powtórzeń**, po jednym przebiegu rozgrzewającym, który jest
  odrzucany. Podajemy też min i max, bo na współdzielonym CPU rozrzut jest
  informacją, a nie szumem do ukrycia.
* **Narzut startu** mierzymy osobno (``-h``), bo dla interpretera Pythona to
  pozycja porównywalna z całym transferem małego pliku — i mieszanie tego
  z przepustowością zaciera obraz.
* **Eksperyment z BUFSIZE** odpowiada na pytanie, co dominuje w czasie.
  W Pythonie stałą podmieniamy po imporcie (``--bufsize``), w C rekompilujemy
  z ``-DBUFSIZE=``. To ten sam parametr w obu językach.
* **RSS** bierzemy z ``os.wait4()``, czyli ``ru_maxrss`` konkretnego procesu
  potomnego — nie z sumy po wszystkich dzieciach.

Użycie::

    python3 ci/bench.py                 # pełny przebieg
    python3 ci/bench.py --quick         # skrócony, do CI
    python3 ci/bench.py --json out.json # zapis wyników
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

HOST = "127.0.0.1"
C_REF = REPO_ROOT / "c_reference" / "pyncat_ref"
SHM = Path("/dev/shm")

#: Podmiana ``BUFSIZE`` po imporcie. ``relay`` i ``netio`` importują stałą
#: przez ``from .constants import BUFSIZE``, więc tworzą własne nazwy
#: modułowe — trzeba podmienić wszystkie trzy.
BUFSIZE_RUNNER = (
    "import sys;"
    "n=int(sys.argv[1]);"
    "import pyncat.constants as c, pyncat.netio as io_, pyncat.relay as r;"
    "c.BUFSIZE=io_.BUFSIZE=r.BUFSIZE=n;"
    "from pyncat.app import main;"
    "sys.exit(main(sys.argv[2:]))"
)


def find_nc() -> str | None:
    for candidate in ("/usr/bin/nc.openbsd", "/usr/bin/nc"):
        if not os.path.exists(candidate):
            continue
        out = subprocess.run(
            [candidate, "-h"], capture_output=True, text=True, timeout=10
        )
        if "OpenBSD netcat" in (out.stdout or "") + (out.stderr or ""):
            return candidate
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def wait_until_bound(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            try:
                probe.bind((HOST, port))
            except OSError:
                return True
        time.sleep(0.01)
    return False


# --------------------------------------------------------------------------
# Definicje implementacji
# --------------------------------------------------------------------------


class Impl:
    """Jedna implementacja: jak ją uruchomić jako klienta i jako serwer."""

    def __init__(self, name: str, base: list[str], listen_uses_p: bool = False):
        self.name = name
        self.base = base
        self.listen_uses_p = listen_uses_p

    def client(self, port: int, *extra: str) -> list[str]:
        return [*self.base, "-N", "-w", "60", *extra, HOST, str(port)]

    def listen(self, port: int, *extra: str) -> list[str]:
        if self.listen_uses_p:
            # Debianowy nc przyjmuje `-l -p PORT`; upstream (i pyncat) nie.
            return [*self.base, "-l", "-p", str(port), *extra]
        return [*self.base, "-l", *extra, str(port)]

    def help_cmd(self) -> list[str]:
        return [*self.base, "-h"]


def build_impls(bufsize: int | None = None) -> list[Impl]:
    impls = []
    if bufsize is None:
        impls.append(Impl("python", [sys.executable, "-m", "pyncat"]))
    else:
        impls.append(
            Impl("python", [sys.executable, "-c", BUFSIZE_RUNNER, str(bufsize)])
        )
    if C_REF.exists():
        impls.append(Impl("c", [str(C_REF)]))
    nc = find_nc()
    if nc:
        impls.append(Impl("nc", [nc], listen_uses_p=True))
    return impls


# --------------------------------------------------------------------------
# Pomiary
# --------------------------------------------------------------------------


def make_blob(size_mib: int) -> Path:
    """Dane źródłowe w tmpfs. Zawartość nieściśliwa, żeby nic po drodze
    nie mogło oszukać (loopback nie kompresuje, ale lepiej nie zakładać)."""
    path = SHM / f"pyncat_bench_{size_mib}MiB.bin"
    want = size_mib * (1 << 20)
    if path.exists() and path.stat().st_size == want:
        return path
    with path.open("wb") as fh:
        chunk = os.urandom(1 << 20)
        for _ in range(size_mib):
            fh.write(chunk)
    return path


def time_transfer(impl: Impl, blob: Path, timeout: float = 180.0) -> float:
    """Jeden transfer. Zwraca czas w sekundach albo podnosi wyjątek."""
    port = free_port()
    devnull = subprocess.DEVNULL
    server = subprocess.Popen(
        impl.listen(port), stdin=devnull, stdout=devnull, stderr=devnull
    )
    try:
        if not wait_until_bound(port):
            raise RuntimeError(f"{impl.name}: serwer nie zbindował portu {port}")
        with blob.open("rb") as src:
            start = time.monotonic()
            client = subprocess.Popen(
                impl.client(port), stdin=src, stdout=devnull, stderr=devnull
            )
            rc = client.wait(timeout=timeout)
            server.wait(timeout=timeout)
            elapsed = time.monotonic() - start
        if rc != 0:
            raise RuntimeError(f"{impl.name}: klient zwrócił {rc}")
        return elapsed
    finally:
        for proc in (server,):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def peak_vmhwm(pid: int) -> int:
    """Szczytowy RSS procesu w KiB, z ``VmHWM`` w ``/proc/<pid>/status``.

    Dlaczego nie ``os.wait4()``/``ru_maxrss``: ``fork()`` z Pythona daje
    dziecku (przez copy-on-write) cały obraz pamięci interpretera, a
    ``ru_maxrss`` to MAKSIMUM w całym życiu procesu — czyli także przed
    ``exec()``. Pomiar w ten sposób pokazywał dla wersji w C równe 11 712 KiB,
    identycznie jak dla ``nc``, bo obie liczby były w rzeczywistości RSS-em
    mierzącego interpretera. ``VmHWM`` czytany z zewnątrz tej wady nie ma.
    """
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def stats(samples: list[float]) -> dict:
    return {
        "median": round(statistics.median(samples), 4),
        "min": round(min(samples), 4),
        "max": round(max(samples), 4),
        "n": len(samples),
    }


def bench_throughput(size_mib: int, reps: int) -> dict:
    blob = make_blob(size_mib)
    results = {}
    for impl in build_impls():
        samples = []
        try:
            time_transfer(impl, blob)  # rozgrzewka, odrzucana
            for _ in range(reps):
                samples.append(time_transfer(impl, blob))
        except Exception as exc:
            results[impl.name] = {"error": str(exc)}
            continue
        s = stats(samples)
        s["mib_per_s"] = round(size_mib / s["median"], 1)
        results[impl.name] = s
    return {"size_mib": size_mib, "reps": reps, "impls": results}


def bench_startup(reps: int) -> dict:
    results = {}
    for impl in build_impls():
        samples = []
        subprocess.run(impl.help_cmd(), capture_output=True, timeout=30)
        for _ in range(reps):
            start = time.monotonic()
            subprocess.run(impl.help_cmd(), capture_output=True, timeout=30)
            samples.append(time.monotonic() - start)
        results[impl.name] = stats(samples)
    return {"reps": reps, "impls": results}


def bench_scan(nports: int, reps: int) -> dict:
    """Skan ``-z`` po zamkniętych portach. Mierzy narzut na port:
    ``socket()`` + ``connect()`` + ``poll()`` + ``close()``, bez transferu."""
    base = free_port()
    spec = f"{base}-{base + nports - 1}"
    results = {}
    for impl in build_impls():
        argv = [*impl.base, "-z", "-w", "2", "-n", HOST, spec]
        samples = []
        subprocess.run(argv, capture_output=True, timeout=120)
        for _ in range(reps):
            start = time.monotonic()
            subprocess.run(argv, capture_output=True, timeout=120)
            samples.append(time.monotonic() - start)
        s = stats(samples)
        s["ports_per_s"] = round(nports / s["median"], 0)
        s["us_per_port"] = round(s["median"] / nports * 1e6, 1)
        results[impl.name] = s
    return {"nports": nports, "reps": reps, "impls": results}


def bench_bufsize(sizes: list[int], size_mib: int, reps: int) -> dict:
    """Przepustowość w funkcji BUFSIZE — odpowiedź na „co dominuje w czasie"."""
    blob = make_blob(size_mib)
    out: dict[str, dict] = {"python": {}, "c": {}}

    for bufsize in sizes:
        impl = build_impls(bufsize=bufsize)[0]
        samples = []
        try:
            time_transfer(impl, blob)
            for _ in range(reps):
                samples.append(time_transfer(impl, blob))
            s = stats(samples)
            s["mib_per_s"] = round(size_mib / s["median"], 1)
            out["python"][str(bufsize)] = s
        except Exception as exc:
            out["python"][str(bufsize)] = {"error": str(exc)}

    if shutil.which("cc") and (REPO_ROOT / "c_reference" / "pyncat_ref.c").exists():
        for bufsize in sizes:
            binary = REPO_ROOT / "c_reference" / f"pyncat_ref_bs{bufsize}"
            compile_cmd = [
                "cc",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                "-Werror",
                "-O2",
                f"-DBUFSIZE={bufsize}",
                "-o",
                str(binary),
                str(REPO_ROOT / "c_reference" / "pyncat_ref.c"),
            ]
            built = subprocess.run(compile_cmd, capture_output=True, text=True)
            if built.returncode != 0:
                out["c"][str(bufsize)] = {
                    "error": f"kompilacja: {built.stderr.strip()[:200]}"
                }
                continue
            impl = Impl("c", [str(binary)])
            samples = []
            try:
                time_transfer(impl, blob)
                for _ in range(reps):
                    samples.append(time_transfer(impl, blob))
                s = stats(samples)
                s["mib_per_s"] = round(size_mib / s["median"], 1)
                out["c"][str(bufsize)] = s
            except Exception as exc:
                out["c"][str(bufsize)] = {"error": str(exc)}
            finally:
                binary.unlink(missing_ok=True)
    else:  # pragma: no cover
        out["c"] = {"error": "brak kompilatora cc"}

    return {"sizes": sizes, "size_mib": size_mib, "reps": reps, "results": out}


def bench_rss(size_mib: int) -> dict:
    """Szczytowy RSS procesu SERWERA podczas transferu.

    Mierzymy serwer, nie klienta, bo serwer żyje przez cały transfer i można
    mu odpytywać ``/proc/<pid>/status`` z zewnątrz. Praca jest ta sama:
    serwer też przepuszcza każdy bajt przez ``fillbuf``/``drainbuf``.
    """
    import threading

    blob = make_blob(size_mib)
    results = {}
    for impl in build_impls():
        port = free_port()
        server = subprocess.Popen(
            impl.listen(port),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        peak = [0]
        stop = threading.Event()

        def sampler(
            pid: int = server.pid,
            stop_ev: threading.Event = stop,
            acc: list[int] = peak,
        ) -> None:
            # Argumenty domyślne wiążą wartości z TEJ iteracji pętli.
            # Bez tego closure patrzyłaby na nazwy z zakresu funkcji, które
            # następna iteracja nadpisuje (ruff B023).
            while not stop_ev.is_set():
                acc[0] = max(acc[0], peak_vmhwm(pid))
                time.sleep(0.002)

        watcher = threading.Thread(target=sampler, daemon=True)
        watcher.start()
        try:
            if not wait_until_bound(port):
                results[impl.name] = {"error": "serwer nie wstał"}
                continue
            with blob.open("rb") as src:
                client = subprocess.Popen(
                    impl.client(port),
                    stdin=src,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                client.wait(timeout=180)
            server.wait(timeout=180)
            results[impl.name] = {"maxrss_kib": peak[0]}
        finally:
            stop.set()
            watcher.join(timeout=2)
            if server.poll() is None:
                server.kill()
                server.wait(timeout=10)
    return {"size_mib": size_mib, "impls": results, "metric": "VmHWM serwera"}


def bench_syscalls(size_mib: int) -> dict:
    """Liczba syscalli na transfer — dowód, czy pętla jest syscall-bound.

    ``strace -c -f`` na kliencie. Interesują nas ``read``/``write``/``poll``:
    ich liczba powinna być w obu językach IDENTYCZNA, bo to ten sam algorytm
    i ten sam BUFSIZE. Jeśli się różni, to różnica projektowa, nie językowa.
    """
    if not shutil.which("strace"):  # pragma: no cover
        return {"error": "brak strace"}
    blob = make_blob(size_mib)
    results = {}
    for impl in build_impls():
        port = free_port()
        server = subprocess.Popen(
            impl.listen(port),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            if not wait_until_bound(port):
                results[impl.name] = {"error": "serwer nie wstał"}
                continue
            with blob.open("rb") as src:
                out = subprocess.run(
                    ["strace", "-c", "-f", *impl.client(port)],
                    stdin=src,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=300,
                )
            counts: dict[str, int] = {}
            for line in out.stderr.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[-1] in (
                    "read",
                    "write",
                    "poll",
                    "ppoll",
                    "sendto",
                    "recvfrom",
                ):
                    try:
                        counts[parts[-1]] = int(parts[-2])
                    except ValueError:
                        continue
            results[impl.name] = counts or {"error": "nie udało się sparsować"}
        finally:
            if server.poll() is None:
                server.kill()
                server.wait(timeout=10)
    return {"size_mib": size_mib, "impls": results}


def environment() -> dict:
    cpu = ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:  # pragma: no cover
        pass
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "cpu": cpu,
        "nproc": os.cpu_count(),
        "nc": find_nc(),
        "c_ref_built": C_REF.exists(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true", help="skrócony przebieg (CI)")
    ap.add_argument("--json", type=Path, help="zapisz wyniki do pliku JSON")
    args = ap.parse_args()

    # Rozmiar transferu jest dobrany tak, zeby narzut startu interpretera
    # (~57 ms, mierzony osobno nizej) byl marginalny. Przy 8 MiB i ~120 MiB/s
    # caly transfer trwa 64 ms, czyli MNIEJ niz sam start - taka liczba nie
    # mowi nic o petli relay. Przy 256 MiB start to okolo 2% czasu.
    if args.quick:
        size_mib, reps, scan_ports = 48, 3, 500
        bufsizes = [512, 4096, 16384, 65536]
    else:
        size_mib, reps, scan_ports = 256, 5, 2000
        bufsizes = [512, 2048, 8192, 16384, 65536, 262144]

    report: dict = {"environment": environment()}

    print("# Środowisko", flush=True)
    for key, value in report["environment"].items():
        print(f"  {key}: {value}", flush=True)

    print(f"\n# Przepustowość ({size_mib} MiB, {reps} powtórzeń)", flush=True)
    report["throughput"] = bench_throughput(size_mib, reps)
    for name, res in report["throughput"]["impls"].items():
        if "error" in res:
            print(f"  {name:8s} BŁĄD: {res['error']}", flush=True)
        else:
            print(
                f"  {name:8s} {res['median']:7.3f} s  "
                f"{res['mib_per_s']:7.1f} MiB/s  "
                f"(min {res['min']:.3f} max {res['max']:.3f})",
                flush=True,
            )

    print(f"\n# Narzut startu (-h, {reps * 3} powtórzeń)", flush=True)
    report["startup"] = bench_startup(reps * 3)
    for name, res in report["startup"]["impls"].items():
        print(f"  {name:8s} {res['median'] * 1000:7.1f} ms", flush=True)

    # Przepustowość samej pętli relay: czas transferu minus narzut startu.
    # Bez tej korekty porównujemy "transfer + start interpretera" z
    # "transfer + start procesu w C", a to dwie różne rzeczy zsumowane w jedną.
    print("\n# Przepustowość skorygowana o narzut startu", flush=True)
    for name, res in report["throughput"]["impls"].items():
        startup = report["startup"]["impls"].get(name, {}).get("median")
        if "error" in res or startup is None:
            continue
        net = res["median"] - startup
        if net <= 0:  # pragma: no cover
            continue
        res["median_minus_startup"] = round(net, 4)
        res["mib_per_s_corrected"] = round(size_mib / net, 1)
        print(
            f"  {name:8s} {net:7.3f} s  {res['mib_per_s_corrected']:7.1f} MiB/s",
            flush=True,
        )

    print(f"\n# Skan -z ({scan_ports} zamkniętych portów)", flush=True)
    report["scan"] = bench_scan(scan_ports, max(2, reps // 2))
    for name, res in report["scan"]["impls"].items():
        print(
            f"  {name:8s} {res['median']:7.3f} s  "
            f"{res['us_per_port']:7.1f} µs/port",
            flush=True,
        )

    print(f"\n# RSS klienta ({size_mib} MiB transfer)", flush=True)
    report["rss"] = bench_rss(size_mib)
    for name, res in report["rss"]["impls"].items():
        if "error" in res:
            print(f"  {name:8s} BŁĄD: {res['error']}", flush=True)
        else:
            print(f"  {name:8s} {res['maxrss_kib']:8d} KiB", flush=True)

    print(f"\n# Syscalle na {size_mib} MiB", flush=True)
    report["syscalls"] = bench_syscalls(size_mib)
    if "error" in report["syscalls"]:
        print(f"  {report['syscalls']['error']}", flush=True)
    else:
        for name, res in report["syscalls"]["impls"].items():
            print(f"  {name:8s} {res}", flush=True)

    print(
        f"\n# BUFSIZE vs przepustowość ({size_mib} MiB, "
        f"{max(2, reps // 2)} powtórzeń)",
        flush=True,
    )
    report["bufsize"] = bench_bufsize(bufsizes, size_mib, max(2, reps // 2))
    for lang in ("python", "c"):
        block = report["bufsize"]["results"].get(lang, {})
        if "error" in block:
            print(f"  {lang}: {block['error']}", flush=True)
            continue
        for bufsize, res in block.items():
            if "error" in res:
                print(f"  {lang:7s} {bufsize:>7s} B  BŁĄD: {res['error']}", flush=True)
            else:
                print(
                    f"  {lang:7s} {bufsize:>7s} B  "
                    f"{res['median']:7.3f} s  {res['mib_per_s']:7.1f} MiB/s",
                    flush=True,
                )

    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"\nWyniki zapisane: {args.json}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
