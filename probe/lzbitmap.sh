#!/usr/bin/env bash
# How libcompression's LZBITMAP encoder writes inputs that each show it one
# thing, a step at a time (compressutil.py study), each encoded, decoded back,
# and kept beside its input; and what its decoder makes of a few of those
# streams with each bit after the first block header changed in turn
# (compressutil.py oracle).
#
# Usage: lzbitmap.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step sdk-path 60 xcrun --show-sdk-path
header="$(cat "$OUT/log/sdk-path.out" 2>/dev/null)/usr/include/compression.h"
step algorithms 30 "$PY" "$HERE/compressutil.py" algorithms "$header" "$OUT/algorithms.json"

section study
step study 600 "$PY" "$HERE/compressutil.py" study "$OUT/algorithms.json" "$OUT/study" "$OUT/study.json"

section oracle
step oracle 900 "$PY" "$HERE/compressutil.py" oracle "$OUT/algorithms.json" "$OUT/oracle.jsonl"

section done
exit 0
