#!/usr/bin/env bash
# How far hdiutil create -srcfolder's reproducibility reaches.
#
#   simple    opsutil.py's fixed tree, imaged twice, and the same tree
#             created in reverse order, imaged once: does the image depend
#             on the source's inode numbers and directory order?
#   rich      fsprobe.py's populated tree (links, special files, sparse and
#             compressed files, extended attributes, awkward names), less
#             its special files and immutable flags, built once and imaged
#             twice
#   variants  the simple tree as a case-sensitive volume, and at a fixed size
#
# Each image is converted to a raw image, checked, listed with
# apfs_checkseal, and dumped. hdiutil's own sizing is recorded by imageinfo.
#
# Usage: srcfolder.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'

# image NAME SRC [hdiutil create args] : SRC as a bare-container image.
image() {
    local n=$1 src=$2 dev cont vdev
    shift 2
    rm -f "$WORK/$n.dmg" "$WORK/$n.cdr"
    sstep "$n-create" 300 hdiutil create -srcfolder "$src" -layout NONE -format UDRW \
        -volname Rep "$@" "$WORK/$n.dmg" || return
    sudo -n chown "$(id -u)" "$WORK/$n.dmg" 2>/dev/null
    step "$n-imageinfo" 60 hdiutil imageinfo "$WORK/$n.dmg"
    step "$n-convert" 300 hdiutil convert "$WORK/$n.dmg" -format UDTO -o "$WORK/$n" || return
    step "$n-fsck" 300 fsck_apfs -n -W "$WORK/$n.cdr"
    if dev=$(attach_raw "$n" "$WORK/$n.cdr"); then
        if cont=$(container_of "$n-list" "$dev") && vdev=$(first_volume "$n-list" "$cont"); then
            sstep "$n-checkseal" 300 "$RES/apfs_checkseal" -q -v "/dev/$vdev"
        fi
        detach "$n" "$dev"
    fi
    dump "$n" "$WORK/$n.cdr"
    rm -f "$WORK/$n.dmg" "$WORK/$n.cdr"
}

section simple
step tree 120 "$PY" "$HERE/opsutil.py" srctree "$WORK/simple"
step tree-reverse 120 "$PY" "$HERE/opsutil.py" srctree-reverse "$WORK/simple-reverse"
step tree-ls 60 ls -laeO@iR "$WORK/simple" "$WORK/simple-reverse"
image simple-1 "$WORK/simple" -fs APFS
image simple-2 "$WORK/simple" -fs APFS
image simple-reverse "$WORK/simple-reverse" -fs APFS

section rich
mkdir -p "$WORK/rich"
sstep rich-populate 900 "$PY" "$HERE/fsprobe.py" populate "$WORK/rich" "$OUT/rich-populate.json"
# hdiutil create -srcfolder refuses a tree holding a socket ("Operation not
# supported on socket") or a device node ("Operation not supported by
# device"), and fails on an immutable file on macOS 15 ("Operation not
# permitted"). On 26 and 27 it did not finish in 10 minutes on a tree that
# still held a FIFO, the likeliest cause. So special files are removed and
# the immutable and append-only flags cleared before the manifest.
sstep rich-specials 60 find "$WORK/rich" \( -type s -o -type b -o -type c -o -type p \) -print -delete
sstep rich-unflag 60 chflags -R nouchg,noschg,nouappnd,nosappnd "$WORK/rich"
sstep rich-manifest 300 "$PY" "$HERE/fsprobe.py" manifest "$WORK/rich" "$OUT/rich-manifest.json"
image rich-1 "$WORK/rich" -fs APFS
image rich-2 "$WORK/rich" -fs APFS

section variants
image simple-cs "$WORK/simple" -fs "Case-sensitive APFS"
image simple-64m "$WORK/simple" -fs APFS -size 64m

section done
sudo -n rm -rf "$WORK/rich" 2>/dev/null
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
