#!/usr/bin/env bash
# Where the kernel stops reading content kept after the compression header:
# types 3, 7, 11 and 13 holding this script's content compressed, zlib's raw
# chunk under type 3, and type 9, at attribute lengths a byte apart from 3,790
# to 3,815, with content that does not compress and with content of more than
# twice its encoding; each file read back by the kernel after a remount.
#
# Usage: inlinelimit.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'

section image
img="$WORK/inline.img"
step mkfile 30 mkfile -n 512m "$img"
dev=$(attach_raw inline "$img") || exit 0
rdev=/dev/r${dev#/dev/}
step newfs 120 newfs_apfs -v Inline "$dev" || sstep newfs-root 120 newfs_apfs -v Inline "$dev"
cont=$(container_of list "$dev") || exit 0
vdev=$(volume_named list "$cont" Inline) || exit 0
mnt="$WORK/mnt"
mount_vol inline "/dev/$vdev" "$mnt" || exit 0
sstep mdutil-off 60 mdutil -i off "$mnt"

section inline
sstep inline 600 "$PY" "$HERE/compressutil.py" inline "$mnt/inline" "$OUT/inline.json"

section verify
unmount_vol inline "/dev/$vdev"
mount_vol remount "/dev/$vdev" "$mnt"
sstep inlineread 900 "$PY" "$HERE/compressutil.py" inlineread "$mnt/inline" "$OUT/inline.json" \
    "$OUT/inlineread.jsonl"
unmount_vol remount "/dev/$vdev"
step fsck 600 fsck_apfs -n -W "$rdev" || sstep fsck-root 600 fsck_apfs -n -W "$rdev"
detach inline "$dev"
dump inline "$img"
rm -f "$img"

section done
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
