#!/usr/bin/env bash
# What diskutil apfs listSnapshots reports as a snapshot's Purgeable word comes
# from, and whether macOS will take a snapshot here that it does not report as
# purgeable.
#
#   tag     a container newfs_apfs formats on the runner, its volume mounted:
#           apfs_systemsnapshot, as root, asked to take a snapshot of it and
#           then to tag that snapshot as the one to root from, the volume's
#           snapshots listed after each, and the container dumped. Then the
#           container sent as accept/root-ramdisk.bd, its volume mounted and
#           apfs_systemsnapshot asked to tag the snapshot it holds, listed and
#           dumped the same way.
#   sweep   purgeableutil.py sweep over accept/root-ramdisk.bd: the objects
#           describing the volume's snapshots changed a few bits at a time,
#           each change attached, its volume mounted read-only and its
#           snapshots listed, for at most MINUTES minutes (default 30).
#           With slice K/N, the Kth of N runs of the words alone, so a sweep
#           can be N dispatches and a runner lost costs one of them. With
#           unsplit, only the words probe/purgeable-unsplit.tsv lists: those an
#           earlier sweep found changing the listing but did not split.
#
# Usage: purgeable.sh OUTDIR [tag] [sweep [MINUTES]] [unsplit] [slice K/N]
#   with no argument, both.

. "$(dirname "$0")/lib.sh" "$1"
shift
AU="$HERE/acceptutil.py"
ACC="$ROOT/accept"
mkdir -p "$WORK/mnt"

do_tag=""
do_sweep=""
minutes=30
part=()
only=()
[ $# -eq 0 ] && do_tag=1 && do_sweep=1
while [ $# -gt 0 ]; do
    case "$1" in
        tag) do_tag=1 ;;
        sweep) do_sweep=1
            if [[ ${2-} =~ ^[0-9]+$ ]]; then minutes=$2; shift; fi ;;
        unsplit) only=(--words "$HERE/purgeable-unsplit.tsv") ;;
        slice)
            [[ ${2-} =~ ^[0-9]+/[0-9]+$ ]] || { echo "slice takes K/N" >&2; exit 2; }
            part=(--slice "$2"); shift ;;
        *) echo "unknown argument $1" >&2; exit 2 ;;
    esac
    shift
done

# snaps NAME VDEV : list VDEV's snapshots as log/NAME.out.
snaps() {
    step "$1" 60 diskutil apfs listSnapshots -plist "$2"
}

# tag_pass N IMAGE NAME : mount the first volume of the container in IMAGE and
# ask apfs_systemsnapshot to take a snapshot called NAME where NAME is given,
# then to tag the volume's first snapshot to root from.
tag_pass() {
    local n=$1 img=$2 name=${3-} dev vdev mnt snap last
    dev=$(attach_raw "$n" "$img") || return 1
    if list_named "$n" "$dev" && vdev=$(first_volume "$LIST" "$CONT"); then
        mnt="$WORK/mnt/$n"
        mount_vol "$n" "$vdev" "$mnt"
        last="$n-snapshots-before"
        snaps "$last" "$vdev"
        if [ -n "$name" ]; then
            sstep "$n-take" 60 "$RES/apfs_systemsnapshot" -s "$name" -v "$mnt"
            last="$n-snapshots-taken"
            snaps "$last" "$vdev"
        fi
        snap=$("$PY" "$HERE/disks.py" snapshots "$OUT/log/$last.out" | head -1)
        if [ -n "$snap" ]; then
            sstep "$n-tag" 60 "$RES/apfs_systemsnapshot" -r "$snap" -v "$mnt"
            snaps "$n-snapshots-tagged" "$vdev"
        fi
        unmount_vol "$n" "$vdev"
    fi
    detach "$n" "$dev"
}

section env
step sw_vers 30 sw_vers
step uname 30 uname -a
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step csrutil 30 csrutil status
step authenticated-root 30 csrutil authenticated-root status

if [ -n "$do_tag" ]; then
    section tag
    if img=$(newfs_file newfs 256m -v Probe); then
        tag_pass newfs "$img" probe-taken
        dump newfs "$img"
        rm -f "$img"
    fi
    img="$WORK/root-ramdisk.img"
    if step root-verify 300 "$PY" "$AU" verify "$ACC" root-ramdisk &&
        step root-restore 300 "$PY" "$ROOT/tools/blockdump.py" restore "$ACC/root-ramdisk.bd" "$img"; then
        tag_pass root "$img"
        dump root "$img"
        rm -f "$img"
    fi
fi

if [ -n "$do_sweep" ]; then
    section sweep
    img="$WORK/sweep-base.img"
    if step sweep-verify 300 "$PY" "$AU" verify "$ACC" root-ramdisk &&
        step sweep-restore 300 "$PY" "$ROOT/tools/blockdump.py" restore "$ACC/root-ramdisk.bd" "$img"; then
        step sweep $((minutes * 60 + 1800)) "$PY" "$HERE/purgeableutil.py" sweep "$img" \
            "$OUT/sweep" --budget $((minutes * 60)) ${part[@]+"${part[@]}"} ${only[@]+"${only[@]}"}
        rm -f "$img"
    fi
fi
