/*
 * pyncat_ref.c - referencyjna implementacja C11 dla projektu pyncat-basic.
 *
 * Po co to jest
 * -------------
 * Ten plik istnieje wyłącznie po to, żeby porównanie z pakietem `pyncat`
 * (Python) było porównaniem "jabłka do jabłek": TEN SAM algorytm, te same
 * nazwy funkcji, te same stałe, ta sama semantyka half-close i propagacji
 * EOF - w dwóch językach. Jeśli kod różni się między wersjami, to ma być
 * różnica językowa (bytearray vs memmove, select.poll vs poll(2)), a nie
 * różnica projektowa.
 *
 * Licencja - twarda granica
 * -------------------------
 * `netcat.c` z OpenBSD (Copyright (c) 2001 Eric Jackson, (c) 2015 Bob Beck)
 * jest na licencji BSD. Z tego pliku NIE skopiowano ani jednej linii kodu.
 * Odtworzone są algorytmy i semantyka, a komentarze odwołują się do nazw
 * funkcji i numerów linii. WSZYSTKIE numery linii w komentarzach odnoszą się
 * do `netcat.c,v 1.241 2026/10/31` (1900 linii).
 *
 * Binarium odniesienia do pomiarów: OpenBSD netcat, Debian patchlevel
 * 1.226-1ubuntu2 (`/usr/bin/nc`). Tam, gdzie 1.226 różni się od rev. 1.241,
 * komentarz mówi, co zmierzono i którą wersję odtwarzamy.
 *
 * Czego tu nie ma i nigdy nie będzie
 * ----------------------------------
 * Żadnego `-e`, żadnego `-c` w znaczeniu "uruchom program i podłącz go do
 * socketu", żadnego reverse shella. OpenBSD świadomie nie przepisało tego
 * z netcata 1.10 i to jest granica tego projektu: narzędzie przekazuje bajty
 * między stdin/stdout a socketem, nie uruchamia procesów.
 * Pominięte świadomie także: TLS, proxy (-x/-X/-P), gniazda uniksowe (-U),
 * fdpass (-F), telnet (-t), SCTP, -S/-V/-D/-T/-I/-O/-M/-m, pledge/unveil.
 *
 * Kontrakt CLI (identyczny z wersją Python):
 *   pyncat [-46dhklNnruvz] [-i sec] [-p port] [-q sec] [-s addr] [-W n]
 *          [-w sec] [cel] [port]
 */

/*
 * Makra dostępności MUSZĄ być przed wszystkimi #include - inaczej glibc
 * zdąży już zdefiniować __USE_* na podstawie domyślnych ustawień.
 *
 * _POSIX_C_SOURCE 200809L deklaruje bazę, z której korzystamy: poll(2),
 * getaddrinfo(3), shutdown(2), strdup(3), fcntl(2), clock_gettime(2).
 * _DEFAULT_SOURCE dokłada NI_MAXHOST/NI_MAXSERV (w glibc schowane pod
 * __USE_MISC) oraz random(3)/srandom(3).
 *
 * POMIAR na glibc 2.39 / gcc 13.3 (ta sama linia kompilacji, plik z usuniętą
 * jedną linią #define):
 *   bez _DEFAULT_SOURCE   -> 9 błędów, m.in. "'NI_MAXHOST' undeclared"
 *                            i "implicit declaration of function 'random'"
 *   bez _POSIX_C_SOURCE   -> 0 błędów
 * Czyli na glibc samo _DEFAULT_SOURCE wystarcza do KOMPILACJI, bo włącza
 * także widoczność POSIX-ową. _POSIX_C_SOURCE zostaje jednak nie tylko jako
 * deklaracja intencji - na glibc ma skutek BEHAWIORALNY: wiąże getopt(3)
 * z wariantem POSIX-owym (__posix_getopt), który NIE permutuje argv.
 * POMIAR (ta sama linia kompilacji, plik z usuniętą jedną linią #define,
 * `nm -D --undefined-only` na gotowej binarce):
 *   z _POSIX_C_SOURCE   -> U __posix_getopt; `pyncat_ref HOST PORT -z -v`
 *                          kończy się błędem użycia (exit 1), bo -z i -v
 *                          są już argumentami pozycyjnymi
 *   bez _POSIX_C_SOURCE -> U getopt (wariant GNU); to samo wywołanie
 *                          DZIAŁA, bo GNU getopt przestawia flagi na przód
 * Zostaje wariant POSIX-owy, i to jest świadoma decyzja: dokładnie tak
 * zachowuje się getopt.getopt() w bliźniaku (pyncat/cli.py), więc oba
 * programy przyjmują ten sam zbiór wywołań. Odstępstwo od /usr/bin/nc,
 * które jest linkowane z GNU-owym getopt i `nc HOST PORT -z -v` przyjmuje.
 * Bez obu makr plik się nie kompiluje (POMIAR: 60 błędów).
 */
#define _POSIX_C_SOURCE 200809L
#define _DEFAULT_SOURCE

#include <sys/types.h>
#include <sys/socket.h>

#include <netinet/in.h>

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <netdb.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

/* [netcat.c:62] i [netcat.c:69] - te same stałe co upstream i co pyncat. */
#define PORT_MAX	65535

/*
 * BUFSIZE da się nadpisać z linii poleceń (-DBUFSIZE=4096), bo to jedyny
 * parametr, który realnie zmienia profil wydajnościowy pętli relay: decyduje
 * o liczbie syscalli na megabajt. Eksperyment, który z tego korzysta, jest
 * w ci/bench.py; wyniki w docs/c-vs-python.md, sekcja "Gdzie idzie czas".
 * Bez -D wartość jest taka sama jak w netcat.c i w pyncat/constants.py.
 */
#ifndef BUFSIZE
#define BUFSIZE		16384
#endif

/*
 * [netcat.c:65-68] Indeksy w tablicy pfd[4].
 * POLL_NETOUT i POLL_NETIN to DWA slots na TYM SAMYM deskryptorze, każdy
 * z własną maską `events` i własnym "życiem". Half-close jest modelowany
 * przez pfd[i].fd = -1 na jednym kierunku, podczas gdy drugi żyje dalej.
 * To jest cały mechanizm - nie ma żadnych osobnych zmiennych stanu.
 */
#define POLL_STDIN	0
#define POLL_NETOUT	1
#define POLL_NETIN	2
#define POLL_STDOUT	3

/*
 * [netcat.c:1356, 1324] Upstream koduje "EAGAIN/EINTR - spróbuj ponownie"
 * sentinelami TLS_WANT_POLLIN = -2 / TLS_WANT_POLLOUT = -3 z libtls.
 * Bez TLS mają one tylko jedno zadanie: odróżnić "ponów" od -1 (błąd trwały)
 * i od 0 (EOF). Trzy różne wartości ujemne o trzech różnych znaczeniach -
 * w pyncat to samo jest enumem.
 */
#define WANT_POLLIN	(-2)
#define WANT_POLLOUT	(-3)

/* [netcat.c:77-121] Flagi z getopt + stan globalny. Te same nazwy. */
static int		dflag;		/* -d: nie czytaj ze stdin */
static unsigned int	iflag;		/* -i: przerwa w sekundach */
static int		kflag;		/* -k: obsługuj wiele połączeń */
static int		lflag;		/* -l: nasłuchuj */
static int		Nflag;		/* -N: shutdown(SHUT_WR) po EOF */
static int		nflag;		/* -n: bez DNS */
static char	       *pflag;		/* -p: port źródłowy */
static int		qflag = -1;	/* -q: brak flagi == -q -1 */
static int		rflag;		/* -r: losowa kolejność portów */
static char	       *sflag;		/* -s: adres źródłowy */
static int		uflag;		/* -u: UDP */
static int		vflag;		/* -v: verbose */
static int		zflag;		/* -z: zero-I/O */

/*
 * [netcat.c:114] recvcount/recvlimit są GLOBALNE. Konsekwencją jest kwirk,
 * który odtwarzamy świadomie: przy -k licznik odczytów akumuluje się MIĘDZY
 * połączeniami, bo readwrite() go nie zeruje. Serwer z -W 2 -k przepuszcza
 * dwa odczyty w pierwszym połączeniu, a w drugim kończy natychmiast po
 * pierwszym odczycie. Patrz komentarz w readwrite().
 */
static int	recvcount;
static int	recvlimit;

/* [netcat.c:115] -w w milisekundach; -1 = bez limitu. Trafia do poll(2). */
static int	timeout = -1;
static int	family = AF_UNSPEC;

/* [netcat.c:117] Lista portów jako stringi, zakończona NULL-em. */
static char	*portlist[PORT_MAX + 1];

static const char *progname = "pyncat_ref";

/* --- deklaracje wyprzedzające: te same nazwy co w pyncat --------------- */

static void	readwrite(int);
static ssize_t	drainbuf(int, unsigned char *, size_t *);
static ssize_t	fillbuf(int, unsigned char *, size_t *);
static int	remote_connect(const char *, const char *, struct addrinfo,
		    char *);
static int	timeout_connect(int, const struct sockaddr *, socklen_t);
static int	local_listen(const char *, const char *, struct addrinfo);
static void	build_ports(char *);
static void	free_ports(void);
static int	strtoport(const char *, int);
static int	udptest(int);
static void	connection_info(const char *, const char *, const char *,
		    const char *);
static void	report_sock(const char *, const struct sockaddr *, socklen_t);
static _Noreturn void help(void);
static void	usage(int);

/* --- obsługa błędów ---------------------------------------------------- */

/*
 * Odpowiedniki err(3)/warn(3) z BSD. Nie używamy <err.h>, bo to rozszerzenie
 * poza C11/POSIX - a Makefile kompiluje z -std=c11 -Wpedantic -Werror.
 * Format komunikatu jest taki jak u netcata: "pyncat: tekst: strerror".
 * UWAGA: bez __attribute__((format(printf))) (rozszerzenie GNU) kompilator
 * nie sprawdzi tych formatów - nigdy nie przekazujemy tu danych użytkownika
 * jako formatu, tylko jako argumenty.
 */
static void
nc_vmsg(int use_errno, int saved_errno, const char *fmt, va_list ap)
{
	fprintf(stderr, "%s: ", progname);
	if (fmt != NULL)
		vfprintf(stderr, fmt, ap);
	if (use_errno) {
		if (fmt != NULL)
			fprintf(stderr, ": ");
		fprintf(stderr, "%s", strerror(saved_errno));
	}
	fprintf(stderr, "\n");
}

static void
nc_warn(const char *fmt, ...)
{
	int saved_errno = errno;
	va_list ap;

	va_start(ap, fmt);
	nc_vmsg(1, saved_errno, fmt, ap);
	va_end(ap);
}

static _Noreturn void
nc_err(const char *fmt, ...)
{
	int saved_errno = errno;
	va_list ap;

	va_start(ap, fmt);
	nc_vmsg(1, saved_errno, fmt, ap);
	va_end(ap);
	exit(1);
}

static _Noreturn void
nc_errx(const char *fmt, ...)
{
	va_list ap;

	va_start(ap, fmt);
	nc_vmsg(0, 0, fmt, ap);
	va_end(ap);
	exit(1);
}

/* --- drobne narzędzia, których nie ma w C11/POSIX --------------------- */

/*
 * [netcat.c:1464] Upstream używa strtonum(3) z OpenBSD. W glibc tego nie ma,
 * więc piszemy odpowiednik o tej samej semantyce: zwraca wartość i ustawia
 * *errstrp na NULL przy sukcesie, albo na "invalid" / "too small" /
 * "too large" przy porażce. errno jest ustawiane tak jak w OpenBSD
 * (EINVAL dla niecyfr, ERANGE dla zakresu), bo strtoport() to rozróżnia.
 */
static long long
nc_strtonum(const char *numstr, long long minval, long long maxval,
    const char **errstrp)
{
	char *ep;
	long long ll;
	int saved_errno = errno;

	*errstrp = NULL;
	if (minval > maxval) {
		*errstrp = "invalid";
		errno = EINVAL;
		return 0;
	}

	errno = 0;
	ll = strtoll(numstr, &ep, 10);
	if (numstr == ep || *ep != '\0') {
		*errstrp = "invalid";
		errno = EINVAL;
		return 0;
	}
	if (errno == ERANGE && ll == LLONG_MIN) {
		*errstrp = "too small";
		return 0;
	}
	if (errno == ERANGE && ll == LLONG_MAX) {
		*errstrp = "too large";
		return 0;
	}
	if (ll < minval) {
		*errstrp = "too small";
		errno = ERANGE;
		return 0;
	}
	if (ll > maxval) {
		*errstrp = "too large";
		errno = ERANGE;
		return 0;
	}

	errno = saved_errno;
	return ll;
}

/*
 * [netcat.c:1505] Upstream losuje przez arc4random_uniform(), którego nie ma
 * ani w C11, ani w POSIX. Istotna jest tylko jedna własność: rozkład
 * równomierny BEZ modulo bias. Odtwarzamy ją przez rejection sampling na
 * random(3) (przestrzeń [0, 2^31-1]). To nie jest źródło kryptograficzne -
 * i nie musi być, bo służy do permutacji numerów portów przy -r.
 */
static unsigned int
rand_below(unsigned int bound)
{
	unsigned int limit, r;

	if (bound < 2)
		return 0;
	limit = 0x80000000u - (0x80000000u % bound);
	do {
		r = (unsigned int)random();
	} while (r >= limit);
	return r % bound;
}

/*
 * [netcat.c:634] Upstream robi nieblokujący accept jednym wywołaniem
 * accept4(..., SOCK_NONBLOCK), a gniazdo klienta tworzy przez
 * socket(..., ai_socktype | SOCK_NONBLOCK, ...). Oba te warianty to
 * rozszerzenia (Linux/BSD), niedostępne w czystym POSIX-ie, więc robimy
 * to dwuetapowo: accept()/socket() + fcntl(O_NONBLOCK). Efekt identyczny,
 * różni się tylko liczbą syscalli (2 zamiast 1) i brakiem atomowości
 * między accept() a fcntl() - bez znaczenia, bo jesteśmy jednowątkowi.
 */
static int
set_nonblock(int fd)
{
	int fl;

	if ((fl = fcntl(fd, F_GETFL)) == -1)
		return -1;
	return fcntl(fd, F_SETFL, fl | O_NONBLOCK);
}

/* Deadline dla -q: zegar MONOTONICZNY, odporny na skoki czasu ściennego. */
static void
set_deadline(struct timespec *deadline, int seconds)
{
	if (clock_gettime(CLOCK_MONOTONIC, deadline) == -1)
		nc_err("clock_gettime");
	deadline->tv_sec += seconds;
}

static long long
ms_left(const struct timespec *deadline)
{
	struct timespec now;

	if (clock_gettime(CLOCK_MONOTONIC, &now) == -1)
		nc_err("clock_gettime");
	return (long long)(deadline->tv_sec - now.tv_sec) * 1000 +
	    (deadline->tv_nsec - now.tv_nsec) / 1000000;
}

/* --- main ------------------------------------------------------------- */

int
main(int argc, char *argv[])
{
	int ch, s = -1, ret;
	char *host = NULL, *uport = NULL;
	char ipaddr[NI_MAXHOST];
	struct addrinfo hints;
	socklen_t len;
	struct sockaddr_storage cliaddr;
	const char *errstr;

	/*
	 * [netcat.c:168] ret startuje z 1. Stąd cały kontrakt kodów wyjścia:
	 * 0 <=> co najmniej jeden port dał się połączyć. Nieudane połączenie
	 * robi `continue` i NIE dotyka ret, więc raz ustawione ret = 0 nie
	 * wraca do 1. POMIAR na 1.226: -z zakres z jednym otwartym portem -> 0.
	 */
	ret = 1;

	/*
	 * progname jest stałe ("pyncat_ref"), a nie basename(argv[0]). err(3)
	 * w netcacie bierze nazwę z getprogname(), więc jego komunikaty zmieniają
	 * się razem z nazwą pliku; tutaj nie zmieniają się nigdy - ani po
	 * symlinku, ani po uruchomieniu pełną ścieżką.
	 * UWAGA, sam PREFIKS nie jest wspólny z bliźniakiem: Python drukuje
	 * "pyncat: " (PROGNAME w pyncat/constants.py), ten plik "pyncat_ref: ".
	 * IDENTYCZNA jest treść PO prefiksie i dokładnie tak porównuje ją
	 * tests/test_parity.py, który obcina oba prefiksy. Jeśli kiedyś ma być
	 * identyczny także prefiks, zmieniać tutaj (jedna linia: progname),
	 * nie w teście.
	 * Jeden komunikat wymyka się tej regule w obu implementacjach:
	 * "invalid option" drukuje sam getopt(3) i używa argv[0] - tak samo
	 * jak netcat.
	 */
	memset(ipaddr, 0, sizeof(ipaddr));
	memset(&cliaddr, 0, sizeof(cliaddr));

	/*
	 * [netcat.c:174] SIGPIPE ignorujemy, bo inaczej zapis do socketu
	 * zamkniętego przez drugą stronę zabiłby proces sygnałem zamiast
	 * zwrócić EPIPE z write(2). Cała obsługa błędów w drainbuf() opiera
	 * się na tym, że write() wraca z -1.
	 */
	signal(SIGPIPE, SIG_IGN);

	/*
	 * Lista portów jest alokowana na stercie (strdup), a wyjście może
	 * nastąpić przez nc_err() z dowolnego miejsca. atexit() gwarantuje,
	 * że LeakSanitizer milczy także na ścieżkach błędów.
	 */
	if (atexit(free_ports) != 0)
		nc_errx("atexit failed");

	while ((ch = getopt(argc, argv, "46dhi:klNnp:q:rs:uvW:w:z")) != -1) {
		switch (ch) {
		case '4':
			family = AF_INET;
			break;
		case '6':
			family = AF_INET6;
			break;
		case 'd':
			dflag = 1;
			break;
		case 'h':
			help();
			/* NOTREACHED */
		case 'i':
			iflag = (unsigned int)nc_strtonum(optarg, 0, UINT_MAX,
			    &errstr);
			if (errstr != NULL)
				nc_errx("błędna wartość -i (%s): %s", errstr, optarg);
			break;
		case 'k':
			kflag = 1;
			break;
		case 'l':
			lflag = 1;
			break;
		case 'N':
			Nflag = 1;
			break;
		case 'n':
			nflag = 1;
			break;
		case 'p':
			pflag = optarg;
			break;
		case 'q':
			/* -q przyjmuje także liczby ujemne: -q -1 == brak -q */
			qflag = (int)nc_strtonum(optarg, INT_MIN, INT_MAX,
			    &errstr);
			if (errstr != NULL)
				nc_errx("błędna wartość -q (%s): %s", errstr, optarg);
			break;
		case 'r':
			rflag = 1;
			break;
		case 's':
			sflag = optarg;
			break;
		case 'u':
			uflag = 1;
			break;
		case 'v':
			vflag = 1;
			break;
		case 'W':
			recvlimit = (int)nc_strtonum(optarg, 1, INT_MAX,
			    &errstr);
			if (errstr != NULL)
				nc_errx("błędny limit odbioru -W (%s): %s", errstr, optarg);
			break;
		case 'w':
			/* [netcat.c] -w trzymamy w ms, bo tyle bierze poll(2) */
			timeout = (int)nc_strtonum(optarg, 0, INT_MAX / 1000,
			    &errstr);
			if (errstr != NULL)
				nc_errx("błędny timeout -w (%s): %s", errstr, optarg);
			timeout *= 1000;
			break;
		case 'z':
			zflag = 1;
			break;
		default:
			usage(1);
		}
	}
	argc -= optind;
	argv += optind;

	/*
	 * [netcat.c:356-365] "Cruft to make sure options are clean".
	 *
	 * ODSTĘPSTWO od rev. 1.241 wymuszone pomiarem. Upstream odrzuca
	 * `-s`+`-l` [netcat.c:425-426] i `-p`+`-l` [netcat.c:427-428].
	 * Debian 1.226 NIE odrzuca: przywraca historyczny styl Hobbita
	 * `nc -l -p port`, w którym -p to port NASŁUCHU, a -s adres
	 * nasłuchu. POMIARY na /usr/bin/nc (1.226):
	 *
	 *   nc -v -l 35999              -> Listening on 0.0.0.0 35999
	 *   nc -v -l -p 35999           -> Listening on 0.0.0.0 35999
	 *   nc -v -l 127.0.0.1 35998    -> Listening on localhost 35998
	 *   nc -v -l -p 35998 -s 127.0.0.1 -> Listening on localhost 35998
	 *   nc -v -l -p 35998 35997     -> usage, exit 1
	 *   nc -v -l -s 127.0.0.1 35998 -> usage, exit 1
	 *   nc -l                       -> nc: missing port number, exit 1
	 *   nc 127.0.0.1                -> nc: missing port number, exit 1
	 *   nc                          -> usage, exit 1
	 *
	 * UWAGA - ODSTĘPSTWO OD ZMIERZONEGO DEBIANA, ŚWIADOME:
	 * Debian 1.226 przyjmuje `nc -l -p PORT` (i to jest idiom z połowy
	 * tutoriali w internecie). Upstream [netcat.c:428-429] tego ZABRANIA:
	 * `cannot use -p and -l`. Idziemy za upstreamem, bo:
	 *   1. kanonicznym źródłem tego projektu jest netcat.c rev. 1.241,
	 *      a nie pakiet Debiana - tak jest napisane w całej dokumentacji;
	 *   2. w wariancie debianowym `-p` znaczy dwie RÓŻNE rzeczy w zależności
	 *      od obecności `-l` (port źródłowy kontra port nasłuchu), a to jest
	 *      dokładnie ten rodzaj brodawki, której narzędzie do nauki nie
	 *      powinno po cichu powtarzać;
	 *   3. pyncat w Pythonie robi to samo, a cały sens tego pliku to
	 *      porównanie "jabłka do jabłek" - różne zachowanie flag
	 *      unieważniłoby je.
	 * Właściwa składnia: `pyncat_ref -l PORT`.
	 * Rozbieżność opisana w docs/tablice-decyzyjne.md sekcja 9.5.
	 */
	if (argc == 1 && lflag)
		uport = argv[0];
	else if (argc == 2) {
		host = argv[0];
		uport = argv[1];
	} else {
		/* pyncat w Pythonie drukuje tu komunikat PRZED usage; upstream
		 * [netcat.c:364] woła samo usage(1). Idziemy za pyncatem, żeby obie
		 * implementacje zachowywały się identycznie - to cel tego pliku. */
		fprintf(stderr,
		    "%s: oczekiwano 'cel port' albo 'port' razem z -l\n", progname);
		usage(1);
	}

	/* [netcat.c:426-433] Wszystkie cztery wykluczalności z upstreamu.
	 * Komunikaty i kolejność identyczne jak w pyncat/cli.py::_validate.
	 * POMIAR na Debianie 1.226: `nc -z -l P` -> "cannot use -z and -l";
	 * `nc -k HOST P` -> "must use -l with -k". Debian przyjmuje natomiast
	 * `-l -p P` i `-l -s A`, czego my świadomie nie robimy (patrz wyżej). */
	if (lflag && sflag != NULL)
		nc_errx("nie można użyć -s razem z -l");
	if (lflag && pflag != NULL)
		nc_errx("nie można użyć -p razem z -l");
	if (lflag && zflag)
		nc_errx("nie można użyć -z razem z -l");
	if (!lflag && kflag)
		nc_errx("-k wymaga -l");

	/*
	 * Poza upstreamem: netcat nie waliduje pflag i wpycha go wprost do
	 * getaddrinfo(), co przy `-p 70000` daje mało mówiący komunikat
	 * "Servname not supported". Świadomy dodatek, identyczny jak
	 * w pyncat/cli.py::_validate - oba programy muszą reagować tak samo.
	 */
	if (pflag != NULL) {
		const char *perr;

		(void)nc_strtonum(pflag, 1, PORT_MAX, &perr);
		if (perr != NULL) {
			if (strcmp(perr, "invalid") != 0)
				nc_errx("błędny port źródłowy -p (%s): %s",
				    perr, pflag);
			if (getservbyname(pflag, uflag ? "udp" : "tcp") == NULL)
				nc_errx("nieznana usługa portu źródłowego \"%s\"",
				    pflag);
		}
	}

	/* [netcat.c:472-479] hints dla getaddrinfo */
	memset(&hints, 0, sizeof(hints));
	hints.ai_family = family;
	hints.ai_socktype = uflag ? SOCK_DGRAM : SOCK_STREAM;
	hints.ai_protocol = uflag ? IPPROTO_UDP : IPPROTO_TCP;
	if (nflag)
		hints.ai_flags |= AI_NUMERICHOST;

	if (lflag) {
		/* [netcat.c:562] Tryb -l kończy się z 0 niezależnie od ruchu. */
		ret = 0;

		/* [netcat.c:580] "Allow only one connection at a time, but
		 * stay alive." Jedna iteracja = jedno obsłużone połączenie. */
		for (;;) {
			/*
			 * [netcat.c:582-584] Socket nasłuchujący jest tworzony
			 * OD NOWA w każdej iteracji -k: close() + local_listen().
			 * Naiwna implementacja trzymałaby jeden listener; ta go
			 * przerzuca. Dlatego upstream ustawia SO_REUSEPORT -
			 * patrz komentarz w local_listen() o odstępstwie.
			 */
			if (s != -1)
				close(s);
			s = local_listen(host, uport, hints);
			if (s == -1)
				/*
				 * [netcat.c:586-587] err(1, NULL): sam
				 * strerror, bez kontekstu. Nie dopisujemy
				 * "bind", bo -1 może przyjść także z socket()
				 * - POMIAR: w kontenerze bez IPv6 `nc -6 -l`
				 * drukuje dosłownie "nc: Address family not
				 * supported by protocol", i nasz komunikat
				 * ma być taki sam.
				 */
				nc_err(NULL);

			if (uflag && kflag) {
				/*
				 * [netcat.c:588-598] Wariant A: UDP + -k.
				 * Bez connect() - socket odbiera datagramy od
				 * wielu nadawców, ale nie ma do kogo pisać
				 * (write() -> ENOTCONN), więc komunikacja jest
				 * jednokierunkowa.
				 */
				readwrite(s);
			} else if (uflag && !kflag) {
				/*
				 * [netcat.c:599-628] Wariant B: UDP bez -k.
				 * recvfrom(MSG_PEEK) czeka na pierwszy datagram
				 * i ujawnia adres nadawcy, po czym connect()
				 * przypina socket do tego jednego nadawcy.
				 * MSG_PEEK jest tu KLUCZOWE: datagram ZOSTAJE
				 * w buforze jądra i zostanie normalnie wczytany
				 * przez readwrite(), więc pierwsze dane nie
				 * przepadają. Bez MSG_PEEK gubimy pierwszy
				 * datagram - a to zwykle cały komunikat.
				 */
				char buf[2048];
				struct sockaddr_storage z;

				memset(&z, 0, sizeof(z));
				len = sizeof(z);
				if (recvfrom(s, buf, sizeof(buf), MSG_PEEK,
				    (struct sockaddr *)&z, &len) == -1)
					nc_err("recvfrom");
				if (connect(s, (struct sockaddr *)&z, len) == -1)
					nc_err("connect");
				if (vflag)
					report_sock("Połączenie przychodzące od",
					    (struct sockaddr *)&z, len);
				readwrite(s);
			} else {
				/* [netcat.c:629-653] Wariant C: TCP. */
				int connfd;

				len = sizeof(cliaddr);
				connfd = accept(s, (struct sockaddr *)&cliaddr,
				    &len);
				if (connfd == -1)
					/* [netcat.c:637] "For now, all errnos
					 * are fatal" */
					nc_err("accept");
				if (set_nonblock(connfd) == -1)
					nc_err("fcntl O_NONBLOCK");
				if (vflag)
					report_sock("Połączenie przychodzące od",
					    (struct sockaddr *)&cliaddr, len);
				readwrite(connfd);
				/* Bez tego close() pętla -k przeciekałaby
				 * deskryptorami po jednym na połączenie. */
				close(connfd);
			}

			if (!kflag)
				break;
		}
	} else {
		int i;

		/* [netcat.c:677] Zbuduj portlist[] */
		build_ports(uport);

		/* [netcat.c:680-730] Przejdź po portach po kolei. */
		for (i = 0; portlist[i] != NULL; i++) {
			if (s != -1)
				close(s);

			/*
			 * ODSTĘPSTWO ŚWIADOME (docs/tablice-decyzyjne.md §9.1):
			 * upstream NIE ma przerwy między portami - iflag
			 * występuje w netcat.c wyłącznie w [netcat.c:1153]
			 * wewnątrz readwrite(). Man page (nc.1:162) obiecuje
			 * "a delay time between connections to multiple ports",
			 * a POMIAR na Debianie 1.226 pokazuje, że -i nie robi
			 * tam NIC. Implementujemy obie semantyki, bo kontrakt
			 * projektu tego wymaga; sleep tylko MIĘDZY portami,
			 * nie przed pierwszym.
			 */
			if (iflag != 0 && i > 0)
				sleep(iflag);

			s = remote_connect(host, portlist[i], hints, ipaddr);
			if (s == -1)
				continue;	/* ret nietknięte - patrz wyżej */

			ret = 0;
			if (vflag || zflag) {
				int print_info = 1;

				if (uflag) {
					/* [netcat.c:709-715] Dla UDP upewnij
					 * się, że port po drugiej stronie
					 * istnieje. -1 = test nieudany. */
					if ((print_info = udptest(s)) == -1) {
						/*
						 * Kwirk: ret = 1 może tu
						 * NADPISAĆ wcześniejszy sukces,
						 * bo pętla leci dalej. Jedyne
						 * miejsce, gdzie "raz 0, już 0"
						 * nie obowiązuje.
						 */
						ret = 1;
						continue;
					}
				}
				if (print_info == 1)
					connection_info(host, portlist[i],
					    uflag ? "udp" : "tcp", ipaddr);
			}
			/* [netcat.c:725] -z = zero-I/O: tylko sprawdź port. */
			if (!zflag)
				readwrite(s);
		}
	}

	if (s != -1)
		close(s);

	return ret;
}

/* --- nawiązywanie połączeń ------------------------------------------- */

/*
 * [netcat.c:929-1003] remote_connect()
 * Zwraca socket podłączony do zdalnego hosta, opcjonalnie zbindowany do
 * adresu/portu źródłowego (-s/-p). -1 przy porażce.
 */
static int
remote_connect(const char *host, const char *port, struct addrinfo hints,
    char *ipaddr)
{
	struct addrinfo *res, *res0;
	int s = -1, error, herr, save_errno;

	if ((error = getaddrinfo(host, port, &hints, &res0)) != 0)
		nc_errx("getaddrinfo for host \"%s\" port %s: %s",
		    host, port, gai_strerror(error));

	for (res = res0; res != NULL; res = res->ai_next) {
		if ((s = socket(res->ai_family, res->ai_socktype,
		    res->ai_protocol)) == -1)
			continue;
		/* [netcat.c:941] upstream: socket(..., |SOCK_NONBLOCK, ...) */
		if (set_nonblock(s) == -1) {
			save_errno = errno;
			close(s);
			errno = save_errno;
			s = -1;
			continue;
		}

		/* [netcat.c:946-963] -s / -p: bind przed connect. */
		if (sflag != NULL || pflag != NULL) {
			struct addrinfo ahints, *ares;

			/*
			 * [netcat.c:950] upstream próbuje tu SO_BINDANY
			 * ("don't insist"). Na Linuksie tej opcji nie ma -
			 * najbliższym odpowiednikiem byłoby IP_TRANSPARENT
			 * wymagające CAP_NET_ADMIN. Pomijamy: bind do
			 * nie-lokalnego adresu po prostu się nie uda.
			 */
			memset(&ahints, 0, sizeof(ahints));
			ahints.ai_family = res->ai_family;
			ahints.ai_socktype = uflag ? SOCK_DGRAM : SOCK_STREAM;
			ahints.ai_protocol = uflag ? IPPROTO_UDP : IPPROTO_TCP;
			ahints.ai_flags = AI_PASSIVE;
			if ((error = getaddrinfo(sflag, pflag, &ahints,
			    &ares)) != 0)
				nc_errx("getaddrinfo: %s", gai_strerror(error));

			if (bind(s, ares->ai_addr, ares->ai_addrlen) == -1) {
				/* freeaddrinfo przed wyjściem - inaczej
				 * LeakSanitizer zgłosiłby wyciek. */
				save_errno = errno;
				freeaddrinfo(ares);
				freeaddrinfo(res0);
				errno = save_errno;
				nc_err("bind failed");
			}
			freeaddrinfo(ares);
		}

		if (ipaddr != NULL) {
			herr = getnameinfo(res->ai_addr, res->ai_addrlen,
			    ipaddr, NI_MAXHOST, NULL, 0, NI_NUMERICHOST);
			if (herr == EAI_SYSTEM) {
				freeaddrinfo(res0);
				nc_err("getnameinfo");
			} else if (herr != 0) {
				freeaddrinfo(res0);
				nc_errx("getnameinfo: %s", gai_strerror(herr));
			}
		}

		if (timeout_connect(s, res->ai_addr, res->ai_addrlen) == 0)
			break;

		if (vflag) {
			/* [netcat.c:983-992] IP drukujemy tylko wtedy, gdy
			 * wnosi coś nowego względem nazwy hosta. */
			if (nflag || ipaddr == NULL ||
			    strncmp(host, ipaddr, NI_MAXHOST) == 0)
				nc_warn("nie udało się połączyć z %s port %s (%s)",
				    host, port, uflag ? "udp" : "tcp");
			else
				nc_warn("nie udało się połączyć z %s (%s) port %s (%s)",
				    host, ipaddr, port, uflag ? "udp" : "tcp");
		}

		save_errno = errno;
		close(s);
		errno = save_errno;
		s = -1;
	}

	freeaddrinfo(res0);

	return s;
}

/*
 * [netcat.c:1005-1031] timeout_connect()
 * Nieblokujący connect + poll(POLLOUT) + odczyt SO_ERROR. To jedyny sposób,
 * żeby nałożyć limit czasu na connect(2) bez alarmu/wątku.
 */
static int
timeout_connect(int s, const struct sockaddr *name, socklen_t namelen)
{
	struct pollfd pfd;
	socklen_t optlen;
	int optval;
	int ret;

	if ((ret = connect(s, name, namelen)) != 0 && errno == EINPROGRESS) {
		memset(&pfd, 0, sizeof(pfd));
		pfd.fd = s;
		pfd.events = POLLOUT;
		if ((ret = poll(&pfd, 1, timeout)) == 1) {
			optlen = sizeof(optval);
			if ((ret = getsockopt(s, SOL_SOCKET, SO_ERROR,
			    &optval, &optlen)) == 0) {
				/* Błąd connect() nie wraca z poll(), tylko
				 * siedzi w SO_ERROR - przepisujemy go do
				 * errno, żeby wyżej działał strerror(). */
				errno = optval;
				ret = optval == 0 ? 0 : -1;
			}
		} else if (ret == 0) {
			errno = ETIMEDOUT;
			ret = -1;
		} else
			nc_err("poll failed");
	}

	return ret;
}

/*
 * [netcat.c:1038-1097] local_listen()
 * Zwraca socket nasłuchujący na lokalnym porcie. -1 przy porażce.
 */
static int
local_listen(const char *host, const char *port, struct addrinfo hints)
{
	struct addrinfo *res, *res0;
	int s = -1, x = 1, save_errno, error;

	hints.ai_flags |= AI_PASSIVE;

	/*
	 * [netcat.c:1048-1053] Przy wildcardzie wymuszamy AF_INET.
	 * Czyli `pyncat -l 1234` nasłuchuje na 0.0.0.0, a NIE na ::.
	 * Zaskakujące, ale celowe i odtwarzamy to 1:1 - inaczej klient IPv4
	 * trafiałby albo nie trafiałby w listener zależnie od konfiguracji
	 * net.ipv6.bindv6only w systemie.
	 */
	if (host == NULL && hints.ai_family == AF_UNSPEC)
		hints.ai_family = AF_INET;

	if ((error = getaddrinfo(host, port, &hints, &res0)) != 0)
		nc_errx("getaddrinfo: %s", gai_strerror(error));

	for (res = res0; res != NULL; res = res->ai_next) {
		if ((s = socket(res->ai_family, res->ai_socktype,
		    res->ai_protocol)) == -1)
			continue;

		/*
		 * ODSTĘPSTWO ŚWIADOME (docs/tablice-decyzyjne.md §9.6):
		 * [netcat.c:1063] upstream ustawia SO_REUSEPORT, bo w pętli -k
		 * zamyka i otwiera listener od nowa. Na Linuksie SO_REUSEPORT
		 * ma INNĄ semantykę niż na BSD: pozwala wielu NIEZALEŻNYM
		 * procesom zbindować ten sam port i rozkłada między nie ruch.
		 * Dla narzędzia, które ma obsłużyć jedno połączenie naraz, to
		 * jest pułapka - dwa równoległe `pyncat -l` na tym samym porcie
		 * wstałyby bez błędu i losowo dzieliły klientów. Potrzebujemy
		 * tylko ominięcia TIME_WAIT, czyli SO_REUSEADDR.
		 */
		if (setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &x,
		    sizeof(x)) == -1) {
			save_errno = errno;
			freeaddrinfo(res0);
			errno = save_errno;
			nc_err("setsockopt SO_REUSEADDR");
		}

		if (bind(s, res->ai_addr, res->ai_addrlen) == 0)
			break;

		save_errno = errno;
		close(s);
		errno = save_errno;
		s = -1;
	}

	/*
	 * [netcat.c:1079-1082] Dla UDP NIE wołamy listen() - datagramowy
	 * socket nie ma kolejki połączeń. Backlog 1: netcat obsługuje jedno
	 * połączenie naraz, drugi klient czeka w kolejce jądra.
	 */
	if (!uflag && s != -1) {
		if (listen(s, 1) == -1)
			nc_err("listen");
	}
	if (vflag && s != -1) {
		struct sockaddr_storage ss;
		socklen_t slen;

		memset(&ss, 0, sizeof(ss));
		slen = sizeof(ss);
		if (getsockname(s, (struct sockaddr *)&ss, &slen) == -1)
			nc_err("getsockname");
		/* [netcat.c:1090] Dla UDP komunikat mówi o przypisaniu gniazda,
		 * nie o nasłuchu ("Bound" kontra "Listening") - bo nic nie
		 * nasłuchuje: gniazdo datagramowe nie ma kolejki połączeń. */
		report_sock(uflag ? "Przypisano gniazdo do" : "Nasłuchuję na",
		    (struct sockaddr *)&ss, slen);
	}

	freeaddrinfo(res0);

	return s;
}

/* --- serce programu --------------------------------------------------- */

/*
 * [netcat.c:1103-1302] readwrite()
 * Pętla poll() na CZTERECH slotach: stdin, net-out, net-in, stdout.
 * Dwa środkowe slots siedzą na TYM SAMYM deskryptorze z osobnymi maskami -
 * dzięki temu half-close jednego kierunku (pfd[i].fd = -1) nie rusza
 * drugiego. Dwa bufory po BUFSIZE, każdy z własnym backpressure.
 */
static void
readwrite(int net_fd)
{
	struct pollfd pfd[4];
	int stdin_fd = STDIN_FILENO;
	int stdout_fd = STDOUT_FILENO;
	unsigned char netinbuf[BUFSIZE];
	size_t netinbufpos = 0;
	unsigned char stdinbuf[BUFSIZE];
	size_t stdinbufpos = 0;
	struct timespec quit_deadline;
	int quitting = 0;		/* -q: deadline już ustawiony */
	int n, num_fds, ptimeout;
	ssize_t ret;

	memset(pfd, 0, sizeof(pfd));
	memset(&quit_deadline, 0, sizeof(quit_deadline));

	/* [netcat.c:1116-1118] -d: w ogóle nie czytaj ze stdin. */
	if (dflag)
		stdin_fd = -1;

	pfd[POLL_STDIN].fd = stdin_fd;
	pfd[POLL_STDIN].events = POLLIN;

	pfd[POLL_NETOUT].fd = net_fd;
	pfd[POLL_NETOUT].events = 0;

	pfd[POLL_NETIN].fd = net_fd;
	pfd[POLL_NETIN].events = POLLIN;

	pfd[POLL_STDOUT].fd = stdout_fd;
	pfd[POLL_STDOUT].events = 0;

	while (1) {
		/*
		 * [netcat.c:1136-1147] TRZY warunki wyjścia, sprawdzane na
		 * POCZĄTKU pętli - przed poll(), bo stan mógł się zmienić na
		 * końcu poprzedniej iteracji (propagacja EOF).
		 */

		/* 1. oba wejścia martwe i bufory puste -> nie ma co robić */
		if (pfd[POLL_STDIN].fd == -1 && pfd[POLL_NETIN].fd == -1 &&
		    stdinbufpos == 0 && netinbufpos == 0)
			return;
		/* 2. oba wyjścia martwe -> nie ma gdzie pisać */
		if (pfd[POLL_NETOUT].fd == -1 && pfd[POLL_STDOUT].fd == -1)
			return;
		/*
		 * 3. tryb -l + sieć zamknięta od strony czytania + bufory
		 * puste. Bez tego warunku serwer po rozłączeniu klienta
		 * wisiałby, czekając na dane ze swojego stdin.
		 */
		if (lflag && pfd[POLL_NETIN].fd == -1 &&
		    stdinbufpos == 0 && netinbufpos == 0)
			return;

		/*
		 * [netcat.c:1149-1153] -i: sleep na POCZĄTKU iteracji.
		 * Komentarz autora upstreamu jest szczery: "-i is for 'wait
		 * between lines sent'. We read and write arbitrary amounts of
		 * data, and we don't want to start scanning for newlines, so
		 * this is as good as it gets". Czyli to przerwa między
		 * PORCJAMI, nie między liniami. POMIAR: w Debianie 1.226 -i
		 * jest no-opem także tutaj.
		 */
		if (iflag != 0)
			sleep(iflag);

		/*
		 * Timeout dla poll(): min(pozostały czas -q, -w).
		 * -w (timeout) jest globalne i trafia tu w milisekundach;
		 * -1 oznacza "czekaj wiecznie".
		 */
		ptimeout = timeout;
		if (quitting) {
			long long rem = ms_left(&quit_deadline);

			if (rem <= 0)
				return;
			if (ptimeout < 0 || rem < (long long)ptimeout)
				ptimeout = (int)rem;
		}

		num_fds = poll(pfd, 4, ptimeout);

		if (num_fds == -1) {
			/*
			 * ODSTĘPSTWO: [netcat.c:1159] traktuje każdy błąd
			 * poll() jako fatalny, więc EINTR zabiłby transfer.
			 * Ponawiamy, bo tak samo zachowa się bliźniak
			 * w Pythonie: PEP 475 (od 3.5) sam ponawia syscalle
			 * przerwane sygnałem.
			 */
			if (errno == EINTR)
				continue;
			nc_err("polling error");
		}

		/* [netcat.c:1162-1164] timeout -w: wychodzimy bez błędu. */
		if (num_fds == 0)
			return;

		/* [netcat.c:1166-1171] Błędy socketu zdejmują slot. */
		for (n = 0; n < 4; n++) {
			if (pfd[n].revents & (POLLERR | POLLNVAL))
				pfd[n].fd = -1;
		}

		/*
		 * [netcat.c:1172-1181] "reading is possible after HUP".
		 * NAJCZĘŚCIEJ MYLONE MIEJSCE: POLLHUP RAZEM z POLLIN NIE
		 * zamyka wejścia, bo w buforze jądra mogą jeszcze być dane.
		 * Dopiero POLLHUP BEZ POLLIN oznacza koniec. Naiwna
		 * implementacja (if revents & POLLHUP -> fd = -1) gubi tu
		 * ogon strumienia - przy transferze pliku zwykle ostatnie
		 * kilkadziesiąt kB.
		 */
		if ((pfd[POLL_STDIN].events & POLLIN) &&
		    (pfd[POLL_STDIN].revents & POLLHUP) &&
		    !(pfd[POLL_STDIN].revents & POLLIN))
			pfd[POLL_STDIN].fd = -1;

		if ((pfd[POLL_NETIN].events & POLLIN) &&
		    (pfd[POLL_NETIN].revents & POLLHUP) &&
		    !(pfd[POLL_NETIN].revents & POLLIN))
			pfd[POLL_NETIN].fd = -1;

		/* [netcat.c:1183-1187] HUP na wyjściu sieciowym. */
		if (pfd[POLL_NETOUT].revents & POLLHUP) {
			if (pfd[POLL_NETOUT].fd != -1 && Nflag)
				shutdown(pfd[POLL_NETOUT].fd, SHUT_WR);
			pfd[POLL_NETOUT].fd = -1;
		}
		/* [netcat.c:1188-1190] HUP na stdout */
		if (pfd[POLL_STDOUT].revents & POLLHUP)
			pfd[POLL_STDOUT].fd = -1;
		/* [netcat.c:1191-1193] nie ma gdzie wysyłać -> nie czytaj stdin */
		if (pfd[POLL_NETOUT].fd == -1)
			pfd[POLL_STDIN].fd = -1;
		/* [netcat.c:1194-1199] nie ma stdout -> nie czytaj z sieci.
		 * shutdown(SHUT_RD) mówi drugiej stronie "nie przyjmuję", żeby
		 * nie zapełniała okna TCP w nieskończoność. */
		if (pfd[POLL_STDOUT].fd == -1) {
			if (pfd[POLL_NETIN].fd != -1)
				shutdown(pfd[POLL_NETIN].fd, SHUT_RD);
			pfd[POLL_NETIN].fd = -1;
		}

		/*
		 * [netcat.c:1201-1288] Cztery bloki I/O. Każdy ma identyczny
		 * szkielet i identyczny wzorzec przełączania zainteresowania:
		 *   po fillbuf:  bufpos > 0        -> uzbrój przeciwny slot
		 *                bufpos == BUFSIZE -> events = 0 (bufor pełny)
		 *   po drainbuf: bufpos == 0       -> events = 0 (nic do pisania)
		 *                bufpos < BUFSIZE  -> uzbrój slot wejściowy
		 * To jest klasyczny backpressure: pełny bufor wyłącza czytanie,
		 * opustoszały wyłącza pisanie. Bez tego szybki producent
		 * (np. `cat bigfile |`) zjadłby pamięć albo kręcił busy-loopem.
		 */

		/* [netcat.c:1201-1217] stdin -> stdinbuf */
		if ((pfd[POLL_STDIN].revents & POLLIN) &&
		    stdinbufpos < BUFSIZE) {
			ret = fillbuf(pfd[POLL_STDIN].fd, stdinbuf,
			    &stdinbufpos);
			if (ret == WANT_POLLIN)
				pfd[POLL_STDIN].events = POLLIN;
			else if (ret == WANT_POLLOUT)
				pfd[POLL_STDIN].events = POLLOUT;
			else if (ret == 0 || ret == -1)
				pfd[POLL_STDIN].fd = -1;
			if (stdinbufpos > 0)
				pfd[POLL_NETOUT].events = POLLOUT;
			if (stdinbufpos == BUFSIZE)
				pfd[POLL_STDIN].events = 0;
		}

		/* [netcat.c:1218-1237] stdinbuf -> sieć */
		if ((pfd[POLL_NETOUT].revents & POLLOUT) && stdinbufpos > 0) {
			ret = drainbuf(pfd[POLL_NETOUT].fd, stdinbuf,
			    &stdinbufpos);
			if (ret == WANT_POLLIN)
				pfd[POLL_NETOUT].events = POLLIN;
			else if (ret == WANT_POLLOUT)
				pfd[POLL_NETOUT].events = POLLOUT;
			else if (ret == -1)
				pfd[POLL_NETOUT].fd = -1;
			if (stdinbufpos == 0)
				pfd[POLL_NETOUT].events = 0;
			if (stdinbufpos < BUFSIZE)
				pfd[POLL_STDIN].events = POLLIN;
		}

		/* [netcat.c:1238-1272] sieć -> netinbuf */
		if ((pfd[POLL_NETIN].revents & POLLIN) &&
		    netinbufpos < BUFSIZE) {
			ret = fillbuf(pfd[POLL_NETIN].fd, netinbuf,
			    &netinbufpos);
			if (ret == WANT_POLLIN)
				pfd[POLL_NETIN].events = POLLIN;
			else if (ret == WANT_POLLOUT)
				pfd[POLL_NETIN].events = POLLOUT;
			else if (ret == -1)
				pfd[POLL_NETIN].fd = -1;
			/* [netcat.c:1251-1255] EOF z sieci (dla UDP: datagram
			 * zerowej długości - upstream nie rozróżnia). */
			if (ret == 0) {
				shutdown(pfd[POLL_NETIN].fd, SHUT_RD);
				pfd[POLL_NETIN].fd = -1;
			}
			/*
			 * [netcat.c:1256-1261] -W liczy UDANE ODCZYTY z sieci,
			 * NIE pakiety (man page kłamie: "packets"). Dla TCP
			 * granice read() nie mają nic wspólnego z granicami
			 * segmentów: jeśli trzy wysłane porcje przyjdą razem
			 * i jeden read() zwróci je wszystkie, -W 2 przepuści
			 * całe trzy. POMIAR: serwer wysyła A, pauza 0,4 s, B,
			 * pauza 0,4 s, C -> klient -W 2 odebrał dokładnie "AB".
			 *
			 * KWIRK, który odtwarzamy świadomie: recvcount jest
			 * GLOBALNY i readwrite() go nie zeruje, więc przy -k
			 * akumuluje się MIĘDZY połączeniami. Serwer
			 * `-l -k -W 2` przyjmie dwa odczyty w pierwszym
			 * połączeniu i zakończy drugie połączenie już po
			 * pierwszym odczycie. To nie jest bug do naprawienia -
			 * to zachowanie upstreamu.
			 */
			if (recvlimit > 0 && ++recvcount >= recvlimit) {
				if (pfd[POLL_NETIN].fd != -1)
					shutdown(pfd[POLL_NETIN].fd, SHUT_RD);
				pfd[POLL_NETIN].fd = -1;
				pfd[POLL_STDIN].fd = -1;
			}
			if (netinbufpos > 0)
				pfd[POLL_STDOUT].events = POLLOUT;
			if (netinbufpos == BUFSIZE)
				pfd[POLL_NETIN].events = 0;
		}

		/* [netcat.c:1273-1289] netinbuf -> stdout */
		if ((pfd[POLL_STDOUT].revents & POLLOUT) && netinbufpos > 0) {
			ret = drainbuf(pfd[POLL_STDOUT].fd, netinbuf,
			    &netinbufpos);
			if (ret == WANT_POLLIN)
				pfd[POLL_STDOUT].events = POLLIN;
			else if (ret == WANT_POLLOUT)
				pfd[POLL_STDOUT].events = POLLOUT;
			else if (ret == -1)
				pfd[POLL_STDOUT].fd = -1;
			if (netinbufpos == 0)
				pfd[POLL_STDOUT].events = 0;
			if (netinbufpos < BUFSIZE)
				pfd[POLL_NETIN].events = POLLIN;
		}

		/*
		 * [netcat.c:1291-1300] PROPAGACJA EOF - najważniejsze miejsce
		 * w całym programie.
		 *
		 * Stdin martwy i kolejka pusta: przestajemy pisać do socketu.
		 * BEZ -N upstream NIE wysyła FIN - tylko zdejmuje slot
		 * POLL_NETOUT. Druga strona nigdy się nie dowiaduje, że
		 * nadawanie się skończyło, i transfer pliku NIGDY się nie
		 * kończy. POMIAR (klient `printf DATA | nc`, serwer milczy):
		 * bez -N klient żył 7,701 s i został zabity przez `timeout`;
		 * z -N wyszedł po 0,008 s.
		 *
		 * Mechanizm z -N: klient shutdown(SHUT_WR) -> serwer dostaje
		 * EOF na netin -> netin.fd = -1, bufor pusty -> stdout.fd = -1
		 * -> warunek 3 (lflag && netin == -1) -> serwer wraca
		 * i zamyka socket -> klient dostaje EOF na netin -> warunek 1
		 * -> wyjście. Dlatego przykład "DATA TRANSFER" w nc.1 (linia
		 * 462) używa `nc -N`.
		 */
		if (pfd[POLL_STDIN].fd == -1 && stdinbufpos == 0) {
			if (pfd[POLL_NETOUT].fd != -1 && Nflag)
				shutdown(pfd[POLL_NETOUT].fd, SHUT_WR);
			pfd[POLL_NETOUT].fd = -1;

			/*
			 * -q: ŁATKA DEBIANA, w rev. 1.241 jej nie ma
			 * (grep qflag w netcat.c -> zero trafień). Semantyka
			 * zmierzona na 1.226:
			 *   -q 0  -> po EOF na stdin wyjdź NATYCHMIAST
			 *            (POMIAR: 0,007 s, dane w locie przepadają)
			 *   -q N>0 -> czekaj N s, potem wyjdź bezwarunkowo
			 *            (POMIAR dla -q 2: 2,007 s)
			 *   -q N<0 -> czekaj wiecznie == brak flagi
			 * Czekanie realizujemy przez DEADLINE na zegarze
			 * monotonicznym, a nie przez odliczanie przy każdym
			 * poll() - inaczej każdy odebrany bajt przedłużałby
			 * życie procesu i "-q 2" nigdy by nie wyszło.
			 *
			 * Uwaga na interakcję: -q 0 bez -N nie wysyła FIN
			 * jawnie; FIN leci przy close() w jądrze, bez
			 * gwarancji, że bufor wyjściowy został opróżniony.
			 */
			if (qflag == 0)
				return;
			if (qflag > 0 && !quitting) {
				set_deadline(&quit_deadline, qflag);
				quitting = 1;
			}
		}
		/* [netcat.c:1297-1300] Sieć zamknięta i kolejka pusta ->
		 * nie ma po co trzymać stdout. */
		if (pfd[POLL_NETIN].fd == -1 && netinbufpos == 0)
			pfd[POLL_STDOUT].fd = -1;
	}
}

/*
 * [netcat.c:1304-1334] drainbuf()
 * Zapisuje bufor do fd i przesuwa ogon na początek. CZĘŚCIOWY ZAPIS JEST
 * NORMĄ, nie błędem - write(2) na nieblokującym sockecie oddaje tyle, ile
 * zmieści się w oknie. memmove() przesuwa resztę; w pyncat odpowiednikiem
 * jest `del buf[:n]` na bytearray (też memmove, ta sama złożoność O(n)).
 */
static ssize_t
drainbuf(int fd, unsigned char *buf, size_t *bufpos)
{
	ssize_t n;
	ssize_t adjust;

	/* Slot już wycofany - nie ma do czego pisać. */
	if (fd == -1)
		return -1;

	n = write(fd, buf, *bufpos);
	/* EAGAIN/EINTR to nie błąd, tylko "ponów" - stąd sentinel. */
	if (n == -1 && (errno == EAGAIN || errno == EINTR))
		n = WANT_POLLOUT;
	if (n <= 0)
		return n;

	adjust = (ssize_t)*bufpos - n;
	if (adjust > 0)
		memmove(buf, buf + n, (size_t)adjust);
	*bufpos -= (size_t)n;

	return n;
}

/*
 * [netcat.c:1336-1362] fillbuf()
 * Doczytuje do bufora od pozycji *bufpos. Zwraca 0 na EOF, -1 na błędzie
 * trwałym, WANT_POLL* przy EAGAIN/EINTR.
 */
static ssize_t
fillbuf(int fd, unsigned char *buf, size_t *bufpos)
{
	size_t num = BUFSIZE - *bufpos;
	ssize_t n;

	if (fd == -1)
		return -1;

	n = read(fd, buf + *bufpos, num);
	if (n == -1 && (errno == EAGAIN || errno == EINTR))
		n = WANT_POLLIN;
	if (n <= 0)
		return n;
	*bufpos += (size_t)n;

	return n;
}

/* --- porty ------------------------------------------------------------ */

/*
 * [netcat.c:1454-1472] strtoport()
 * Najpierw liczba 1..65535, przy porażce nazwa usługi z /etc/services.
 */
static int
strtoport(const char *portstr, int udp)
{
	struct servent *entry;
	const char *errstr;
	const char *proto;
	int port;

	proto = udp ? "udp" : "tcp";

	port = (int)nc_strtonum(portstr, 1, PORT_MAX, &errstr);
	if (errstr == NULL)
		return port;

	/*
	 * [netcat.c:1467-1471] Kolejność jest istotna: poza zakresem kończymy
	 * błędem OD RAZU, bez próby getservbyname(). Dzięki temu
	 * `pyncat_ref host 99999` mówi "port number too large", a nie
	 * "service unknown".
	 *
	 * Debian 1.226 drukuje tu errstr ze strtonum we WSZYSTKICH przypadkach
	 * (`nc -z 127.0.0.1 nosuchsvc` -> `port number invalid: nosuchsvc`).
	 * Idziemy za UPSTREAMEM (`service "%s" unknown`), bo to on jest
	 * kanonicznym źródłem projektu i bo tak samo robi pyncat w Pythonie.
	 */
	if (strcmp(errstr, "invalid") != 0)
		nc_errx("port number %s: %s", errstr, portstr);

	if ((entry = getservbyname(portstr, proto)) == NULL)
		nc_errx("service \"%s\" unknown", portstr);

	return ntohs((uint16_t)entry->s_port);
}

static char *
port_strdup(int port)
{
	char buf[16];
	char *p;

	snprintf(buf, sizeof(buf), "%d", port);
	if ((p = strdup(buf)) == NULL)
		nc_err("strdup");
	return p;
}

/*
 * [netcat.c:1479-1526] build_ports()
 * Buduje portlist[] - pojedynczy port albo zakres lo-hi.
 */
static void
build_ports(char *p)
{
	char *n;
	int hi, lo, cp;
	int x = 0;

	/*
	 * [netcat.c:1486] Zakres rozpoznajemy TYLKO wtedy, gdy pierwszy znak
	 * jest cyfrą i w stringu jest '-'. Dzięki temu nazwa usługi
	 * z minusem (http-alt) nie jest brana za zakres.
	 */
	if (isdigit((unsigned char)*p) && (n = strchr(p, '-')) != NULL) {
		*n = '\0';
		n++;

		/* [netcat.c:1490-1497] lo > hi -> swap. POMIAR: zakres
		 * 34830-34828 skanuje 34828, 34829, 34830 - rosnąco. */
		hi = strtoport(n, uflag);
		lo = strtoport(p, uflag);
		if (lo > hi) {
			cp = hi;
			hi = lo;
			lo = cp;
		}

		if (rflag) {
			/*
			 * [netcat.c:1499-1509] Permutacja Knutha (jak
			 * ip_randomid() w sys/netinet/ip_id.c). Kolejność
			 * przypisań jest krytyczna: NAJPIERW przenosimy
			 * portlist[cp] na portlist[x], POTEM nadpisujemy
			 * portlist[cp] nowym portem. Odwrotnie - gubimy
			 * element i zostaje dziura w liście.
			 */
			srandom((unsigned int)(time(NULL) ^ getpid()));
			for (x = 0; x <= hi - lo; x++) {
				cp = (int)rand_below((unsigned int)x + 1);
				portlist[x] = portlist[cp];
				portlist[cp] = port_strdup(x + lo);
			}
		} else {
			/* [netcat.c:1510-1516] Porty po kolei. */
			for (cp = lo; cp <= hi; cp++)
				portlist[x++] = port_strdup(cp);
		}
	} else {
		hi = strtoport(p, uflag);
		portlist[0] = port_strdup(hi);
	}
}

/*
 * Upstream nigdy nie zwalnia portlist[] - proces i tak się kończy.
 * My zwalniamy, bo Makefile ma cel `asan` i LeakSanitizer ma milczeć.
 * Wywoływane przez atexit(), więc działa także na ścieżkach błędów.
 */
static void
free_ports(void)
{
	int i;

	for (i = 0; i <= PORT_MAX; i++) {
		free(portlist[i]);
		portlist[i] = NULL;
	}
}

/* --- UDP, komunikaty -------------------------------------------------- */

/*
 * [netcat.c:1533-1549] udptest()
 * Zwraca 1, gdy port UDP wygląda na otwarty, -1 gdy zamknięty,
 * 0 gdy testu nie wykonano.
 */
static int
udptest(int s)
{
	int i, ret = 0;

	/* [netcat.c:1538-1540] Piszemy tylko w trybie skanowania albo
	 * interaktywnym - inaczej wstrzykiwalibyśmy śmieci w dane. */
	if (!zflag && !isatty(STDIN_FILENO))
		return 0;

	/*
	 * CZTERY zapisy (i = 0..3), nie jeden - i to jest cała sztuczka.
	 * Na zamkniętym porcie UDP pierwszy write() SIĘ UDAJE, bo jądro nic
	 * jeszcze nie wie: datagram wyszedł i tyle. Dopiero po nim wraca
	 * ICMP port unreachable, które ustawia SO_ERROR na gniazdku,
	 * a KOLEJNY write() zwraca ECONNREFUSED. Jeden zapis nie wykryłby
	 * zamkniętego portu nigdy. Cztery zapisy dają ICMP czas na powrót
	 * także wtedy, gdy nie jest to loopback.
	 * Uwaga: to NIE jest skan bez danych, mimo nazwy "Zero-I/O mode" -
	 * do celu lecą 4 bajty "X".
	 */
	for (i = 0; i <= 3; i++) {
		if (write(s, "X", 1) == 1)
			ret = 1;
		else
			ret = -1;
	}

	return ret;
}

/*
 * [netcat.c:1551-1580] connection_info()
 * POMIAR formatu na 1.226 (gwiazdka zapisana tu jako [ast], bo dosłowne
 * "/" i gwiazdka w komentarzu to -Werror=comment):
 *   Connection to 127.0.0.1 34829 port [tcp/[ast]] succeeded!
 * Gwiazdka jest wtedy, gdy getservbyport() nie zna portu; z -n lookup się
 * nie wykonuje, więc zawsze jest gwiazdka.
 */
static void
connection_info(const char *host, const char *port, const char *proto,
    const char *ipaddr)
{
	struct servent *sv;
	const char *service = "*";

	if (!nflag) {
		const char *errstr;
		int p = (int)nc_strtonum(port, 1, PORT_MAX, &errstr);

		if (errstr != NULL)
			nc_errx("port number %s: %s", errstr, port);
		sv = getservbyport(htons((uint16_t)p), proto);
		if (sv != NULL)
			service = sv->s_name;
	}

	fprintf(stderr, "Połączenie z %s", host);
	/* IP drukujemy tylko, gdy wnosi coś względem nazwy hosta. */
	if (!nflag && strcmp(host, ipaddr) != 0)
		fprintf(stderr, " (%s)", ipaddr);
	fprintf(stderr, " port %s [%s/%s] - udane\n", port, proto, service);
}

/*
 * [netcat.c:1806-1834] report_sock()
 * "Nasłuchuję na 0.0.0.0 35100" / "Połączenie przychodzące od 127.0.0.1 54321".
 * Format jak w pyncat/report.py: polską frazę podaje wołający, bo polski
 * wymaga tu różnych przyimków, a oryginał sklejał wszystko z "on".
 */
static void
report_sock(const char *msg, const struct sockaddr *sa, socklen_t salen)
{
	char host[NI_MAXHOST], port[NI_MAXSERV];
	int herr;
	int flags = NI_NUMERICSERV;

	if (nflag)
		flags |= NI_NUMERICHOST;

	herr = getnameinfo(sa, salen, host, sizeof(host), port, sizeof(port),
	    flags);
	if (herr == EAI_SYSTEM)
		nc_err("getnameinfo");
	else if (herr != 0)
		nc_errx("getnameinfo: %s", gai_strerror(herr));

	fprintf(stderr, "%s %s %s\n", msg, host, port);
}

/* --- pomoc ------------------------------------------------------------ */

static void
usage(int ret)
{
	fprintf(stderr,
	    "użycie: pyncat_ref [-46dhklNnruvz] [-i sek] [-p port] [-q sek] "
	    "[-s adres]\n"
	    "                   [-W liczba] [-w sek] [cel] [port]\n");
	if (ret)
		exit(1);
}

static _Noreturn void
help(void)
{
	usage(0);
	fprintf(stderr,
	    "\n"
	    "    -4            wymuś IPv4\n"
	    "    -6            wymuś IPv6\n"
	    "    -d            nie czytaj ze stdin (detach)\n"
	    "    -h            ta pomoc\n"
	    "    -i sek        przerwa: w pętli relay oraz między portami\n"
	    "    -k            nie kończ po rozłączeniu klienta (wymaga -l)\n"
	    "    -l            nasłuchuj zamiast łączyć się\n"
	    "    -N            shutdown(SHUT_WR) po EOF na stdin\n"
	    "    -n            bez DNS (adresy i porty tylko numerycznie)\n"
	    "    -p port       port źródłowy (nie można razem z -l)\n"
	    "    -q sek        po EOF na stdin: 0 = wyjdź od razu, >0 = czekaj sek,\n"
	    "                  <0 = czekaj bez końca (domyślne)\n"
	    "    -r            losowa kolejność portów w zakresie\n"
	    "    -s adres      adres źródłowy\n"
	    "    -u            UDP zamiast TCP\n"
	    "    -v            więcej komunikatów\n"
	    "    -W liczba     zakończ po tylu odczytach z sieci\n"
	    "    -w sek        timeout na connect oraz idle timeout w pętli relay\n"
	    "    -z            tryb zero-I/O: sprawdź port, nie przesyłaj danych\n"
	    "\n"
	    "    Cel i port mogą być nazwami. Port może być zakresem: 20-25.\n"
	    "    Listy po przecinku NIE są obsługiwane - tak jak w oryginale.\n"
	    "\n"
	    "To jest referencyjna implementacja w C11 do porównania z wersją\n"
	    "w Pythonie (pyncat). Ten sam algorytm, te same flagi, te same kody\n"
	    "wyjścia - zobacz docs/c-vs-python.md.\n"
	    "\n"
	    "OSTRZEŻENIE: narzędzie służy do nauki. Łączyć się i nasłuchiwać wolno\n"
	    "wyłącznie na własnej infrastrukturze albo za pisemną zgodą\n"
	    "właściciela.\n");
	/*
	 * POMIAR: `nc -h` na Debianie 1.226 kończy się kodem 0, mimo że
	 * [netcat.c:1881] ma `exit(1)`. Idziemy za pomiarem - pomoc nie jest
	 * błędem - i tak samo robi pyncat w Pythonie.
	 */
	exit(0);
}
