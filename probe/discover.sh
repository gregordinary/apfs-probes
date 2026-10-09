#!/usr/bin/env bash
# What Apple's tools write, and what they accept.
#
#   env         versions of macOS, the APFS driver and the tools, and their usage
#   file-path   newfs_apfs and fsck_apfs pointed at a plain file
#   populated   a container with a case-insensitive and a case-sensitive volume,
#               each holding a fixed tree, snapshotted, changed, checked, dumped
#   ladder      fresh containers from 1 MiB to 16 TiB, and at block sizes
#               other than 4 KiB
#   hdiutil     the images `hdiutil create -fs APFS` makes, with and without a
#               partition map
#
# Usage: discover.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

section env
step sw_vers 30 sw_vers
step uname 30 uname -a
step sysctl 30 sysctl hw.model hw.pagesize hw.ncpu hw.memsize kern.osversion kern.osproductversion kern.version
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_OS|RUNNER_LABEL)=" | sort'
step bash-version 30 bash --version
step id 30 id
step sudo 30 sudo -n true
step df 30 df -h
step mounts-start 30 mount
step diskutil-list 60 diskutil list
step apfs-list-host 60 diskutil apfs list
step apfs-kext-plist 30 plutil -p /System/Library/Extensions/apfs.kext/Contents/Info.plist
step apfs-fs-plist 30 plutil -p /System/Library/Filesystems/apfs.fs/Contents/Info.plist
step apfs-fs-resources 30 ls -la /System/Library/Filesystems/apfs.fs/Contents/Resources
step kextstat-apfs 60 sh -c 'kmutil showloaded --list-only 2>/dev/null | grep -i apfs; kextstat -l 2>/dev/null | grep -i apfs'
step tools 30 sh -c 'for t in newfs_apfs fsck_apfs mount_apfs hdiutil diskutil ditto tmutil mdutil compression_tool afscexpand; do printf "%s: " "$t"; command -v "$t" || echo missing; done'
step python 30 "$PY" -c 'import sys; print(sys.version)'
step usage-newfs_apfs 30 newfs_apfs
step usage-fsck_apfs 30 fsck_apfs
step usage-diskutil-apfs 30 diskutil apfs
step usage-addVolume 30 diskutil apfs addVolume
step usage-createContainer 30 diskutil apfs createContainer
step usage-hdiutil-create 30 hdiutil create -help
step usage-hdiutil-attach 30 hdiutil attach -help
step usage-apfs.util 30 /System/Library/Filesystems/apfs.fs/Contents/Resources/apfs.util
step seek-support 30 "$PY" "$ROOT/tools/blockdump.py" seek-support "$WORK"

# Block zero of the container this runner booted from: the features a full
# macOS installation leaves set.
step host-root-info 60 diskutil info -plist /
host_store=$("$PY" - "$OUT/log/host-root-info.out" <<'EOF'
import plistlib, sys
data = open(sys.argv[1], "rb").read()
info = plistlib.loads(data[data.find(b"<?xml"):])
stores = info.get("APFSPhysicalStores") or []
print(stores[0]["APFSPhysicalStore"] if stores else "")
EOF
)
if [ -n "$host_store" ]; then
    sstep host-container-block0 60 dd if="/dev/r$host_store" of="$OUT/images/host-container-block0.bin" bs=4096 count=1
    sudo -n chown "$(id -u)" "$OUT/images/host-container-block0.bin" 2>/dev/null
fi

section file-path
img="$WORK/file.img"
step file-mkfile 30 mkfile -n 64m "$img"
step file-newfs-user 120 newfs_apfs -v FileTarget "$img" ||
    sstep file-newfs-root 120 newfs_apfs -v FileTarget "$img"
step file-fsck-user 300 fsck_apfs -n -W "$img"
sstep file-fsck-root 300 fsck_apfs -n -W "$img"
sudo -n chown "$(id -u)" "$img" 2>/dev/null
dump file-newfs "$img"
rm -f "$img"
step file2-mkfile 30 mkfile -n 64m "$img"
step file2-newfs-user 120 newfs_apfs -v FileTarget "$img"
dump file-newfs-2 "$img"
rm -f "$img"

section populated
NEWFS=sstep
img="$WORK/populated.img"
step pop-mkfile 30 mkfile -n 2g "$img"
if dev=$(attach_raw pop "$img"); then
    rdev=/dev/r${dev#/dev/}
    if step pop-newfs-user 120 newfs_apfs -v Default "$dev"; then
        NEWFS=step
    else
        sstep pop-newfs-root 120 newfs_apfs -v Default "$dev" ||
            sstep pop-createContainer 120 diskutil apfs createContainer "$dev"
    fi
    step pop-mounts-after-newfs 30 mount
    apfs_list pop-list-1
    if cont=$("$PY" "$HERE/disks.py" container-of "$OUT/log/pop-list-1.out" "$dev"); then
        $NEWFS pop-addvol-casefold 120 newfs_apfs -A -i -v CaseFold "$dev" ||
            sstep pop-addvol-casefold-diskutil 120 diskutil apfs addVolume "$cont" APFS CaseFold -nomount
        $NEWFS pop-addvol-casekeep 120 newfs_apfs -A -e -v CaseKeep "$dev" ||
            sstep pop-addvol-casekeep-diskutil 120 diskutil apfs addVolume "$cont" "Case-sensitive APFS" CaseKeep -nomount
        apfs_list pop-list-2
        for vol in CaseFold CaseKeep; do
            vdev=$("$PY" "$HERE/disks.py" volume-named "$OUT/log/pop-list-2.out" "$cont" "$vol") || continue
            p=pop-$vol
            mnt="$WORK/mnt-$vol"
            mkdir -p "$mnt"
            sstep "$p-mount" 120 diskutil mount -mountPoint "$mnt" "$vdev" || continue
            sstep "$p-mdutil-off" 60 mdutil -i off "$mnt"
            sstep "$p-ownership" 60 diskutil enableOwnership "$vdev"
            step "$p-info" 60 diskutil info -plist "$vdev"
            sstep "$p-populate" 900 "$PY" "$HERE/fsprobe.py" populate "$mnt" "$OUT/$p-populate.json"
            sstep "$p-manifest-before" 300 "$PY" "$HERE/fsprobe.py" manifest "$mnt" "$OUT/$p-manifest-before.json"
            sstep "$p-snapshot-syscall" 60 "$PY" "$HERE/fsprobe.py" snapshot "$mnt" probe-syscall "$OUT/$p-snapshot.json"
            sstep "$p-snapshot-tmutil" 180 tmutil localsnapshot "$mnt"
            step "$p-snapshots-diskutil" 60 diskutil apfs listSnapshots "$vdev"
            step "$p-snapshots-tmutil" 60 tmutil listlocalsnapshots "$mnt"
            sstep "$p-mutate" 120 "$PY" "$HERE/fsprobe.py" mutate "$mnt" "$OUT/$p-mutate.json"
            sstep "$p-manifest" 300 "$PY" "$HERE/fsprobe.py" manifest "$mnt" "$OUT/$p-manifest.json"
            sstep "$p-ls" 120 ls -laeO@R "$mnt"
            sstep "$p-unmount" 120 diskutil unmount "$vdev" ||
                sstep "$p-unmount-force" 120 diskutil unmount force "$vdev"
        done
    fi
    step pop-mounts-before-fsck 30 mount
    step pop-fsck-user 600 fsck_apfs -n -W "$rdev"
    sstep pop-fsck-root 600 fsck_apfs -n -W "$rdev"
    sstep pop-fsck-xml 600 fsck_apfs -n -W -x "$rdev"
    detach pop "$dev"
    dump populated "$img"
    # Whether an ordinary attach mounts a container with no partition map.
    step pop-reattach 120 hdiutil attach -plist -imagekey diskimage-class=CRawDiskImage "$img"
    step pop-reattach-mounts 30 mount
    if dev2=$("$PY" "$HERE/disks.py" attach-dev "$OUT/log/pop-reattach.out"); then
        detach pop-reattach "$dev2"
    fi
fi
rm -f "$img"

section ladder
# ladder NAME SIZE [newfs_apfs args] : format a fresh container of SIZE.
ladder() {
    local n=$1 size=$2 img dev
    shift 2
    img="$WORK/$n.img"
    step "$n-mkfile" 30 mkfile -n "$size" "$img" || return
    if dev=$(attach_raw "$n" "$img"); then
        $NEWFS "$n-newfs" 600 newfs_apfs "$@" -v Ladder "$dev"
        step "$n-mounts" 30 mount
        apfs_list "$n-list"
        detach "$n" "$dev"
        dump "$n" "$img"
    fi
    rm -f "$img"
}
sparse=no
grep -q '"seek_data": true' "$OUT/log/seek-support.out" && sparse=yes
for size in 1m 2m 4m 8m 16m 32m 64m 128m 256m 512m 1g 2g 4g 8g 16g 32g; do
    ladder "ladder-$size" "$size"
done
if [ "$sparse" = yes ]; then
    for size in 64g 128g 256g 512g 1024g 2048g 4096g 8192g 16384g; do
        ladder "ladder-$size" "$size"
    done
fi
for bs in 8192 16384 65536; do
    ladder "ladder-1g-b$bs" 1g -b "$bs"
done

section hdiutil
for layout in NONE default; do
    n=hc-$layout
    if [ "$layout" = NONE ]; then
        step "$n-create" 300 hdiutil create -size 64m -fs APFS -volname HC -layout NONE "$WORK/$n.dmg" || continue
    else
        step "$n-create" 300 hdiutil create -size 64m -fs APFS -volname HC "$WORK/$n.dmg" || continue
    fi
    step "$n-imageinfo" 60 hdiutil imageinfo "$WORK/$n.dmg"
    step "$n-convert" 300 hdiutil convert "$WORK/$n.dmg" -format UDTO -o "$WORK/$n"
    [ -f "$WORK/$n.cdr" ] && dump "$n" "$WORK/$n.cdr"
done

section done
step mounts-end 30 mount
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
