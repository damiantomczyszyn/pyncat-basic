#!/usr/bin/env python3
"""Realny test sieciowy: pyncat i wersja C przeciwko prawdziwym usługom.

Po co to istnieje
=================

Testy w ``tests/`` chodzą po loopbacku. Loopback jest **skrótem**: jądro
nie buduje ramek ethernetowych, nie robi ARP-a, nie przechodzi przez
routing ani przez MTU. Implementacja może działać po ``127.0.0.1`` i psuć
się na prawdziwym interfejsie — na przykład przez błędną obsługę zapisów
częściowych, które na loopbacku prawie nie występują, bo bufory są ogromne.

Ten skrypt stawia usługi w kontenerach i łączy się z nimi **po adresie
kontenera na mostku** ``docker0``, czyli przez prawdziwy ARP, bridge
i routing jądra.

**Każda rozbieżność z oczekiwanym wynikiem kończy proces kodem ≠ 0.**
Skrypt nie loguje ostrzeżeń „do przejrzenia" — albo sieć zachowuje się jak
trzeba, albo build pada.

Dwa backendy
============

``--backend docker``   (domyślny w CI) — kontenery + adresy na mostku.
``--backend loopback`` — te same asercje przeciwko listenerom uruchomionym
                         lokalnie w tym procesie.

Backend ``loopback`` **nie zastępuje** testu sieciowego: nie ma w nim ARP-a
ani routingu. Służy do zweryfikowania, że *logika tego skryptu* jest
poprawna (asercje, parsowanie, ścieżki porażki), żeby CI nie padało na
literówce. Został użyty dokładnie do tego podczas pisania, bo w środowisku
autorskim nie było ani demona Dockera, ani polecenia ``ip`` do postawienia
network namespace — więc **ścieżka kontenerowa była pierwszy raz uruchomiona
w CI, nie lokalnie.**
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
C_REF = REPO_ROOT / "c_reference" / "pyncat_ref"
PYNCAT = [sys.executable, "-m", "pyncat"]

HTTP_REQUEST = b"GET / HTTP/1.0\r\nHost: test\r\nConnection: close\r\n\r\n"
REDIS_PING = b"PING\r\n"
UDP_PROBE = b"HELLO-PYNCAT"


# --------------------------------------------------------------------------
# Raportowanie
# --------------------------------------------------------------------------


@dataclass
class Result:
    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append(Result(name, ok, detail))
        mark = "OK  " if ok else "BŁĄD"
        print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)

    @property
    def failed(self) -> list[Result]:
        return [r for r in self.results if not r.ok]


# --------------------------------------------------------------------------
# Uruchamianie klientów
# --------------------------------------------------------------------------


def run_client(
    impl: list[str],
    args: list[str],
    stdin_data: bytes = b"",
    timeout: float = 30.0,
) -> tuple[int, bytes, str]:
    """Uruchom klienta i zwróć ``(kod, stdout, stderr)``."""
    try:
        proc = subprocess.run(
            [*impl, *args],
            input=stdin_data,
            capture_output=True,
            timeout=timeout,
            cwd=REPO_ROOT,
        )
    except subprocess.TimeoutExpired:
        return 124, b"", f"timeout po {timeout} s"
    return proc.returncode, proc.stdout, proc.stderr.decode(errors="replace")


# --------------------------------------------------------------------------
# Backend: docker
# --------------------------------------------------------------------------


def docker(*args: str, check: bool = True, timeout: float = 300.0) -> str:
    proc = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(args)} → {proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def container_ip(name: str) -> str:
    fmt = "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"
    out = docker("inspect", "-f", fmt, name)
    ip = out.strip()
    if not ip:
        raise RuntimeError(f"kontener {name} nie ma adresu IP")
    return ip


def wait_tcp(ip: str, port: int, timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((ip, port), timeout=2):
                return True
        except OSError:
            time.sleep(0.4)
    return False


UDP_ECHO_SCRIPT = (
    "import socket;"
    "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
    "s.bind(('0.0.0.0',9999));"
    "print('udp echo gotowy',flush=True);"
    "[s.sendto(b'ECHO:'+d,a) for d,a in iter(lambda:s.recvfrom(4096),None)]"
)


class DockerBackend:
    """nginx (HTTP), redis (protokół tekstowy), echo UDP w kontenerze."""

    names = ("pyncat-nginx", "pyncat-redis", "pyncat-udpecho")

    def __init__(self) -> None:
        self.ips: dict[str, str] = {}

    def start(self) -> None:
        self.cleanup()
        print("# Stawianie kontenerów", flush=True)
        docker("run", "-d", "--name", "pyncat-nginx", "nginx:alpine")
        docker("run", "-d", "--name", "pyncat-redis", "redis:alpine")
        docker(
            "run", "-d", "--name", "pyncat-udpecho",
            "python:3.12-alpine", "python", "-u", "-c", UDP_ECHO_SCRIPT,
        )
        for name in self.names:
            self.ips[name] = container_ip(name)
            print(f"  {name}: {self.ips[name]}", flush=True)

        if not wait_tcp(self.ips["pyncat-nginx"], 80):
            raise RuntimeError("nginx nie wstał")
        if not wait_tcp(self.ips["pyncat-redis"], 6379):
            raise RuntimeError("redis nie wstał")
        # Echo UDP nie da się sprawdzić przez connect(); czekamy na log.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if "udp echo gotowy" in docker("logs", "pyncat-udpecho", check=False):
                break
            time.sleep(0.4)
        else:
            raise RuntimeError("echo UDP nie wstało")

        # Dowód, że NIE idziemy po loopbacku.
        for name, ip in self.ips.items():
            if ip.startswith("127."):
                raise RuntimeError(
                    f"{name} ma adres loopback ({ip}) — test sieciowy byłby fikcją"
                )

    def cleanup(self) -> None:
        for name in self.names:
            docker("rm", "-f", name, check=False, timeout=120)

    @property
    def http(self) -> tuple[str, int]:
        return self.ips["pyncat-nginx"], 80

    @property
    def redis(self) -> tuple[str, int]:
        return self.ips["pyncat-redis"], 6379

    @property
    def udp(self) -> tuple[str, int]:
        return self.ips["pyncat-udpecho"], 9999

    @property
    def closed_host(self) -> str:
        return self.ips["pyncat-nginx"]


# --------------------------------------------------------------------------
# Backend: loopback (weryfikacja logiki skryptu)
# --------------------------------------------------------------------------


class LoopbackBackend:
    """Te same asercje, ale listenery w wątkach tego procesu.

    Nie ma tu ARP-a ani routingu — zobacz docstring modułu.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._http_port = 0
        self._redis_port = 0
        self._udp_port = 0

    @staticmethod
    def _bind_tcp() -> socket.socket:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(8)
        return s

    def _serve_tcp(self, sock: socket.socket, responder) -> None:
        sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = sock.accept()
            except (TimeoutError, OSError):
                continue
            with conn:
                conn.settimeout(5)
                try:
                    data = conn.recv(65536)
                    conn.sendall(responder(data))
                    conn.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
        sock.close()

    def start(self) -> None:
        print("# Listenery lokalne (backend loopback)", flush=True)
        http_sock = self._bind_tcp()
        self._http_port = http_sock.getsockname()[1]
        body = b"<html><head><title>Welcome to nginx!</title></head></html>"
        http_sock_resp = (
            b"HTTP/1.1 200 OK\r\nServer: nginx/1.27.0\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )

        redis_sock = self._bind_tcp()
        self._redis_port = redis_sock.getsockname()[1]

        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_sock.bind(("127.0.0.1", 0))
        self._udp_port = udp_sock.getsockname()[1]

        def udp_echo() -> None:
            udp_sock.settimeout(0.5)
            while not self._stop.is_set():
                try:
                    data, addr = udp_sock.recvfrom(4096)
                except (TimeoutError, OSError):
                    continue
                try:
                    udp_sock.sendto(b"ECHO:" + data, addr)
                except OSError:
                    pass
            udp_sock.close()

        for target, args in (
            (self._serve_tcp, (http_sock, lambda _d: http_sock_resp)),
            (self._serve_tcp, (redis_sock, lambda _d: b"+PONG\r\n")),
            (udp_echo, ()),
        ):
            t = threading.Thread(target=target, args=args, daemon=True)
            t.start()
            self._threads.append(t)

        print(
            f"  http:{self._http_port} redis:{self._redis_port} "
            f"udp:{self._udp_port}",
            flush=True,
        )

    def cleanup(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3)

    @property
    def http(self) -> tuple[str, int]:
        return "127.0.0.1", self._http_port

    @property
    def redis(self) -> tuple[str, int]:
        return "127.0.0.1", self._redis_port

    @property
    def udp(self) -> tuple[str, int]:
        return "127.0.0.1", self._udp_port

    @property
    def closed_host(self) -> str:
        return "127.0.0.1"


# --------------------------------------------------------------------------
# Asercje
# --------------------------------------------------------------------------


def free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def check_impl(label: str, impl: list[str], backend, report: Report) -> None:
    print(f"\n# {label}", flush=True)

    http_host, http_port = backend.http
    redis_host, redis_port = backend.redis
    udp_host, udp_port = backend.udp

    # --- stan portów ---
    rc, _out, err = run_client(
        impl, ["-z", "-v", "-w", "10", "-n", http_host, str(http_port)]
    )
    report.add(
        f"{label}: -z na otwartym porcie TCP {http_host}:{http_port} → exit 0",
        rc == 0,
        f"exit={rc} stderr={err.strip()[:120]}",
    )

    closed = free_port()
    rc, _out, _err = run_client(
        impl, ["-z", "-w", "10", "-n", backend.closed_host, str(closed)]
    )
    report.add(
        f"{label}: -z na zamkniętym porcie TCP {closed} → exit 1",
        rc == 1,
        f"exit={rc}",
    )

    # --- realny transfer danych: HTTP ---
    rc, out, err = run_client(
        impl,
        ["-N", "-w", "20", "-n", http_host, str(http_port)],
        stdin_data=HTTP_REQUEST,
    )
    ok = rc == 0 and b"HTTP/1." in out and b" 200 " in out and b"nginx" in out.lower()
    if out:
        detail = (
            f"exit={rc} {len(out)} B, pierwsza linia={out.splitlines()[0][:60]!r}"
        )
    else:
        detail = f"exit={rc} pusto, stderr={err.strip()[:120]}"
    report.add(
        f"{label}: HTTP GET przez sieć → odpowiedź 200 od nginxa", ok, detail
    )

    # --- realny transfer danych: protokół tekstowy redisa ---
    rc, out, err = run_client(
        impl,
        ["-N", "-w", "20", "-n", redis_host, str(redis_port)],
        stdin_data=REDIS_PING,
    )
    report.add(
        f"{label}: redis PING przez sieć → +PONG",
        rc == 0 and out.startswith(b"+PONG"),
        f"exit={rc} out={out[:40]!r} stderr={err.strip()[:120]}",
    )

    # --- UDP: round-trip ---
    rc, out, err = run_client(
        impl,
        ["-u", "-w", "6", "-n", udp_host, str(udp_port)],
        stdin_data=UDP_PROBE,
        timeout=40,
    )
    report.add(
        f"{label}: UDP round-trip przez sieć → ECHO:{UDP_PROBE.decode()}",
        out.startswith(b"ECHO:" + UDP_PROBE),
        f"exit={rc} out={out[:48]!r} stderr={err.strip()[:120]}",
    )

    # --- UDP: stan portu ---
    rc, _out, err = run_client(
        impl, ["-u", "-z", "-v", "-w", "10", "-n", udp_host, str(udp_port)]
    )
    report.add(
        f"{label}: -u -z na otwartym porcie UDP → exit 0",
        rc == 0,
        f"exit={rc} stderr={err.strip()[:120]}",
    )

    udp_closed = free_port(socket.SOCK_DGRAM)
    rc, _out, err = run_client(
        impl, ["-u", "-z", "-w", "10", "-n", udp_host, str(udp_closed)]
    )
    # udptest() opiera się na ICMP port unreachable. Jeśli to padnie, znaczy,
    # że ICMP nie wraca — zobacz docs/tablice-decyzyjne.md §6.
    report.add(
        f"{label}: -u -z na zamkniętym porcie UDP {udp_closed} → exit 1 "
        f"(wymaga ICMP port unreachable)",
        rc == 1,
        f"exit={rc} stderr={err.strip()[:120]}",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--backend",
        choices=("docker", "loopback"),
        default="docker",
        help="docker = realna sieć przez mostek; loopback = weryfikacja logiki",
    )
    ap.add_argument("--json", type=Path, help="zapisz wynik do pliku JSON")
    ap.add_argument(
        "--skip-c",
        action="store_true",
        help="pomiń wersję C (gdy nie zbudowana)",
    )
    args = ap.parse_args()

    backend = DockerBackend() if args.backend == "docker" else LoopbackBackend()
    report = Report()

    try:
        backend.start()
    except Exception as exc:
        print(f"\nNIE UDAŁO SIĘ POSTAWIĆ USŁUG: {exc}", file=sys.stderr)
        print(
            "To jest porażka testu, nie powód do pominięcia go.",
            file=sys.stderr,
        )
        if isinstance(backend, DockerBackend):
            backend.cleanup()
        return 2

    try:
        check_impl("pyncat (Python)", PYNCAT, backend, report)
        if C_REF.exists() and not args.skip_c:
            check_impl("pyncat_ref (C)", [str(C_REF)], backend, report)
        else:
            print("\n# pyncat_ref (C) pominięty — brak binarki", flush=True)
            report.add(
                "wersja C zbudowana",
                args.skip_c,
                "brak c_reference/pyncat_ref",
            )
    finally:
        backend.cleanup()

    print(
        f"\n# Podsumowanie: {len(report.results) - len(report.failed)}"
        f"/{len(report.results)} przeszło",
        flush=True,
    )
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "backend": args.backend,
                    "results": [
                        {"name": r.name, "ok": r.ok, "detail": r.detail}
                        for r in report.results
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    if report.failed:
        print("\nNIEZGODNOŚCI:", file=sys.stderr)
        for r in report.failed:
            print(f"  - {r.name}: {r.detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
