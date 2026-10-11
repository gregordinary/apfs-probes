#!/usr/bin/env bash
# What newfs_apfs's options change, and whether filling a container with the
# same tree twice writes the same blocks.
#
#   options    one 64 MiB container per option a formatter would offer, each
#              in a plain file, two of them twice
#   twostep    a container made with -C, then given a volume with -A
#   ditto      one source tree with fixed times, copied by the kernel into two
#              fresh containers
#   srcfolder  the same tree made into an image twice by hdiutil create
#              -srcfolder
#
# Usage: format.sh OUTDIR

. "$(dirname "$0")/lib.sh" "$1"

section env
step sw_vers 30 sw_vers
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'

section options
# opt NAME [newfs_apfs args] : one container formatted with the given args.
opt() {
    local n=$1 img
    shift
    img=$(newfs_file "opt-$n" 64m "$@") || return
    step "opt-$n-fsck" 120 fsck_apfs -n -W "$img"
    dump "opt-$n" "$img"
    rm -f "$img"
}
opt default -v Fmt
opt default-again -v Fmt
opt container-only -C
opt case-sensitive -e -v Fmt
opt owner-root -U 0 -G 0 -v Fmt
opt owner-other -U 1234 -G 5678 -v Fmt
opt conformance -o conformance -v Fmt
opt quota -q 32m -v Fmt
opt reserve -r 16m -v Fmt
opt fixed-size -s 32m -v Fmt
opt role-vm -R v -v Fmt
opt role-uuid -R v -D -v Fmt
opt role-uuid-again -R v -D -v Fmt
opt name-empty -v ""
opt name-unicode -v "École Ω"
opt name-255 -v "$("$PY" -c 'print("N" * 255)')"
opt name-256 -v "$("$PY" -c 'print("N" * 256)')"
opt block-explicit -b 4096 -v Fmt

section twostep
if img=$(newfs_file two 64m -C); then
    if ! step two-add-file 120 newfs_apfs -A -v Fmt "$img"; then
        if dev=$(attach_raw two "$img"); then
            cont=$(container_of two-list "$dev") &&
                step two-add-disk 120 newfs_apfs -A -v Fmt "/dev/$cont"
            detach two "$dev"
        fi
    fi
    step two-fsck 120 fsck_apfs -n -W "$img"
    dump twostep "$img"
    rm -f "$img"
fi

section ditto
step srctree 120 "$PY" "$HERE/opsutil.py" srctree "$WORK/src"
step srctree-ls 60 ls -laeO@R "$WORK/src"
for i in 1 2; do
    n=ditto-$i
    img=$(newfs_file "$n" 256m -v Rep) || continue
    if dev=$(attach_raw "$n" "$img"); then
        if cont=$(container_of "$n-list" "$dev") && vdev=$(first_volume "$n-list" "$cont") &&
            mount_vol "$n" "$vdev" "$WORK/mnt-$n"; then
            sstep "$n-mdutil" 60 mdutil -i off "$WORK/mnt-$n"
            sstep "$n-copy" 300 ditto "$WORK/src" "$WORK/mnt-$n/src"
            sstep "$n-sync" 60 sync
            unmount_vol "$n" "$vdev"
        fi
        detach "$n" "$dev"
    fi
    step "$n-fsck" 300 fsck_apfs -n -W "$img"
    dump "$n" "$img"
    rm -f "$img"
done

section srcfolder
for i in 1 2; do
    n=srcfolder-$i
    step "$n-create" 300 hdiutil create -srcfolder "$WORK/src" -fs APFS -layout NONE \
        -format UDRW -volname Rep "$WORK/$n.dmg" ||
        step "$n-create-gpt" 300 hdiutil create -srcfolder "$WORK/src" -fs APFS \
            -format UDRW -volname Rep "$WORK/$n.dmg" || continue
    step "$n-imageinfo" 60 hdiutil imageinfo "$WORK/$n.dmg"
    step "$n-convert" 300 hdiutil convert "$WORK/$n.dmg" -format UDTO -o "$WORK/$n"
    if [ -f "$WORK/$n.cdr" ]; then
        step "$n-fsck" 300 fsck_apfs -n -W "$WORK/$n.cdr"
        dump "$n" "$WORK/$n.cdr"
    fi
    rm -f "$WORK/$n.dmg" "$WORK/$n.cdr"
done

section done
step mounts-end 30 mount
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
