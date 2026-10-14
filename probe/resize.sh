#!/usr/bin/env bash
# What a container records once diskutil has resized it. Each disk is a sparse
# 64 GiB image given a GUID partition map by diskutil partitionDisk, its
# container in the APFS partition:
#
#   small-made     a 2 GiB APFS partition, the rest of the disk free
#   small-shrunk   that container after diskutil apfs resizeContainer to 1 GiB
#   small-grown    and after diskutil apfs resizeContainer to the disk's end
#   whole-made     an APFS partition over the whole disk
#   whole-shrunk   that container after diskutil apfs resizeContainer to 2 GiB
#
# After each, the container is listed, its volume is mounted read-write, given
# a file, synced and unmounted, fsck_apfs -n checks the partition, diskutil
# info records where the partition begins, and the image is detached and
# dumped whole, partition map included, as images/STAGE.bd. The kernel's APFS
# messages during the run are kept in system-log.ndjson.
#
# Usage: resize.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"
mkdir -p "$WORK/mnt"

# stage NAME DEV IMAGE : list the container on DEV, write a file to its volume,
# check it, record its partition, then detach DEV and dump IMAGE. diskutil
# mounts the volume of a partition it has just made, so it is unmounted first.
stage() {
    local n=$1 dev=$2 img=$3 vdev store
    step "$n-disks" 60 diskutil list "$dev"
    if list_named "$n" "$dev"; then
        if store=$("$PY" "$HERE/disks.py" store "$OUT/log/$LIST.out" "$CONT"); then
            step "$n-store-info" 60 diskutil info -plist "/dev/$store"
        fi
        if vdev=$(first_volume "$LIST" "$CONT") && [ -n "$vdev" ]; then
            sstep "$n-unmount-first" 60 diskutil unmount "$vdev"
            if mount_vol "$n-rw" "$vdev" "$WORK/mnt/$n"; then
                sstep "$n-write" 60 sh -c 'echo "$1" >"$2/$1.txt"' sh "$n" "$WORK/mnt/$n"
                sstep "$n-sync" 60 sync
                unmount_vol "$n-rw" "$vdev"
            fi
        fi
        [ -n "${store-}" ] && sstep "$n-fsck" 600 fsck_apfs -n "/dev/$store"
    fi
    detach "$n" "$dev"
    dump "$n" "$img"
}

# resize NAME IMAGE SIZE : attach IMAGE, resize its container to SIZE as
# diskutil apfs resizeContainer takes it, and run the stage.
resize() {
    local n=$1 img=$2 size=$3 dev store
    dev=$(attach_raw "$n" "$img") || return 1
    if list_named "$n-before" "$dev" &&
        store=$("$PY" "$HERE/disks.py" store "$OUT/log/$LIST.out" "$CONT"); then
        sstep "$n-resize" 900 diskutil apfs resizeContainer "$store" "$size"
    fi
    stage "$n" "$dev" "$img"
}

# disk NAME IMAGE PARTITIONS... : a fresh sparse 64 GiB image, partitioned by
# diskutil partitionDisk with PARTITIONS, and its first stage.
disk() {
    local n=$1 img=$2 dev
    shift 2
    rm -f "$img"
    step "$n-mkfile" 30 mkfile -n 64g "$img" || return 1
    dev=$(attach_raw "$n" "$img") || return 1
    sstep "$n-partition" 300 diskutil partitionDisk "$dev" "$@"
    stage "$n" "$dev" "$img"
}

section env
step sw_vers 30 sw_vers
step uname 30 uname -a
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step date 30 date -u '+%Y-%m-%dT%H:%M:%SZ %z'
started=$(date '+%Y-%m-%d %H:%M:%S')

section small
img="$WORK/small.img"
disk small-made "$img" 2 GPT APFS Resized 2g "Free Space" Free R &&
    resize small-shrunk "$img" 1g &&
    resize small-grown "$img" 0
rm -f "$img"

section whole
img="$WORK/whole.img"
disk whole-made "$img" 1 GPT APFS Resized R &&
    resize whole-shrunk "$img" 2g
rm -f "$img"

section done
sstep system-log 600 sh -c 'log show --start "$1" --style ndjson --predicate "$2" >"$3"' sh "$started" \
    'senderImagePath CONTAINS[c] "apfs" OR process == "fsck_apfs"' "$OUT/system-log.ndjson"
step mounts-end 30 mount
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
