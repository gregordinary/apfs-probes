#!/usr/bin/env bash
# Which compression type numbers the kernel reads a file under, and whether it
# reads the algorithms libcompression declares beyond those the earlier probes
# stored.
#
#   env      versions of macOS, the algorithm numbers libcompression's public
#            header declares, compression_tool's usage, and the kernel's
#            sysctl names that mention compression
#   types    on an attached image, under every type number from 0 to 63, in
#            the decmpfs attribute and in the resource fork: LZRAVEN's and
#            LZMESH's encoding of known content in the layout type 8 has, and
#            a payload no algorithm wrote; LZFSE and LZBITMAP at the type
#            numbers the kernel reads them under, as controls
#   verify   the volume unmounted and mounted again, every file read back by
#            the kernel in a process of its own under a time limit, the tree's
#            manifest, and what the system log says about compressed files
#            over the probe's run
#
# On a release whose header declares neither algorithm, the payload no
# algorithm wrote is still stored under every type number, so the releases
# compare on that alone.
#
# Usage: decmpfs.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step sdk-path 60 xcrun --show-sdk-path
header="$(cat "$OUT/log/sdk-path.out" 2>/dev/null)/usr/include/compression.h"
step algorithms 30 "$PY" "$HERE/compressutil.py" algorithms "$header" "$OUT/algorithms.json"
step compression_tool-usage 30 compression_tool -h
step sysctl-compression 60 sh -c 'sysctl -a 2>/dev/null | grep -i -E "decmpfs|compress"'
started=$(date '+%Y-%m-%d %H:%M:%S')

section image
img="$WORK/decmpfs.img"
step mkfile 30 mkfile -n 512m "$img"
dev=$(attach_raw decmpfs "$img") || exit 0
rdev=/dev/r${dev#/dev/}
step newfs 120 newfs_apfs -v Decmpfs "$dev" || sstep newfs-root 120 newfs_apfs -v Decmpfs "$dev"
cont=$(container_of list "$dev") || exit 0
vdev=$(volume_named list "$cont" Decmpfs) || exit 0
mnt="$WORK/mnt"
mount_vol decmpfs "/dev/$vdev" "$mnt" || exit 0
sstep mdutil-off 60 mdutil -i off "$mnt"

section types
sstep types 900 "$PY" "$HERE/compressutil.py" types "$OUT/algorithms.json" "$mnt/types" "$OUT/types.json"
sstep ls 120 ls -laeO@R "$mnt/types"

section verify
unmount_vol decmpfs "/dev/$vdev"
mount_vol remount "/dev/$vdev" "$mnt"
sstep typeread 1500 "$PY" "$HERE/compressutil.py" typeread "$mnt/types" "$OUT/typeread.jsonl"
sstep manifest 600 "$PY" "$HERE/fsprobe.py" manifest "$mnt" "$OUT/manifest.json"
unmount_vol remount "/dev/$vdev"
step fsck 600 fsck_apfs -n -W "$rdev" || sstep fsck-root 600 fsck_apfs -n -W "$rdev"
detach decmpfs "$dev"
dump decmpfs "$img"
rm -f "$img"
sstep system-log 300 log show --start "$started" --style compact \
    --predicate 'eventMessage CONTAINS[c] "decmpfs" OR eventMessage CONTAINS[c] "compress" OR sender CONTAINS[c] "AppleFSCompression"'

section done
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
