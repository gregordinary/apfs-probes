#!/usr/bin/env bash
# Three rules a reader relies on, each asked of macOS directly.
#
#   made     a fresh container newfs_apfs formats, its volume mounted
#            read-write: sourcesutil.py rawinline stores files whose content
#            after the compression header is the byte 0x06 then the content,
#            type 7, beside controls; files of known sizes are staged and
#            copied by `ditto --hfsCompression`; fs_snapshot_create(2) is asked
#            for snapshots named by 255, 256 and 1024 bytes. The volume is
#            mounted again, every stored file read back through the kernel,
#            and the first 16 bytes of every compressed file's
#            com.apple.decmpfs attribute recorded with its size; checked by
#            fsck_apfs -n -W, and dumped
#   reserve  a fresh container of 4 GiB, room for three volumes, to which
#            diskutil adds a volume reserving
#            8 MiB and one reserving 64 MiB; 40 MiB written to the first, past
#            its reserve, and 4 MiB to the second, within it; each unmounted,
#            the container listed, checked by fsck_apfs -n -W, and dumped, so
#            what its space manager counts as allocated from the reserves is
#            read from the blocks
#
# The kernel's APFS messages are kept in system-log.ndjson.
#
# Usage: sources.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"
SU="$HERE/sourcesutil.py"
CU="$HERE/compressutil.py"
mnt="$WORK/mnt"
mkdir -p "$mnt" "$OUT/sources"

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
                sstep made-raw 300 "$PY" "$SU" rawinline "$mnt/raw" "$OUT/sources/raw.json"
                sstep made-stage 300 "$PY" "$CU" stage "$mnt/stage" "$OUT/sources/stage.json"
                sstep made-ditto 600 ditto --hfsCompression "$mnt/stage" "$mnt/ditto"
                sstep made-snapnames 120 "$PY" "$SU" snapnames "$mnt" "$OUT/sources/snapnames.json"
                sstep made-ls 120 ls -laeO@R "$mnt"
                sstep made-sync 60 sync
                unmount_vol made "/dev/$vdev"
            fi
            if mount_vol made-again "/dev/$vdev" "$mnt"; then
                sstep made-again-raw 300 "$PY" "$SU" rawread "$mnt/raw" "$OUT/sources/raw.json" \
                    "$OUT/sources/raw-read.jsonl"
                sstep made-again-headers 300 "$PY" "$SU" headers "$mnt/ditto" "$OUT/sources/headers.json"
                unmount_vol made-again "/dev/$vdev"
            fi
        fi
        detach made "$dev"
    fi
    step made-fsck 600 fsck_apfs -n -W "$img"
    dump made "$img"
    rm -f "$img"
fi

section reserve
if img=$(newfs_file reserve 4g -v Base); then
    if dev=$(attach_raw reserve "$img"); then
        if cont=$(container_of reserve-list "$dev"); then
            sstep reserve-add-over 120 diskutil apfs addVolume "$cont" APFS Over -reserve 8388608B -nomount
            sstep reserve-add-under 120 diskutil apfs addVolume "$cont" APFS Under -reserve 64m -nomount
            if list_named reserve-added "$dev"; then
                for v in Over:40 Under:4; do
                    name=${v%%:*}
                    mib=${v##*:}
                    vdev=$(volume_named "$LIST" "$CONT" "$name") || continue
                    if mount_vol "reserve-$name" "/dev/$vdev" "$mnt"; then
                        sstep "reserve-$name-mdutil-off" 60 mdutil -i off "$mnt"
                        sstep "reserve-$name-fill" 600 "$PY" "$SU" fill "$mnt/fill" "$mib" \
                            "$OUT/sources/fill-$name.json"
                        sstep "reserve-$name-sync" 60 sync
                        unmount_vol "reserve-$name" "/dev/$vdev"
                    fi
                done
                apfs_list reserve-filled-list
            fi
        fi
        detach reserve "$dev"
    fi
    step reserve-fsck 600 fsck_apfs -n -W "$img"
    dump reserve "$img"
    rm -f "$img"
fi

section done
sstep system-log 600 sh -c 'log show --start "$1" --style ndjson --predicate "$2" >"$3"' sh "$started" \
    'senderImagePath CONTAINS[c] "apfs" OR senderImagePath CONTAINS[c] "compression" OR process == "fsck_apfs"' \
    "$OUT/system-log.ndjson"
step mounts-end 30 mount
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
