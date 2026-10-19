#!/usr/bin/env python3
"""Which extended attribute names macOS sets, lists and reads on APFS.

    xattrnamesutil.py sweep DIR OUT.json
    xattrnamesutil.py list DIR XATTRNAMES NAME OUT.json
    xattrnamesutil.py rw DIR XATTRNAMES NAME OUT.json

`sweep` sets one attribute after another on a fresh file in DIR through
setxattr(2) as its manual page declares it, each then read back by
getxattr(2), looked for in listxattr(2) and removed. The names are `n`
repeated to every length from 1 to 300 bytes and to 383 to 385, 511 to 513,
767, 768, 1,000, 1,023 to 1,025, 1,969, 2,000 and 4,000; a character of two,
three and four bytes repeated to either side of 127 bytes and to 127
characters; and the names of namespaces macOS gives no meaning to, on a file
and on a directory. Each records the name's bytes and characters and 0 or
the errno of each call.

XATTRNAMES is the xattrnames/ directory, whose images.json states each
container's files and the attributes each holds, by name in hex with the
SHA-256 of the value. `list` lists every stated file of the container NAME
mounted at DIR, reads every name listed and every name stated, and records
what it found beside the statement; `user.after`, which `rw` sets, is noted
apart from the names held to it. `rw` sets `user.after` on every stated
file and sets each stated attribute again to the value it holds, recording 0
or the errno of each call.
"""

import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import sys

LIBC = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
for fn, res in (("setxattr", ctypes.c_int), ("getxattr", ctypes.c_ssize_t),
                ("listxattr", ctypes.c_ssize_t), ("removexattr", ctypes.c_int)):
    getattr(LIBC, fn).restype = res
LIBC.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                          ctypes.c_uint32, ctypes.c_int]
LIBC.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                          ctypes.c_uint32, ctypes.c_int]
LIBC.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
LIBC.removexattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
NOFOLLOW = 0x0001
AFTER = b"user.after"


def read_json(path):
    with open(path) as f:
        return json.load(f)


def write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f, indent=1, sort_keys=True, ensure_ascii=True)
        f.write("\n")


def err():
    e = ctypes.get_errno()
    return errno.errorcode.get(e, str(e))


def setx(path, name, value):
    buf = ctypes.create_string_buffer(value, len(value))
    rc = LIBC.setxattr(path.encode(), name, buf, len(value), 0, NOFOLLOW)
    return 0 if rc == 0 else err()


def getx(path, name):
    """The value, or the errno."""
    n = LIBC.getxattr(path.encode(), name, None, 0, 0, NOFOLLOW)
    if n < 0:
        return err()
    buf = ctypes.create_string_buffer(max(n, 1))
    n = LIBC.getxattr(path.encode(), name, buf, n, 0, NOFOLLOW)
    if n < 0:
        return err()
    return buf.raw[:n]


def listx(path):
    """The names, or the errno."""
    n = LIBC.listxattr(path.encode(), None, 0, NOFOLLOW)
    if n < 0:
        return err()
    buf = ctypes.create_string_buffer(max(n, 1))
    n = LIBC.listxattr(path.encode(), buf, n, NOFOLLOW)
    if n < 0:
        return err()
    return [x for x in buf.raw[:n].split(b"\x00") if x]


def removex(path, name):
    rc = LIBC.removexattr(path.encode(), name, NOFOLLOW)
    return 0 if rc == 0 else err()


def digest(value):
    return value if isinstance(value, str) else hashlib.sha256(value).hexdigest()


LINUX = [b"security.selinux", b"security.capability", b"trusted.overlay.opaque",
         b"user.comment", b"system.posix_acl_access", b"system.posix_acl_default"]


def names():
    out = [b"n" * k for k in list(range(1, 301)) + [383, 384, 385, 511, 512, 513, 767, 768,
                                                     1000, 1023, 1024, 1025, 1969, 2000, 4000]]
    for ch in ("é", "€", "\U0001d11e"):
        w = len(ch.encode())
        for count in sorted({126 // w, 127 // w, 127 // w + 1, 128 // w + 1, 127}):
            out.append((ch * count).encode())
    return out + LINUX


def attempt(path, name):
    value = b"value of " + str(len(name)).encode()
    rec = {"name_hex": name.hex() if len(name) <= 64 else None, "bytes": len(name),
           "chars": len(name.decode("utf-8", "replace")), "set": setx(path, name, value)}
    if len(name) > 64:
        rec["name_sha256"] = hashlib.sha256(name).hexdigest()
        rec["name_head"] = name[:16].hex()
    got = getx(path, name)
    rec["get"] = 0 if got == value else (got if isinstance(got, str) else "other value")
    listed = listx(path)
    rec["listed"] = listed if isinstance(listed, str) else name in listed
    rec["remove"] = removex(path, name) if rec["set"] == 0 else None
    return rec


def sweep(d, out):
    f = os.path.join(d, "sweep")
    with open(f, "wb") as fh:
        fh.write(b"x\n")
    sub = os.path.join(d, "sweep-dir")
    os.mkdir(sub)
    rec = {"file": [attempt(f, n) for n in names()],
           "directory": [attempt(sub, n) for n in LINUX]}
    write_json(out, rec)
    took = [r["bytes"] for r in rec["file"] if r["set"] == 0 and r["name_hex"] and
            set(bytes.fromhex(r["name_hex"])) == {ord("n")}]
    refused = sorted({r["set"] for r in rec["file"] if r["set"] != 0})
    print(json.dumps({"longest_n_taken": max(took) if took else None, "refusals": refused}))


def stated(xn, name):
    for image in read_json(os.path.join(xn, "images.json"))["images"]:
        if image["name"] == name:
            return image
    raise SystemExit("no image named %s" % name)


def listing(d, xn, name, out):
    image = stated(xn, name)
    rec, same = {}, True
    for path, attrs in sorted(image["files"].items()):
        p = os.path.join(d, path)
        listed = listx(p)
        entry = {"listed": listed if isinstance(listed, str) else
                 sorted(x.hex() for x in listed if x != AFTER)}
        entry["after_listed"] = not isinstance(listed, str) and AFTER in listed
        entry["read_listed"] = {} if isinstance(listed, str) else {
            x.hex(): digest(getx(p, x)) for x in listed}
        entry["read_stated"] = {h: digest(getx(p, bytes.fromhex(h))) for h in attrs}
        entry["same"] = (entry["listed"] == sorted(attrs) and entry["read_stated"] == attrs)
        same = same and entry["same"]
        rec[path] = entry
    write_json(out, {"name": name, "same": same, "files": rec})
    print(json.dumps({"same": same, "differ": sorted(k for k, v in rec.items()
                                                      if not v["same"])}))


def rw(d, xn, name, out):
    image = stated(xn, name)
    rec = {}
    for path, attrs in sorted(image["files"].items()):
        p = os.path.join(d, path)
        entry = {"after": setx(p, AFTER, b"after"), "again": {}}
        for h in attrs:
            value = getx(p, bytes.fromhex(h))
            entry["again"][h] = (value if isinstance(value, str)
                                 else setx(p, bytes.fromhex(h), value))
        rec[path] = entry
    write_json(out, {"name": name, "files": rec})
    print(json.dumps({k: [v["after"], sorted(set(map(str, v["again"].values())))]
                      for k, v in rec.items()}, sort_keys=True))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "sweep" and len(argv) == 4:
        sweep(argv[2], argv[3])
    elif cmd == "list" and len(argv) == 6:
        listing(*argv[2:])
    elif cmd == "rw" and len(argv) == 6:
        rw(*argv[2:])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
