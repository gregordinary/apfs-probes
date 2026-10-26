#!/usr/bin/env bash
# Attributes in three places macOS may not keep them, and how macOS's own
# compressor stores a chunk that does not compress.
#
#   made     a fresh container newfs_apfs formats, its volume mounted
#            read-write: edgesutil.py make sets an attribute on a character
#            device, a block device and a named pipe, attribute names that
#            are not UTF-8, and resource forks of no bytes and of three,
#            recording each call; files whose 64 KiB chunks mix text with
#            bytes that do not compress are staged and copied by
#            `ditto --hfsCompression`; the volume mounted again and every one
#            of them read back; checked by fsck_apfs -n -W, and dumped
#   images   the containers under edges/, which ferrosys's APFS writer
#            formatted with the same three kinds of attribute, and a control:
#            each checked by fsck_apfs -n -W, its volume mounted read-only,
#            listed and read, and listed by apfs_checkseal; mounted
#            read-write, an attribute set on every path and each held one set
#            again; checked by fsck_apfs again, mounted read-only and listed
#            again, and dumped as macOS left it
#
# The kernel's APFS messages are kept in system-log.ndjson.
#
# Usage: edges.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"
EU="$HERE/edgesutil.py"
ED="$ROOT/edges"
mnt="$WORK/mnt"
mkdir -p "$mnt" "$OUT/edges"

# mount_ro NAME VDEV : mount VDEV read-only at $mnt.
mount_ro() {
    sstep "$1-mount" 120 diskutil mount readOnly nobrowse -mountPoint "$mnt" "$2" ||
        sstep "$1-mount-plain" 120 diskutil mount readOnly -mountPoint "$mnt" "$2"
}

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step apfs-kext-plist 30 plutil -p /System/Library/Extensions/apfs.kext/Contents/Info.plist
started=$(date '+%Y-%m-%d %H:%M:%S')

section made
if img=$(newfs_file made 256m -v Made); then
    if dev=$(attach_raw made "$img"); then
        if cont=$(container_of made-list "$dev") && vdev=$(volume_named made-list "$cont" Made); then
            if mount_vol made "/dev/$vdev" "$mnt"; then
                sstep made-mdutil-off 60 mdutil -i off "$mnt"
                sstep made-nodes 120 mkdir "$mnt/made"
                sstep made-make 300 "$PY" "$EU" make "$mnt/made" "$OUT/edges/made.json"
                sstep made-stage 300 "$PY" "$EU" stage "$mnt/stage" "$OUT/edges/stage.json"
                sstep made-ditto 600 ditto --hfsCompression "$mnt/stage" "$mnt/ditto"
                sstep made-ls 120 ls -laeO@R "$mnt"
                sstep made-sync 60 sync
                unmount_vol made "/dev/$vdev"
            fi
            if mount_vol made-again "/dev/$vdev" "$mnt"; then
                sstep made-again-held 300 "$PY" "$EU" again "$mnt/made" "$OUT/edges/made-again.json"
                sstep made-again-ditto 300 "$PY" "$EU" compressed "$mnt/ditto" \
                    "$OUT/edges/stage.json" "$OUT/edges/ditto.json"
                sstep made-again-stage 300 "$PY" "$EU" compressed "$mnt/stage" \
                    "$OUT/edges/stage.json" "$OUT/edges/stage-read.json"
                unmount_vol made-again "/dev/$vdev"
            fi
        fi
        detach made "$dev"
    fi
    step made-fsck 600 fsck_apfs -n -W "$img"
    dump made "$img"
    rm -f "$img"
fi

section images
for n in $("$PY" -c 'import json, sys
for i in json.load(open(sys.argv[1]))["images"]:
    print(i["name"])' "$ED/images.json"); do
    img="$WORK/$n.img"
    step "$n-restore" 300 "$PY" "$ROOT/tools/blockdump.py" restore "$ED/$n.bd" "$img" || continue
    step "$n-sha256" 120 shasum -a 256 "$img"
    step "$n-fsck" 600 fsck_apfs -n -W "$img"
    if dev=$(attach_raw "$n" "$img") && list_named "$n" "$dev" &&
        vdev=$(first_volume "$LIST" "$CONT"); then
        if mount_ro "$n-ro" "/dev/$vdev"; then
            sstep "$n-ro-list" 300 "$PY" "$EU" list "$mnt" "$ED" "$n" "$OUT/edges/$n-ro.json"
            sstep "$n-ro-ls" 120 ls -laeO@R "$mnt"
            sstep "$n-ro-checkseal" 120 "$RES/apfs_checkseal" -q -v "/dev/$vdev"
            unmount_vol "$n-ro" "/dev/$vdev"
        fi
        if mount_vol "$n-rw" "/dev/$vdev" "$mnt"; then
            sstep "$n-rw-mdutil-off" 60 mdutil -i off "$mnt"
            sstep "$n-rw-set" 300 "$PY" "$EU" rw "$mnt" "$ED" "$n" "$OUT/edges/$n-rw.json"
            sstep "$n-rw-sync" 60 sync
            unmount_vol "$n-rw" "/dev/$vdev"
        fi
        detach "$n" "$dev"
    fi
    step "$n-fsck-written" 600 fsck_apfs -n -W "$img"
    if dev=$(attach_raw "$n-back" "$img") && list_named "$n-back" "$dev" &&
        vdev=$(first_volume "$LIST" "$CONT"); then
        if mount_ro "$n-back" "/dev/$vdev"; then
            sstep "$n-back-list" 300 "$PY" "$EU" list "$mnt" "$ED" "$n" "$OUT/edges/$n-back.json"
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
