#!/usr/bin/env bash
# What a file stored compressed looks like on a volume, for each compression
# type macOS reads.
#
#   env       versions of macOS and of afsctool, installed from Homebrew, and
#             the algorithm numbers libcompression's public header declares
#   afsctool  files of known content, compressed in place on an attached
#             image by afsctool with each compressor it offers
#   own       the same contents stored by compressutil.py through
#             libcompression with LZVN, LZFSE and LZBITMAP, each in the
#             layout type 8 has, in the attribute and in the resource fork
#   verify    the volume unmounted and mounted again, so every read goes
#             through the volume and not through what a writer left in
#             memory; every file read back by the kernel against its known
#             content, and the whole tree's manifest
#   streams   libcompression's encoding of small known inputs by each
#             algorithm, with each input beside it
#
# Usage: compress.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step sdk-path 60 xcrun --show-sdk-path
header="$(cat "$OUT/log/sdk-path.out" 2>/dev/null)/usr/include/compression.h"
step algorithms 30 "$PY" "$HERE/compressutil.py" algorithms "$header" "$OUT/algorithms.json"
step brew-version 60 brew --version
step brew-install-afsctool 1200 env HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 \
    brew install afsctool
step afsctool-version 60 brew list --versions afsctool
afsctool=$(command -v afsctool || echo "$(brew --prefix 2>/dev/null)/bin/afsctool")

section image
img="$WORK/compress.img"
step mkfile 30 mkfile -n 512m "$img"
dev=$(attach_raw compress "$img") || exit 0
rdev=/dev/r${dev#/dev/}
step newfs 120 newfs_apfs -v Compress "$dev" || sstep newfs-root 120 newfs_apfs -v Compress "$dev"
cont=$(container_of list "$dev") || exit 0
vdev=$(volume_named list "$cont" Compress) || exit 0
mnt="$WORK/mnt"
mount_vol compress "/dev/$vdev" "$mnt" || exit 0
sstep mdutil-off 60 mdutil -i off "$mnt"

section afsctool
step stage 120 "$PY" "$HERE/compressutil.py" stage "$WORK/stage" "$OUT/stage.json"
for c in ZLIB LZVN LZFSE; do
    d="$mnt/afsctool-$(echo "$c" | tr 'A-Z' 'a-z')"
    sstep "copy-$c" 120 cp -R "$WORK/stage" "$d"
    sstep "afsctool-$c" 600 "$afsctool" -c -T "$c" "$d"
    sstep "afsctool-$c-report" 120 "$afsctool" -v "$d"
done

section own
sstep own 600 "$PY" "$HERE/compressutil.py" own "$OUT/algorithms.json" "$mnt/own" "$OUT/own.json"
sstep ls 120 ls -laeO@R "$mnt"

section verify
unmount_vol compress "/dev/$vdev"
mount_vol remount "/dev/$vdev" "$mnt"
sstep verify 600 "$PY" "$HERE/compressutil.py" verify "$mnt" "$OUT/verify.json"
sstep manifest 600 "$PY" "$HERE/fsprobe.py" manifest "$mnt" "$OUT/manifest.json"
unmount_vol remount "/dev/$vdev"
step fsck 600 fsck_apfs -n -W "$rdev" || sstep fsck-root 600 fsck_apfs -n -W "$rdev"
detach compress "$dev"
dump compress "$img"
rm -f "$img"

section streams
step streams 600 "$PY" "$HERE/compressutil.py" streams "$OUT/algorithms.json" "$OUT/streams" "$OUT/streams.json"

section done
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
