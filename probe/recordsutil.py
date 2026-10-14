#!/usr/bin/env python3
"""Records a populated APFS volume holds where no earlier image shows the rule.

    recordsutil.py names DIR OUT.json
    recordsutil.py attrs DIR OUT.json
    recordsutil.py check DIR OUT.json

`names` creates, in two directories of DIR, every name of the groups below:
in `forward` each group's names in the order listed, in `reverse` in the
opposite order. The names of a group share one 22-bit directory hash: the
CRC-32C of the name as UTF-32 little-endian, computed here by the script
before anything is created, and asserted equal within each group. They are
lowercase ASCII, so a volume that folds case hashes them as one that does not.
Some groups pair names of one length and some of two lengths, and two are
three names.

`attrs` creates in DIR/attrs one file per size from 1 to 4096 bytes, each
with one attribute `user.probe` of that size, and in DIR/links one symbolic
link per target length from 1 to 1023 bytes. A dump then shows at which size
an attribute and a link target stop being held in the record.

`check` looks every name and file up again by path, after a remount, and
records what lstat(2) and getxattr(2) return for each.

Every value is a repeating pattern with no zero bytes.
"""

import ctypes
import ctypes.util
import json
import os
import sys

LIBC = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
LIBC.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                          ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
LIBC.setxattr.restype = ctypes.c_int
LIBC.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                          ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
LIBC.getxattr.restype = ctypes.c_ssize_t
XATTR_NOFOLLOW = 1

GROUPS = [
    # One length.
    ("saas68", "saqaaf"), ("saas69", "saqaag"), ("saas66", "saqaah"), ("saas67", "saqaai"),
    ("saas64", "saqaaj"), ("saas65", "saqaak"), ("saas62", "saqaal"), ("saas63", "saqaam"),
    # Two lengths.
    ("d29x", "daappad"), ("d29y", "daappae"), ("d29z", "daappaf"), ("d29t", "daappah"),
    ("d29u", "daappai"), ("d29v", "daappaj"), ("d29w", "daappak"), ("d29p", "daappal"),
    # Three names.
    ("sajbff", "sazp18", "sa0aav"), ("sajbfg", "sazp19", "sa0aaw"),
]

ATTR_SIZES = range(1, 4097)
LINK_LENGTHS = range(1, 1024)


def _table():
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        table.append(c)
    return table


TABLE = _table()


def name_hash(name):
    """The 22 bits a lowercase ASCII name is stored under."""
    c = 0xFFFFFFFF
    for b in b"".join(ord(ch).to_bytes(4, "little") for ch in name):
        c = TABLE[(c ^ b) & 0xFF] ^ (c >> 8)
    return c & 0x3FFFFF


def pattern(n, seed):
    unit = bytes((seed + i) % 255 + 1 for i in range(255))
    return (unit * (n // 255 + 1))[:n]


def names(d, out):
    for group in GROUPS:
        assert len({name_hash(n) for n in group}) == 1, group
    result = {"groups": [], "created": {}}
    for sub, order in (("forward", 1), ("reverse", -1)):
        os.makedirs(os.path.join(d, sub))
        for group in GROUPS:
            for n in group[::order]:
                path = os.path.join(d, sub, n)
                try:
                    with open(path, "wb") as f:
                        f.write(n.encode())
                    result["created"][sub + "/" + n] = os.lstat(path).st_ino
                except OSError as e:
                    result["created"][sub + "/" + n] = "errno %d" % e.errno
    result["groups"] = [{"names": list(g), "hash": "%06x" % name_hash(g[0])} for g in GROUPS]
    with open(out, "w") as f:
        json.dump(result, f, indent=1)


def attrs(d, out):
    result = {"attrs": {}, "links": {}}
    os.makedirs(os.path.join(d, "attrs"))
    os.makedirs(os.path.join(d, "links"))
    for size in ATTR_SIZES:
        path = os.path.join(d, "attrs", "a%04d" % size).encode()
        with open(path, "wb"):
            pass
        value = pattern(size, size)
        rc = LIBC.setxattr(path, b"user.probe", value, len(value), 0, XATTR_NOFOLLOW)
        result["attrs"][str(size)] = 0 if rc == 0 else ctypes.get_errno()
    for length in LINK_LENGTHS:
        target = ("t" * length).encode()
        path = os.path.join(d, "links", "l%04d" % length).encode()
        try:
            os.symlink(target, path)
            result["links"][str(length)] = 0
        except OSError as e:
            result["links"][str(length)] = e.errno
    with open(out, "w") as f:
        json.dump(result, f, indent=1)


def check(d, out):
    result = {"names": {}, "attrs": {}, "links": {}}
    for sub in ("forward", "reverse"):
        for group in GROUPS:
            for n in group:
                path = os.path.join(d, sub, n)
                try:
                    st = os.lstat(path)
                    with open(path, "rb") as f:
                        same = f.read() == n.encode()
                    result["names"][sub + "/" + n] = [st.st_ino, same]
                except OSError as e:
                    result["names"][sub + "/" + n] = "errno %d" % e.errno
    buf = ctypes.create_string_buffer(8192)
    for size in ATTR_SIZES:
        path = os.path.join(d, "attrs", "a%04d" % size).encode()
        got = LIBC.getxattr(path, b"user.probe", buf, len(buf), 0, XATTR_NOFOLLOW)
        if got < 0:
            result["attrs"][str(size)] = "errno %d" % ctypes.get_errno()
        else:
            result["attrs"][str(size)] = [got, buf.raw[:got] == pattern(size, size)]
    for length in LINK_LENGTHS:
        path = os.path.join(d, "links", "l%04d" % length)
        try:
            result["links"][str(length)] = os.readlink(path) == "t" * length
        except OSError as e:
            result["links"][str(length)] = "errno %d" % e.errno
    with open(out, "w") as f:
        json.dump(result, f, indent=1)


if __name__ == "__main__":
    {"names": names, "attrs": attrs, "check": check}[sys.argv[1]](sys.argv[2], sys.argv[3])
