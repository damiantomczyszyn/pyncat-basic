"""Testy parsowania opcji i walidacji. Odpowiada ``netcat.c`` 176–480."""

from __future__ import annotations

import socket

import pytest

from pyncat.cli import Options, _HelpRequested, parse_args
from pyncat.report import FatalError, UsageError


class TestPozycyjne:
    """[netcat.c:356-364] „Cruft to make sure options are clean"."""

    def test_host_i_port(self) -> None:
        opts = parse_args(["example.com", "80"])
        assert (opts.host, opts.uport) == ("example.com", "80")

    def test_listen_tylko_port(self) -> None:
        opts = parse_args(["-l", "1234"])
        assert (opts.host, opts.uport) == (None, "1234")
        assert opts.lflag

    def test_listen_z_adresem(self) -> None:
        opts = parse_args(["-l", "127.0.0.1", "1234"])
        assert (opts.host, opts.uport) == ("127.0.0.1", "1234")

    def test_brak_argumentow(self) -> None:
        with pytest.raises(UsageError):
            parse_args([])

    def test_tylko_host_bez_portu(self) -> None:
        """``nc example.com`` bez portu to błąd użycia, nie domyślny port."""
        with pytest.raises(UsageError):
            parse_args(["example.com"])

    def test_za_duzo_argumentow(self) -> None:
        with pytest.raises(UsageError):
            parse_args(["a", "b", "c"])


class TestFlagi:
    def test_rodzina_adresow(self) -> None:
        assert parse_args(["-4", "h", "1"]).family == socket.AF_INET
        assert parse_args(["-6", "h", "1"]).family == socket.AF_INET6
        assert parse_args(["h", "1"]).family == socket.AF_UNSPEC

    def test_ostatnia_rodzina_wygrywa(self) -> None:
        """getopt nie scala flag — ostatnie wystąpienie nadpisuje."""
        assert parse_args(["-4", "-6", "h", "1"]).family == socket.AF_INET6

    def test_udp_wplywa_na_socktype(self) -> None:
        tcp = parse_args(["h", "1"])
        udp = parse_args(["-u", "h", "1"])
        assert (tcp.socktype, tcp.proto) == (
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
        )
        assert (udp.socktype, udp.proto) == (
            socket.SOCK_DGRAM,
            socket.IPPROTO_UDP,
        )

    def test_n_ustawia_ai_numerichost(self) -> None:
        """[netcat.c:477-479]"""
        assert parse_args(["-n", "h", "1"]).ai_flags & socket.AI_NUMERICHOST
        assert not parse_args(["h", "1"]).ai_flags & socket.AI_NUMERICHOST

    def test_w_jest_konwertowany_na_milisekundy(self) -> None:
        """[netcat.c:288-293] ``timeout *= 1000``"""
        assert parse_args(["-w", "5", "h", "1"]).timeout_ms == 5000

    def test_brak_w_to_brak_limitu(self) -> None:
        """[netcat.c:115] ``int timeout = -1`` → u nas ``None``."""
        assert parse_args(["h", "1"]).timeout_ms is None

    def test_w_zero_to_zero_milisekund_nie_brak_limitu(self) -> None:
        """Pułapka: ``-w 0`` to poll() bez blokowania, a NIE „czekaj wiecznie"."""
        assert parse_args(["-w", "0", "h", "1"]).timeout_ms == 0

    def test_q_przyjmuje_wartosc_ujemna(self) -> None:
        """Powód, dla którego używamy ``getopt``, a nie ``argparse``.

        ``-q -1`` to udokumentowana składnia netcata (czekaj bez końca).
        """
        assert parse_args(["-q", "-1", "h", "1"]).qflag == -1
        assert parse_args(["-q", "0", "h", "1"]).qflag == 0
        assert parse_args(["-q", "5", "h", "1"]).qflag == 5
        assert parse_args(["h", "1"]).qflag is None

    def test_klastrowanie_flag(self) -> None:
        """``getopt(3)`` scala flagi bez argumentów: ``-nvz`` == ``-n -v -z``."""
        opts = parse_args(["-nvz", "h", "1"])
        assert (opts.nflag, opts.vflag, opts.zflag) == (True, True, True)

    def test_wartosc_zbita_z_flaga(self) -> None:
        """``-w5`` == ``-w 5``, jak w C."""
        assert parse_args(["-w5", "h", "1"]).timeout_ms == 5000

    def test_W_wymaga_co_najmniej_jednego(self) -> None:
        """[netcat.c:283-287] ``strtonum(optarg, 1, INT_MAX)``"""
        assert parse_args(["-W", "1", "h", "1"]).recvlimit == 1
        with pytest.raises(FatalError, match="too small"):
            parse_args(["-W", "0", "h", "1"])

    def test_i_przyjmuje_zero(self) -> None:
        assert parse_args(["-i", "0", "h", "1"]).iflag == 0
        assert parse_args(["-i", "3", "h", "1"]).iflag == 3

    def test_bledne_wartosci_liczbowe(self) -> None:
        for argv in (
            ["-w", "abc", "h", "1"],
            ["-i", "x", "h", "1"],
            ["-W", "y", "h", "1"],
            ["-q", "z", "h", "1"],
        ):
            with pytest.raises(FatalError, match="invalid"):
                parse_args(argv)

    def test_h_zglasza_prosbe_o_pomoc(self) -> None:
        with pytest.raises(_HelpRequested):
            parse_args(["-h"])

    def test_nieznana_flaga(self) -> None:
        with pytest.raises(UsageError):
            parse_args(["-Q", "h", "1"])


class TestWalidacja:
    """[netcat.c:425-456] Wzajemna wykluczalność flag."""

    @pytest.mark.parametrize(
        ("argv", "fragment"),
        [
            (["-l", "-s", "127.0.0.1", "1"], "-s"),
            (["-l", "-p", "1234", "1"], "-p"),
            (["-l", "-z", "1"], "-z"),
            (["-k", "h", "1"], "-k"),
        ],
    )
    def test_zle_kombinacje(self, argv: list[str], fragment: str) -> None:
        with pytest.raises(FatalError) as exc:
            parse_args(argv)
        assert fragment in str(exc.value)

    def test_k_z_l_jest_ok(self) -> None:
        opts = parse_args(["-l", "-k", "1234"])
        assert opts.kflag and opts.lflag

    def test_zly_port_zrodlowy(self) -> None:
        with pytest.raises(FatalError):
            parse_args(["-p", "nie-ma-takiej-uslugi-xyz", "h", "1"])
        with pytest.raises(FatalError, match="too large"):
            parse_args(["-p", "70000", "h", "1"])

    def test_port_zrodlowy_jako_nazwa_uslugi(self) -> None:
        opts = parse_args(["-p", "ssh", "h", "1"])
        assert opts.pflag == "ssh"


class TestPominieteFlagi:
    """Pominięte flagi mają dawać komunikat „dlaczego", nie „nieznana opcja"."""

    @pytest.mark.parametrize(
        ("flag", "fragment"),
        [
            (["-c"], "TLS"),
            (["-x", "1.2.3.4:1080"], "proxy"),
            (["-U"], "uniksowe"),
            (["-t"], "telnet"),
            (["-F"], "SCM_RIGHTS"),
            (["-S"], "MD5"),
            (["-T", "lowdelay"], "poza zakresem"),
        ],
    )
    def test_komunikat_wyjasnia_powod(
        self, flag: list[str], fragment: str
    ) -> None:
        with pytest.raises(FatalError) as exc:
            parse_args([*flag, "h", "1"])
        assert fragment in str(exc.value)

    def test_e_nie_jest_wykonaniem_polecenia(self) -> None:
        """Najważniejszy test w tym pliku.

        ``-e`` w netcacie 1.10 uruchamiało program i podłączało go do socketu.
        W OpenBSD ``-e`` oznacza coś zupełnie innego (oczekiwana nazwa
        w certyfikacie TLS) i jest przez nas pominięte. Komunikat musi
        rozwiewać tę pomyłkę, bo to jedyna flaga, której ktoś może tu szukać
        z nadzieją na reverse shella.
        """
        with pytest.raises(FatalError) as exc:
            parse_args(["-e", "/bin/sh", "h", "1"])
        message = str(exc.value)
        assert "NIE" in message
        assert "1.10" in message

    def test_nie_ma_zadnej_flagi_uruchamiajacej_program(self) -> None:
        """Granica projektu: żadna opcja nie przyjmuje polecenia do wykonania."""
        import inspect

        import pyncat.app
        import pyncat.cli
        import pyncat.endpoints
        import pyncat.relay

        for module in (pyncat.cli, pyncat.app, pyncat.relay, pyncat.endpoints):
            source = inspect.getsource(module)
            for forbidden in (
                "subprocess",
                "os.exec",
                "os.system",
                "os.popen",
                "os.fork",
                "os.spawn",
                "pty.",
            ):
                assert forbidden not in source, (
                    f"{module.__name__} zawiera {forbidden!r} — pyncat nie ma "
                    "prawa uruchamiać procesów"
                )


class TestOptionsDefaults:
    def test_domyslny_stan(self) -> None:
        opts = Options()
        assert not any(
            (
                opts.dflag,
                opts.kflag,
                opts.lflag,
                opts.Nflag,
                opts.nflag,
                opts.rflag,
                opts.uflag,
                opts.vflag,
                opts.zflag,
            )
        )
        assert opts.iflag == 0
        assert opts.recvlimit == 0
        assert opts.timeout_ms is None
        assert opts.qflag is None
