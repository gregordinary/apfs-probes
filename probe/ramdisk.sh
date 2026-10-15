#!/usr/bin/env bash
# What macOS reads of Apple's restore RAM disk: the oracle for ferrosys's opt-in
# gate over it.
#
# The macOS 26.6.2 restore image carries a RAM disk as an IM4P whose payload is
# one plain APFS container. Apple serves it from its CDN, so the runner fetches
# the one member by HTTP range requests (apple-ramdisk.py member), holds it and
# the payload to the digests ferrosys pins, and keeps neither: what this probe
# keeps is macOS's reading of it.
#
#   fetch   the member fetched and held to its sha256, the payload taken out of
#           its wrapper and held to its size and sha256
#   ro      the payload attached read-only as a raw disk, listed by diskutil
#           apfs list, its one volume mounted read-only with ownership on,
#           described by diskutil info, and every entry recorded as the system
#           calls report it (corpusutil.py manifest): names, modes, owners,
#           flags, four times, sizes, link targets, every attribute's name,
#           length and digest, a compressed file's type, and every file's
#           digest. None of Apple's bytes are kept.
#   after   the payload's sha256 again, so the listing describes the bytes the
#           fetch pinned
#
# Usage: ramdisk.sh OUTDIR
. "$(dirname "$0")/lib.sh" "$1"
CU="$HERE/corpusutil.py"
AR="$HERE/apple-ramdisk.py"

URL="https://updates.cdn-apple.com/2026SummerFCS/fullrestores/140-75212/A2A24B94-1FC1-45A3-93F7-C51B02AF1F4D/UniversalMac_26.6.2_25G83_Restore.ipsw"
MEMBER="094-96989-093.dmg"
MEMBER_SHA256="934c21768e2fe036c33ba884bcce727d4e9f8e10c72733bf6d6f40da2dd99be8"
PAYLOAD_BYTES=211812352
PAYLOAD_SHA256="ece2816da790ad6601dd67e20b1c444bd7780e2b70005da06776705f6b604a9c"

step sw_vers 30 sw_vers

section fetch
IM4P="$WORK/member.im4p"
IMG="$WORK/ramdisk.apfs"
step member 900 "$PY" "$AR" member "$URL" "$MEMBER" "$IM4P" || exit 1
step member-sha256 60 shasum -a 256 "$IM4P"
grep -q "^$MEMBER_SHA256 " "$OUT/log/member-sha256.out" || { echo "the member is not the pinned one" >&2; exit 1; }
step payload 120 "$PY" "$AR" payload "$IM4P" "$IMG" || exit 1
rm -f "$IM4P"
step payload-sha256 60 shasum -a 256 "$IMG"
grep -q "^$PAYLOAD_SHA256 " "$OUT/log/payload-sha256.out" || { echo "the payload is not the pinned one" >&2; exit 1; }
[ "$(stat -f %z "$IMG")" = "$PAYLOAD_BYTES" ] || { echo "the payload is not the pinned length" >&2; exit 1; }

section ro
if dev=$(attach_raw ramdisk "$IMG" -readonly); then
    if cont=$(container_of ramdisk-list "$dev"); then
        vdev=$(first_volume ramdisk-list "$cont")
        if [ -n "$vdev" ] && sstep ramdisk-mount 120 diskutil mount readOnly "$vdev"; then
            sstep ramdisk-ownership 60 diskutil enableOwnership "$vdev"
            step ramdisk-info 60 diskutil info -plist "$vdev"
            if mnt=$("$PY" "$HERE/disks.py" key "$OUT/log/ramdisk-info.out" MountPoint); then
                sstep ramdisk-listing 900 "$PY" "$CU" manifest "$mnt" "$OUT/listing.json"
            fi
            unmount_vol ramdisk "$vdev"
        fi
    fi
    detach ramdisk "$dev"
fi

section after
step payload-sha256-after 60 shasum -a 256 "$IMG"
rm -f "$IMG"
step mounts-end 30 mount
sudo -n chown -R "$(id -u)" "$OUT" 2>/dev/null
exit 0
