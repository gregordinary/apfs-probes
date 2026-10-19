#!/usr/bin/env bash
# Which extended attribute names macOS takes on APFS, by two routes.
#
#   sweep   a fresh container newfs_apfs formats, its volume mounted
#           read-write, and xattrnamesutil.py sweep setting, reading, listing
#           and removing one name after another: every length to 300 bytes
#           and beyond, characters of two to four bytes, and the namespaces
#           macOS gives no meaning to
#   images  the containers under xattrnames/, which ferrosys's APFS writer
#           formatted with names up to the longest it holds, and attributes
#           of those namespaces: each checked by fsck_apfs -n -W, its volume
#           mounted read-only, listed and read, and listed by apfs_checkseal;
#           mounted read-write, an attribute set on every file and each held
#           one set again; checked by fsck_apfs again, mounted read-only and
#           listed again, and dumped as macOS left it
#
# The kernel's APFS messages are kept in system-log.ndjson.
#
# Usage: xattrnames.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"
XU="$HERE/xattrnamesutil.py"
AU="$HERE/acceptutil.py"
XN="$ROOT/xattrnames"
mnt="$WORK/mnt"
mkdir -p "$mnt" "$OUT/names"

# mount_ro NAME VDEV : mount VDEV read-only at $mnt.
mount_ro() {
    sstep "$1-mount" 120 diskutil mount readOnly nobrowse -mountPoint "$mnt" "$2" ||
        sstep "$1-mount-plain" 120 diskutil mount readOnly -mountPoint "$mnt" "$2"
}

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step apfs-kext-plist 30 plutil -p /System/Library/Extensions/apfs.kext/Contents/Info.plist
step images 30 "$PY" -c 'import json, sys; print(json.dumps(json.load(open(sys.argv[1]))["made_by"]))' \
    "$XN/images.json"
started=$(date '+%Y-%m-%d %H:%M:%S')

section sweep
if img=$(newfs_file sweep 64m -v Sweep); then
    if dev=$(attach_raw sweep "$img"); then
        if cont=$(container_of sweep-list "$dev") && vdev=$(volume_named sweep-list "$cont" Sweep); then
            if mount_vol sweep "/dev/$vdev" "$mnt"; then
                sstep sweep-mdutil-off 60 mdutil -i off "$mnt"
                step sweep-names 600 "$PY" "$XU" sweep "$mnt" "$OUT/names/sweep.json"
                unmount_vol sweep "/dev/$vdev"
            fi
        fi
        detach sweep "$dev"
    fi
    step sweep-fsck 600 fsck_apfs -n -W "$img"
    rm -f "$img"
fi

section images
for n in $("$PY" -c 'import json, sys
for i in json.load(open(sys.argv[1]))["images"]:
    print(i["name"])' "$XN/images.json"); do
    img="$WORK/$n.img"
    step "$n-verify" 300 "$PY" "$AU" verify "$XN" "$n" || continue
    step "$n-restore" 300 "$PY" "$ROOT/tools/blockdump.py" restore "$XN/$n.bd" "$img" || continue
    step "$n-fsck" 600 fsck_apfs -n -W "$img"
    if dev=$(attach_raw "$n" "$img") && list_named "$n" "$dev" &&
        vdev=$(first_volume "$LIST" "$CONT"); then
        if mount_ro "$n-ro" "/dev/$vdev"; then
            step "$n-ro-list" 300 "$PY" "$XU" list "$mnt" "$XN" "$n" "$OUT/names/$n-ro.json"
            sstep "$n-ro-checkseal" 120 "$RES/apfs_checkseal" -q -v "/dev/$vdev"
            unmount_vol "$n-ro" "/dev/$vdev"
        fi
        if mount_vol "$n-rw" "/dev/$vdev" "$mnt"; then
            sstep "$n-rw-mdutil-off" 60 mdutil -i off "$mnt"
            sstep "$n-rw-set" 300 "$PY" "$XU" rw "$mnt" "$XN" "$n" "$OUT/names/$n-rw.json"
            sstep "$n-rw-sync" 60 sync
            unmount_vol "$n-rw" "/dev/$vdev"
        fi
        detach "$n" "$dev"
    fi
    step "$n-fsck-written" 600 fsck_apfs -n -W "$img"
    if dev=$(attach_raw "$n-back" "$img") && list_named "$n-back" "$dev" &&
        vdev=$(first_volume "$LIST" "$CONT"); then
        if mount_ro "$n-back" "/dev/$vdev"; then
            step "$n-back-list" 300 "$PY" "$XU" list "$mnt" "$XN" "$n" "$OUT/names/$n-back.json"
            unmount_vol "$n-back" "/dev/$vdev"
        fi
        detach "$n-back" "$dev"
    fi
    dump "$n-written" "$img"
    rm -f "$img"
done

section done
sstep system-log 600 sh -c 'log show --start "$1" --style ndjson --predicate "$2" >"$3"' sh "$started" \
    'senderImagePath CONTAINS[c] "apfs" OR process == "fsck_apfs"' "$OUT/system-log.ndjson"
step mounts-end 30 mount
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
