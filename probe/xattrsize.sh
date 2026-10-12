#!/usr/bin/env bash
# How large an extended attribute macOS sets on APFS: an ordinary attribute
# and a resource fork, each at sizes doubling from 4 KiB to 2 GiB and bisected
# to the byte at the first refusal, each read back by size; and a resource
# fork written at two positions 16 MiB apart, in a container dumped after it,
# for how a fork with a gap is stored.
#
# Usage: xattrsize.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step df 30 df -h "$WORK"

section ladder
img="$WORK/size.img"
step mkfile 30 mkfile -n 8g "$img"
dev=$(attach_raw size "$img") || exit 0
step newfs 120 newfs_apfs -v Size "$dev" || sstep newfs-root 120 newfs_apfs -v Size "$dev"
cont=$(container_of list "$dev") || exit 0
vdev=$(volume_named list "$cont" Size) || exit 0
mnt="$WORK/mnt"
mount_vol size "/dev/$vdev" "$mnt" || exit 0
sstep mdutil-off 60 mdutil -i off "$mnt"
sstep ordinary 1200 "$PY" "$HERE/xattrsizeutil.py" ladder "$mnt" user.probe 31 "$OUT/ordinary.jsonl"
sstep fork 1200 "$PY" "$HERE/xattrsizeutil.py" ladder "$mnt" com.apple.ResourceFork 31 "$OUT/fork.jsonl"
unmount_vol size "/dev/$vdev"
detach size "$dev"
rm -f "$img"

section gap
img="$WORK/gap.img"
step gap-mkfile 30 mkfile -n 256m "$img"
dev=$(attach_raw gap "$img") || exit 0
rdev=/dev/r${dev#/dev/}
step gap-newfs 120 newfs_apfs -v Gap "$dev" || sstep gap-newfs-root 120 newfs_apfs -v Gap "$dev"
cont=$(container_of gap-list "$dev") || exit 0
vdev=$(volume_named gap-list "$cont" Gap) || exit 0
mount_vol gap "/dev/$vdev" "$mnt" || exit 0
sstep gap-mdutil-off 60 mdutil -i off "$mnt"
sstep gap 120 "$PY" "$HERE/xattrsizeutil.py" gap "$mnt/gapped" "$OUT/gap.json"
unmount_vol gap "/dev/$vdev"
step gap-fsck 600 fsck_apfs -n -W "$rdev" || sstep gap-fsck-root 600 fsck_apfs -n -W "$rdev"
detach gap "$dev"
dump gap "$img"
rm -f "$img"

section done
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
