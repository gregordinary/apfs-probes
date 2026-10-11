#!/usr/bin/env bash
# How libcompression's LZBITMAP encoder writes inputs that each show it one
# thing, a step at a time (compressutil.py study), each encoded, decoded back,
# and kept beside its input; what its decoder makes of a few of those streams
# with each bit after the first block header changed in turn (compressutil.py
# oracle); what it makes of streams built by hand, each asking it one question
# about the format (compressutil.py rules, the streams in zbmcases.py); and
# how it encodes inputs of many blocks (compressutil.py corpus).
#
# Usage: lzbitmap.sh OUTDIR [study] [oracle] [rules] [corpus]
#        With no part named, every part runs.

. "$(dirname "$0")/lib.sh" "$1"
shift

parts=${*:-study oracle rules corpus}
for p in $parts; do
    case $p in study|oracle|rules|corpus) ;; *) echo "lzbitmap.sh: no part named $p" >&2; exit 1 ;; esac
done
wants() { case " $parts " in *" $1 "*) return 0 ;; esac; return 1; }

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step sdk-path 60 xcrun --show-sdk-path
header="$(cat "$OUT/log/sdk-path.out" 2>/dev/null)/usr/include/compression.h"
step algorithms 30 "$PY" "$HERE/compressutil.py" algorithms "$header" "$OUT/algorithms.json"

if wants study; then
    section study
    step study 600 "$PY" "$HERE/compressutil.py" study "$OUT/algorithms.json" "$OUT/study" "$OUT/study.json"
fi

if wants oracle; then
    section oracle
    step oracle 900 "$PY" "$HERE/compressutil.py" oracle "$OUT/algorithms.json" "$OUT/oracle.jsonl"
fi

if wants rules; then
    section rules
    step rules 300 "$PY" "$HERE/compressutil.py" rules "$OUT/algorithms.json" "$OUT/rules.jsonl"
fi

if wants corpus; then
    section corpus
    step corpus 600 "$PY" "$HERE/compressutil.py" corpus "$OUT/algorithms.json" "$OUT/corpus" "$OUT/corpus.json"
fi

section done
exit 0
