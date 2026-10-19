#!/usr/bin/env bash
# How much of a container's free space macOS writes into: the containers under
# reserve/, which ferrosys's APFS writer formatted around one file of zeros
# with a stated count of blocks free, each filled until the kernel refuses a
# write. images.json states each container's block size, block count and free
# blocks, and the trials it goes through.
#
# Each trial restores the container from its dump and goes through one pass:
#
#   before  the dump checked against images.json's digests and restored, and
#           sm_stats -v run on the image
#   fill    attached, the first volume mounted read-write, described by
#           diskutil info, and written by reserveutil.py fill until a call
#           fails; for a container of two volumes, the second mounted
#           read-write too and asked for one block; then each unmounted
#   after   detached, fsck_apfs -n -W and sm_stats -v run on the image
#
# The kernel's APFS messages are kept in system-log.ndjson, and times/NAME-TRIAL
# bounds the ones each trial drew: the kernel names the blocks a change asks
# for and the margin beside them where it refuses one.
#
# Usage: reserve.sh OUTDIR [bNk | block | user | mib | files | NAME]... [K/N]
#   with no argument, every trial of every container; see reserveutil.py
#   select for the words.

. "$(dirname "$0")/lib.sh" "$1"
shift
RU="$HERE/reserveutil.py"
AU="$HERE/acceptutil.py"
RSV="$ROOT/reserve"
mkdir -p "$WORK/mnt" "$OUT/fills" "$OUT/times"

now() {
    "$PY" -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat())' \
        >>"$OUT/times/$1"
}

# block_size NAME : the block size images.json states of NAME.
block_size() {
    "$PY" -c 'import json, sys
for i in json.load(open(sys.argv[1]))["images"]:
    if i["name"] == sys.argv[2]:
        print(i["block_size"])' "$RSV/images.json" "$1"
}

# trial N T : restore N, fill its first volume by trial T, and check it after.
trial() {
    local n=$1 t=$2 p="$1-$2" img="$WORK/$1.img" dev bs vdev mnt k=0 run=sstep
    section "$p"
    now "$p"
    bs=$(block_size "$n")
    if step "$p-verify" 300 "$PY" "$AU" verify "$RSV" "$n" &&
        step "$p-restore" 300 "$PY" "$ROOT/tools/blockdump.py" restore "$RSV/$n.bd" "$img"; then
        step "$p-smstats-before" 120 "$RES/sm_stats" -v "$img"
        if dev=$(attach_raw "$p" "$img") && list_named "$p" "$dev"; then
            for vdev in $("$PY" "$HERE/disks.py" volumes "$OUT/log/$LIST.out" "$CONT" | cut -f1); do
                k=$((k + 1))
                mnt="$WORK/mnt/$p-v$k"
                mount_vol "$p-v$k" "/dev/$vdev" "$mnt" || continue
                step "$p-v$k-info" 60 diskutil info -plist "$vdev"
                if [ "$k" = 1 ]; then
                    [ "$t" = user ] && run=step
                    $run "$p-v$k-fill" 300 "$PY" "$RU" fill "$mnt" "$t" "$bs" "$n" \
                        "$OUT/fills/$p.json"
                else
                    sstep "$p-v$k-one" 120 "$PY" "$RU" one "$mnt" "$bs" "$n" \
                        "$OUT/fills/$p-v$k.json"
                fi
                sstep "$p-v$k-sync" 60 sync
            done
            for vdev in $("$PY" "$HERE/disks.py" volumes "$OUT/log/$LIST.out" "$CONT" | cut -f1); do
                unmount_vol "$p-$vdev" "$vdev"
            done
            detach "$p" "$dev"
        fi
        step "$p-fsck" 600 fsck_apfs -n -W "$img"
        step "$p-smstats-after" 120 "$RES/sm_stats" -v "$img"
    fi
    rm -f "$img"
    now "$p"
}

section env
step sw_vers 30 sw_vers
step uname 30 uname -a
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step date 30 date -u '+%Y-%m-%dT%H:%M:%SZ %z'
step python 30 "$PY" -c 'import sys; print(sys.version)'
step df 30 df -h "$WORK"
step apfs-kext-plist 30 plutil -p /System/Library/Extensions/apfs.kext/Contents/Info.plist
step images 30 "$PY" -c 'import json, sys; print(json.dumps(json.load(open(sys.argv[1]))["made_by"]))' \
    "$RSV/images.json"
started=$(date '+%Y-%m-%d %H:%M:%S')

step select 30 "$PY" "$RU" select "$RSV" "$@" || exit 1
while read -r n t <&3; do
    trial "$n" "$t"
done 3<"$OUT/log/select.out"

section done
sstep system-log 600 sh -c 'log show --start "$1" --style ndjson --predicate "$2" >"$3"' sh "$started" \
    'senderImagePath CONTAINS[c] "apfs" OR process == "fsck_apfs"' "$OUT/system-log.ndjson"
step index 120 "$PY" "$RU" index "$RSV" "$OUT"
step mounts-end 30 mount
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
