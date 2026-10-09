#!/usr/bin/env bash
# What taking, changing under, mounting and deleting a snapshot writes.
#
# Every stage copies an earlier image, mounts its volume once, does one thing,
# and unmounts, as the ops probe does; a stage and the no-op stage made from
# the same image differ by that one thing. Snapshots are the ones Time Machine
# takes of a volume included in the backup (`tmutil localsnapshot`), which
# needs backupd loaded, as the timemachine probe found. The volume is mounted
# under /Volumes, where that probe saw Time Machine snapshot it.
#
#   chain    base (newfs_apfs, 1 GiB) -> m0 (first mount: Spotlight off,
#            .fseventsd/no_log, the source tree) -> m1 (mount, nothing)
#   include  inc (from m0: the volume included in the backup)
#            -> n0, n0b (nothing) and s1 (a snapshot)
#   change   s1 -> n1 (nothing), c1 (a change); inc -> c0 (the same change,
#            with no snapshot to keep)
#   second   c1 -> n2 (nothing), s2 (a second snapshot); s2 -> n3
#            (nothing), c2 (another change)
#   views    from c2: the live volume mounted read-only, each snapshot
#            mounted read-only beside it, and every tree recorded by
#            fsprobe.py manifest; and n4 (nothing)
#   delete   c2 -> d1 (the older snapshot deleted) -> d1w (mount, wait)
#            -> d2 (the other snapshot deleted) -> d2w (mount, wait)
#
# Each snapshot stage records the wall-clock time in nanoseconds before and
# after `tmutil localsnapshot`, and every stage lists the volume's snapshots
# with `diskutil apfs listSnapshots -plist`, so each value a snapshot stores
# can be set beside what Apple's tools say about it.
#
# Usage: snapshots.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

VOL=Snap
KEEP=" base m0 inc s1 c1 s2 c2 d1 d1w d2 "

copy_img() {
    cp -c "$WORK/$1.img" "$WORK/$2.img" 2>/dev/null || cp "$WORK/$1.img" "$WORK/$2.img"
}

done_img() {
    case "$KEEP" in
        *" $1 "*) ;;
        *) rm -f "$WORK/$1.img" ;;
    esac
}

now_ns() {
    step "$1" 30 "$PY" -c 'import time; print(time.time_ns())'
}

# Each op_NAME STAGE VDEV MNT runs one stage's operation on the mounted
# volume.
op_nothing() { :; }

op_populate() {
    local n=$1 mnt=$3
    sstep "$n-mdutil-off" 60 mdutil -i off "$mnt"
    sstep "$n-fseventsd" 30 sh -c 'mkdir -p "$1/.fseventsd" && touch "$1/.fseventsd/no_log"' sh "$mnt"
    sstep "$n-mkdir" 30 mkdir "$mnt/tree"
    sstep "$n-chown" 30 chown "$(id -u):$(id -g)" "$mnt/tree"
    step "$n-tree" 120 "$PY" "$HERE/opsutil.py" srctree "$mnt/tree"
}

op_include() {
    local n=$1 mnt=$3
    step "$n-isexcluded-before" 30 tmutil isexcluded "$mnt"
    sstep "$n-include" 60 tmutil removeexclusion -v "$mnt"
    step "$n-isexcluded" 30 tmutil isexcluded "$mnt"
    sstep "$n-prefs" 30 defaults read /Library/Preferences/com.apple.TimeMachine
}

# op_snapshot STAGE VDEV MNT : one local snapshot, including the volume
# first if the inclusion inc made did not carry over to this copy.
op_snapshot() {
    local n=$1 mnt=$3
    step "$n-isexcluded" 30 tmutil isexcluded "$mnt"
    if grep -q Excluded "$OUT/log/$n-isexcluded.out"; then
        sstep "$n-include" 60 tmutil removeexclusion -v "$mnt"
    fi
    now_ns "$n-t0"
    sstep "$n-localsnapshot" 300 tmutil localsnapshot
    now_ns "$n-t1"
    sstep "$n-listlocalsnapshots" 60 tmutil listlocalsnapshots "$mnt"
}

op_change() {
    local n=$1 t=$3/tree/top
    step "$n-write" 30 "$PY" "$HERE/opsutil.py" write "$t/added" 5000 9
    step "$n-extend" 30 "$PY" "$HERE/opsutil.py" extend "$t/hundred" 8192
    step "$n-remove" 30 rm "$t/sub2/deep/a"
    step "$n-rename" 30 mv "$t/one" "$t/one-renamed"
}

op_change2() {
    local n=$1 t=$3/tree/top
    step "$n-write" 30 "$PY" "$HERE/opsutil.py" write "$t/later" 3000 11
    step "$n-unlink" 30 rm "$t/three"
    step "$n-xattr" 30 "$PY" "$HERE/opsutil.py" setxattr "$t/under" user.later 100
}

# op_delete STAGE VDEV MNT : delete the oldest snapshot, by diskutil, or by
# Time Machine where diskutil refuses.
op_delete() {
    local n=$1 vdev=$2 mnt=$3 name date
    step "$n-before" 60 diskutil apfs listSnapshots -plist "$vdev" || return
    name=$("$PY" "$HERE/disks.py" snapshots "$OUT/log/$n-before.out" | head -1)
    [ -n "$name" ] || return
    if ! sstep "$n-delete" 300 diskutil apfs deleteSnapshot "$vdev" -name "$name"; then
        date=${name#com.apple.TimeMachine.}
        date=${date%.local}
        sstep "$n-delete-tm" 300 tmutil deletelocalsnapshots "$date"
    fi
}

op_wait() {
    sleep 20
}

# stage NAME FROM OP : copy image FROM, mount its volume, run op_OP, unmount,
# check and keep the result.
stage() {
    local n=$1 from=$2 op=$3 img dev cont vdev mnt
    img="$WORK/$n.img"
    copy_img "$from" "$n" || return 1
    if dev=$(attach_raw "$n" "$img"); then
        if cont=$(container_of "$n-list" "$dev") && vdev=$(volume_named "$n-list" "$cont" "$VOL") &&
            sstep "$n-mount" 120 diskutil mount "$vdev"; then
            sstep "$n-ownership" 60 diskutil enableOwnership "$vdev"
            step "$n-info" 60 diskutil info -plist "$vdev"
            if mnt=$("$PY" "$HERE/disks.py" key "$OUT/log/$n-info.out" MountPoint); then
                "op_$op" "$n" "$vdev" "$mnt"
                sstep "$n-sync" 60 sync
            fi
            step "$n-snapshots" 60 diskutil apfs listSnapshots -plist "$vdev"
            unmount_vol "$n" "$vdev"
        fi
        detach "$n" "$dev"
    fi
    step "$n-fsck" 300 fsck_apfs -n -W "$img"
    dump "$n" "$img"
    done_img "$n"
}

# views NAME FROM : copy image FROM, mount its volume read-only, record the
# live tree and each snapshot's tree, and keep the image, which nothing
# should have changed.
views() {
    local n=$1 from=$2 img dev cont vdev mnt i=0 name smnt
    img="$WORK/$n.img"
    copy_img "$from" "$n" || return 1
    if dev=$(attach_raw "$n" "$img"); then
        if cont=$(container_of "$n-list" "$dev") && vdev=$(volume_named "$n-list" "$cont" "$VOL") &&
            sstep "$n-mount" 120 diskutil mount readOnly "$vdev"; then
            step "$n-info" 60 diskutil info -plist "$vdev"
            if mnt=$("$PY" "$HERE/disks.py" key "$OUT/log/$n-info.out" MountPoint); then
                sstep "$n-live" 300 "$PY" "$HERE/fsprobe.py" manifest "$mnt/tree" "$OUT/$n-live.json"
            fi
            if step "$n-snapshots" 60 diskutil apfs listSnapshots -plist "$vdev"; then
                for name in $("$PY" "$HERE/disks.py" snapshots "$OUT/log/$n-snapshots.out"); do
                    i=$((i + 1))
                    smnt="$WORK/snap-$n-$i"
                    mkdir -p "$smnt"
                    if sstep "$n-snap$i-mount" 120 mount_apfs -o rdonly -s "$name" "/dev/$vdev" "$smnt"; then
                        sstep "$n-snap$i" 300 "$PY" "$HERE/fsprobe.py" manifest "$smnt/tree" "$OUT/$n-snap$i.json"
                        sstep "$n-snap$i-unmount" 120 umount "$smnt"
                    fi
                done
            fi
            unmount_vol "$n" "$vdev"
        fi
        detach "$n" "$dev"
    fi
    step "$n-fsck" 300 fsck_apfs -n -W "$img"
    dump "$n" "$img"
    done_img "$n"
}

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step date 30 date -u '+%Y-%m-%dT%H:%M:%SZ %z'
step tm-version 30 tmutil version
sstep tm-prefs 30 defaults read /Library/Preferences/com.apple.TimeMachine

section backupd
BACKUPD=/System/Library/LaunchDaemons/com.apple.backupd.plist
sstep backupd-enable 30 launchctl enable system/com.apple.backupd
sstep backupd-bootstrap 60 launchctl bootstrap system "$BACKUPD" ||
    sstep backupd-load 60 launchctl load -w "$BACKUPD"
step backupd-loaded 30 launchctl print system/com.apple.backupd

section chain
newfs_file base 1g -v "$VOL" >/dev/null || exit 0
step base-fsck 120 fsck_apfs -n -W "$WORK/base.img"
dump base "$WORK/base.img"
stage m0 base populate
stage m1 m0 nothing

section include
stage inc m0 include
stage n0 inc nothing
stage n0b inc nothing
stage s1 inc snapshot

section change
stage n1 s1 nothing
stage c1 s1 change
stage c0 inc change

section second
stage n2 c1 nothing
stage s2 c1 snapshot
stage n3 s2 nothing
stage c2 s2 change2

section views
views views c2
stage n4 c2 nothing

section delete
stage d1 c2 delete
stage d1w d1 wait
stage d2 d1w delete
stage d2w d2 wait

section after
sstep tm-localsnapshots-end 60 tmutil listlocalsnapshots /
sstep tm-prefs-end 30 defaults read /Library/Preferences/com.apple.TimeMachine

section done
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
