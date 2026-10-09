#!/usr/bin/env bash
# Snapshots Time Machine makes on attached APFS images.
#
# `tmutil localsnapshot` snapshots every APFS volume included in the backup,
# an attached one too. And on macOS 11 and later each backup to an APFS
# destination is kept as a snapshot of the destination volume: everything on
# the data volume but one small folder is excluded, and the folder is backed
# up, changed, and backed up again. Each image's snapshots are listed, each
# one mounted read-only and listed, and the image checked and dumped.
#
#   env     Time Machine's state before the probe changes anything
#   backupd the daemon every backup and local snapshot goes through, which
#           the runner images ship disabled; with System Integrity
#           Protection off, root enables and loads it
#   source  the folder, and fixed-path exclusions of everything else
#   local   a second image's volume, included in the backup, with no
#           destination set: `tmutil localsnapshot`, a change, another
#           `tmutil localsnapshot` and another change, so the live tree
#           differs from both snapshots
#   plain   the destination is the one volume newfs_apfs makes in a raw image
#   role    a case-sensitive destination volume with the Time Machine role;
#           run only when the plain destination holds fewer than two
#           snapshots
#
# Usage: timemachine.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

DATA=/System/Volumes/Data
SRC=$DATA/tmprobe
TM_LOG='subsystem == "com.apple.TimeMachine" OR process == "backupd" OR process == "backupd-helper" OR process == "tmutil" OR (process == "kernel" AND eventMessage CONTAINS[c] "snapshot")'
# The number of snapshots the last destination held.
SNAPS=0

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step csrutil 30 csrutil status
step tm-version 30 tmutil version
step tm-status 30 tmutil status -X
sstep tm-destinations 30 tmutil destinationinfo -X
sstep tm-prefs 30 defaults read /Library/Preferences/com.apple.TimeMachine
step tm-localsnapshots 30 tmutil listlocalsnapshots /
step data-snapshots 30 diskutil apfs listSnapshots -plist "$DATA"
step backupd-launchd 30 launchctl print system/com.apple.backupd
step backupd-helper-launchd 30 launchctl print system/com.apple.backupd-helper
step launchd-disabled 30 launchctl print-disabled system
sstep tcc-fda 30 sqlite3 "/Library/Application Support/com.apple.TCC/TCC.db" \
    "select client, client_type, auth_value from access where service = 'kTCCServiceSystemPolicyAllFiles'"
step data-entries 30 ls -la "$DATA"

section backupd
BACKUPD=/System/Library/LaunchDaemons/com.apple.backupd.plist
step backupd-plists 30 sh -c 'ls -l /System/Library/LaunchDaemons | grep -i backup'
sstep backupd-enable 30 launchctl enable system/com.apple.backupd
sstep backupd-bootstrap 60 launchctl bootstrap system "$BACKUPD" ||
    sstep backupd-load 60 launchctl load -w "$BACKUPD"
step backupd-loaded 30 launchctl print system/com.apple.backupd

section source
sstep src-mkdir 30 mkdir "$SRC"
sstep src-chown 30 chown "$(id -u):$(id -g)" "$SRC"
# Mount points stay unexcluded: a volume is included or excluded by its own
# volume exclusion, which the local section relies on.
ls -A "$DATA" | while IFS= read -r e; do
    case "$e" in tmprobe | Volumes) continue ;; esac
    k=$(printf '%s' "$e" | tr -c 'A-Za-z0-9' _)
    sstep "exclude-data-$k" 30 tmutil addexclusion -p "$DATA/$e"
    if [ -e "/$e" ]; then
        sstep "exclude-root-$k" 30 tmutil addexclusion -p "/$e"
    fi
done
sstep tm-skippaths 30 defaults read /Library/Preferences/com.apple.TimeMachine SkipPaths
step isexcluded 60 tmutil isexcluded "$SRC" /Users /Applications /Library /private/var "$DATA/Users"

# backup NAME ID : one backup to destination ID, waiting for it to finish,
# with Time Machine's status polled beside it into log/NAME-status.out.
backup() {
    local n=$1 id=$2 poll rc
    (while :; do date '+%T'; tmutil status; sleep 10; done) >"$OUT/log/$n-status.out" 2>&1 &
    poll=$!
    if [ -n "$id" ]; then
        sstep "$n" 600 tmutil startbackup --block --destination "$id"
    else
        sstep "$n" 600 tmutil startbackup --block
    fi
    rc=$?
    kill "$poll" 2>/dev/null
    wait "$poll" 2>/dev/null
    # The time limit ends tmutil, not the backup it started.
    if [ "$rc" -eq 142 ]; then
        sstep "$n-stop" 120 tmutil stopbackup
    fi
    sstep "$n-latest" 120 tmutil latestbackup
    return "$rc"
}

# change V TREE : one of each kind of change to the tree at TREE.
change() {
    local t=$2/top
    step "$1-change-write" 30 "$PY" "$HERE/opsutil.py" write "$t/added" 5000 9
    step "$1-change-extend" 30 "$PY" "$HERE/opsutil.py" extend "$t/hundred" 8192
    step "$1-change-remove" 30 rm "$t/sub2/deep/a"
    step "$1-change-rename" 30 mv "$t/one" "$t/one-renamed"
}

# snapshots V VDEV : list VDEV's snapshots, set SNAPS, and mount and list
# each one.
snapshots() {
    local v=$1 vdev=$2 i=0 name smnt
    step "$v-snapshots" 60 diskutil apfs listSnapshots -plist "$vdev" || return
    for name in $("$PY" "$HERE/disks.py" snapshots "$OUT/log/$v-snapshots.out"); do
        i=$((i + 1))
        smnt="$WORK/snap-$v-$i"
        mkdir -p "$smnt"
        if sstep "$v-snap$i-mount" 120 mount_apfs -o rdonly -s "$name" "/dev/$vdev" "$smnt"; then
            sstep "$v-snap$i-ls" 300 ls -laeO@iR "$smnt"
            sstep "$v-snap$i-unmount" 120 umount "$smnt"
        fi
    done
    SNAPS=$i
}

# local_test : the local section.
local_test() {
    local v=local img dev cont vdev mnt t
    img=$(newfs_file $v 256m -v Src) || return
    if dev=$(attach_raw $v "$img"); then
        if cont=$(container_of $v-list "$dev") && vdev=$(volume_named $v-list "$cont" Src) &&
            sstep $v-mount 120 diskutil mount "$vdev"; then
            sstep $v-ownership 60 diskutil enableOwnership "$vdev"
            step $v-info 60 diskutil info -plist "$vdev"
            if mnt=$("$PY" "$HERE/disks.py" key "$OUT/log/$v-info.out" MountPoint); then
                t=$mnt/tree
                sstep $v-mdutil-off 60 mdutil -i off "$mnt"
                sstep $v-mkdir 30 mkdir "$t"
                sstep $v-chown 30 chown "$(id -u):$(id -g)" "$t"
                step $v-tree 120 "$PY" "$HERE/opsutil.py" srctree "$t"
                step $v-isexcluded-before 30 tmutil isexcluded "$mnt"
                sstep $v-include 60 tmutil removeexclusion -v "$mnt"
                step $v-isexcluded 30 tmutil isexcluded "$mnt"
                step $v-tree-ls-1 60 ls -laeO@iR "$t"
                sstep $v-localsnapshot-1 300 tmutil localsnapshot
                # A snapshot's name carries its time to the second.
                sleep 2
                change $v "$t"
                step $v-tree-ls-2 60 ls -laeO@iR "$t"
                sstep $v-localsnapshot-2 300 tmutil localsnapshot
                step $v-change2-write 30 "$PY" "$HERE/opsutil.py" write "$t/top/later" 3000 11
                step $v-change2-unlink 30 rm "$t/top/three"
                step $v-change2-xattr 30 "$PY" "$HERE/opsutil.py" setxattr "$t/top/under" user.later 100
                step $v-tree-ls-3 60 ls -laeO@iR "$t"
                sstep $v-listlocalsnapshots 60 tmutil listlocalsnapshots "$mnt"
                sstep $v-data-localsnapshots 60 tmutil listlocalsnapshots /
                snapshots $v "$vdev"
            fi
            unmount_vol $v "$vdev"
        fi
        detach $v "$dev"
    fi
    step $v-fsck 300 fsck_apfs -n -W "$img"
    dump $v "$img"
    rm -f "$img"
}

# on_destination V VOL ROLE DEV : with the image attached as DEV, make its
# volume VOL the destination, back up twice with a change between, and list
# what the destination holds.
on_destination() {
    local v=$1 vol=$2 role=$3 dev=$4 cont vdev mnt id= rc
    cont=$(container_of "$v-list" "$dev") || return
    vdev=$(volume_named "$v-list" "$cont" "$vol") || return
    if [ -n "$role" ]; then
        sstep "$v-role" 60 diskutil apfs changeVolumeRole "$vdev" "$role"
    fi
    sstep "$v-mount" 120 diskutil mount "$vdev" || return
    sstep "$v-ownership" 60 diskutil enableOwnership "$vdev"
    step "$v-info" 60 diskutil info -plist "$vdev"
    mnt=$("$PY" "$HERE/disks.py" key "$OUT/log/$v-info.out" MountPoint) || return
    sstep "$v-setdestination" 120 tmutil setdestination "$mnt"
    sstep "$v-destinations" 30 tmutil destinationinfo -X &&
        id=$("$PY" "$HERE/disks.py" tm-dest "$OUT/log/$v-destinations.out" "$mnt")
    sstep "$v-prefs" 30 defaults read /Library/Preferences/com.apple.TimeMachine

    step "$v-tree-ls-1" 60 ls -laeO@iR "$SRC/tree"
    backup "$v-backup-1" "$id"
    rc=$?
    step "$v-snapshots-1" 60 diskutil apfs listSnapshots -plist "$vdev"
    if [ "$rc" -ne 142 ]; then
        change "$v" "$SRC/tree"
        step "$v-tree-ls-2" 60 ls -laeO@iR "$SRC/tree"
        backup "$v-backup-2" "$id"
    fi
    sstep "$v-listbackups" 120 tmutil listbackups
    sstep "$v-live-ls" 120 ls -laeO@iR "$mnt"
    step "$v-mounts-after" 30 mount
    snapshots "$v" "$vdev"
    if [ -n "$id" ]; then
        sstep "$v-removedestination" 60 tmutil removedestination "$id"
    fi
    unmount_vol "$v" "$vdev"
}

# variant V VOL ROLE [newfs_apfs args] : a fresh image whose volume VOL is
# the destination, checked and dumped afterwards.
variant() {
    local v=$1 vol=$2 role=$3 img dev t0
    shift 3
    t0=$(date '+%Y-%m-%d %H:%M:%S')
    SNAPS=0
    sudo -n rm -rf "$SRC/tree"
    step "$v-tree" 120 "$PY" "$HERE/opsutil.py" srctree "$SRC/tree"
    img=$(newfs_file "$v" 2g -v "$vol" "$@") || return
    if dev=$(attach_raw "$v" "$img"); then
        on_destination "$v" "$vol" "$role" "$dev"
        detach "$v" "$dev"
    fi
    sstep "$v-log" 300 log show --start "$t0" --style compact --info --predicate "$TM_LOG"
    step "$v-fsck" 600 fsck_apfs -n -W "$img"
    dump "$v" "$img"
    rm -f "$img"
}

section local
local_test

section plain
variant plain Dest-plain ""

section role
if [ "$SNAPS" -lt 2 ]; then
    variant role Dest-role T -e
else
    printf '#\t%s\n' "role skipped: the plain destination holds $SNAPS snapshots" >>"$STEPS"
fi

section after
sstep tm-destinations-end 30 tmutil destinationinfo -X
step data-snapshots-end 30 diskutil apfs listSnapshots -plist "$DATA"

section done
sudo -n rm -rf "$SRC" 2>/dev/null
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
