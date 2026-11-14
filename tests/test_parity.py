"""Parzystość implementacji: Python i C muszą zachowywać się identycznie.

To jest test, który pilnuje głównej tezy projektu. `docs/c-vs-python.md`
twierdzi, że różnice w czasie i w kodzie są różnicami **językowymi**, a nie
projektowymi. Teza jest prawdziwa tylko wtedy, gdy oba programy robią
dokładnie to samo — więc to trzeba sprawdzać, a nie zakładać.

Historia, która uzasadnia istnienie tego pliku: pierwsza wersja
`c_reference/pyncat_ref.c` **rozchodziła się** z `pyncat` w trzech miejscach
naraz:

1. brakowało walidacji `-s`/`-p` razem z `-l` (C przyjmował `-l -p PORT`
   w wariancie debianowym, Python odrzucał w wariancie upstreamowym),
2. `strtoport()` szedł za Debianem (`port number invalid: nosuchsvc`),
   a Python za upstreamem (`service "nosuchsvc" unknown`),
3. komunikaty były po angielsku, a w Pythonie po polsku.

Żaden z testów interop tego nie łapał, bo one porównują **przesłane bajty**,
a nie diagnostykę. Stąd ten plik.
"""

from __future__ import annotations

import socket
import subprocess
import sys

import pytest
from conftest import (
    C_REF_BIN,
    HOST,
    PYNCAT_CMD,
    free_port,
    requires_c_ref,
    wait_until_bound,
)

pytestmark = [
    requires_c_ref,
    pytest.mark.skipif(
        sys.platform != "linux", reason="testy parzystości tylko na Linuksie"
    ),
]


def _run(cmd: list[str], args: list[str], stdin: bytes = b"") -> tuple[int, str]:
    """Uruchom i zwróć ``(kod, pierwsza linia stderr bez prefiksu progname)``."""
    proc = subprocess.run(
        [*cmd, *args], input=stdin, capture_output=True, timeout=30
    )
    err = proc.stderr.decode(errors="replace")
    first = err.splitlines()[0] if err.splitlines() else ""
    for prefix in ("pyncat: ", "pyncat_ref: "):
        if first.startswith(prefix):
            first = first[len(prefix) :]
            break
    return proc.returncode, first


#: Przypadki, w których OBA programy mają zawieść z tym samym komunikatem.
#: Prefiks nazwy programu jest obcinany — to jedyna dopuszczalna różnica.
BLEDY = [
    pytest.param(["-l", "-p", "5", "1234"], id="l+p"),
    pytest.param(["-l", "-s", HOST, "1234"], id="l+s"),
    pytest.param(["-l", "-z", "1234"], id="l+z"),
    pytest.param(["-k", HOST, "80"], id="k-bez-l"),
    pytest.param([HOST, "99999"], id="port-za-duzy"),
    pytest.param([HOST, "0"], id="port-za-maly"),
    pytest.param([HOST, "nie-ma-takiej-uslugi-xyz"], id="nieznana-usluga"),
    pytest.param([HOST, "80,443"], id="przecinek"),
    pytest.param(["-l"], id="l-bez-portu"),
    pytest.param([], id="bez-argumentow"),
    pytest.param([HOST], id="host-bez-portu"),
    pytest.param(["-i", "x", HOST, "80"], id="i-niepoprawne"),
    pytest.param(["-W", "0", HOST, "80"], id="W-zero"),
    pytest.param(["-q", "z", HOST, "80"], id="q-niepoprawne"),
    pytest.param(["-w", "abc", HOST, "80"], id="w-niepoprawne"),
    pytest.param(["-p", "70000", HOST, "80"], id="port-zrodlowy-za-duzy"),
]


@pytest.mark.parametrize("args", BLEDY)
def test_identyczna_diagnostyka(args: list[str]) -> None:
    """Ten sam komunikat i ten sam kod wyjścia w obu implementacjach."""
    rc_py, msg_py = _run(PYNCAT_CMD, args)
    rc_c, msg_c = _run([str(C_REF_BIN)], args)
    assert rc_py == rc_c, (
        f"różne kody wyjścia dla {args}: Python {rc_py}, C {rc_c}"
    )
    assert msg_py == msg_c, (
        f"różne komunikaty dla {args}:\n  Python: {msg_py!r}\n  C:      {msg_c!r}"
    )


def test_oba_odrzucaja_l_z_p() -> None:
    """Osobno, bo to najczęstsza praktyczna różnica wobec Debianowego nc.

    Debian przyjmuje `nc -l -p PORT` (idiom z połowy tutoriali). Upstream
    [netcat.c:428-429] tego zabrania i oba nasze programy idą za upstreamem.
    Jeśli ten test padnie, to znaczy, że jedna z implementacji zmieniła
    stronę — i trzeba poprawić `docs/tablice-decyzyjne.md` §9.5, nie test.
    """
    for cmd in (PYNCAT_CMD, [str(C_REF_BIN)]):
        rc, msg = _run(cmd, ["-l", "-p", "1234", "1234"])
        assert rc == 1
        assert "-p" in msg and "-l" in msg


@pytest.mark.parametrize(
    "spec",
    ["80", "20-25", "25-20", "ssh"],
    ids=["jeden", "zakres", "zakres-odwrotny", "nazwa-uslugi"],
)
def test_identyczna_lista_portow(spec: str) -> None:
    """Oba programy mają odwiedzić dokładnie te same porty w tej samej kolejności.

    Odczytujemy to z komunikatów `-v`: każda próba połączenia zostawia ślad
    z numerem portu.
    """

    def ports_visited(cmd: list[str]) -> list[str]:
        base = free_port()
        # Zakres przeliczamy na realnie wolne porty, żeby nie trafić
        # w cokolwiek, co słucha w systemie.
        if "-" in spec:
            lo, _, hi = spec.partition("-")
            width = abs(int(hi) - int(lo))
            arg = f"{base}-{base + width}" if int(lo) <= int(hi) else f"{base + width}-{base}"
        elif spec.isdigit():
            arg = str(base)
        else:
            arg = spec
        proc = subprocess.run(
            [*cmd, "-z", "-v", "-w", "2", "-n", HOST, arg],
            capture_output=True,
            timeout=60,
        )
        err = proc.stderr.decode(errors="replace")
        out = []
        for line in err.splitlines():
            for token in line.replace(",", " ").split():
                if token.isdigit() and 1 <= int(token) <= 65535:
                    out.append(token)
                    break
        return out

    # Ta sama pula portów dla obu nie da się zagwarantować (free_port()
    # losuje), więc porównujemy LICZBĘ odwiedzonych portów i to, że
    # kolejność jest rosnąca — czyli strukturę, nie konkretne numery.
    py = ports_visited(PYNCAT_CMD)
    c = ports_visited([str(C_REF_BIN)])
    assert len(py) == len(c), (
        f"różna liczba odwiedzonych portów dla {spec!r}: "
        f"Python {len(py)} ({py}), C {len(c)} ({c})"
    )
    assert [int(x) for x in py] == sorted(int(x) for x in py)
    assert [int(x) for x in c] == sorted(int(x) for x in c)


def test_identyczne_kody_wyjscia_na_zywym_porcie() -> None:
    """`-z` na otwartym i zamkniętym porcie: 0 i 1 w obu."""
    # Serwer bez -k obsługuje DOKŁADNIE JEDNO połączenie i kończy pracę,
    # więc każdy klient potrzebuje własnego listenera. Pierwsza wersja tego
    # testu tego nie uwzględniała i oblewała wersję w C za to, że drugi
    # listener już nie istniał.
    for cmd in (PYNCAT_CMD, [str(C_REF_BIN)]):
        port = free_port()
        server = subprocess.Popen(
            [*PYNCAT_CMD, "-l", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert wait_until_bound(port)
            rc, _ = _run(cmd, ["-z", "-w", "5", "-n", HOST, str(port)])
            assert rc == 0, f"{cmd[0]} nie połączył się z otwartym portem"
        finally:
            if server.poll() is None:
                server.kill()
            server.wait(timeout=10)

    closed = free_port()
    codes = {}
    for name, cmd in (("py", PYNCAT_CMD), ("c", [str(C_REF_BIN)])):
        codes[name], _ = _run(cmd, ["-z", "-w", "5", "-n", HOST, str(closed)])
    assert codes["py"] == codes["c"] == 1


def test_identyczne_zachowanie_h() -> None:
    """`-h` kończy się kodem 0 w obu (wbrew `exit(1)` z upstreamu)."""
    for cmd in (PYNCAT_CMD, [str(C_REF_BIN)]):
        proc = subprocess.run([*cmd, "-h"], capture_output=True, timeout=20)
        assert proc.returncode == 0, f"{cmd[0]} -h zwrócił {proc.returncode}"
        text = (proc.stdout + proc.stderr).decode(errors="replace")
        assert "OSTRZEŻENIE" in text, f"{cmd[0]} -h nie zawiera ostrzeżenia"


def test_zadna_implementacja_nie_uruchamia_procesow() -> None:
    """Granica projektu, sprawdzana po stronie C przez `nm`/`strings`.

    Dla Pythona to samo robi `tests/test_cli.py`. Tutaj szukamy w binarce
    symboli, które pozwoliłyby uruchomić program — gdyby ktoś kiedyś dopisał
    `-e`, ten test padnie.
    """
    out = subprocess.run(
        ["nm", "-u", str(C_REF_BIN)], capture_output=True, text=True, timeout=30
    )
    if out.returncode != 0:  # pragma: no cover — brak nm
        pytest.skip("nm niedostępne")
    forbidden = {
        "execl", "execle", "execlp", "execv", "execve", "execvp",
        "system", "popen", "fork", "vfork", "posix_spawn", "forkpty",
    }
    found = {
        sym
        for line in out.stdout.splitlines()
        for sym in [line.split("@")[0].split()[-1] if line.split() else ""]
        if sym in forbidden
    }
    assert not found, (
        f"pyncat_ref importuje symbole do uruchamiania procesów: {found}. "
        "pyncat nie ma prawa uruchamiać programów — zobacz README."
    )


def test_oba_maja_ten_sam_bufsize() -> None:
    """BUFSIZE musi być ten sam, inaczej benchmark porównuje dwie różne rzeczy.

    Liczymy syscalle `write` przy transferze o znanym rozmiarze: przy tym
    samym BUFSIZE musi ich być tyle samo.
    """
    import shutil

    if not shutil.which("strace"):
        pytest.skip("brak strace")

    payload = b"x" * (64 * 1024)  # 4 × BUFSIZE przy 16 KiB

    def write_count(cmd: list[str]) -> int:
        port = free_port()
        server = subprocess.Popen(
            [*PYNCAT_CMD, "-l", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert wait_until_bound(port)
            proc = subprocess.run(
                ["strace", "-c", "-f", *cmd, "-N", "-w", "20", HOST, str(port)],
                input=payload,
                capture_output=True,
                timeout=120,
            )
            server.wait(timeout=30)
        finally:
            if server.poll() is None:
                server.kill()
                server.wait(timeout=10)
        for line in proc.stderr.decode(errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[-1] == "write":
                return int(parts[-2])
        return -1

    py = write_count(PYNCAT_CMD)
    c = write_count([str(C_REF_BIN)])
    assert py > 0 and c > 0, f"nie udało się policzyć write(): py={py} c={c}"
    # 64 KiB / 16 KiB = 4 zapisy danych; tolerancja na zapisy diagnostyczne.
    assert abs(py - c) <= 1, (
        f"różna liczba write() przy tym samym transferze: Python {py}, C {c} "
        "— najprawdopodobniej różny BUFSIZE, co unieważnia benchmark"
    )


def test_socket_type_dla_udp_jest_taki_sam() -> None:
    """Oba programy mają używać SOCK_DGRAM dla `-u` — sprawdzane zachowaniem.

    Gdyby jeden z nich wysyłał UDP, a drugi TCP, listener UDP odebrałby
    dane tylko od jednego.
    """
    port = free_port(socket.SOCK_DGRAM)
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind((HOST, port))
    srv.settimeout(10)
    try:
        for name, cmd in (("py", PYNCAT_CMD), ("c", [str(C_REF_BIN)])):
            probe = f"UDP-{name}".encode()
            subprocess.run(
                [*cmd, "-u", "-w", "2", "-n", HOST, str(port)],
                input=probe,
                capture_output=True,
                timeout=30,
            )
            data, _addr = srv.recvfrom(4096)
            assert data == probe, f"{name}: odebrano {data!r}, oczekiwano {probe!r}"
    finally:
        srv.close()
