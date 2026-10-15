#!/usr/bin/env bash
# Whether macOS accepts the containers under accept/: empty containers ferrosys's
# APFS writer formatted, each stored as a block dump, with images.json stating
# what macOS should report of it. Beside them, three containers newfs_apfs
# formats on the runner go through the same batch, so a check that fails on
# Apple's own container is a check that is wrong, not a container.
#
# Each container goes through one batch:
#
#   sent    the dump checked against images.json's digests and restored, and
#           fsck_apfs -n -W run on the image
#   ro      attached, listed by diskutil apfs list and held to images.json;
#           every volume mounted read-only, described by diskutil info, its
#           entries recorded as the system calls report them, and listed by
#           apfs_checkseal -q -v
#   rw      attached again, every volume mounted read-write, a file written to
#           it and synced, and the volume unmounted; then fsck_apfs -n -W again
#           and the image dumped as macOS left it
#   back    attached again, every volume mounted read-only and the file read
#           back
#
# acceptutil.py verdict judges each container from its steps, and
# acceptutil.py index writes verdicts.json, with the release, at the end. The
# kernel's APFS messages during the run are kept in system-log.ndjson.
#
# Usage: accept.sh OUTDIR [few | many | controls | limits | trees | corpus | NAME...]
#   with no argument, every container; few, those of at most five volumes;
#   many, those of more; controls, the newfs_apfs containers; limits, those
#   whose names begin limit-; trees, those whose names begin tree-; corpus,
#   those whose names begin corpus-

. "$(dirname "$0")/lib.sh" "$1"
shift
AU="$HERE/acceptutil.py"
CU="$HERE/corpusutil.py"
ACC="$ROOT/accept"
mkdir -p "$WORK/mnt" "$OUT/volumes" "$OUT/files"

# now N : the time, to the microsecond, a line of times/N: N's first and last
# lines bound the kernel messages its batch drew.
now() {
    "$PY" -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat())' \
        >>"$OUT/times/$1"
}

# mount_at NAME VDEV MNT [readOnly] : mount VDEV at MNT.
mount_at() {
    mkdir -p "$3"
    sstep "$1-mount" 120 diskutil mount ${4-} -mountPoint "$3" "$2"
}

# devices LIST CONTAINER : every volume of CONTAINER in LIST, one device a line.
devices() {
    "$PY" "$HERE/disks.py" volumes "$OUT/log/$1.out" "$2" | cut -f1
}

# volume_uuid NAME : the identifier diskutil info reported in log/NAME.out.
volume_uuid() {
    "$PY" "$HERE/disks.py" key "$OUT/log/$1.out" VolumeUUID
}

# pass_ro N DEV : list the container on DEV and check every volume read-only.
pass_ro() {
    local n=$1 dev=$2 k=0 vdev p mnt
    list_named "$n-ro" "$dev" || return 1
    step "$n-listing" 60 "$PY" "$AU" listing "$ACC" "$OUT" "$n" "$OUT/log/$LIST.out" "$CONT"
    for vdev in $(cat "$OUT/log/$n-listing.out"); do
        k=$((k + 1))
        p="$n-ro-v$k"
        mnt="$WORK/mnt/$p"
        mount_at "$p" "$vdev" "$mnt" readOnly || continue
        sstep "$p-ownership" 60 diskutil enableOwnership "$vdev"
        step "$p-info" 60 diskutil info -plist "$vdev"
        sstep "$p-manifest" 300 "$PY" "$CU" manifest "$mnt" "$OUT/volumes/$p.json"
        sstep "$p-checkseal" 120 "$RES/apfs_checkseal" -q -v "/dev/$vdev"
        unmount_vol "$p" "$vdev"
    done
}

# pass_rw N DEV : write a file to every volume of the container on DEV.
pass_rw() {
    local n=$1 dev=$2 k=0 vdev p mnt uuid
    list_named "$n-rw" "$dev" || return 1
    for vdev in $(devices "$LIST" "$CONT"); do
        k=$((k + 1))
        p="$n-rw-v$k"
        mnt="$WORK/mnt/$p"
        mount_at "$p" "$vdev" "$mnt" || continue
        step "$p-info" 60 diskutil info -plist "$vdev"
        if uuid=$(volume_uuid "$p-info"); then
            sstep "$p-write" 120 "$PY" "$AU" write "$mnt" "$n" "$uuid" "$OUT/files/$p.json"
        fi
        sstep "$p-sync" 60 sync
        unmount_vol "$p" "$vdev"
    done
}

# pass_back N DEV : read the file back from every volume of the container on DEV.
pass_back() {
    local n=$1 dev=$2 k=0 vdev p mnt uuid
    list_named "$n-back" "$dev" || return 1
    for vdev in $(devices "$LIST" "$CONT"); do
        k=$((k + 1))
        p="$n-back-v$k"
        mnt="$WORK/mnt/$p"
        mount_at "$p" "$vdev" "$mnt" readOnly || continue
        step "$p-info" 60 diskutil info -plist "$vdev"
        if uuid=$(volume_uuid "$p-info"); then
            sstep "$p-read" 120 "$PY" "$AU" read "$mnt" "$n" "$uuid" "$OUT/files/$p.json"
        fi
        unmount_vol "$p" "$vdev"
    done
}

# batch N IMAGE : every pass over the container in IMAGE, then its verdict.
batch() {
    local n=$1 img=$2 dev
    step "$n-fsck" 600 fsck_apfs -n -W "$img"
    if dev=$(attach_raw "$n-ro" "$img"); then
        pass_ro "$n" "$dev"
        detach "$n-ro" "$dev"
    fi
    if dev=$(attach_raw "$n-rw" "$img"); then
        pass_rw "$n" "$dev"
        detach "$n-rw" "$dev"
    fi
    step "$n-fsck-written" 600 fsck_apfs -n -W "$img"
    dump "$n-written" "$img"
    if dev=$(attach_raw "$n-back" "$img"); then
        pass_back "$n" "$dev"
        detach "$n-back" "$dev"
    fi
    step "$n-verdict" 60 "$PY" "$AU" verdict "$ACC" "$OUT" "$n"
}

# sent N : restore N's dump and run the batch over it.
sent() {
    local n=$1 img="$WORK/$1.img"
    section "$n"
    now "$n"
    if step "$n-verify" 300 "$PY" "$AU" verify "$ACC" "$n" &&
        step "$n-restore" 300 "$PY" "$ROOT/tools/blockdump.py" restore "$ACC/$n.bd" "$img"; then
        batch "$n" "$img"
    else
        step "$n-verdict" 60 "$PY" "$AU" verdict "$ACC" "$OUT" "$n"
    fi
    rm -f "$img"
    now "$n"
}

# control N SIZE BYTES BLOCK-SIZE KIND : a container newfs_apfs formats in a file
# of SIZE, as mkfile takes it, or BYTES, through the batch.
control() {
    local n=$1 size=$2 bytes=$3 bs=$4 kind=$5 img keep=""
    section "$n"
    now "$n"
    [ "$kind" = keep ] && keep=-e
    step "$n-stated" 30 "$PY" "$AU" control "$OUT" "$n" "$bs" "$bytes" "$kind" Control
    if img=$(newfs_file "$n" "$size" -b "$bs" $keep -v Control); then
        batch "$n" "$img"
    else
        step "$n-verdict" 60 "$PY" "$AU" verdict "$ACC" "$OUT" "$n"
    fi
    rm -f "$WORK/$n.img"
    now "$n"
}

mkdir -p "$OUT/times"
section env
step sw_vers 30 sw_vers
step uname 30 uname -a
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step date 30 date -u '+%Y-%m-%dT%H:%M:%SZ %z'
step python 30 "$PY" -c 'import sys; print(sys.version)'
step apfs-fs-plist 30 plutil -p /System/Library/Filesystems/apfs.fs/Contents/Info.plist
step apfs-kext-plist 30 plutil -p /System/Library/Extensions/apfs.kext/Contents/Info.plist
step images 30 "$PY" -c 'import json, sys; print(json.dumps(json.load(open(sys.argv[1]))["made_by"]))' \
    "$ACC/images.json"
started=$(date '+%Y-%m-%d %H:%M:%S')

step select 30 "$PY" "$AU" select "$ACC" "$@" || exit 1
for n in $(cat "$OUT/log/select.out"); do
    case "$n" in
        control-b4k-mid) control "$n" 4g 4294967296 4096 fold ;;
        control-b16k-far-keep) control "$n" 25165824k 25769803776 16384 keep ;;
        control-b64k-floor) control "$n" 64m 67108864 65536 fold ;;
        *) sent "$n" ;;
    esac
done

section done
sstep system-log 600 sh -c 'log show --start "$1" --style ndjson --predicate "$2" >"$3"' sh "$started" \
    'senderImagePath CONTAINS[c] "apfs" OR process == "fsck_apfs"' "$OUT/system-log.ndjson"
step index 120 "$PY" "$AU" index "$ACC" "$OUT"
step mounts-end 30 mount
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
