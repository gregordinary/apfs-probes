#!/usr/bin/env bash
# What a volume records as its encryption is turned on and off, and volumes
# encrypted from the start.
#
#   conv   one 256 MiB container with one volume, Conv, through six stages,
#          each in one attach of the image:
#            1 plain       a small tree written to the volume
#            2 encrypted   diskutil apfs encryptVolume with a passphrase
#            3 decrypted   diskutil apfs decryptVolume
#            4 used        one more file written
#            5 encrypted   diskutil apfs encryptVolume again
#            6 decrypted   diskutil apfs decryptVolume again
#          A stage that changes the encryption keeps the volume mounted until
#          diskutil apfs list reports the change complete on three listings
#          in a row (cryptogenutil.py wait), or until a time limit.
#   born   a volume made encrypted, by newfs_apfs -E -S on the attached
#          disk of a plain file (born-newfs) and by diskutil apfs addVolume
#          -passphrase in an empty container (born-diskutil); each is
#          unlocked, given the same tree, and locked again by unmounting it.
#
# An encrypted volume that a plain mount refuses is unlocked with
# diskutil apfs unlockVolume. Every stage records what diskutil lists of the
# volume, its cryptographic users, and every file as macOS reads it back
# (fsprobe.py manifest). The volume is then unmounted, the container checked
# with fsck_apfs -n and -n -W on its own disk while attached, which is where
# fsck_apfs reads an encrypted volume's keybag from, and the image detached and
# dumped, so each stage leaves an image of its own. cryptogenutil.py check holds every
# stage's reading to the files written.
#
# Every passphrase is PASS below, so each encrypted image can be unlocked.
#
# Usage: cryptogen.sh OUTDIR [conv] [born]
#   with neither, both

. "$(dirname "$0")/lib.sh" "$1"
shift
CU="$HERE/cryptogenutil.py"
PASS=apfs-probes
WAIT=240
mkdir -p "$OUT/manifests" "$OUT/waits"

ONLY=" $* "
for a in "$@"; do
    case "$a" in
        conv | born) ;;
        *) echo "cryptogen.sh: no part named $a" >&2; exit 1 ;;
    esac
done
[ "$ONLY" = "  " ] && ONLY=" conv born "

# open_volume NAME IMAGE VOLUME : attach IMAGE and mount VOLUME, unlocking it
# if a plain mount is refused; sets DEV, VDEV and MNT.
open_volume() {
    local n=$1
    DEV="" VDEV="" MNT=""
    DEV=$(attach_raw "$n" "$2") || return 1
    list_named "$n" "$DEV" || return 1
    VDEV=$(volume_named "$LIST" "$CONT" "$3") || return 1
    sstep "$n-mount" 120 diskutil mount "$VDEV" ||
        sstep "$n-unlock" 120 diskutil apfs unlockVolume "$VDEV" -passphrase "$PASS" ||
        return 1
    step "$n-info" 60 diskutil info -plist "$VDEV"
    MNT=$("$PY" "$HERE/disks.py" key "$OUT/log/$n-info.out" MountPoint)
}

# populate NAME : the tree every volume holds, written to the open volume.
populate() {
    local p=$1
    sstep "$p-mdutil-off" 60 mdutil -i off "$MNT"
    sstep "$p-fseventsd" 30 sh -c 'mkdir -p "$1/.fseventsd" && touch "$1/.fseventsd/no_log"' sh "$MNT"
    sstep "$p-mkdir" 30 mkdir "$MNT/tree"
    sstep "$p-tree" 120 "$PY" "$HERE/opsutil.py" tree "$MNT/tree"
    sstep "$p-data" 60 "$PY" "$HERE/opsutil.py" write "$MNT/data" 1048576 7
    sstep "$p-sync" 60 sync
}

# convert NAME VERB STATE : start VERB, encryptVolume or decryptVolume, on the
# open volume, and wait until it is listed as STATE.
convert() {
    local p=$1 verb=$2 state=$3
    apfs_list "$p-before"
    step "$p-before-text" 60 diskutil apfs list "$CONT"
    sstep "$p-$verb" 300 diskutil apfs "$verb" "$VDEV" -user disk -passphrase "$PASS" &&
        step "$p-wait" $((WAIT + 60)) "$PY" "$CU" wait "$VDEV" "$state" "$WAIT" "$OUT/waits/$p.json"
}

# finish NAME IMAGE : record the open volume, close it, check the container
# while it is attached, detach it, and dump the image. fsck_apfs reads an
# encrypted volume's keybag through the disk, so a check of the file alone
# fails for want of it, and checks no container holding a locked volume; the
# check that counts is of the container's own disk with its volume unlocked,
# and the physical store under it and diskutil's verification are kept beside
# it.
finish() {
    local p=$1 img=$2
    if [ -n "$VDEV" ]; then
        apfs_list "$p-state"
        step "$p-state-text" 60 diskutil apfs list "$CONT"
        sstep "$p-users" 60 diskutil apfs listCryptoUsers "$VDEV"
        step "$p-volume-info" 60 diskutil info -plist "$VDEV"
        [ -n "$MNT" ] &&
            sstep "$p-manifest" 300 "$PY" "$HERE/fsprobe.py" manifest "$MNT" "$OUT/manifests/$p.json"
        unmount_vol "$p" "$VDEV"
    fi
    if [ -z "$DEV" ]; then
        DEV=$(attach_raw "$p-check" "$img") && list_named "$p-check" "$DEV" || DEV=""
    fi
    if [ -n "$DEV" ]; then
        # Neither fsck_apfs nor diskutil checks a container while a volume in it is
        # encrypted and locked, so an encrypted one is unlocked, not mounted, first;
        # for a volume that is not encrypted this step fails and changes nothing.
        [ -n "$VDEV" ] &&
            sstep "$p-check-unlock" 120 diskutil apfs unlockVolume "$VDEV" -passphrase "$PASS" -nomount
        sstep "$p-fsck" 600 fsck_apfs -n "/dev/r$CONT"
        sstep "$p-fsck-warnings" 600 fsck_apfs -n -W "/dev/r$CONT"
        sstep "$p-fsck-store" 600 fsck_apfs -n "/dev/r${DEV#/dev/}"
        sstep "$p-verify" 600 diskutil verifyVolume "$CONT"
        detach "$p" "$DEV"
    fi
    dump "$p" "$img"
}

# conv_stage K NAME : stage K of the conv image, named conv-K-NAME.
conv_stage() {
    local p="conv-$1-$2" img="$WORK/conv.img"
    section "$p"
    if open_volume "$p" "$img" Conv; then
        case "$2" in
            plain) populate "$p" ;;
            encrypted) convert "$p" encryptVolume encrypted ;;
            decrypted) convert "$p" decryptVolume decrypted ;;
            used)
                sstep "$p-write" 60 "$PY" "$HERE/opsutil.py" write "$MNT/later" 65536 8
                sstep "$p-sync" 60 sync
                ;;
        esac
    fi
    finish "$p" "$img"
}

section env
step sw_vers 30 sw_vers
step uname 30 uname -a
step runner-env 30 sh -c 'env | grep -E "^(ImageOS|ImageVersion|RUNNER_ARCH|RUNNER_LABEL)=" | sort'
step date 30 date -u '+%Y-%m-%dT%H:%M:%SZ %z'
step python 30 "$PY" -c 'import sys; print(sys.version)'
step passphrase 30 echo "$PASS"

case "$ONLY" in
    *" conv "*)
        if newfs_file conv 256m -v Conv >/dev/null; then
            conv_stage 1 plain
            conv_stage 2 encrypted
            conv_stage 3 decrypted
            conv_stage 4 used
            conv_stage 5 encrypted
            conv_stage 6 decrypted
        fi
        rm -f "$WORK/conv.img"
        ;;
esac

case "$ONLY" in
    *" born "*)
        p=born-newfs
        section "$p"
        img="$WORK/$p.img"
        # newfs_apfs encrypts only a container it can publish as a disk, which a
        # plain file is not, so the file is attached first.
        if step "$p-mkfile" 30 mkfile -n 256m "$img" && dev=$(attach_raw "$p-format" "$img"); then
            step "$p-newfs" 300 newfs_apfs -E -S "$PASS" -v Born "$dev" ||
                sstep "$p-newfs-root" 300 newfs_apfs -E -S "$PASS" -v Born "$dev"
            detach "$p-format" "$dev"
            open_volume "$p" "$img" Born && populate "$p"
            finish "$p" "$img"
        fi
        rm -f "$img"

        p=born-diskutil
        section "$p"
        img="$WORK/$p.img"
        if newfs_file "$p" 256m -C >/dev/null && dev=$(attach_raw "$p-add" "$img"); then
            list_named "$p-add" "$dev" &&
                sstep "$p-addvol" 120 diskutil apfs addVolume "$CONT" APFS Born -passphrase "$PASS" -nomount
            detach "$p-add" "$dev"
            open_volume "$p" "$img" Born && populate "$p"
            finish "$p" "$img"
        fi
        rm -f "$img"
        ;;
esac

section done
step mounts-end 30 mount
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
step check 120 "$PY" "$CU" check "$OUT"
exit 0
