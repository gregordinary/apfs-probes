#!/usr/bin/env bash
# Where, between two sizes the ladder measured, newfs_apfs starts laying a
# fresh container out differently: every size in a range, densely enough that
# a second run over the sizes left between two forms finds the exact one.
#
# A range is BS/FIRST/LAST/STEP: containers of FIRST, FIRST + STEP, and so on,
# and LAST, blocks of BS bytes, each in a plain sparse file of exactly that
# many blocks. With no ranges given, these:
#
#   4096-byte blocks, each range between two ladder sizes laid out differently:
#     256,000 to 262,144 blocks, every 64
#     881,743 to 1,048,576, every 512
#     1,482,910 to 1,763,487, every 512
#     the smallest and the largest size in each of the eight spans of
#     4,128,768 blocks that follow 2,080,899,072
#     one less than each power of two among them, 262,143 and 1,048,575, and
#     1,572,863 and 1,572,864, either side of 6 GiB
#   8, 16, 32 and 64 KiB blocks, at the same block counts more sparsely, and
#     at eight sizes per doubling from 2^17 to 2^22 blocks
#
# Each container is formatted, checked, dumped and removed, as the ladder's
# are.
#
# Usage: bounds.sh OUTDIR [BS/FIRST/LAST/STEP ...]

. "$(dirname "$0")/lib.sh" "$1"
shift

for r in "$@"; do
    [[ $r =~ ^[0-9]+/[0-9]+/[0-9]+/[0-9]+$ ]] || { echo "bounds.sh: a range is BS/FIRST/LAST/STEP, not $r" >&2; exit 1; }
done

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step seek-support 30 "$PY" "$ROOT/tools/blockdump.py" seek-support "$WORK"

# One line per container, NAME KIB BS, from Python so bash 3.2's arithmetic
# never sees a size.
"$PY" - "$@" >"$OUT/sizes.txt" <<'EOF' || exit 1
import sys

SIZES = (4096, 8192, 16384, 32768, 65536)
LIMIT = 4000


def grid(bs, first, last, step):
    if bs not in SIZES or first < 1 or last < first or step < 1:
        raise SystemExit(f"bounds.sh: no range {bs}/{first}/{last}/{step}")
    return [(bs, n) for n in list(range(first, last + 1, step)) + [last]]


ranges = [tuple(int(x) for x in a.split("/")) for a in sys.argv[1:]]
if not ranges:
    near = [(256000, 262144), (881743, 1048576), (1482910, 1763487)]
    ranges = [(4096, 256000, 262144, 64), (4096, 881743, 1048576, 512),
              (4096, 1482910, 1763487, 512),
              (4096, 2080899073, 2109800449, 4128768),
              (4096, 2085027840, 2113929216, 4128768),
              (4096, 262143, 262143, 1), (4096, 1048575, 1048575, 1),
              (4096, 1572863, 1572864, 1)]
    for bs in SIZES[1:]:
        for (first, last), step in zip(near, (1024, 8192, 16384)):
            ranges.append((bs, first, last, step))
        for e in range(17 * 8, 22 * 8 + 1):
            n = int(2 ** (e / 8))
            ranges.append((bs, n, n, 1))
seen = sorted(set(c for r in ranges for c in grid(*r)))
if len(seen) > LIMIT:
    raise SystemExit(f"bounds.sh: {len(seen)} containers, more than {LIMIT}")
for bs, n in seen:
    print(f"b{bs}-{n}", n * bs // 1024, bs)
EOF

step count 30 wc -l "$OUT/sizes.txt"

section bounds
while read -r n kib bs; do
    args=()
    [ "$bs" = 4096 ] || args=(-b "$bs")
    img=$(newfs_file "$n" "${kib}k" "${args[@]+"${args[@]}"}" -v Bounds) || { rm -f "$WORK/$n.img"; continue; }
    step "$n-fsck" 300 fsck_apfs -n -W "$img"
    dump "$n" "$img"
    rm -f "$img"
done <"$OUT/sizes.txt"

section done
exit 0
