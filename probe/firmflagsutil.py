#!/usr/bin/env python3
"""How a firmlink's records are kept on disk, read from the runner's own
system volume without naming any path.

    firmflagsutil.py system OUT.json
    firmflagsutil.py image IMAGE.bd OUT.json

`system`, as root, reads the container the system's root is on from its raw
device, finds the volume whose UUID `diskutil info` gives for the root's
volume, and walks that volume's file-system tree. `image` does the same over
a dump blockdump.py wrote, taking the volume with the system role, or else the
first; it is how the reading is checked on an image whose records are known.

The walk keeps, for every inode carrying the attribute com.apple.fs.firmlink:
the attribute record's flags word, whether its value is held in the record or
in a stream, whether the value ends in a NUL, and whether the value without
its NUL is a second field of /usr/share/firmlinks (system only); and the
inode's bsd_flags and internal_flags. Each is written as a count per distinct
value. Beside them, for every other directory, the same two inode words as
counts per value, and for every other attribute, its flags word as counts per
value, with the names beginning com.apple.fs. counted by name. Of the walk
itself: the block size, the volume's role, fs_flags, root tree type and tree
flags, and how many nodes were read, how many were checked by the hash their
parent holds, how many carry no header to check, and how many failed. No
path, name of a file, value, device or identifier is written.

Layouts are those of the Apple File System Reference (2020-06-22); page
numbers refer to it.
"""

import hashlib
import json
import os
import plistlib
import re
import struct
import subprocess
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import blockdump  # noqa: E402

FIRMLINKS = "/usr/share/firmlinks"
FIRMLINK = b"com.apple.fs.firmlink"
PREFIX = b"com.apple.fs."

# obj_phys_t (p. 10) and the object types this reads.
OBJ = struct.Struct("<8sQQII")
OBJECT_TYPE_MASK = 0x0000FFFF
OBJ_STORAGETYPE_MASK = 0xC0000000
OBJ_PHYSICAL = 0x40000000
OBJECT_TYPE_NX_SUPERBLOCK = 0x01
NX_MAGIC = b"NXSB"
APFS_MAGIC = b"APSB"

# nx_superblock_t (p. 27): the fields this reads, by offset.
NX_BLOCK_SIZE = 36
NX_XP_DESC_BLOCKS = 104
NX_XP_DESC_BASE = 112
NX_NEXT_XID = 96
NX_OMAP_OID = 160
NX_FS_OID = 184
NX_MAX_FILE_SYSTEMS = 100

# apfs_superblock_t (p. 51): the fields this reads, by offset.
APFS_ROOT_TREE_TYPE = 116
APFS_OMAP_OID = 128
APFS_ROOT_TREE_OID = 136
APFS_VOL_UUID = 240
APFS_FS_FLAGS = 264
APFS_ROLE = 964
APFS_VOL_ROLE_SYSTEM = 0x0001

# btree_node_phys_t (p. 123) and btree_info_t (p. 126).
BTNODE_ROOT = 0x1
BTNODE_LEAF = 0x2
BTNODE_FIXED_KV_SIZE = 0x4
BTNODE_NOHEADER = 0x10
BTREE_HASHED = 0x80
BTREE_INFO_SIZE = 40
# A hashed tree's index value is btn_index_node_val_t (p. 127): the child's
# oid, then its hash in BTREE_NODE_HASH_SIZE_MAX bytes, of which SHA-256,
# APFS_HASH_DEFAULT (p. 153), fills the first 32. A node of a tree stored
# without object headers has a zero btn_o and no checksum to check.
SHA256_SIZE = 32

# File-system records (p. 71 on), and j_xattr_key_t and j_xattr_val_t (p. 82).
OBJ_ID_MASK = 0x0FFFFFFFFFFFFFFF
OBJ_TYPE_SHIFT = 60
APFS_TYPE_INODE = 3
APFS_TYPE_XATTR = 4
S_IFMT = 0o170000
S_IFDIR = 0o040000
XATTR_DATA_STREAM = 0x1
XATTR_DATA_EMBEDDED = 0x2

# j_inode_val_t up to its extended fields (p. 73): internal_flags is the
# seventh word, bsd_flags the eleventh, mode the fourteenth.
INODE = struct.Struct("<QQQQQQQiIIIIIHHQ")


def fletcher_ok(block):
    """Whether the object's checksum is the Fletcher 64 checksum the
    reference names (p. 10): 32-bit words after the checksum field, modulus
    2^32-1, with check values that bring the sums over the whole object to
    zero."""
    m = 0xFFFFFFFF
    s1 = s2 = 0
    for (w,) in struct.iter_unpack("<I", block[8:]):
        s1 = (s1 + w) % m
        s2 = (s2 + s1) % m
    c1 = m - ((s1 + s2) % m)
    c2 = m - ((s1 + c1) % m)
    return struct.pack("<II", c1, c2) == block[:8]


class Device:
    """The blocks of a raw device, read at the container's block size."""

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDONLY)
        first = os.pread(self.fd, 4096, 0)
        self.bs = struct.unpack_from("<I", first, NX_BLOCK_SIZE)[0]

    def block(self, n):
        data = os.pread(self.fd, self.bs, n * self.bs)
        return data + bytes(self.bs - len(data))


class Dump:
    """The blocks of a dump blockdump.py wrote, at the container's block
    size; absent bytes are zeros."""

    def __init__(self, path):
        it = blockdump.read_dump(path)
        _, self.unit = next(it)
        self.units = {}
        for first, data in it:
            for i in range(len(data) // self.unit):
                self.units[first + i] = data[i * self.unit:(i + 1) * self.unit]
        self.bs = self.unit
        self.bs = struct.unpack_from("<I", self.block(0), NX_BLOCK_SIZE)[0]

    def block(self, n):
        per = self.bs // self.unit
        return b"".join(self.units.get(n * per + i, bytes(self.unit)) for i in range(per))


class Reader:
    """Nodes read from IMG, each checked, with a count of each way."""

    def __init__(self, img):
        self.img = img
        self.read = 0
        self.failed = 0
        self.by_hash = 0
        self.hash_differs = 0
        self.unchecked = 0

    def node(self, addr, hashed=None):
        """The node at ADDR, checked by the hash its parent gives, HASHED, or
        else by its checksum; None where that check fails twice."""
        self.read += 1
        for _ in range(2):
            # A live container moves on while it is read; a second read tells
            # a block caught mid-write from one that does not verify.
            block = self.img.block(addr)
            if hashed is not None:
                if hashlib.sha256(block).digest() == hashed[:SHA256_SIZE]:
                    self.by_hash += 1
                    return Node(block)
            elif fletcher_ok(block):
                return Node(block)
            elif (struct.unpack_from("<H", block, 32)[0] & BTNODE_NOHEADER
                  and block[:32] == bytes(32)):
                self.unchecked += 1
                return Node(block)
        if hashed is not None:
            self.hash_differs += 1
        else:
            self.failed += 1
        return None


class Node:
    def __init__(self, block):
        self.block = block
        (self.flags, self.level, self.nkeys, toff, tlen) = struct.unpack_from(
            "<HHIHH", block, 32)
        self.leaf = bool(self.flags & BTNODE_LEAF)
        self.fixed = bool(self.flags & BTNODE_FIXED_KV_SIZE)
        self.toc = 56 + toff
        self.keys = self.toc + tlen
        self.vend = len(block) - (BTREE_INFO_SIZE if self.flags & BTNODE_ROOT else 0)

    def info(self):
        """btree_info_t's bt_flags, bt_key_size and bt_val_size."""
        return struct.unpack_from("<I4xII", self.block, len(self.block) - BTREE_INFO_SIZE)

    def entries(self, key_size, val_size):
        for i in range(self.nkeys):
            if self.fixed:
                k, v = struct.unpack_from("<HH", self.block, self.toc + 4 * i)
                kl, vl = key_size, (val_size if self.leaf else 8)
            else:
                k, kl, v, vl = struct.unpack_from("<HHHH", self.block, self.toc + 8 * i)
            key = self.block[self.keys + k:self.keys + k + kl]
            value = None if v == 0xFFFF else self.block[self.vend - v:self.vend - v + vl]
            yield key, value


def walk(reader, root_addr, resolve=None, first_key=None):
    """Every (key, value) of a B-tree in order, or, with FIRST_KEY, of the
    leaves whose range may hold a key whose first_key(key) is the one given:
    an index entry's child holds the keys from its own key up to the next
    entry's. A child is a physical address unless RESOLVE maps it."""
    root = reader.node(root_addr)
    if root is None:
        return
    bt_flags, ks, vs = root.info()
    hashed = bool(bt_flags & BTREE_HASHED)

    def visit(node):
        entries = list(node.entries(ks, vs))
        for i, (key, value) in enumerate(entries):
            if node.leaf:
                yield key, value
                continue
            if first_key is not None:
                want, of = first_key
                if of(key) > want:
                    break
                if i + 1 < len(entries) and of(entries[i + 1][0]) < want:
                    continue
            child = struct.unpack_from("<Q", value, 0)[0]
            addr = resolve(child) if resolve else child
            if addr is None:
                continue
            below = reader.node(addr, value[8:8 + SHA256_SIZE] if hashed else None)
            if below is not None:
                yield from visit(below)

    yield from visit(root)


def omap_lookup(reader, omap_addr, max_xid=None):
    """A resolver over the object map at OMAP_ADDR (p. 44): a virtual oid to
    the physical address of its newest mapping at or before MAX_XID."""
    omap = reader.node(omap_addr)
    if omap is None:
        return None
    tree = struct.unpack_from("<Q", omap.block, 48)[0]
    cache = {}

    def resolve(oid):
        if oid not in cache:
            best = None
            for key, value in walk(reader, tree, first_key=(oid, lambda k: struct.unpack_from(
                    "<Q", k, 0)[0])):
                k_oid, k_xid = struct.unpack_from("<QQ", key, 0)
                if k_oid != oid or (max_xid is not None and k_xid > max_xid):
                    continue
                if best is None or k_xid > best[0]:
                    best = (k_xid, struct.unpack_from("<Q", value, 8)[0])
            cache[oid] = best[1] if best else None
        return cache[oid]

    return resolve


def latest_nx(reader):
    """The container superblock with the largest xid in the checkpoint
    descriptor area whose checksum verifies (p. 26), else block zero."""
    b0 = reader.img.block(0)
    base = struct.unpack_from("<Q", b0, NX_XP_DESC_BASE)[0]
    count = struct.unpack_from("<I", b0, NX_XP_DESC_BLOCKS)[0] & 0x7FFFFFFF
    best = None
    for i in range(count):
        block = reader.img.block(base + i)
        _, _, xid, otype, _ = OBJ.unpack_from(block, 0)
        if otype & OBJECT_TYPE_MASK != OBJECT_TYPE_NX_SUPERBLOCK or block[32:36] != NX_MAGIC:
            continue
        if not fletcher_ok(block):
            continue
        if best is None or xid > best[0]:
            best = (xid, block)
    return best[1] if best else b0


def volumes(reader):
    """(apfs_superblock_t block) for each volume the container lists."""
    nx = latest_nx(reader)
    next_xid = struct.unpack_from("<Q", nx, NX_NEXT_XID)[0]
    resolve = omap_lookup(reader, struct.unpack_from("<Q", nx, NX_OMAP_OID)[0], next_xid)
    out = []
    if resolve is None:
        return out
    for oid in struct.unpack_from("<%dQ" % NX_MAX_FILE_SYSTEMS, nx, NX_FS_OID):
        if oid == 0:
            continue
        addr = resolve(oid)
        if addr is None:
            continue
        block = reader.img.block(addr)
        if block[32:36] == APFS_MAGIC:
            out.append(block)
    return out


def bump(table, key):
    table[key] = table.get(key, 0) + 1


def tally(table, value):
    bump(table, "%#x" % value)


def read_volume(reader, sb, seconds):
    """The counts the module's description names, for the volume whose
    superblock is SB, with SECONDS the set of the list's second fields or
    None."""
    tree_type = struct.unpack_from("<I", sb, APFS_ROOT_TREE_TYPE)[0]
    root_oid = struct.unpack_from("<Q", sb, APFS_ROOT_TREE_OID)[0]
    resolve = omap_lookup(reader, struct.unpack_from("<Q", sb, APFS_OMAP_OID)[0])
    out = {
        "role": "%#x" % struct.unpack_from("<H", sb, APFS_ROLE)[0],
        "fs_flags": "%#x" % struct.unpack_from("<Q", sb, APFS_FS_FLAGS)[0],
        "root_tree_type": "%#x" % tree_type,
    }
    physical = tree_type & OBJ_STORAGETYPE_MASK == OBJ_PHYSICAL
    root = root_oid if physical else (resolve(root_oid) if resolve else None)
    if root is None:
        out["error"] = "root tree not found"
        return out
    node = reader.node(root)
    if node is None:
        out["error"] = "root tree node does not verify"
        return out
    out["tree_flags"] = "%#x" % node.info()[0]
    child_resolve = None if physical else resolve

    firm = {"inodes": 0, "record_flags": {}, "held": {}, "ends_with_nul": {},
            "value_is_a_second_field": {}, "bsd_flags": {}, "internal_flags": {},
            "mode_is_directory": {}}
    other_dirs = {"count": 0, "bsd_flags": {}, "internal_flags": {}}
    other_attrs = {"count": 0, "record_flags": {}, "fs_names": {}}
    inode = None
    pending = None

    def close(inode, pending):
        """Count the inode just passed: as a firmlink's, or as a directory."""
        if inode is None:
            return
        oid, internal, bsd, mode = inode
        if pending is not None and pending == oid:
            tally(firm["bsd_flags"], bsd)
            tally(firm["internal_flags"], internal)
            bump(firm["mode_is_directory"], str(mode & S_IFMT == S_IFDIR))
        elif mode & S_IFMT == S_IFDIR:
            other_dirs["count"] += 1
            tally(other_dirs["bsd_flags"], bsd)
            tally(other_dirs["internal_flags"], internal)

    for key, value in walk(reader, root, child_resolve):
        if value is None:
            continue
        h = struct.unpack_from("<Q", key, 0)[0]
        oid, rtype = h & OBJ_ID_MASK, h >> OBJ_TYPE_SHIFT
        if rtype == APFS_TYPE_INODE:
            close(inode, pending)
            f = INODE.unpack_from(value, 0)
            inode, pending = (oid, f[6], f[10], f[13]), None
        elif rtype == APFS_TYPE_XATTR:
            nlen = struct.unpack_from("<H", key, 8)[0]
            name = key[10:10 + nlen].rstrip(b"\0")
            flags, dlen = struct.unpack_from("<HH", value, 0)
            if name == FIRMLINK:
                firm["inodes"] += 1
                tally(firm["record_flags"], flags)
                held = "stream" if flags & XATTR_DATA_STREAM else (
                    "record" if flags & XATTR_DATA_EMBEDDED else "neither")
                bump(firm["held"], held)
                if held == "record":
                    data = value[4:4 + dlen]
                    bump(firm["ends_with_nul"], str(data.endswith(b"\0")))
                    if seconds is not None:
                        bump(firm["value_is_a_second_field"], str(data.rstrip(b"\0") in seconds))
                if inode is not None and inode[0] == oid:
                    pending = oid
            else:
                other_attrs["count"] += 1
                tally(other_attrs["record_flags"], flags)
                if name.startswith(PREFIX):
                    n = name.decode("ascii", "replace")
                    other_attrs["fs_names"].setdefault(n, {})
                    tally(other_attrs["fs_names"][n], flags)
    close(inode, pending)
    out["firmlinks"] = firm
    out["other_directories"] = other_dirs
    out["other_attributes"] = other_attrs
    return out


def second_fields():
    """The set of /usr/share/firmlinks's second fields, or None."""
    try:
        with open(FIRMLINKS, "rb") as f:
            data = f.read()
    except OSError:
        return None
    out = set()
    for line in data.split(b"\n"):
        parts = line.split(b"\t")
        if len(parts) == 2:
            out.add(parts[1])
    return out


def info(target):
    r = subprocess.run(["diskutil", "info", "-plist", target], stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, timeout=60)
    return plistlib.loads(r.stdout)


def system(out_path):
    result = {"route": "system"}
    try:
        root = info("/")
        dev = root.get("DeviceIdentifier") or ""
        vol = re.sub(r"s[0-9]+$", "", dev) if re.fullmatch(
            r"disk[0-9]+s[0-9]+s[0-9]+", dev) else dev
        container = root.get("APFSContainerReference") or re.sub(r"s[0-9]+.*$", "", dev)
        want = uuid.UUID(info(vol)["VolumeUUID"]).bytes
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired,
            plistlib.InvalidFileException) as e:
        result["error"] = "device: %s" % type(e).__name__
        return finish(result, out_path)
    seconds = second_fields()
    result["list_read"] = seconds is not None
    for path in ("/dev/r" + container, "/dev/" + container):
        try:
            img = Device(path)
            result["device"] = "raw" if path.startswith("/dev/r") else "buffered"
            break
        except OSError as e:
            result.setdefault("open_errors", []).append(os.strerror(e.errno))
    else:
        return finish(result, out_path)
    reader = Reader(img)
    result["block_size"] = img.bs
    vols = volumes(reader)
    result["volumes"] = len(vols)
    chosen = [sb for sb in vols if sb[APFS_VOL_UUID:APFS_VOL_UUID + 16] == want]
    result["volume_matched_by_uuid"] = bool(chosen)
    if chosen:
        result["volume"] = read_volume(reader, chosen[0], seconds)
    result["nodes"] = {"read": reader.read, "failed": reader.failed,
                       "by_hash": reader.by_hash, "hash_differs": reader.hash_differs,
                       "unchecked": reader.unchecked}
    return finish(result, out_path)


def image(path, out_path):
    img = Dump(path)
    reader = Reader(img)
    result = {"route": "image", "block_size": img.bs}
    vols = volumes(reader)
    result["volumes"] = len(vols)
    system_role = [sb for sb in vols
                   if struct.unpack_from("<H", sb, APFS_ROLE)[0] & APFS_VOL_ROLE_SYSTEM]
    chosen = system_role or vols
    if chosen:
        result["volume"] = read_volume(reader, chosen[0], None)
    result["nodes"] = {"read": reader.read, "failed": reader.failed,
                       "by_hash": reader.by_hash, "hash_differs": reader.hash_differs,
                       "unchecked": reader.unchecked}
    return finish(result, out_path)


def finish(result, out_path):
    with open(out_path, "w") as f:
        json.dump(result, f, indent=1, sort_keys=True)
    print(json.dumps(result.get("volume", {}).get("firmlinks", {}), sort_keys=True))
    return 0


def main(argv):
    if len(argv) == 3 and argv[1] == "system":
        return system(argv[2])
    if len(argv) == 4 and argv[1] == "image":
        return image(argv[2], argv[3])
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
