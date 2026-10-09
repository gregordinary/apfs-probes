#!/usr/bin/env bash
# Which names APFS accepts, and which it takes for the same name.
#
# One 1 GiB container holds a case-insensitive volume and a case-sensitive
# one. In each, opsutil.py sweep creates one file per code point, in
# ascending order, and records each refusal, rewriting its result every
# 10,000 names. SWEEP_PART selects the code points (names-bmp.sh and
# names-rest.sh set it); unset, the sweep covers planes 0 to 3, plane 14's
# assigned block and a sample of every other plane.
#
# The container lives on a RAM disk, so creating names costs no disk-image
# I/O; it is copied to an image file once both volumes are unmounted. The
# image holds the stored name and hash of every name that was accepted.
#
# Usage: names.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

PART=${SWEEP_PART:-all}

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step part 30 echo "$PART"

section sweep
step names-ram 60 hdiutil attach -nomount ram://2097152 || exit 0
dev=$(awk 'NR == 1 {print $1}' "$OUT/log/names-ram.out")
rdev=/dev/r${dev#/dev/}
step names-newfs 300 newfs_apfs -i -v CaseFold "$dev" ||
    sstep names-newfs-root 300 newfs_apfs -i -v CaseFold "$dev"
if cont=$(container_of names-list "$dev"); then
    step names-add 120 newfs_apfs -A -e -v CaseKeep "/dev/$cont" ||
        sstep names-add-root 120 newfs_apfs -A -e -v CaseKeep "/dev/$cont"
    apfs_list names-list-2
    for vol in CaseFold CaseKeep; do
        vdev=$(volume_named names-list-2 "$cont" "$vol") || continue
        mnt="$WORK/mnt-$vol"
        mount_vol "names-$vol" "$vdev" "$mnt" || continue
        sstep "names-$vol-mdutil" 60 mdutil -i off "$mnt"
        sstep "names-$vol-nolog" 30 sh -c 'mkdir -p "$1/.fseventsd" && touch "$1/.fseventsd/no_log"' sh "$mnt"
        step "names-$vol-info" 60 diskutil info -plist "$vdev"
        sstep "names-$vol-sweep" 900 env SWEEP_PART="${SWEEP_PART:-}" \
            "$PY" "$HERE/opsutil.py" sweep "$mnt/sweep" "$OUT/sweep-$vol.json"
        sstep "names-$vol-sync" 60 sync
        unmount_vol "names-$vol" "$vdev"
    done
fi
sstep names-copy 600 dd if="$rdev" of="$WORK/names.img" bs=1048576
sudo -n chown "$(id -u)" "$WORK/names.img" 2>/dev/null
detach names "$dev"
step names-fsck 600 fsck_apfs -n -W "$WORK/names.img"
dump names "$WORK/names.img"

section done
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
