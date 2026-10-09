#!/usr/bin/env bash
# What the tools macOS ships beside fsck_apfs print, on containers whose
# contents are known.
#
#   usage    every executable in apfs.fs's Resources, run with no arguments
#            and with -h
#   fresh    a fresh container: sm_stats verbose, by file and by raw disk;
#            slurpAPFSMeta into a plain image, by physical and by container
#            disk; apfs_checkseal listing the volume; fsck_apfs's space
#            summary and XML report
#   loaded   the same for a container holding opsutil.py's small tree
#
# Tools run with their working directory under OUTDIR/cwd, so a file one
# writes where it stands is kept with the results.
#
# Usage: tools.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step resources 30 ls -la "$RES"

mkdir -p "$OUT/cwd"
cd "$OUT/cwd" || exit 1

section usage
for path in "$RES"/*; do
    [ -f "$path" ] && [ -x "$path" ] || continue
    t=$(basename "$path")
    step "usage-$t" 20 "$path"
    step "help-$t" 20 "$path" -h
done

# slurp NAME DISK : slurpAPFSMeta's copy of DISK's metadata as a plain
# image, kept as a dump when it is one.
slurp() {
    local n=$1 out="$WORK/$1.dmg"
    rm -f "$out"
    sstep "$n" 300 "$RES/slurpAPFSMeta" -d "$2" -g "$out" || return
    sudo -n chown "$(id -u)" "$out" 2>/dev/null
    step "$n-imageinfo" 60 hdiutil imageinfo "$out"
    dump "$n" "$out"
}

# examine NAME IMAGE : every tool against one container image.
examine() {
    local n=$1 img=$2 dev rdev cont vdev
    step "$n-sm_stats-file" 120 "$RES/sm_stats" -v "$img"
    step "$n-fsck-space" 120 fsck_apfs -n -s -d "$img"
    step "$n-fsck-xml" 120 fsck_apfs -n -x "$img"
    step "$n-diskimage_map" 60 "$RES/apfs_diskimage_map" -o json "$img"
    dev=$(attach_raw "$n" "$img") || return
    rdev=/dev/r${dev#/dev/}
    if cont=$(container_of "$n-list" "$dev"); then
        vdev=$(first_volume "$n-list" "$cont")
        step "$n-sm_stats-rdisk" 120 "$RES/sm_stats" -v "$rdev"
        sstep "$n-sm_stats-rdisk-root" 120 "$RES/sm_stats" -v "$rdev"
        slurp "$n-slurp-physical" "$dev"
        slurp "$n-slurp-container" "/dev/$cont"
        sstep "$n-checkseal-list" 120 "$RES/apfs_checkseal" -q -v "/dev/$vdev"
        sstep "$n-checkseal-unsigned" 120 "$RES/apfs_checkseal" -P -v "/dev/$vdev"
    fi
    detach "$n" "$dev"
}

section fresh
if img=$(newfs_file fresh 64m -v Fresh); then
    examine fresh "$img"
    dump fresh "$img"
fi

section loaded
if img=$(newfs_file loaded 64m -v Loaded); then
    if dev=$(attach_raw loaded-build "$img"); then
        if cont=$(container_of loaded-build-list "$dev") && vdev=$(first_volume loaded-build-list "$cont") &&
            mount_vol loaded "$vdev" "$WORK/mnt-loaded"; then
            sstep loaded-ownership 60 diskutil enableOwnership "$vdev"
            sstep loaded-mdutil 60 mdutil -i off "$WORK/mnt-loaded"
            sstep loaded-tree 120 "$PY" "$HERE/opsutil.py" tree "$WORK/mnt-loaded"
            sstep loaded-sync 60 sync
            step loaded-util-dirstats 30 "$RES/apfs.util" -S "$WORK/mnt-loaded"
            unmount_vol loaded "$vdev"
        fi
        detach loaded-build "$dev"
    fi
    examine loaded "$img"
    dump loaded "$img"
fi

section done
step mounts-end 30 mount
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
