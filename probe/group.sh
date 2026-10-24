#!/usr/bin/env bash
# What macOS writes when it makes a System and Data volume group, and how a
# firmlink is kept.
#
#   firmlinks  grouputil.py firmlinks, as root: the shape of
#              /usr/share/firmlinks and how the directories it names are
#              kept, as counts and flag values alone, and the recipe drawn
#              from them (log/firmlinks.json).
#   group      one container in a sparse 4 GiB file through three stages,
#              each in one attach of the image and each ending in fsck_apfs
#              -n -W and a dump:
#                made     diskutil apfs createContainer makes the container
#                         on the attached disk, or, where it refuses,
#                         newfs_apfs formats the file with a volume Base;
#                         diskutil apfs addVolume adds System with the system
#                         role, then Data with the data role, grouped with it
#                         (-groupWith). diskutil apfs list, listVolumeGroups
#                         and info are recorded after each step.
#                marked   both volumes mounted. Data is given private/var,
#                         private/tmp and Users with a file in each; System is
#                         given the directories fl-a and fl-b, fl-a kept as a
#                         firmlink to Data's Users by grouputil.py mark with
#                         the recipe, fl-b left plain. grouputil.py marks reads
#                         both back.
#                again    both volumes mounted again, read-only and then
#                         read-write; the marks read back, a file written to
#                         Data, and the group listed once more.
#
# Usage: group.sh OUTDIR [firmlinks] [group]
#   with neither, both.

. "$(dirname "$0")/lib.sh" "$1"
shift
GU="$HERE/grouputil.py"
mkdir -p "$WORK/mnt"

do_fl=""
do_group=""
[ $# -eq 0 ] && do_fl=1 && do_group=1
for a in "$@"; do
    case "$a" in
        firmlinks) do_fl=1 ;;
        group) do_group=1 ;;
        *) echo "unknown argument $a" >&2; exit 2 ;;
    esac
done

# record P : the APFS listing, the volume groups and each volume's info.
record() {
    local p=$1 v
    list_named "$p" "$DEV" || return 1
    step "$p-groups" 60 diskutil apfs listVolumeGroups -plist
    for v in $(devices "$LIST" "$CONT"); do
        step "$p-info-$v" 60 diskutil info -plist "$v"
    done
}

# devices LIST CONTAINER : every volume of CONTAINER in LIST.
devices() {
    "$PY" "$HERE/disks.py" volumes "$OUT/log/$1.out" "$2" | cut -f1
}

# end P IMAGE : detach, check and dump.
end() {
    detach "$1" "$DEV"
    step "$1-fsck" 600 fsck_apfs -n -W "$2"
    dump "$1" "$2"
}

section env
step sw_vers 30 sw_vers
step uname 30 uname -a
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step diskutil-apfs-usage 30 diskutil apfs
step addvolume-usage 30 diskutil apfs addVolume

RECIPE="$OUT/log/firmlinks.json"
if [ -n "$do_fl" ]; then
    section firmlinks
    sstep firmlinks 300 "$PY" "$GU" firmlinks "$RECIPE"
    sudo -n chown "$(id -u)" "$RECIPE" 2>/dev/null
fi

if [ -n "$do_group" ]; then
    section group
    img="$WORK/grp.img"
    if step grp-mkfile 30 mkfile -n 4g "$img" && DEV=$(attach_raw made "$img"); then
        if sstep made-create 300 diskutil apfs createContainer "$DEV" &&
            list_named made-created "$DEV"; then
            :
        else
            detach made "$DEV"
            rm -f "$img"
            img=$(newfs_file grp 4g -v Base) && DEV=$(attach_raw made-newfs "$img") &&
                list_named made-created "$DEV"
        fi
        if [ -n "${CONT-}" ] &&
            sstep made-addsys 120 diskutil apfs addVolume "$CONT" APFS System -role S -nomount &&
            list_named made-addsys "$DEV" && sys=$(volume_named "$LIST" "$CONT" System); then
            record made-role
            sstep made-adddata 120 diskutil apfs addVolume "$CONT" APFS Data -role D \
                -groupWith "$sys" -nomount
            record made-data
        fi
        end made "$img"

        if DEV=$(attach_raw marked "$img") && record marked-start; then
            sys=$("$PY" "$HERE/disks.py" volumes "$OUT/log/$LIST.out" "$CONT" | awk -F'\t' '$2=="System"{print $1; exit}')
            data=$(volume_named "$LIST" "$CONT" Data)
            smnt="$WORK/mnt/sys"
            dmnt="$WORK/mnt/data"
            [ -n "$sys" ] && mount_vol marked-sys "$sys" "$smnt"
            [ -n "$data" ] && mount_vol marked-data "$data" "$dmnt"
            if [ -n "$data" ] && [ -d "$dmnt" ]; then
                for d in private/var private/tmp Users; do
                    sstep "marked-data-mkdir-${d//\//-}" 30 mkdir -p "$dmnt/$d"
                    sstep "marked-data-file-${d//\//-}" 30 sh -c "echo probe > '$dmnt/$d/probe.txt'"
                done
            fi
            if [ -n "$sys" ] && [ -d "$smnt" ]; then
                sstep marked-sys-mkdir 30 mkdir -p "$smnt/fl-a" "$smnt/fl-b"
                if [ -s "$RECIPE" ]; then
                    sstep marked-mark 60 "$PY" "$GU" mark "$smnt/fl-a" Users "$RECIPE"
                fi
                sstep marked-marks-a 30 "$PY" "$GU" marks "$smnt/fl-a"
                sstep marked-marks-b 30 "$PY" "$GU" marks "$smnt/fl-b"
                step marked-sys-ls 30 ls -laO "$smnt"
            fi
            [ -n "$sys" ] && unmount_vol marked-sys "$sys"
            [ -n "$data" ] && unmount_vol marked-data "$data"
            end marked "$img"
        fi

        if DEV=$(attach_raw again "$img") && record again-start; then
            sys=$("$PY" "$HERE/disks.py" volumes "$OUT/log/$LIST.out" "$CONT" | awk -F'\t' '$2=="System"{print $1; exit}')
            data=$(volume_named "$LIST" "$CONT" Data)
            smnt="$WORK/mnt/sys-again"
            dmnt="$WORK/mnt/data-again"
            if [ -n "$sys" ]; then
                mkdir -p "$smnt"
                sstep again-sys-mount-ro 120 diskutil mount readOnly nobrowse -mountPoint "$smnt" "$sys" &&
                    sstep again-marks-a 30 "$PY" "$GU" marks "$smnt/fl-a"
                unmount_vol again-sys "$sys"
            fi
            if [ -n "$data" ]; then
                mount_vol again-data "$data" "$dmnt" &&
                    sstep again-data-write 30 sh -c "echo again > '$dmnt/private/var/again.txt'"
                step again-data-ls 30 ls -laOR "$dmnt"
                unmount_vol again-data "$data"
            fi
            record again-end
            end again "$img"
        fi
        rm -f "$img"
    fi
fi
