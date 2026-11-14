"""``pyncat`` — OpenBSD netcat przepisany z C na Pythona.

Projekt edukacyjny. Odwzorowuje podzbiór ``nc(1)`` z OpenBSD: tryb klienta
i nasłuchu (TCP i UDP), zakresy portów, tryb zero-I/O, półzamknięcie
połączenia i propagację EOF.

Mapowanie modułów na części ``netcat.c`` (rev. 1.241, 1900 linii):

==================  ====================  ==================================
moduł               linie w netcat.c      co odwzorowuje
==================  ====================  ==================================
``constants.py``    60–123                ``#define`` i zmienne globalne
``cli.py``          176–347, 356–364,     ``getopt``, argumenty pozycyjne,
                    452–480, 1837–1898    walidacja, ``help()``/``usage()``
``ports.py``        1455–1532             ``strtoport()``, ``build_ports()``
``netio.py``        1305–1367             ``fillbuf()``, ``drainbuf()``
``relay.py``        1104–1303             ``readwrite()`` — serce programu
``endpoints.py``    930–1102, 1534–1550   ``remote_connect()``,
                                          ``timeout_connect()``,
                                          ``local_listen()``, ``udptest()``
``report.py``       1552–1581, 1807–1835  ``connection_info()``,
                                          ``report_sock()``, ``err``/``warn``
``app.py``          152–174, 561–730      ścieżka wykonania ``main()``
==================  ====================  ==================================

Czego tu nie ma — rozmyślnie: wykonywania poleceń (``-e``), TLS, proxy,
gniazd uniksowych, SCTP. Uzasadnienia: ``docs/netcat-architecture.md`` §7
oraz README.

OSTRZEŻENIE: łączyć się i nasłuchiwać wolno wyłącznie na własnej
infrastrukturze albo za pisemną zgodą właściciela.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__", "main"]


def main(argv: list[str] | None = None) -> int:
    """Punkt wejścia CLI — opakowanie na ``pyncat.app.main``.

    Import jest leniwy, żeby ``import pyncat`` nie ciągnął ``select``
    i ``socket``; ma to znaczenie dla demo w przeglądarce, gdzie
    ``select.poll`` nie istnieje.
    """
    from .app import main as _main

    return _main(argv)
