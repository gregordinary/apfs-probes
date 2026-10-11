#!/usr/bin/env bash
# Which compression types macOS itself writes: the type of every compressed
# file Apple ships on the runner, and the type its compressing copies choose.
#
#   writers  contents of several kinds, among them copies of Apple's own
#            executables and a 20 MiB text, copied by `ditto --hfsCompression`
#            onto an attached image and onto the runner's own volume; the
#            image's copy archived with `aa archive` and extracted with
#            `aa extract`; each file's compression type
#   survey   every regular file under /System (not entering /System/Volumes),
#            /usr, /bin, /sbin, /Library and /Applications, each walked
#            within a time budget of its own, tallied by the compression type
#            each compressed file records, with the devices, directories and
#            first paths of each type
#
# Usage: typesurvey.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'

section writers
src="$WORK/src"
step writers 120 "$PY" "$HERE/compressutil.py" writers "$src" "$OUT/writers.json"
img="$WORK/survey.img"
step mkfile 30 mkfile -n 512m "$img"
if dev=$(attach_raw survey "$img"); then
    rdev=/dev/r${dev#/dev/}
    step newfs 120 newfs_apfs -v Survey "$dev" || sstep newfs-root 120 newfs_apfs -v Survey "$dev"
    if cont=$(container_of list "$dev") && vdev=$(volume_named list "$cont" Survey) &&
        mount_vol survey "/dev/$vdev" "$WORK/mnt"; then
        mnt="$WORK/mnt"
        sstep mdutil-off 60 mdutil -i off "$mnt"
        sstep ditto-image 300 ditto --hfsCompression "$src" "$mnt/ditto"
        sstep aa-archive 300 aa archive -d "$mnt/ditto" -o "$WORK/ditto.aar"
        sstep aa-extract 300 aa extract -d "$mnt/aa" -i "$WORK/ditto.aar"
        sstep typesof-image 120 "$PY" "$HERE/compressutil.py" typesof "$mnt" "$OUT/typesof-image.json"
        unmount_vol survey "/dev/$vdev"
        step fsck 600 fsck_apfs -n -W "$rdev" || sstep fsck-root 600 fsck_apfs -n -W "$rdev"
    fi
    detach survey "$dev"
    dump survey "$img"
fi
rm -f "$img"
step ditto-own 300 ditto --hfsCompression "$src" "$WORK/ditto-own"
step typesof-own 120 "$PY" "$HERE/compressutil.py" typesof "$WORK/ditto-own" "$OUT/typesof-own.json"
rm -rf "$src" "$WORK/ditto-own" "$WORK/ditto.aar"

section survey
for spec in System:/System:540 usr:/usr:120 bin:/bin:30 sbin:/sbin:30 Library:/Library:240 \
    Applications:/Applications:420; do
    name=${spec%%:*} rest=${spec#*:}
    root=${rest%%:*} budget=${rest##*:}
    sstep "survey-$name" $((budget + 120)) "$PY" "$HERE/compressutil.py" survey "$root" \
        "$OUT/survey-$name.json" "$budget"
done

section done
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
