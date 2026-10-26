#!/usr/bin/env python3
"""Attributes in three places macOS may not keep them, and how macOS's own
compressor stores a chunk that does not compress.

    edgesutil.py make DIR OUT.json
    edgesutil.py again DIR OUT.json
    edgesutil.py list DIR EDGES NAME OUT.json
    edgesutil.py rw DIR EDGES NAME OUT.json
    edgesutil.py stage DIR OUT.json
    edgesutil.py compressed DIR STAGE.json OUT.json

`make` builds, on the volume mounted at DIR, a character device, a block
device and a named pipe each with an attribute; files with attribute names
that are not UTF-8; and files with a resource fork of no bytes and of three,
recording 0 or the errno of each call and what listxattr(2) and getxattr(2)
then answer. It runs as root, which a device node needs. `again` records
what listxattr(2) and getxattr(2) answer for each of them once more, after
the volume is mounted again.

EDGES is the edges/ directory, whose images.json states each container's
paths and the attributes each holds, by name in hex with the SHA-256 of the
value. `list` lists every stated path of the container NAME mounted at DIR,
reads every name listed and every name stated, and records what it found
beside the statement, with each regular file's content. `rw` sets
`user.after` on every stated path and each stated attribute again to the
value it holds.

`stage` writes files whose 64 KiB chunks mix text with bytes that do not
compress, in every order, into DIR. `compressed` records, for each file of
STAGE.json found under DIR, its flags, the compression type and size its
header records, and whether the kernel reads it back as its content.
"""

import errno
import hashlib
import json
import os
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fsprobe import get_xattr, list_xattrs, pattern, set_xattr, text  # noqa: E402

CHUNK = 64 * 1024
FORK = b"com.apple.ResourceFork"
AFTER = b"user.after"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    with open(path, "w") as f:
        json.dump(value, f, indent=1, sort_keys=True, ensure_ascii=True)
        f.write("\n")


def code(e):
    return errno.errorcode.get(e.errno, str(e.errno))


def attempt(fn, *args):
    try:
        fn(*args)
        return 0
    except OSError as e:
        return code(e)


def held(path):
    """What listxattr(2) and getxattr(2) answer for path, names in hex."""
    out = {}
    try:
        names = list_xattrs(path)
    except OSError as e:
        return {"list": code(e)}
    out["listed"] = [n.hex() for n in names]
    values = {}
    for n in names:
        try:
            v = get_xattr(path, n)
            values[n.hex()] = {"len": len(v), "sha256": sha(v)}
        except OSError as e:
            values[n.hex()] = code(e)
    out["values"] = values
    return out


# Nodes `make` creates: a path, how it is made, and the attributes then set.
MADE = [
    ("cdev", "chr", [(b"user.a", b"on a character device")]),
    ("bdev", "blk", [(b"user.b", b"on a block device")]),
    ("fifo", "fifo", [(b"user.c", b"on a named pipe")]),
    ("f", "file", [(b"user.\xff\xfe", b"not UTF-8")]),
    ("g", "file", [(b"user.\xe9t\xe9", b"Latin-1")]),
    ("e", "file", [(FORK, b"")]),
    ("n", "file", [(FORK, b"abc")]),
    ("plain", "file", [(b"user.kept", b"kept")]),
]


def make(directory, out):
    record = {}
    for name, how, xattrs in MADE:
        path = os.path.join(directory, name).encode()
        entry = {}
        if how == "file":
            with open(path, "wb") as f:
                f.write(name.encode() + b"\n")
            entry["made"] = 0
        elif how == "fifo":
            entry["made"] = attempt(os.mkfifo, path, 0o644)
        else:
            mode = (stat.S_IFCHR if how == "chr" else stat.S_IFBLK) | 0o644
            entry["made"] = attempt(os.mknod, path, mode, os.makedev(1, 3))
        if entry["made"] == 0:
            entry["set"] = {n.hex(): attempt(set_xattr, path, n, v) for n, v in xattrs}
            entry["held"] = held(path)
        record[name] = entry
    write_json(out, record)


def again(directory, out):
    record = {}
    for name, _, _ in MADE:
        path = os.path.join(directory, name).encode()
        try:
            os.lstat(path)
        except OSError as e:
            record[name] = {"lstat": code(e)}
            continue
        record[name] = held(path)
    write_json(out, record)


def statement(edges, name):
    with open(os.path.join(edges, "images.json")) as f:
        for image in json.load(f)["images"]:
            if image["name"] == name:
                return image
    raise SystemExit("no image named %s" % name)


def list_image(directory, edges, name, out):
    image = statement(edges, name)
    record = {}
    for path, stated in image["paths"].items():
        full = (directory + path).encode()
        entry = {"stated": stated}
        try:
            st = os.lstat(full)
            entry["mode"] = "0o%o" % st.st_mode
            entry["flags"] = "0x%x" % getattr(st, "st_flags", 0)
        except OSError as e:
            entry["lstat"] = code(e)
            record[path] = entry
            continue
        entry.update(held(full))
        found = {}
        for n in stated:
            try:
                found[n] = sha(get_xattr(full, bytes.fromhex(n)))
            except OSError as e:
                found[n] = code(e)
        entry["read_stated"] = found
        entry["as_stated"] = all(found[n] == v for n, v in stated.items())
        if stat.S_ISREG(st.st_mode):
            try:
                with open(full, "rb") as f:
                    entry["content_sha256"] = sha(f.read())
            except OSError as e:
                entry["content"] = code(e)
        record[path] = entry
    write_json(out, record)


def rw(directory, edges, name, out):
    image = statement(edges, name)
    record = {}
    for path, stated in image["paths"].items():
        full = (directory + path).encode()
        entry = {"after": attempt(set_xattr, full, AFTER, b"after")}
        again = {}
        for n in stated:
            try:
                value = get_xattr(full, bytes.fromhex(n))
            except OSError as e:
                again[n] = code(e)
                continue
            again[n] = attempt(set_xattr, full, bytes.fromhex(n), value)
        entry["again"] = again
        record[path] = entry
    write_json(out, record)


def staged():
    """Each staged file's content, by name: text is t, bytes that do not
    compress are r, a chunk each."""
    out = {}
    for layout in ["trt", "rtt", "ttr", "rrt", "trr", "r", "tr", "rt"]:
        data = b""
        for i, c in enumerate(layout):
            data += text(CHUNK, 100 + i) if c == "t" else pattern(CHUNK, 200 + i)
        out["mixed-" + layout] = data
    # A last chunk shorter than the rest, of each kind.
    out["mixed-tr-short"] = text(CHUNK, 300) + pattern(5000, 301)
    out["mixed-rt-short"] = pattern(CHUNK, 302) + text(5000, 303)
    return out


def stage(directory, out):
    os.makedirs(directory, exist_ok=True)
    record = {}
    for name, data in staged().items():
        with open(os.path.join(directory, name), "wb") as f:
            f.write(data)
        record[name] = {"size": len(data), "sha256": sha(data)}
    write_json(out, record)


def compressed(directory, stage_json, out):
    import struct
    with open(stage_json) as f:
        want = json.load(f)
    record = {}
    for name, w in sorted(want.items()):
        path = os.path.join(directory, name).encode()
        entry = {}
        try:
            st = os.lstat(path)
            entry["flags"] = "0x%x" % getattr(st, "st_flags", 0)
            entry["size"] = st.st_size
        except OSError as e:
            entry["lstat"] = code(e)
            record[name] = entry
            continue
        try:
            header = get_xattr(path, b"com.apple.decmpfs")
            if len(header) >= 16 and header[:4] == b"fpmc":
                kind, size = struct.unpack("<IQ", header[4:16])
                entry.update(type=kind, header_size=size, decmpfs_len=len(header))
        except OSError as e:
            entry["decmpfs"] = code(e)
        try:
            with open(path, "rb") as f:
                entry["read"] = "match" if sha(f.read()) == w["sha256"] else "mismatch"
        except OSError as e:
            entry["read"] = code(e)
        record[name] = entry
    write_json(out, record)


def main(argv):
    cmd = argv[1]
    if cmd == "make":
        make(argv[2], argv[3])
    elif cmd == "again":
        again(argv[2], argv[3])
    elif cmd == "list":
        list_image(argv[2], argv[3], argv[4], argv[5])
    elif cmd == "rw":
        rw(argv[2], argv[3], argv[4], argv[5])
    elif cmd == "stage":
        stage(argv[2], argv[3])
    elif cmd == "compressed":
        compressed(argv[2], argv[3], argv[4])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
