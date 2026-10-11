#!/usr/bin/env bash
# The geometry newfs_apfs chooses, at enough sizes to fit a model to.
#
#   steps     fresh containers from 1 MiB to 16 TiB at four sizes per
#             doubling, each in a plain sparse file
#   address   containers between 4 TiB and 8 TiB, in steps of 1/8 TiB
#   odd       sizes that are not a power of two, not a whole number of
#             mebibytes, or not a whole number of 4 KiB blocks
#   blocks    8, 16 and 64 KiB blocks at six sizes each
#   device    four of the sizes again, formatted through an attached disk
#             rather than the file, as a control on the file path
#
# Only the container is kept: each image is dumped and removed.
#
# Usage: ladder.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step seek-support 30 "$PY" "$ROOT/tools/blockdump.py" seek-support "$WORK"

# one NAME SIZE [newfs_apfs args] : format, check, dump and remove.
one() {
    local n=$1 size=$2 img
    shift 2
    img=$(newfs_file "$n" "$size" "$@" -v Ladder) || { rm -f "$WORK/$n.img"; return; }
    step "$n-fsck" 300 fsck_apfs -n -W "$img"
    dump "$n" "$img"
    rm -f "$img"
}

# Sizes in KiB, from the shell so bash 3.2's arithmetic never sees them.
sizes() {
    "$PY" - "$@" <<'EOF'
import sys
kind = sys.argv[1]
KiB, MiB, GiB, TiB = 1, 1024, 1024 ** 2, 1024 ** 3
if kind == "steps":
    out = []
    for e in range(0, 25):              # 1 MiB .. 16 TiB
        for q in range(4):
            k = int(MiB * 2 ** (e + q / 4))
            out.append(k - k % 4)
    out = sorted(set(x for x in out if x <= 16 * TiB))
elif kind == "address":
    out = [int(TiB * (4 + i / 8)) for i in range(1, 32)]
elif kind == "odd":
    out = [1000 * MiB, 3 * GiB + 12, 5 * GiB + 4092, 100 * MiB + 4, 129 * MiB, 513 * MiB,
           127 * MiB, 33 * MiB, 17 * MiB, 3 * MiB, 100 * MiB + 1, 64 * MiB + 2]
else:
    raise SystemExit(kind)
for k in out:
    print(k)
EOF
}

section steps
for k in $(sizes steps); do
    one "steps-${k}k" "${k}k"
done

section address
for k in $(sizes address); do
    one "address-${k}k" "${k}k"
done

section odd
for k in $(sizes odd); do
    one "odd-${k}k" "${k}k"
done

section blocks
for bs in 8192 16384 65536; do
    for size in 64m 256m 1g 4g 64g 1024g; do
        one "blocks-$bs-$size" "$size" -b "$bs"
    done
done

section device
for size in 64m 1g 64g 4096g; do
    n=device-$size
    img="$WORK/$n.img"
    step "$n-mkfile" 30 mkfile -n "$size" "$img" || continue
    if dev=$(attach_raw "$n" "$img"); then
        step "$n-newfs" 600 newfs_apfs -v Ladder "$dev"
        detach "$n" "$dev"
        dump "$n" "$img"
    fi
    rm -f "$img"
done

section done
exit 0
