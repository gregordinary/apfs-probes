#!/usr/bin/env bash
# Compression type 9's layout, asked of files of this script's own content
# only: the content after the marker byte 0xCC, as the files macOS ships under
# type 9 hold theirs, at sizes from 0 to 1 MiB; every other marker value; a
# header size that cuts or extends the content; and type 10, the content in
# the resource fork, alone and in type 8's table. Each file is read back by the
# kernel after a remount, and the system log over the reads is kept.
#
# Usage: type9layout.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

T9="$HERE/type9util.py"

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'

section image
img="$WORK/layout.img"
step mkfile 30 mkfile -n 512m "$img"
dev=$(attach_raw layout "$img") || exit 0
rdev=/dev/r${dev#/dev/}
step newfs 120 newfs_apfs -v Layout "$dev" || sstep newfs-root 120 newfs_apfs -v Layout "$dev"
cont=$(container_of list "$dev") || exit 0
vdev=$(volume_named list "$cont" Layout) || exit 0
mnt="$WORK/mnt"
mount_vol layout "/dev/$vdev" "$mnt" || exit 0
sstep mdutil-off 60 mdutil -i off "$mnt"

section layout
sstep layout 300 "$PY" "$T9" layout "$mnt/layout" "$OUT/layout.json"

section verify
unmount_vol layout "/dev/$vdev"
started=$(date '+%Y-%m-%d %H:%M:%S')
mount_vol remount "/dev/$vdev" "$mnt"
sstep layoutread 900 "$PY" "$T9" layoutread "$mnt/layout" "$OUT/layoutread.jsonl"
unmount_vol remount "/dev/$vdev"
step fsck 600 fsck_apfs -n -W "$rdev" || sstep fsck-root 600 fsck_apfs -n -W "$rdev"
detach layout "$dev"
dump layout "$img"
rm -f "$img"
sstep system-log 300 log show --start "$started" --style compact \
    --predicate 'eventMessage CONTAINS[c] "decmpfs" OR sender CONTAINS[c] "AppleFSCompression"'

section done
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
