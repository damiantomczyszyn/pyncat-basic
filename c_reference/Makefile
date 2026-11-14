# Makefile dla referencyjnej implementacji C11 (pyncat-basic/c_reference)
#
# Cele:
#   all     -> pyncat_ref        (-O2, pelne ostrzezenia, -Werror)
#   debug   -> pyncat_ref_debug  (-O0 -g3, do gdb)
#   asan    -> pyncat_ref_asan   (AddressSanitizer + UBSan + LeakSanitizer)
#   clean   -> usuwa wszystkie binarki
#
# Ma dzialac wywolany z katalogu repo jako `make -C c_reference`, dlatego
# wszystkie sciezki sa relatywne wzgledem tego Makefile'a.
#
# Zadnych rozszerzen GNU w kodzie: -std=c11 bez -std=gnu11, wiec plik sam
# ustawia _POSIX_C_SOURCE/_DEFAULT_SOURCE przed #include.

CC      ?= cc

STD      = -std=c11
# -Werror jest tu celowo na stale: plik ma kompilowac sie bez ANI JEDNEGO
# ostrzezenia, a jedyny sposob, zeby to utrzymac, to traktowac je jak bledy.
WARN     = -Wall -Wextra -Wpedantic -Werror
OPT      = -O2
DBGOPT   = -O0 -g3
# UBSan razem z ASan: oba sa tanie w tym kodzie, a UBSan wylapuje
# przepelnienia przy arytmetyce na size_t/ssize_t w drainbuf().
SAN      = -fsanitize=address,undefined -fno-omit-frame-pointer -g

SRC      = pyncat_ref.c
BIN      = pyncat_ref
BIN_DBG  = pyncat_ref_debug
BIN_ASAN = pyncat_ref_asan

.PHONY: all debug asan clean

all: $(BIN)

$(BIN): $(SRC)
	$(CC) $(STD) $(WARN) $(OPT) $(CPPFLAGS) $(CFLAGS) -o $@ $(SRC) $(LDFLAGS)

debug: $(BIN_DBG)

$(BIN_DBG): $(SRC)
	$(CC) $(STD) $(WARN) $(DBGOPT) $(CPPFLAGS) $(CFLAGS) -o $@ $(SRC) \
	    $(LDFLAGS)

asan: $(BIN_ASAN)

$(BIN_ASAN): $(SRC)
	$(CC) $(STD) $(WARN) $(DBGOPT) $(SAN) $(CPPFLAGS) $(CFLAGS) -o $@ \
	    $(SRC) $(SAN) $(LDFLAGS)

clean:
	rm -f $(BIN) $(BIN_DBG) $(BIN_ASAN)
