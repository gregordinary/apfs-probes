#!/usr/bin/env bash
# Whether a container made where another container was keeps the older one's
# checkpoints, and which of the two macOS and fsck_apfs then read.
#
#   old-raw   a 2 GiB image holding one container on the whole disk, with
#             no partition map, made by newfs_apfs on the attached disk
#   old-gpt   a 2 GiB image with a GUID partition map, made by diskutil
#             eraseDisk APFS, the container in its APFS partition
#
# Each old container gets a second volume, OldTwo, and its first volume, Old,
# is then mounted, given a small file, synced and unmounted, over and over,
# until fsck_apfs checks a transaction of at least TARGET: by then the
# checkpoint descriptor area has been written round several times. Each is
# dumped fresh (old-LAYOUT-fresh) and aged (old-LAYOUT), with the container's
# UUID as diskutil lists it and the transaction fsck_apfs checks.
#
# Each route makes a new container over a copy of an old image, restored from
# its dump, in one way a person might reformat a disk, and names the new
# volume New:
#
#   raw-newfs-file      newfs_apfs on the image file, not attached
#   raw-newfs-dev       newfs_apfs on the attached whole disk
#   raw-erasevolume     diskutil eraseVolume APFS on the attached whole disk
#   raw-apfs-recreate   diskutil apfs deleteContainer, which formats the disk
#                       as something else, then diskutil apfs createContainer
#                       on the disk and diskutil apfs addVolume
#   raw-erasedisk       diskutil eraseDisk APFS GPT, which adds a partition map
#   gpt-newfs           newfs_apfs on the APFS partition
#   gpt-erasedisk       diskutil eraseDisk APFS GPT, as old-gpt was made
#   gpt-partitiondisk   diskutil partitionDisk with one APFS partition
#   gpt-erasevolume     diskutil eraseVolume APFS on the APFS partition
#   gpt-apfs-recreate   diskutil apfs deleteContainer, which formats the
#                       partition as something else, then createContainer
#                       on it and addVolume
#
# Every route but raw-erasedisk asks for the new container where the old one
# was, at its size. After the route the image is detached and dumped
# (ROUTE-made). A route that leaves no partition map is then checked on the
# image file with fsck_apfs -n and -n -W. The image is attached without
# mounting anything and listed, and checked with fsck_apfs -n and -n -W on
# the physical store of the container macOS lists; every volume listed is
# mounted and its root listed; and the image is detached and dumped again
# (ROUTE-mounted). listings.tsv names the listing taken of each image and the
# container on it, and reformatutil.py summary writes summary.json from them.
#
# Usage: reformat.sh OUTDIR [ROUTE...]
#   with no ROUTE, every route; the old images the routes need are made first

. "$(dirname "$0")/lib.sh" "$1"
shift
RU="$HERE/reformatutil.py"
SIZE=2g
TARGET=40
ROUTES="raw-newfs-file raw-newfs-dev raw-erasevolume raw-apfs-recreate raw-erasedisk"
ROUTES="$ROUTES gpt-newfs gpt-erasedisk gpt-partitiondisk gpt-erasevolume gpt-apfs-recreate"

ONLY=""
for a in "$@"; do
    case " $ROUTES " in
        *" $a "*) ONLY="$ONLY $a" ;;
        *) echo "reformat.sh: no route named $a" >&2; exit 1 ;;
    esac
done
[ -n "$ONLY" ] || ONLY=$ROUTES

# remember NAME : note in listings.tsv the listing list_named took last, and
# the container it found.
remember() {
    printf '%s\t%s\t%s\n' "$1" "$LIST" "$CONT" >>"$OUT/listings.tsv"
}

# age NAME VDEV COUNT : COUNT times, mount VDEV, write one new small file,
# sync and unmount. AGED numbers the files across calls.
AGED=0
age() {
    local n=$1 vdev=$2 count=$3 p mnt="$WORK/mnt-$1"
    for _ in $(seq 1 "$count"); do
        AGED=$((AGED + 1))
        p="$n-age$AGED"
        mount_vol "$p" "$vdev" "$mnt" || continue
        if [ "$AGED" = 1 ]; then
            sstep "$p-mdutil-off" 60 mdutil -i off "$mnt"
            sstep "$p-fseventsd" 30 sh -c 'mkdir -p "$1/.fseventsd" && touch "$1/.fseventsd/no_log"' sh "$mnt"
        fi
        sstep "$p-write" 60 "$PY" "$HERE/opsutil.py" write "$mnt/aged-$AGED" 4096 "$AGED"
        sstep "$p-sync" 60 sync
        unmount_vol "$p" "$vdev"
    done
}

# fsck_store NAME STORE [-W] : fsck_apfs -n on the attached physical store,
# as root if the invoking user cannot.
fsck_store() {
    local n=$1 store=$2
    shift 2
    step "$n" 600 fsck_apfs -n "$@" "/dev/r$store" ||
        sstep "$n-root" 600 fsck_apfs -n "$@" "/dev/r$store"
}

# old LAYOUT : make, age and dump old-LAYOUT.
old() {
    local l=$1 n=old-$1 img dev vdev store round x mnt
    section "$n"
    img="$WORK/$n.img"
    AGED=0
    step "$n-mkfile" 30 mkfile -n "$SIZE" "$img" || return
    dev=$(attach_raw "$n-format" "$img") || return
    if [ "$l" = raw ]; then
        step "$n-newfs" 300 newfs_apfs -v Old "$dev" ||
            sstep "$n-newfs-root" 300 newfs_apfs -v Old "$dev"
    else
        sstep "$n-erasedisk" 300 diskutil eraseDisk APFS Old GPT "$dev"
    fi
    if list_named "$n-format" "$dev"; then
        step "$n-addvol" 120 newfs_apfs -A -v OldTwo "/dev/$CONT" ||
            sstep "$n-addvol-root" 120 newfs_apfs -A -v OldTwo "/dev/$CONT"
    fi
    step "$n-format-disks" 60 diskutil list "$dev"
    detach "$n-format" "$dev"
    dump "$n-fresh" "$img"

    dev=$(attach_raw "$n" "$img") || return
    if list_named "$n-age" "$dev" && vdev=$(volume_named "$LIST" "$CONT" Old) &&
        store=$("$PY" "$HERE/disks.py" store "$OUT/log/$LIST.out" "$CONT"); then
        step "$n-store-info" 60 diskutil info -plist "$store"
        age "$n" "$vdev" 12
        for round in 1 2 3 4; do
            fsck_store "$n-fsck-$round" "$store"
            x=$("$PY" "$RU" xid "$OUT/log/$n-fsck-$round.out" "$OUT/log/$n-fsck-$round-root.out") || x=0
            [ "$x" -ge "$TARGET" ] && break
            [ "$round" = 4 ] || age "$n" "$vdev" 8
        done
        mnt="$WORK/mnt-$n"
        if mount_vol "$n-final" "$vdev" "$mnt"; then
            sstep "$n-final-ls" 60 ls -la "$mnt"
            unmount_vol "$n-final" "$vdev"
        fi
        fsck_store "$n-fsck" "$store"
        fsck_store "$n-fsck-warnings" "$store" -W
        step "$n-container-info" 60 diskutil info -plist "$CONT"
        list_named "$n-aged" "$dev"
        remember "$n"
    fi
    step "$n-disks" 60 diskutil list "$dev"
    detach "$n" "$dev"
    if [ "$l" = raw ]; then
        step "$n-fsck-file" 600 fsck_apfs -n "$img"
    fi
    dump "$n" "$img"
    rm -f "$img"
}

# recreate NAME DEV LAYOUT : diskutil apfs deleteContainer on the container
# open on DEV, then createContainer where it was, and addVolume.
recreate() {
    local n=$1 dev=$2 target=$2
    [ -n "$CONT" ] || return
    [ "$3" = gpt ] && target=/dev/$STORE
    sstep "$n-delete" 300 diskutil apfs deleteContainer "$CONT" Scratch
    step "$n-delete-disks" 60 diskutil list "$dev"
    sstep "$n-create" 300 diskutil apfs createContainer "$target" || return
    list_named "$n-created" "$dev" &&
        sstep "$n-addvol" 120 diskutil apfs addVolume "$CONT" APFS New -nomount
}

# examine NAME IMAGE : check the image as the route left it, then attach it,
# list and check what macOS finds on it, mount and list every volume, and
# dump it again.
examine() {
    local n=$1 img=$2 dev store i vdev name
    if [ "$("$PY" "$RU" layout "$img")" = raw ]; then
        step "$n-fsck-file" 600 fsck_apfs -n "$img"
        step "$n-fsck-file-warnings" 600 fsck_apfs -n -W "$img"
    fi
    if dev=$(attach_raw "$n" "$img"); then
        step "$n-disks" 60 diskutil list "$dev"
        list_named "$n" "$dev"
        remember "$n"
        if [ -n "$CONT" ]; then
            step "$n-container-info" 60 diskutil info -plist "$CONT"
            if store=$("$PY" "$HERE/disks.py" store "$OUT/log/$LIST.out" "$CONT"); then
                step "$n-store-info" 60 diskutil info -plist "$store"
                fsck_store "$n-fsck" "$store"
                fsck_store "$n-fsck-warnings" "$store" -W
            fi
            "$PY" "$HERE/disks.py" volumes "$OUT/log/$LIST.out" "$CONT" >"$WORK/$n-volumes.txt"
            i=0
            while IFS="	" read -r vdev name <&3; do
                i=$((i + 1))
                if mount_vol "$n-v$i" "$vdev" "$WORK/mnt-$n-$i"; then
                    sstep "$n-v$i-ls" 60 ls -la "$WORK/mnt-$n-$i"
                    unmount_vol "$n-v$i" "$vdev"
                fi
            done 3<"$WORK/$n-volumes.txt"
        fi
        detach "$n" "$dev"
    fi
    dump "$n-mounted" "$img"
}

# route NAME : one route, on a copy of the old image it needs.
route() {
    local n=$1 l=${1%%-*} img dev
    section "$n"
    img="$WORK/$n.img"
    if [ ! -f "$OUT/images/old-$l.bd" ]; then
        printf '#\t%s\n' "$n skipped: there is no old-$l image" >>"$STEPS"
        return
    fi
    step "$n-restore" 300 "$PY" "$ROOT/tools/blockdump.py" restore "$OUT/images/old-$l.bd" "$img" || return
    if [ "$n" = raw-newfs-file ]; then
        step "$n-newfs" 300 newfs_apfs -v New "$img"
    elif dev=$(attach_raw "$n-route" "$img"); then
        STORE=""
        list_named "$n-before" "$dev" &&
            STORE=$("$PY" "$HERE/disks.py" store "$OUT/log/$LIST.out" "$CONT")
        remember "$n-before"
        case "$n" in
            raw-newfs-dev)
                step "$n-newfs" 300 newfs_apfs -v New "$dev" ||
                    sstep "$n-newfs-root" 300 newfs_apfs -v New "$dev"
                ;;
            gpt-newfs)
                if [ -n "$STORE" ]; then
                    step "$n-newfs" 300 newfs_apfs -v New "/dev/$STORE" ||
                        sstep "$n-newfs-root" 300 newfs_apfs -v New "/dev/$STORE"
                fi
                ;;
            raw-erasevolume)
                sstep "$n-erasevolume" 300 diskutil eraseVolume APFS New "$dev"
                ;;
            gpt-erasevolume)
                [ -n "$STORE" ] && sstep "$n-erasevolume" 300 diskutil eraseVolume APFS New "/dev/$STORE"
                ;;
            raw-erasedisk | gpt-erasedisk)
                sstep "$n-erasedisk" 300 diskutil eraseDisk APFS New GPT "$dev"
                ;;
            gpt-partitiondisk)
                sstep "$n-partitiondisk" 300 diskutil partitionDisk "$dev" 1 GPT APFS New R
                ;;
            raw-apfs-recreate | gpt-apfs-recreate)
                recreate "$n" "$dev" "$l"
                ;;
        esac
        step "$n-after-disks" 60 diskutil list "$dev"
        list_named "$n-after" "$dev"
        remember "$n-after"
        detach "$n-route" "$dev"
    fi
    dump "$n-made" "$img"
    examine "$n" "$img"
    rm -f "$img"
}

section env
step sw_vers 30 sw_vers
step uname 30 uname -a
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step date 30 date -u '+%Y-%m-%dT%H:%M:%SZ %z'
step python 30 "$PY" -c 'import sys; print(sys.version)'
step image-size 30 echo "$SIZE"
step target 30 echo "$TARGET"
step filesystems 60 diskutil listFilesystems

case "$ONLY" in *raw-*) old raw ;; esac
case "$ONLY" in *gpt-*) old gpt ;; esac
for n in $ONLY; do
    route "$n"
done

section done
step mounts-end 30 mount
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
step summary 120 "$PY" "$RU" summary "$OUT"
exit 0
