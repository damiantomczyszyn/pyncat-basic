#!/bin/bash
# Przebieg pod AddressSanitizer + UBSan + LeakSanitizer.
# Pokrywa: transfer 1 MiB, wszystkie sciezki bledow (najczestsze zrodlo
# wyciekow z getaddrinfo), zakres 30 portow, petla -k, UDP.
# Konczy sie kodem != 0, gdy ktorykolwiek sanitizer cokolwiek zglosi.
set -u
cd "$(dirname "$0")/.."
SP="$(mktemp -d)"
trap 'rm -rf "$SP"' EXIT
export ASAN_OPTIONS=detect_leaks=1:abort_on_error=0:exitcode=99
export UBSAN_OPTIONS=print_stacktrace=1:halt_on_error=0
BIN=./c_reference/pyncat_ref_asan
if [ ! -x "$BIN" ]; then
	echo "brak $BIN - zbuduj przez: make -C c_reference asan" >&2
	exit 2
fi
# Klient do testu -k: najchetniej prawdziwy netcat (sprawdza interop pod
# ASAN-em), ale gdy go nie ma, wystarczy nasza wlasna binarka.
if [ -x /usr/bin/nc ] && /usr/bin/nc -h 2>&1 | grep -q "OpenBSD netcat"; then
	KCLIENT="/usr/bin/nc -N -w 3"
else
	KCLIENT="$BIN -N -w 3"
fi
fail=0

run_case() {
  local desc="$1"; shift
  "$@" >/dev/null 2>$SP/asan_case.log; local rc=$?
  if grep -qE 'ERROR: (Address|Leak)Sanitizer|runtime error|SUMMARY: ' $SP/asan_case.log; then
    echo "  BLAD SANITIZERA: $desc"; sed -n '1,12p' $SP/asan_case.log; fail=1
  else
    echo "  OK  $desc (exit=$rc)"
  fi
}

echo "### 1. Transfer 1 MiB pod ASAN, obie strony"
head -c 1048576 /dev/urandom > $SP/asan_in.bin
P=38910
( timeout 40 $BIN -l $P > $SP/asan_out.bin 2>$SP/asan_srv.log ) &
SRV=$!
sleep 2
timeout 40 $BIN -N -w 15 127.0.0.1 $P < $SP/asan_in.bin >/dev/null 2>$SP/asan_cli.log
echo "  klient exit=$?"
wait $SRV 2>/dev/null
if cmp -s $SP/asan_in.bin $SP/asan_out.bin; then echo "  dane: IDENTYCZNE ($(stat -c%s $SP/asan_out.bin) B)"; else echo "  dane: ROZNE"; fail=1; fi
for f in $SP/asan_srv.log $SP/asan_cli.log; do
  if grep -qE 'ERROR: (Address|Leak)Sanitizer|runtime error' $f; then echo "  BLAD w $f:"; sed -n '1,15p' $f; fail=1; fi
done
[ -s $SP/asan_srv.log ] && { echo "  (stderr serwera:)"; cat $SP/asan_srv.log; }
[ -s $SP/asan_cli.log ] && { echo "  (stderr klienta:)"; cat $SP/asan_cli.log; }

echo "### 2. Sciezki bledow (najczestsze zrodlo wyciekow z getaddrinfo)"
run_case "bledny host"            timeout 20 $BIN -w 2 -n nie.istnieje.invalid 80
run_case "port poza zakresem"     timeout 20 $BIN 127.0.0.1 99999
run_case "nieznana usluga"        timeout 20 $BIN 127.0.0.1 nosuchsvcxyz
run_case "zla kombinacja flag"    timeout 20 $BIN -l -z 1234
run_case "zly port zrodlowy"      timeout 20 $BIN -p 70000 127.0.0.1 80
run_case "-h"                     timeout 20 $BIN -h
run_case "bez argumentow"         timeout 20 $BIN

echo "### 3. Zakres 30 portow (30x getaddrinfo + socket + close)"
run_case "zakres -z"              timeout 60 $BIN -z -w 1 -n 127.0.0.1 39000-39029

echo "### 4. Petla -k z trzema polaczeniami (najgorszy przypadek na deskryptory)"
P=38920
( timeout 40 $BIN -l -k $P > $SP/asan_k.bin 2>$SP/asan_k.log ) &
SRV=$!
sleep 2
for m in A B C; do printf "$m" | timeout 15 $KCLIENT 127.0.0.1 $P >/dev/null 2>&1; sleep 0.4; done
sleep 0.5; kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
echo "  -k zebral: [$(cat $SP/asan_k.bin 2>/dev/null)]"
if grep -qE 'ERROR: (Address|Leak)Sanitizer|runtime error' $SP/asan_k.log; then echo "  BLAD:"; sed -n '1,15p' $SP/asan_k.log; fail=1; else echo "  OK  brak bledow sanitizera"; fi

echo "### 5. UDP pod ASAN"
P=38930
( timeout 25 $BIN -u -l $P > $SP/asan_u.bin 2>$SP/asan_u.log ) &
SRV=$!
sleep 2
printf 'UDP-ASAN' | timeout 15 $BIN -u -w 3 127.0.0.1 $P >/dev/null 2>$SP/asan_u2.log
sleep 0.5; kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
echo "  odebrano: [$(cat $SP/asan_u.bin 2>/dev/null)]"
for f in $SP/asan_u.log $SP/asan_u2.log; do
  grep -qE 'ERROR: (Address|Leak)Sanitizer|runtime error' $f && { echo "  BLAD w $f:"; sed -n '1,12p' $f; fail=1; }
done
echo
[ $fail -eq 0 ] && echo "=== ASAN + UBSAN + LeakSanitizer: CZYSTO ===" || echo "=== SA BLEDY ==="
exit $fail
