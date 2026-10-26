#!/usr/bin/env python3
"""Three rules a reader relies on, each asked of macOS directly.

    sourcesutil.py rawinline DIR OUT.json
    sourcesutil.py rawread MNT RECORD.json OUT.jsonl
    sourcesutil.py headers DIR OUT.json
    sourcesutil.py fill DIR MIB OUT.json
    sourcesutil.py snapnames DIR OUT.json

`rawinline` stores files whose content sits after the 16-byte header in the
file's com.apple.decmpfs attribute, type 7, as the byte 0x06 followed by the
content itself, which is how `ditto --hfsCompression` keeps an LZVN chunk that
does not compress in the resource fork (type 8). The question is whether the
kernel reads the same form kept after the header. Beside them: type 7 holding
libcompression's LZVN encoding (0x900) and type 3 holding 0xFF and the
content, both forms the kernel is already known to read there, as controls;
and 0x06 forms whose header states a size other than the content's length.
`rawread` reads each back through the kernel, in a process of its own, and
records what it gives.

`headers` records, for every file under DIR, its size and flags as stat(2)
reports them and the first 16 bytes of its com.apple.decmpfs attribute as
they are, so that where the file's size sits in the header is found by
searching those bytes for it, with no layout assumed.

`fill` writes MIB mebibytes of bytes that do not compress into DIR, a
mebibyte a file, and syncs, recording what each write returned.

`snapnames` asks fs_snapshot_create(2), on the volume holding DIR, for
snapshots named by 255, 256 and 1024 bytes, and records 0 or the errno of each,
then deletes any it made.
"""

import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import stat
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compressutil import DECMPFS, Codec, header, libc, set_xattr  # noqa: E402
from fsprobe import get_xattr, pattern, text  # noqa: E402

LZVN = 0x900
RAW_LZVN = b"\x06"
RAW_ZLIB = b"\xff"
# The longest content a 3,802-byte attribute holds after its header and the
# one marker byte: the attribute limit the inlinelimit probe found.
LONGEST = 3802 - 16 - 1


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    with open(path, "w") as f:
        json.dump(value, f, indent=1, ensure_ascii=True)


def content(family, n):
    return pattern(n, 80) if family == "random" else text(n, 81)


def cases():
    """Each file `rawinline` writes: its name, the type its header records, the
    content's family and length, the size the header states, and how the
    content is kept after the header."""
    out = []
    for family in ("random", "text"):
        for n in (1, 100, 1000, 3000, LONGEST):
            out.append(("t7-raw-%s-%d" % (family, n), 7, family, n, n, "raw-lzvn"))
            out.append(("t3-raw-%s-%d" % (family, n), 3, family, n, n, "raw-zlib"))
        out.append(("t7-lzvn-%s-1000" % family, 7, family, 1000, 1000, "lzvn"))
        out.append(("t7-raw-%s-1000-size-990" % family, 7, family, 1000, 990, "raw-lzvn"))
        out.append(("t7-raw-%s-1000-size-1010" % family, 7, family, 1000, 1010, "raw-lzvn"))
    return out


def rawinline(directory, out):
    codec = Codec()
    lib = libc()
    os.makedirs(directory, exist_ok=True)
    record = []
    for name, kind, family, n, size, form in cases():
        data = content(family, n)
        if form == "raw-lzvn":
            kept = RAW_LZVN + data
        elif form == "raw-zlib":
            kept = RAW_ZLIB + data
        else:
            kept = codec.encode(data, LZVN) or b""
        value = header(kind, size) + kept
        entry = {"name": name, "type": kind, "family": family, "content_len": n,
                 "header_size": size, "form": form, "attr_len": len(value),
                 "content_sha256": sha(data)}
        record.append(entry)
        path = os.path.join(directory, name).encode()
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
            entry["how"] = set_xattr(lib, path, DECMPFS, value)
            os.chflags(path, stat.UF_COMPRESSED)
            entry["ok"] = True
        except OSError as e:
            entry["error"] = errno.errorcode.get(e.errno, str(e.errno))
    write_json(out, record)


READ_CHILD = r"""
import hashlib, json, sys
try:
    with open(sys.argv[1], "rb") as f:
        got = f.read(1 << 20)
    print(json.dumps({"len": len(got), "sha256": hashlib.sha256(got).hexdigest(),
                      "head": got[:16].hex()}))
except OSError as e:
    print(json.dumps({"errno": e.errno}))
"""


def rawread(mnt, record_json, out):
    with open(record_json) as f:
        record = json.load(f)
    with open(out, "w") as log:
        for entry in record:
            if not entry.get("ok"):
                continue
            path = os.path.join(mnt, entry["name"])
            data = content(entry["family"], entry["content_len"])
            got = {"name": entry["name"]}
            try:
                st = os.lstat(path)
                got.update(st_size=st.st_size, flags="0x%x" % getattr(st, "st_flags", 0))
            except OSError as e:
                got["stat"] = errno.errorcode.get(e.errno, str(e.errno))
            try:
                child = subprocess.run([sys.executable, "-c", READ_CHILD, path],
                                       capture_output=True, text=True, timeout=10)
                answer = json.loads(child.stdout or "{}")
            except (subprocess.TimeoutExpired, ValueError):
                answer = {"errno": -1}
            if "errno" in answer:
                got["read"] = "error %s" % errno.errorcode.get(answer["errno"], str(answer["errno"]))
            else:
                got["len"] = answer["len"]
                if answer["sha256"] == sha(data):
                    got["read"] = "content"
                elif answer["sha256"] == sha(data[:answer["len"]]):
                    got["read"] = "content's first bytes"
                elif answer["len"] > len(data) and answer["sha256"] == sha(
                        data + bytes(answer["len"] - len(data))):
                    got["read"] = "content then zeros"
                elif answer["len"] == 0:
                    got["read"] = "nothing"
                else:
                    got["read"] = "other"
                    got["head"] = answer["head"]
            log.write(json.dumps(got) + "\n")
            log.flush()


def headers(directory, out):
    record = []
    for top, dirs, files in os.walk(directory):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(top, name)
            st = os.lstat(path)
            entry = {"file": os.path.relpath(path, directory), "st_size": st.st_size,
                     "flags": "0x%x" % getattr(st, "st_flags", 0)}
            try:
                value = get_xattr(path.encode(), DECMPFS)
                entry["decmpfs_len"] = len(value)
                entry["first16"] = value[:16].hex()
            except OSError as e:
                entry["decmpfs"] = errno.errorcode.get(e.errno, str(e.errno))
            record.append(entry)
    write_json(out, record)


def fill(directory, mib, out):
    os.makedirs(directory, exist_ok=True)
    record = []
    for i in range(int(mib)):
        path = os.path.join(directory, "fill-%04d" % i)
        entry = {"file": os.path.basename(path)}
        try:
            with open(path, "wb") as f:
                f.write(pattern(1 << 20, 1000 + i))
                f.flush()
                os.fsync(f.fileno())
            entry["ok"] = True
        except OSError as e:
            entry["error"] = errno.errorcode.get(e.errno, str(e.errno))
        record.append(entry)
    write_json(out, record)


def snapnames(directory, out):
    lib = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    lib.fs_snapshot_create.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    lib.fs_snapshot_delete.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    fd = os.open(directory, os.O_RDONLY)
    record = []
    for n in (255, 256, 1024):
        name = b"s" * n
        entry = {"bytes": n}
        if lib.fs_snapshot_create(fd, name, 0) == 0:
            entry["create"] = 0
            entry["delete"] = 0 if lib.fs_snapshot_delete(fd, name, 0) == 0 else \
                errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))
        else:
            e = ctypes.get_errno()
            entry["create"] = errno.errorcode.get(e, str(e))
        record.append(entry)
    os.close(fd)
    write_json(out, record)


def main(argv):
    commands = {"rawinline": (rawinline, 2), "rawread": (rawread, 3), "headers": (headers, 2),
                "fill": (fill, 3), "snapnames": (snapnames, 2)}
    if len(argv) < 2 or argv[1] not in commands or len(argv) != commands[argv[1]][1] + 2:
        raise SystemExit(__doc__)
    commands[argv[1]][0](*argv[2:])


if __name__ == "__main__":
    main(sys.argv)
