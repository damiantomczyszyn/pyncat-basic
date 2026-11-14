"""Testy parsowania portów. Odpowiada ``netcat.c`` 1455–1532."""

from __future__ import annotations

import socket

import pytest

from pyncat.ports import build_ports, strtonum, strtoport
from pyncat.report import FatalError


class TestStrtonum:
    """``strtonum(3)`` — trzy komunikaty błędu, które netcat wkleja w wyjście."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("1", 1), ("65535", 65535), ("80", 80), ("+80", 80), ("  80", 80)],
    )
    def test_poprawne(self, text: str, expected: int) -> None:
        value, err = strtonum(text, 1, 65535)
        assert (value, err) == (expected, None)

    @pytest.mark.parametrize(
        ("text", "err"),
        [
            ("0", "too small"),
            ("-1", "too small"),
            ("65536", "too large"),
            ("99999999", "too large"),
            ("abc", "invalid"),
            ("", "invalid"),
            ("80x", "invalid"),
            ("8 0", "invalid"),
            ("0x50", "invalid"),
            # Python int() przyjmuje podkreślenia, strtoll(3) nie — pilnujemy,
            # żeby nie wpuścić tej liberalności do parsera portów.
            ("1_0", "invalid"),
            # strtoll pomija wiodące białe znaki, ale nie końcowe.
            ("80  ", "invalid"),
        ],
    )
    def test_bledne(self, text: str, err: str) -> None:
        value, got = strtonum(text, 1, 65535)
        assert value is None
        assert got == err


class TestStrtoport:
    def test_liczba(self) -> None:
        assert strtoport("443", udp=False) == 443

    def test_nazwa_uslugi(self) -> None:
        # Jedna z nielicznych nazw, na którą można liczyć w każdym /etc/services.
        assert strtoport("ssh", udp=False) == 22

    def test_nazwa_uslugi_udp(self) -> None:
        assert strtoport("domain", udp=True) == 53

    def test_nieznana_usluga(self) -> None:
        with pytest.raises(FatalError, match="unknown"):
            strtoport("nie-ma-takiej-uslugi-xyz", udp=False)

    def test_przekroczony_zakres_nie_probuje_nazwy(self) -> None:
        """[netcat.c:1467-1468] Poza zakresem → błąd od razu.

        Kolejność ma znaczenie dla komunikatu: ``nc host 99999`` mówi
        „port number too large", a nie „service unknown".
        """
        with pytest.raises(FatalError, match="too large"):
            strtoport("99999", udp=False)
        with pytest.raises(FatalError, match="too small"):
            strtoport("0", udp=False)


class TestBuildPorts:
    def test_pojedynczy(self) -> None:
        assert build_ports("80", udp=False, randomize=False) == ["80"]

    def test_nazwa_uslugi_zamieniana_na_numer(self) -> None:
        assert build_ports("ssh", udp=False, randomize=False) == ["22"]

    def test_zakres(self) -> None:
        assert build_ports("20-25", udp=False, randomize=False) == [
            "20",
            "21",
            "22",
            "23",
            "24",
            "25",
        ]

    def test_zakres_jednoelementowy(self) -> None:
        assert build_ports("80-80", udp=False, randomize=False) == ["80"]

    def test_zakres_odwrotny_jest_zamieniany(self) -> None:
        """[netcat.c:1490-1494] ``lo > hi`` → swap.

        Zweryfikowane na nc.openbsd 1.226: ``nc -z -v 127.0.0.1 34830-34828``
        skanuje 34828, 34829, 34830 — rosnąco.
        """
        assert build_ports("25-20", udp=False, randomize=False) == [
            "20",
            "21",
            "22",
            "23",
            "24",
            "25",
        ]

    def test_przecinki_nie_sa_obslugiwane(self) -> None:
        """netcat to nie nmap — lista po przecinku jest błędem.

        Zweryfikowane: ``nc -z 127.0.0.1 34701,40010`` →
        ``nc: port number invalid: 34701,40010``.
        """
        with pytest.raises(FatalError):
            build_ports("80,443", udp=False, randomize=False)

    def test_nazwa_z_minusem_nie_jest_zakresem(self) -> None:
        """[netcat.c:1486] Zakres wymaga CYFRY na pierwszej pozycji.

        Dzięki temu nazwa usługi z minusem nie jest brana za zakres.
        Jeśli ta nazwa nie istnieje w systemie, oczekujemy błędu „unknown"
        — a nie prób parsowania zakresu.
        """
        try:
            expected = socket.getservbyname("http-alt", "tcp")
        except OSError:
            with pytest.raises(FatalError, match="unknown"):
                build_ports("http-alt", udp=False, randomize=False)
        else:
            assert build_ports("http-alt", udp=False, randomize=False) == [
                str(expected)
            ]

    def test_dzielenie_na_pierwszym_minusie(self) -> None:
        """``strchr``, nie ``strrchr`` — ``20-25-30`` daje górną granicę
        ``"25-30"``, co jest błędem. Identycznie jak w C."""
        with pytest.raises(FatalError):
            build_ports("20-25-30", udp=False, randomize=False)

    def test_randomizacja_zachowuje_zbior(self) -> None:
        plain = build_ports("1000-1050", udp=False, randomize=False)
        mixed = build_ports("1000-1050", udp=False, randomize=True)
        assert sorted(mixed, key=int) == plain
        assert len(mixed) == len(plain)

    def test_randomizacja_faktycznie_miesza(self) -> None:
        """Statystyczny, nie deterministyczny: przy 500 portach
        prawdopodobieństwo trafienia kolejności rosnącej jest pomijalne."""
        plain = build_ports("2000-2500", udp=False, randomize=False)
        shuffles = [
            build_ports("2000-2500", udp=False, randomize=True) for _ in range(3)
        ]
        assert any(s != plain for s in shuffles)

    def test_duzy_zakres(self) -> None:
        ports = build_ports("1-65535", udp=False, randomize=False)
        assert len(ports) == 65535
        assert ports[0] == "1"
        assert ports[-1] == "65535"
