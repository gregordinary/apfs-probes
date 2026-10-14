#!/usr/bin/env python3
"""Records a populated APFS volume holds where no earlier image shows the rule.

    recordsutil.py names DIR OUT.json
    recordsutil.py attrs DIR OUT.json
    recordsutil.py attrnames DIR OUT.json
    recordsutil.py check DIR OUT.json

`names` creates, in two directories of DIR, every name of the groups below:
in `forward` each group's names in the order listed, in `reverse` in the
opposite order. The names of a group share one 22-bit directory hash: the
CRC-32C, as UTF-32 little-endian, of the name's canonical decomposition, case
folded and decomposed again on a volume that folds case. The script computes
it before anything is created and asserts it equal within each group, on the
kinds of volume the group is for. The first groups are lowercase ASCII, so
both kinds hash them alike: some pair names of one length and some of two,
and two are three names. Then come pairs that set one candidate order against
another: a shorter name that is the greater in bytes; on a volume that folds
case, a name whose capital sorts before the other in bytes and after it once
folded; and a name with a precomposed letter, greater in bytes than the other
and smaller once decomposed.

`attrs` creates in DIR/attrs one file per size from 1 to 4096 bytes, each
with one attribute `user.probe` of that size, and in DIR/links one symbolic
link per target length from 1 to 1023 bytes. A dump then shows at which size
an attribute and a link target stop being held in the record.

`attrnames` sets attributes named with 1, 10 and 120 bytes at every size from
3,780 to 3,820 bytes, one file each, so a dump shows whether the size at
which an attribute stops being held in the record depends on its name.

`check` looks every name and file up again by path, after a remount, and
records what lstat(2) and getxattr(2) return for each.

Every value is a repeating pattern with no zero bytes.
"""

import ctypes
import ctypes.util
import json
import os
import sys
import unicodedata

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
    # The shorter name is the greater in bytes.
    ("z610", "aaahapa"), ("z613", "aaahapb"), ("z612", "aaahapc"),
    ("z615", "aaahapd"), ("z614", "aaahape"), ("z617", "aaahapf"),
    # One hash only where case is folded: the capital is smaller in bytes, and larger folded.
    ("Bpk99", "aaaav"), ("Bpk98", "aaaaw"), ("Bpk97", "aaaax"),
    ("Bpk96", "aaaay"), ("Bpk95", "aaaaz"), ("Bpk9z", "aaaa5"),
    # A precomposed letter: greater in bytes, smaller decomposed.
    ("\u00e9b5it", "faaaaa"), ("\u00e9b5iw", "faaaab"), ("\u00e9b5iv", "faaaac"),
    ("\u00e9b5iq", "faaaad"), ("\u00e9b5ip", "faaaae"), ("\u00e9b5is", "faaaaf"),
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


ATTR_NAMES = (b"a", b"user.probe", b"user." + b"n" * 115)
ATTR_NAME_SIZES = range(3780, 3821)


def name_hash(name, fold):
    """The 22 bits a name is stored under, on a volume that folds case or not."""
    form = unicodedata.normalize("NFD", name)
    if fold:
        form = unicodedata.normalize("NFD", form.casefold())
    c = 0xFFFFFFFF
    for b in b"".join(ord(ch).to_bytes(4, "little") for ch in form):
        c = TABLE[(c ^ b) & 0xFF] ^ (c >> 8)
    return c & 0x3FFFFF


def shared_by(group):
    """The kinds of volume on which a group's names share one hash."""
    return [fold for fold in (True, False) if len({name_hash(n, fold) for n in group}) == 1]


def pattern(n, seed):
    unit = bytes((seed + i) % 255 + 1 for i in range(255))
    return (unit * (n // 255 + 1))[:n]


def names(d, out):
    for group in GROUPS:
        assert shared_by(group), group
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
    result["groups"] = [
        {"names": list(g), "hash": "%06x" % name_hash(g[0], shared_by(g)[0]),
         "folding": True in shared_by(g), "keeping": False in shared_by(g)}
        for g in GROUPS
    ]
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


def attrnames(d, out):
    result = {}
    os.makedirs(os.path.join(d, "attrnames"))
    for i, name in enumerate(ATTR_NAMES):
        for size in ATTR_NAME_SIZES:
            path = os.path.join(d, "attrnames", "n%d-%04d" % (len(name), size)).encode()
            with open(path, "wb"):
                pass
            value = pattern(size, size + i)
            rc = LIBC.setxattr(path, name, value, len(value), 0, XATTR_NOFOLLOW)
            result["%d-%d" % (len(name), size)] = 0 if rc == 0 else ctypes.get_errno()
    with open(out, "w") as f:
        json.dump(result, f, indent=1)


def check(d, out):
    result = {"names": {}, "attrs": {}, "links": {}, "attrnames": {}}
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
    for i, name in enumerate(ATTR_NAMES):
        for size in ATTR_NAME_SIZES:
            path = os.path.join(d, "attrnames", "n%d-%04d" % (len(name), size)).encode()
            if not os.path.exists(path):
                continue
            got = LIBC.getxattr(path, name, buf, len(buf), 0, XATTR_NOFOLLOW)
            ok = got >= 0 and buf.raw[:got] == pattern(size, size + i)
            result["attrnames"]["%d-%d" % (len(name), size)] = ok
    for size in ATTR_SIZES:
        path = os.path.join(d, "attrs", "a%04d" % size).encode()
        if not os.path.exists(path):
            continue
        got = LIBC.getxattr(path, b"user.probe", buf, len(buf), 0, XATTR_NOFOLLOW)
        if got < 0:
            result["attrs"][str(size)] = "errno %d" % ctypes.get_errno()
        else:
            result["attrs"][str(size)] = [got, buf.raw[:got] == pattern(size, size)]
    for length in LINK_LENGTHS:
        path = os.path.join(d, "links", "l%04d" % length)
        if not os.path.lexists(path):
            continue
        try:
            result["links"][str(length)] = os.readlink(path) == "t" * length
        except OSError as e:
            result["links"][str(length)] = "errno %d" % e.errno
    with open(out, "w") as f:
        json.dump(result, f, indent=1)


if __name__ == "__main__":
    {"names": names, "attrs": attrs, "attrnames": attrnames, "check": check}[sys.argv[1]](
        sys.argv[2], sys.argv[3])
