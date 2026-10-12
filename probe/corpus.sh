#!/usr/bin/env bash
# The corpus a reader of APFS is held to: containers of every shape a reader
# meets, each beside macOS's own reading of every volume and snapshot in it.
#
# Each row of ROWS is one container, a covering array over four things: the
# block size (all five), which name kind the first volume has, whether a
# second volume of the other kind sits beside it, and whether the first
# volume keeps two snapshots. Every pair of values of any two of them is in
# some row. Every volume holds the same tree (corpusutil.py tree), whose
# contents are text, so an image compresses to little. A volume with
# snapshots is changed between them and after the second, so its live tree
# and each snapshot differ in known ways.
#
#   write   format, add the second volume, and on each volume, mounted once:
#           Spotlight off, .fseventsd/no_log, ownership honoured, the tree,
#           and the snapshots and changes; then fsck_apfs -n and -n -W, and
#           the image dumped
#   pass1   every volume mounted read-only, its live tree and each snapshot
#           recorded by corpusutil.py manifest, and the image dumped again:
#           this dump and these manifests are the corpus
#   pass2   pass1 again
#   check   corpusutil.py check: macOS read every file as written, both
#           passes recorded the same, and the three dumps are one image
#
# The check is what lets the corpus state no exceptions: a manifest is taken
# of exactly the bytes that are kept. A row whose check fails keeps every
# dump and both passes, so the difference can be found.
#
# Usage: corpus.sh OUTDIR [ROW... | K/N]
#   with no ROW, every row; K/N, the Kth of N shards, every Nth row from the Kth

. "$(dirname "$0")/lib.sh" "$1"
shift
ONLY=" $* "
CU="$HERE/corpusutil.py"
MAN="$OUT/manifests"

# name block-size first-volume's-kind volumes snapshots
ROWS="
b4k-fold-one-none 4096 fold one none
b4k-keep-two-snap 4096 keep two snap
b8k-fold-two-snap 8192 fold two snap
b8k-keep-one-none 8192 keep one none
b16k-fold-one-snap 16384 fold one snap
b16k-keep-two-none 16384 keep two none
b32k-fold-two-none 32768 fold two none
b32k-keep-one-snap 32768 keep one snap
b64k-fold-one-none 65536 fold one none
b64k-keep-two-snap 65536 keep two snap
"

now_ns() {
    step "$1" 30 "$PY" -c 'import time; print(time.time_ns())'
}

# snapshot NAME MNT : one Time Machine local snapshot of the volume at MNT,
# which must be included in the backup and mounted under /Volumes.
snapshot() {
    now_ns "$1-t0"
    sstep "$1" 300 tmutil localsnapshot
    now_ns "$1-t1"
    sstep "$1-list" 60 tmutil listlocalsnapshots "$2"
    # A snapshot's name carries its time to the second.
    sleep 1
}

# write_volume ROW LIST CONTAINER VOLUME SNAPSHOTS : mount VOLUME once and
# build the tree on it, with the snapshots and changes SNAPSHOTS asks for.
write_volume() {
    local n=$1 list=$2 cont=$3 vol=$4 snaps=$5 vdev mnt d p
    p="$n-$vol"
    d="$MAN/$n/$vol"
    mkdir -p "$d"
    vdev=$(volume_named "$list" "$cont" "$vol") || return 1
    sstep "$p-mount" 120 diskutil mount "$vdev" || return 1
    sstep "$p-ownership" 60 diskutil enableOwnership "$vdev"
    step "$p-info" 60 diskutil info -plist "$vdev"
    if mnt=$("$PY" "$HERE/disks.py" key "$OUT/log/$p-info.out" MountPoint); then
        sstep "$p-mdutil-off" 60 mdutil -i off "$mnt"
        sstep "$p-fseventsd" 30 sh -c 'mkdir -p "$1/.fseventsd" && touch "$1/.fseventsd/no_log"' sh "$mnt"
        sstep "$p-tree" 900 "$PY" "$CU" tree "$mnt" "$AFSCTOOL" "$OUT/algorithms.json" "$d/expect-0.json"
        if [ "$snaps" = snap ]; then
            sstep "$p-include" 60 tmutil removeexclusion -v "$mnt"
            step "$p-isexcluded" 30 tmutil isexcluded "$mnt"
            snapshot "$p-snapshot-1" "$mnt"
            sstep "$p-change-1" 120 "$PY" "$CU" change "$mnt" 1 "$d/expect-0.json" "$d/expect-1.json"
            snapshot "$p-snapshot-2" "$mnt"
            sstep "$p-change-2" 120 "$PY" "$CU" change "$mnt" 2 "$d/expect-1.json" "$d/expect-2.json"
        fi
        sstep "$p-sync" 60 sync
        sstep "$p-ls" 120 ls -laeO@R "$mnt"
    fi
    step "$p-snapshots" 60 diskutil apfs listSnapshots -plist "$vdev"
    unmount_vol "$p" "$vdev"
}

# read_pass ROW IMAGE K : attach IMAGE, mount every volume read-only, record
# its live tree and each snapshot under pass K, and dump the image.
read_pass() {
    local n=$1 img=$2 k=$3 dev cont vol vdev mnt d p i name smnt
    if dev=$(attach_raw "$n-pass$k" "$img"); then
        if cont=$(container_of "$n-pass$k-list" "$dev"); then
            "$PY" "$HERE/disks.py" volumes "$OUT/log/$n-pass$k-list.out" "$cont" | cut -f2 \
                >"$WORK/$n-pass$k-volumes.txt"
            [ "$k" = 1 ] && cp "$WORK/$n-pass$k-volumes.txt" "$MAN/$n/volumes.txt"
            for vol in $(cat "$WORK/$n-pass$k-volumes.txt"); do
                p="$n-pass$k-$vol"
                d="$MAN/$n/$vol/pass$k"
                mkdir -p "$d"
                vdev=$(volume_named "$n-pass$k-list" "$cont" "$vol") || continue
                sstep "$p-mount" 120 diskutil mount readOnly "$vdev" || continue
                sstep "$p-ownership" 60 diskutil enableOwnership "$vdev"
                step "$p-info" 60 diskutil info -plist "$vdev"
                if mnt=$("$PY" "$HERE/disks.py" key "$OUT/log/$p-info.out" MountPoint); then
                    sstep "$p-live" 600 "$PY" "$CU" manifest "$mnt" "$d/live.json"
                fi
                step "$p-snapshots" 60 diskutil apfs listSnapshots -plist "$vdev"
                i=0
                for name in $("$PY" "$CU" snapshots "$OUT/log/$p-snapshots.out" "$d/snapshots.json"); do
                    i=$((i + 1))
                    smnt="$WORK/snap-$p-$i"
                    mkdir -p "$smnt"
                    if sstep "$p-snap$i-mount" 120 mount_apfs -o rdonly -s "$name" "/dev/$vdev" "$smnt"; then
                        sstep "$p-snap$i" 600 "$PY" "$CU" manifest "$smnt" "$d/snap-$i.json"
                        sstep "$p-snap$i-unmount" 120 umount "$smnt"
                    fi
                done
                unmount_vol "$p" "$vdev"
            done
        fi
        detach "$n-pass$k" "$dev"
    fi
    if [ "$k" = 1 ]; then
        dump "$n" "$img"
    else
        dump "$n-again" "$img"
    fi
}

# row NAME BLOCK-SIZE KIND VOLUMES SNAPSHOTS : one container of the corpus.
row() {
    local n=$1 bs=$2 kind=$3 vols=$4 snaps=$5 img dev cont first second own other personality list i
    section "$n"
    mkdir -p "$MAN/$n"
    step "$n-row" 30 "$PY" "$CU" row "$MAN/$n/row.json" "$n" "$bs" "$kind" "$vols" "$snaps"
    # A container's first volume is case-insensitive unless -e asks otherwise.
    if [ "$kind" = fold ]; then
        first=CaseFold second=CaseKeep own="" other=-e personality="Case-sensitive APFS"
    else
        first=CaseKeep second=CaseFold own=-e other=-i personality=APFS
    fi
    img="$WORK/$n.img"
    step "$n-mkfile" 30 mkfile -n 2g "$img" || return
    if dev=$(attach_raw "$n" "$img"); then
        step "$n-newfs" 300 newfs_apfs -b "$bs" $own -v "$first" "$dev" ||
            sstep "$n-newfs-root" 300 newfs_apfs -b "$bs" $own -v "$first" "$dev"
        if cont=$(container_of "$n-list-1" "$dev"); then
            if [ "$vols" = two ]; then
                step "$n-addvol" 120 newfs_apfs -A "$other" -v "$second" "/dev/$cont" ||
                    sstep "$n-addvol-root" 120 newfs_apfs -A "$other" -v "$second" "/dev/$cont" ||
                    sstep "$n-addvol-diskutil" 120 diskutil apfs addVolume "$cont" "$personality" "$second" -nomount
            fi
            # macOS 27 can list a volume just added before it reports the
            # volume's name, so the listing is taken again until it names it.
            list="$n-list-2"
            apfs_list "$list"
            if [ "$vols" = two ]; then
                for i in 1 2 3 4 5 6 7 8 9 10; do
                    volume_named "$list" "$cont" "$second" >/dev/null 2>&1 && break
                    sleep 1
                    list="$n-list-2-again$i"
                    apfs_list "$list"
                done
            fi
            write_volume "$n" "$list" "$cont" "$first" "$snaps"
            [ "$vols" = two ] && write_volume "$n" "$list" "$cont" "$second" none
        fi
        detach "$n" "$dev"
    fi
    step "$n-fsck-written" 600 fsck_apfs -n "$img"
    step "$n-fsck-written-warnings" 600 fsck_apfs -n -W "$img"
    dump "$n-written" "$img"
    read_pass "$n" "$img" 1
    read_pass "$n" "$img" 2
    step "$n-fsck-read" 600 fsck_apfs -n "$img"
    step "$n-check" 120 "$PY" "$CU" check "$OUT" "$n"
    rm -f "$img"
}

section env
step sw_vers 30 sw_vers
step uname 30 uname -a
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step date 30 date -u '+%Y-%m-%dT%H:%M:%SZ %z'
step python 30 "$PY" -c 'import sys; print(sys.version)'
step sdk-path 60 xcrun --show-sdk-path
header="$(cat "$OUT/log/sdk-path.out" 2>/dev/null)/usr/include/compression.h"
step algorithms 30 "$PY" "$HERE/compressutil.py" algorithms "$header" "$OUT/algorithms.json"
step brew-install-afsctool 1200 env HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 \
    brew install afsctool
step afsctool-version 60 brew list --versions afsctool
AFSCTOOL=$(command -v afsctool || echo "$(brew --prefix 2>/dev/null)/bin/afsctool")
[ -x "$AFSCTOOL" ] || AFSCTOOL=""

section backupd
# Time Machine takes the snapshots, and its daemon is disabled on the runner
# images until it is enabled and loaded.
BACKUPD=/System/Library/LaunchDaemons/com.apple.backupd.plist
sstep backupd-enable 30 launchctl enable system/com.apple.backupd
sstep backupd-bootstrap 60 launchctl bootstrap system "$BACKUPD" ||
    sstep backupd-load 60 launchctl load -w "$BACKUPD"
step backupd-loaded 30 launchctl print system/com.apple.backupd

# The rows come in on descriptor 3, so nothing a row runs reads them.
SHARD=""
if [ $# -eq 1 ] && [[ $1 =~ ^[1-9][0-9]*/[1-9][0-9]*$ ]]; then
    SHARD=$1
else
    for a in "$@"; do
        case "$ROWS" in
            *"
$a "*) ;;
            *) echo "corpus.sh: no row named $a" >&2; exit 1 ;;
        esac
    done
fi
i=0
while read -r n bs kind vols snaps <&3; do
    [ -n "$n" ] || continue
    i=$((i + 1))
    if [ -n "$SHARD" ]; then
        [ $(((i - 1) % ${SHARD#*/})) -eq $((${SHARD%/*} - 1)) ] || continue
    else
        case "$ONLY" in
            "  ") ;;
            *" $n "*) ;;
            *) continue ;;
        esac
    fi
    row "$n" "$bs" "$kind" "$vols" "$snaps"
done 3<<EOF
$ROWS
EOF

section done
step index 120 "$PY" "$CU" index "$OUT"
step mounts-end 30 mount
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
