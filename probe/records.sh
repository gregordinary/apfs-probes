#!/usr/bin/env bash
# Records a populated volume holds where no earlier image shows the rule:
#
#   names    names that share one directory hash, created in both orders on a
#            volume that folds case and on one that does not (newfs_apfs -e),
#            to show the order a directory keeps them in
#   attrs    an ordinary attribute at every size from 1 to 4,096 bytes and a
#            symbolic link at every target length from 1 to 1,023 bytes, to
#            show where each stops being held in its record
#   attrnames  attributes named with 1, 10 and 120 bytes at every size from
#            3,780 to 3,820 bytes, to show whether that size depends on the name
#   blocks   opsutil.py's fixed tree copied by ditto into containers of 8, 16
#            and 64 KiB blocks, to show a populated tree at each
#
# Each container is unmounted, mounted again and looked up by path, unmounted,
# checked with fsck_apfs and dumped.
#
# Usage: records.sh OUTDIR [names] [attrs] [attrnames] [blocks]
#        (no argument: all four)

. "$(dirname "$0")/lib.sh" "$1"
shift
PARTS=${*:-names attrs attrnames blocks}

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'

# populate NAME SIZE ACTION [newfs_apfs args] : a fresh container in a file,
# ACTION run on its mounted volume, then a remount, a lookup of everything
# ACTION made, a check and a dump.
populate() {
    local n=$1 size=$2 action=$3 img dev cont vdev mnt="$WORK/mnt-$1"
    shift 3
    img=$(newfs_file "$n" "$size" -v Rec "$@") || return
    if dev=$(attach_raw "$n" "$img"); then
        if cont=$(container_of "$n-list" "$dev") && vdev=$(first_volume "$n-list" "$cont") &&
            mount_vol "$n" "$vdev" "$mnt"; then
            sstep "$n-mdutil" 60 mdutil -i off "$mnt"
            $action "$n" "$mnt"
            sstep "$n-sync" 60 sync
            unmount_vol "$n" "$vdev"
            if mount_vol "$n-again" "$vdev" "$mnt"; then
                [ "$action" = copy_tree ] ||
                    sstep "$n-check" 600 "$PY" "$HERE/recordsutil.py" check "$mnt" "$OUT/$n-check.json"
                unmount_vol "$n-again" "$vdev"
            fi
        fi
        detach "$n" "$dev"
    fi
    step "$n-fsck" 600 fsck_apfs -n -W "$img"
    dump "$n" "$img"
    rm -f "$img"
}

make_names() {
    sstep "$1-names" 300 "$PY" "$HERE/recordsutil.py" names "$2" "$OUT/$1.json"
}

make_attrs() {
    sstep "$1-attrs" 900 "$PY" "$HERE/recordsutil.py" attrs "$2" "$OUT/$1.json"
}

make_attrnames() {
    sstep "$1-attrnames" 300 "$PY" "$HERE/recordsutil.py" attrnames "$2" "$OUT/$1.json"
}

copy_tree() {
    sstep "$1-copy" 300 ditto "$WORK/src" "$2/src"
}

for part in $PARTS; do
    case $part in
    names)
        section names
        populate names-folding 64m make_names
        populate names-keeping 64m make_names -e
        ;;
    attrs)
        section attrs
        populate attrs 256m make_attrs
        ;;
    attrnames)
        section attrnames
        populate attrnames 64m make_attrnames
        ;;
    blocks)
        section blocks
        step srctree 120 "$PY" "$HERE/opsutil.py" srctree "$WORK/src"
        for bs in 8192 16384 65536; do
            populate "blocks-$bs" 512m copy_tree -b "$bs"
        done
        ;;
    esac
done

section done
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
