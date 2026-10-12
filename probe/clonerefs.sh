#!/usr/bin/env bash
# What macOS records about blocks that clones share, as the files that share
# them change: a clone's source removed or overwritten, a source written while
# its clone is not, a clone made after a snapshot of blocks the snapshot
# holds, a clone of a clone, two clones of one file, snapshots deleted while
# clones use their blocks, and a clone of a file kept in its resource fork.
# The scenes are in clonerefsutil.py.
#
# Each scene has a container of its own with one volume. Each stage of it is
# made in one mount; the volume is then unmounted and the image detached,
# checked with fsck_apfs -n and -n -W, and dumped, so every stage leaves an
# image of its own. After every stage, clonerefsutil.py records each file as
# macOS reads it back, beside the content the operations fixed for it.
#
# Usage: clonerefs.sh OUTDIR [b4k|b8k|b16k|b32k|b64k] [SCENE...]
#   the block size defaults to 4 KiB; with no SCENE, every scene

. "$(dirname "$0")/lib.sh" "$1"
shift
CU="$HERE/clonerefsutil.py"
SCENES_DIR="$OUT/scenes"

BS=4096
ONLY=""
for a in "$@"; do
    case "$a" in
        b4k) BS=4096 ;;
        b8k) BS=8192 ;;
        b16k) BS=16384 ;;
        b32k) BS=32768 ;;
        b64k) BS=65536 ;;
        *)
            "$PY" "$CU" stages "$a" >/dev/null || exit 1
            ONLY="$ONLY $a"
            ;;
    esac
done
[ -n "$ONLY" ] || ONLY=$("$PY" "$CU" scenes)

# open_volume NAME IMAGE : attach IMAGE, mount its one volume, and set DEV,
# VDEV and MNT.
open_volume() {
    local n=$1 img=$2 cont
    DEV="" VDEV="" MNT=""
    DEV=$(attach_raw "$n" "$img") || return 1
    cont=$(container_of "$n-list" "$DEV") || return 1
    VDEV=$(first_volume "$n-list" "$cont") || return 1
    [ -n "$VDEV" ] || return 1
    sstep "$n-mount" 120 diskutil mount "$VDEV" || return 1
    step "$n-info" 60 diskutil info -plist "$VDEV"
    MNT=$("$PY" "$HERE/disks.py" key "$OUT/log/$n-info.out" MountPoint) || return 1
    # Time Machine snapshots only the volumes a backup includes.
    sstep "$n-include" 60 tmutil removeexclusion -v "$MNT"
}

# close_volume NAME : unmount and detach what open_volume opened.
close_volume() {
    local n=$1
    [ -n "$VDEV" ] && unmount_vol "$n" "$VDEV"
    [ -n "$DEV" ] && detach "$n" "$DEV"
}

# scene NAME : a fresh container, then every stage of scene NAME.
scene() {
    local n=$1 img dev count k p
    section "$n"
    mkdir -p "$SCENES_DIR/$n"
    count=$("$PY" "$CU" stages "$n") || return
    img="$WORK/$n.img"
    step "$n-mkfile" 30 mkfile -n 1g "$img" || return
    if dev=$(attach_raw "$n-format" "$img"); then
        step "$n-newfs" 300 newfs_apfs -b "$BS" -v CloneRefs "$dev" ||
            sstep "$n-newfs-root" 300 newfs_apfs -b "$BS" -v CloneRefs "$dev"
        detach "$n-format" "$dev"
    fi
    for k in $(seq 1 "$count"); do
        p="$n-s$k"
        if open_volume "$p" "$img"; then
            if [ "$k" = 1 ]; then
                sstep "$p-mdutil-off" 60 mdutil -i off "$MNT"
                sstep "$p-fseventsd" 30 sh -c 'mkdir -p "$1/.fseventsd" && touch "$1/.fseventsd/no_log"' sh "$MNT"
            fi
            sstep "$p-stage" 600 "$PY" "$CU" stage "$MNT" "$VDEV" "$BS" "$n" "$k" \
                "$SCENES_DIR/$n/s$k.json"
            sstep "$p-sync" 60 sync
            step "$p-snapshots" 60 diskutil apfs listSnapshots -plist "$VDEV"
        fi
        close_volume "$p"
        step "$p-fsck" 600 fsck_apfs -n "$img"
        step "$p-fsck-warnings" 600 fsck_apfs -n -W "$img"
        dump "$p" "$img"
    done
    rm -f "$img"
}

section env
step sw_vers 30 sw_vers
step uname 30 uname -a
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step date 30 date -u '+%Y-%m-%dT%H:%M:%SZ %z'
step python 30 "$PY" -c 'import sys; print(sys.version)'
step block-size 30 echo "$BS"

section backupd
# Time Machine takes the snapshots, and its daemon is disabled on the runner
# images until it is enabled and loaded.
BACKUPD=/System/Library/LaunchDaemons/com.apple.backupd.plist
sstep backupd-enable 30 launchctl enable system/com.apple.backupd
sstep backupd-bootstrap 60 launchctl bootstrap system "$BACKUPD" ||
    sstep backupd-load 60 launchctl load -w "$BACKUPD"
step backupd-loaded 30 launchctl print system/com.apple.backupd

for n in $ONLY; do
    scene "$n"
done

section done
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
step check 120 "$PY" "$CU" check "$OUT"
step mounts-end 30 mount
exit 0
