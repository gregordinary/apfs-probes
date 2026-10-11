#!/usr/bin/env bash
# What one operation changes on disk.
#
# Every stage copies an earlier image, mounts its volume once with ownership
# honoured, runs one command as root, and unmounts. A stage and the no-op
# stage made from the same image differ by that one command, plus whatever a
# mount writes on its own, which the two no-op stages m1 and m1b measure. The
# first mount turns Spotlight off and leaves .fseventsd/no_log, so later
# mounts do not log file-system events into the volume.
#
#   chain    base (newfs_apfs, 1 GiB, so a second volume fits) -> m0 (first
#            mount) -> m1, m1b (mount, nothing) -> m2 (from m1: nothing)
#   create   from m0: files of 0, 1, 10 and 1024 blocks, a directory, a
#            symlink, a file written past a hole, 300 files in a directory,
#            a compressed file
#   change   from f0 (m0 plus two files): f1 (nothing), delete, clone, hard
#            link, rename, small and large xattrs, xattr removal, extend,
#            shrink, chmod, chown, a snapshot, a snapshot then a change
#   volume   from base, unmounted: a second volume, case-insensitive and
#            case-sensitive
#
# Usage: ops.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'

# stage NAME FROM OP : copy image FROM, mount its volume, run the shell command
# OP as root with $M the mount point, unmount, check and keep the result.
# Images later stages start from; every other stage image is removed once
# it is dumped. Copies are clones, so a 1 GiB image costs only what differs.
KEEP=" base m0 m1 f0 xattr-small "

# copy_img FROM NAME : clone image FROM as NAME, or copy it where cloning fails.
copy_img() {
    cp -c "$WORK/$1.img" "$WORK/$2.img" 2>/dev/null || cp "$WORK/$1.img" "$WORK/$2.img"
}

# done_img NAME : remove NAME's image unless a later stage starts from it.
done_img() {
    case "$KEEP" in
        *" $1 "*) ;;
        *) rm -f "$WORK/$1.img" ;;
    esac
}

stage() {
    local n=$1 from=$2 op=$3 img dev cont vdev
    img="$WORK/$n.img"
    copy_img "$from" "$n" || return 1
    if dev=$(attach_raw "$n" "$img"); then
        if cont=$(container_of "$n-list" "$dev") && vdev=$(first_volume "$n-list" "$cont") &&
            mount_vol "$n" "$vdev" "$WORK/mnt-$n"; then
            sstep "$n-ownership" 60 diskutil enableOwnership "$vdev"
            sstep "$n-op" 300 env M="$WORK/mnt-$n" PY="$PY" OPS="$HERE/opsutil.py" RES="$RES" sh -c "$op"
            sstep "$n-sync" 60 sync
            step "$n-snapshots" 60 diskutil apfs listSnapshots -plist "$vdev"
            unmount_vol "$n" "$vdev"
        fi
        detach "$n" "$dev"
    fi
    step "$n-fsck" 120 fsck_apfs -n -W "$img"
    dump "$n" "$img"
    done_img "$n"
}

# addvol NAME [newfs_apfs args] : a volume added to a copy of base, unmounted.
addvol() {
    local n=$1 img dev cont
    shift
    img="$WORK/$n.img"
    copy_img base "$n" || return 1
    if dev=$(attach_raw "$n" "$img"); then
        if cont=$(container_of "$n-list" "$dev"); then
            step "$n-add" 120 newfs_apfs -A "$@" "/dev/$cont" ||
                sstep "$n-add-root" 120 newfs_apfs -A "$@" "/dev/$cont"
            apfs_list "$n-list-after"
        fi
        detach "$n" "$dev"
    fi
    step "$n-fsck" 120 fsck_apfs -n -W "$img"
    dump "$n" "$img"
    done_img "$n"
}

section chain
newfs_file base 1g -v Ops >/dev/null || exit 0
step base-fsck 120 fsck_apfs -n -W "$WORK/base.img"
dump base "$WORK/base.img"
stage m0 base 'mdutil -i off "$M"; mkdir -p "$M/.fseventsd" && touch "$M/.fseventsd/no_log"'
stage m1 m0 'true'
stage m1b m0 'true'
stage m2 m1 'true'

section create
stage empty m0 ': > "$M/a"'
stage block m0 '"$PY" "$OPS" write "$M/b" 4096 1'
stage ten m0 '"$PY" "$OPS" write "$M/c" 40960 2'
stage mega m0 '"$PY" "$OPS" write "$M/big" 4194304 3'
stage mkdir m0 'mkdir "$M/d"'
stage symlink m0 'ln -s target "$M/s"'
stage hole m0 '"$PY" "$OPS" write "$M/h" 4096 6 && "$PY" "$OPS" extend "$M/h" 1048576 && "$PY" -c "import os, sys; f = os.open(sys.argv[1], os.O_WRONLY); os.pwrite(f, b\"tail\" * 1024, 1048576); os.close(f)" "$M/h"'
stage many m0 'mkdir "$M/many" && i=0; while [ $i -lt 300 ]; do : > "$M/many/entry-$i"; i=$((i + 1)); done'
stage compressed m0 '"$PY" -c "import sys; open(sys.argv[1], \"wb\").write(b\"compressible line of text\\n\" * 4000)" "$M/plain.txt" && ditto --hfsCompression "$M/plain.txt" "$M/packed.txt" && rm "$M/plain.txt"'

section change
stage f0 m0 '"$PY" "$OPS" write "$M/victim" 40960 4 && "$PY" "$OPS" write "$M/keep" 8192 5'
stage f1 f0 'true'
stage delete f0 'rm "$M/victim"'
stage clone f0 'cp -c "$M/victim" "$M/clone"'
stage link f0 'ln "$M/keep" "$M/keep2"'
stage rename f0 'mv "$M/keep" "$M/kept"'
stage xattr-small f0 '"$PY" "$OPS" setxattr "$M/keep" user.small 16'
stage xattr-big f0 '"$PY" "$OPS" setxattr "$M/keep" user.big 8000'
stage xattr-remove xattr-small 'xattr -d user.small "$M/keep"'
stage extend f0 '"$PY" "$OPS" extend "$M/keep" 1048576'
stage shrink f0 '"$PY" "$OPS" extend "$M/victim" 4096'
stage chmod f0 'chmod 600 "$M/keep"'
stage chown f0 'chown 1234:5678 "$M/keep"'
stage snapshot f0 '"$RES/apfs_systemsnapshot" -s probe-snap -v "$M"'
stage snapshot-change f0 '"$RES/apfs_systemsnapshot" -s probe-snap -v "$M" && "$PY" "$OPS" write "$M/after" 4096 7 && rm "$M/victim"'

section volume
addvol addvol -v Second
addvol addvol-cs -e -v Second

section done
step mounts-end 30 mount
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
