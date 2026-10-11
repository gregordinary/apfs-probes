#!/usr/bin/env bash
# What the kernel reads compression type 9 as: the type of ~30,000 small files
# every macOS release ships, and of none the earlier probes wrote.
#
#   codecs   every algorithm number libcompression encodes and decodes with
#   own      on an attached image, files of type 9 and 10 holding this
#            script's bytes, varied one field at a time, each read back by the
#            kernel after a remount, with what the system log says during
#            each read
#   apple    every file of type 9 or 10 under /System, /usr, /Library, /bin
#            and /sbin: its sizes, and verdicts about its payload against the
#            content the kernel reads for it, among them whether a decoder
#            started at one of its first 33 offsets gives that content
#   copies   on a second attached image, which is never kept, copies of the
#            compression attribute of a sample of those files, unchanged, with
#            one byte of the payload changed, or with the header's size
#            changed; each read back by the kernel after a remount and
#            compared with what it reads from the original
#   log      the system log's messages during each read; for the copies, with
#            every number and everything quoted replaced, and the field the
#            kernel names as an unknown scheme when it changes
#
# The files macOS ships stay on the runner: the records keep their paths,
# sizes, digests where a digest cannot be inverted, and verdicts, as
# type9util.py states in full.
#
# Usage: type9.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

T9="$HERE/type9util.py"

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'

section codecs
step codecs 900 "$PY" "$T9" codecs "$OUT/codecs.json"

started=$(date '+%Y-%m-%d %H:%M:%S')

section own
img="$WORK/own.img"
step own-mkfile 30 mkfile -n 512m "$img"
if dev=$(attach_raw own "$img"); then
    rdev=/dev/r${dev#/dev/}
    step own-newfs 120 newfs_apfs -v Own "$dev" || sstep own-newfs-root 120 newfs_apfs -v Own "$dev"
    if cont=$(container_of own-list "$dev") && vdev=$(volume_named own-list "$cont" Own) &&
        mount_vol own "/dev/$vdev" "$WORK/own-mnt"; then
        sstep own-mdutil-off 60 mdutil -i off "$WORK/own-mnt"
        sstep own 300 "$PY" "$T9" own "$OUT/codecs.json" "$WORK/own-mnt/own" "$OUT/own.json"
        unmount_vol own "/dev/$vdev"
        mount_vol own-remount "/dev/$vdev" "$WORK/own-mnt"
        sstep ownread 900 "$PY" "$T9" ownread "$WORK/own-mnt/own" "$OUT/ownread.jsonl"
        unmount_vol own-remount "/dev/$vdev"
        step own-fsck 600 fsck_apfs -n -W "$rdev" || sstep own-fsck-root 600 fsck_apfs -n -W "$rdev"
    fi
    detach own "$dev"
    dump own "$img"
fi
rm -f "$img"

section apple
sstep apple 1500 "$PY" "$T9" apple "$OUT/codecs.json" "$OUT/apple.jsonl" "$OUT/apple-summary.json" 600 \
    /System /usr /Library /bin /sbin

section copies
img="$WORK/copy.img"
step copy-mkfile 30 mkfile -n 512m "$img"
if [ -s "$OUT/apple.jsonl" ] && dev=$(attach_raw copy "$img"); then
    step copy-newfs 120 newfs_apfs -v Copies "$dev" || sstep copy-newfs-root 120 newfs_apfs -v Copies "$dev"
    if cont=$(container_of copy-list "$dev") && vdev=$(volume_named copy-list "$cont" Copies) &&
        mount_vol copy "/dev/$vdev" "$WORK/copy-mnt"; then
        sstep copy-mdutil-off 60 mdutil -i off "$WORK/copy-mnt"
        sstep copies 300 "$PY" "$T9" copies "$OUT/apple.jsonl" "$WORK/copy-mnt/c" "$OUT/copies.json" \
            "$WORK/private.json"
        unmount_vol copy "/dev/$vdev"
        mount_vol copy-remount "/dev/$vdev" "$WORK/copy-mnt"
        sstep copyread 900 "$PY" "$T9" copyread "$WORK/copy-mnt/c" "$OUT/copies.json" "$OUT/copyread.jsonl"
        unmount_vol copy-remount "/dev/$vdev"
    fi
    detach copy "$dev"
fi
rm -f "$img"

section log
# The log itself stays in the work directory: the copies' messages leave only
# through copylog.
sstep system-log 600 sh -c 'log show --start "$1" --style ndjson --predicate "$2" >"$3"' sh "$started" \
    'eventMessage CONTAINS[c] "decmpfs" OR eventMessage CONTAINS[c] "compress" OR sender CONTAINS[c] "AppleFSCompression"' \
    "$WORK/system.ndjson"
sstep ownlog 300 "$PY" "$T9" ownlog "$WORK/system.ndjson" "$OUT/ownread.jsonl" "$OUT/ownlog.jsonl"
sstep copylog 300 "$PY" "$T9" copylog "$WORK/system.ndjson" "$OUT/copyread.jsonl" "$WORK/private.json" \
    "$OUT/copylog.json"
sstep schemes 300 "$PY" "$T9" schemes "$OUT/apple.jsonl" "$OUT/copylog.json" "$OUT/schemes.json"
sudo -n rm -rf "$WORK/copy-mnt" "$WORK/private.json" "$WORK/system.ndjson"

section done
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
