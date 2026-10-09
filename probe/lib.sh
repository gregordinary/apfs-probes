# Shared steps for the probes. Source with the output directory as $1.
#
# Every command runs as a named step under a time limit. Its stdout goes to
# log/NAME.out, its stderr to log/NAME.err, and one line per step goes to
# steps.tsv: name, exit status, seconds, command. A failed step is a result
# rather than an abort, so one refusal never hides the observations after it.
# Exit status 142 means the time limit ended the step.

set -u

OUT=${1:?output directory}
mkdir -p "$OUT/log" "$OUT/images"
OUT=$(cd "$OUT" && pwd)
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
PY=$(command -v python3)
WORK=$(mktemp -d "${RUNNER_TEMP:-/tmp}/probe.XXXXXX")
STEPS="$OUT/steps.tsv"
: >"$STEPS"

# _record NAME STATUS SECONDS CMD...
_record() {
    local name=$1 rc=$2 secs=$3
    shift 3
    printf '%s\t%s\t%s\t%s\n' "$name" "$rc" "$secs" "$*" >>"$STEPS"
    printf '%-40s exit=%-4s %4ss\n' "$name" "$rc" "$secs" >&2
}

# step NAME LIMIT CMD... : run CMD with LIMIT seconds as the invoking user.
step() {
    local name=$1 limit=$2 rc t0
    shift 2
    t0=$(date +%s)
    perl -e 'alarm shift; exec { $ARGV[0] } @ARGV or die "exec: $!\n"' "$limit" "$@" \
        >"$OUT/log/$name.out" 2>"$OUT/log/$name.err" </dev/null
    rc=$?
    [ -s "$OUT/log/$name.err" ] || rm -f "$OUT/log/$name.err"
    _record "$name" "$rc" $(($(date +%s) - t0)) "$@"
    return "$rc"
}

# sstep NAME LIMIT CMD... : as step, as root.
sstep() {
    local name=$1 limit=$2 rc t0
    shift 2
    t0=$(date +%s)
    sudo -n perl -e 'alarm shift; exec { $ARGV[0] } @ARGV or die "exec: $!\n"' "$limit" "$@" \
        >"$OUT/log/$name.out" 2>"$OUT/log/$name.err" </dev/null
    rc=$?
    [ -s "$OUT/log/$name.err" ] || rm -f "$OUT/log/$name.err"
    _record "$name" "$rc" $(($(date +%s) - t0)) sudo "$@"
    return "$rc"
}

section() {
    printf '\n== %s\n' "$1"
    printf '#\t%s\n' "$1" >>"$STEPS"
}

# attach_raw NAME IMAGE [hdiutil args] : attach IMAGE as a raw disk without
# mounting anything, and print the whole-disk device.
attach_raw() {
    local name=$1 img=$2
    shift 2
    step "$name-attach" 120 hdiutil attach -plist -nomount \
        -imagekey diskimage-class=CRawDiskImage "$@" "$img" || return 1
    "$PY" "$HERE/disks.py" attach-dev "$OUT/log/$name-attach.out"
}

# detach NAME DEV : detach, retrying while the disk is busy, then forcing.
detach() {
    local name=$1 dev=$2 i
    for i in 1 2 3 4 5; do
        step "$name-detach-$i" 60 hdiutil detach "$dev" && return 0
        sleep 2
    done
    step "$name-detach-force" 60 hdiutil detach -force "$dev"
}

# dump NAME IMAGE : keep IMAGE's nonzero blocks as images/NAME.bd.
dump() {
    step "$1-dump" 900 "$PY" "$ROOT/tools/blockdump.py" dump "$2" "$OUT/images/$1.bd"
}

# apfs_list NAME : the APFS listing as a plist, saved as log/NAME.out.
apfs_list() {
    step "$1" 60 diskutil apfs list -plist
}

RES=/System/Library/Filesystems/apfs.fs/Contents/Resources

# container_of LIST DEV : save the APFS listing as log/LIST.out and print the
# container on the attached disk DEV.
container_of() {
    apfs_list "$1" || return 1
    "$PY" "$HERE/disks.py" container-of "$OUT/log/$1.out" "$2"
}

# volume_named LIST CONTAINER NAME : the device of volume NAME in CONTAINER,
# read from log/LIST.out.
volume_named() {
    "$PY" "$HERE/disks.py" volume-named "$OUT/log/$1.out" "$2" "$3"
}

# first_volume LIST CONTAINER : the device of CONTAINER's first volume.
first_volume() {
    "$PY" "$HERE/disks.py" volumes "$OUT/log/$1.out" "$2" | head -1 | cut -f1
}

# mount_vol NAME VDEV MNT : mount VDEV at MNT out of the Finder's view, falling
# back to a plain mount where the option is refused.
mount_vol() {
    mkdir -p "$3"
    sstep "$1-mount" 120 diskutil mount nobrowse -mountPoint "$3" "$2" ||
        sstep "$1-mount-plain" 120 diskutil mount -mountPoint "$3" "$2"
}

# unmount_vol NAME VDEV : unmount, forcing if the volume stays busy.
unmount_vol() {
    sstep "$1-unmount" 120 diskutil unmount "$2" ||
        sstep "$1-unmount-force" 120 diskutil unmount force "$2"
}

# newfs_file NAME SIZE [newfs_apfs args] : a fresh container in a plain file,
# formatted as the invoking user; prints the file's path.
newfs_file() {
    local n=$1 size=$2 img
    shift 2
    img="$WORK/$n.img"
    rm -f "$img"
    step "$n-mkfile" 30 mkfile -n "$size" "$img" || return 1
    step "$n-newfs" 300 newfs_apfs "$@" "$img" || return 1
    echo "$img"
}
