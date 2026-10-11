#!/usr/bin/env bash
# What fsck_apfs says when one byte of a container's metadata is wrong, for
# every byte, with the bytes chosen by position rather than by meaning.
#
#   fresh    a fresh 64 MiB container: every byte of every nonzero block
#   loaded   a 64 MiB container holding opsutil.py's small tree: every byte
#            of every object block
#
# Each base is checked and kept as a dump, then mutate.py changes one byte
# of a copy at a time and keeps what the checker prints. The optional
# arguments sweep one base, and one shard K of N of its blocks, so a base
# too large for one job can be shared out between runs.
#
# Usage: mutate.sh OUTDIR [fresh|loaded|both [K/N]]

. "$(dirname "$0")/lib.sh" "$1"

which=${2:-both}
shard=${3:-1/1}
case $which in fresh|loaded|both) ;; *) echo "mutate.sh: no base named $which" >&2; exit 1 ;; esac
[[ $shard =~ ^[0-9]+/[0-9]+$ ]] || { echo "mutate.sh: shard must be K/N" >&2; exit 1; }
budget=1680
[ "$which" = both ] && budget=780

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step ncpu 30 sysctl -n hw.ncpu

# sweep NAME IMAGE [mutate.py args] : check IMAGE, keep it, and sweep it.
sweep() {
    local n=$1 img=$2
    shift 2
    step "$n-fsck" 120 fsck_apfs -n -W "$img"
    dump "$n" "$img"
    step "$n-blocks" 60 "$PY" "$HERE/mutate.py" blocks "$img"
    step "$n-sweep" $((budget + 120)) "$PY" "$HERE/mutate.py" run "$img" "$OUT/mutate/$n" \
        --shard "$shard" --budget "$budget" "$@"
}

if [ "$which" != loaded ]; then
    section fresh
    img=$(newfs_file fresh 64m -v Mutate) && sweep fresh "$img"
fi

if [ "$which" != fresh ]; then
    section loaded
    if img=$(newfs_file loaded 64m -v Mutate) && dev=$(attach_raw loaded "$img"); then
        if cont=$(container_of loaded-list "$dev") && vdev=$(first_volume loaded-list "$cont") &&
            mount_vol loaded "$vdev" "$WORK/mnt-loaded"; then
            sstep loaded-mdutil 60 mdutil -i off "$WORK/mnt-loaded"
            sstep loaded-tree 120 "$PY" "$HERE/opsutil.py" tree "$WORK/mnt-loaded"
            sstep loaded-sync 60 sync
            unmount_vol loaded "$vdev"
        fi
        detach loaded "$dev"
        sweep loaded "$img" --objects
    fi
fi

section done
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
