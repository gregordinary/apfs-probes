#!/usr/bin/env python3
"""How large an extended attribute macOS sets on APFS and reads back.

    xattrsizeutil.py ladder DIR NAME MAXLOG OUT.jsonl
    xattrsizeutil.py gap FILE OUT.json

`ladder` sets attribute NAME on a fresh file in DIR to a value of 2^k bytes
for k from 12 to MAXLOG, through setxattr(2) as its manual page declares it,
and after the first size refused bisects between the last size taken and it,
to the byte. Each attempt records the size asked for, 0 or the errno
setxattr set, and the size getxattr(2) then reports. A resource fork is
written in 1 MiB pieces at increasing positions, since a resource fork is the
one attribute setxattr(2) writes at a position; any other attribute is
written in one call. Each file is removed after its attempt, so one attempt's
blocks are free for the next.

`gap` writes FILE's resource fork as 4 KiB at position 0 and 4 KiB at
position 16 MiB, and records the size getxattr reports, so a dump shows how a
fork written with a gap is stored.

Every value is a repeating pattern with no zero bytes.
"""

import ctypes
import ctypes.util
import json
import os
import sys
import time

LIBC = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
LIBC.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                          ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
LIBC.setxattr.restype = ctypes.c_int
LIBC.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                          ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
LIBC.getxattr.restype = ctypes.c_ssize_t

FORK = b"com.apple.ResourceFork"
PIECE = 1 << 20


def pattern(n):
    """n bytes of the pattern, filled a piece at a time so that only the
    buffer itself is ever held."""
    unit = bytes(range(1, 256))
    piece = (unit * (PIECE // len(unit) + 1))[:PIECE]
    buf = bytearray(n)
    for at in range(0, n, PIECE):
        end = min(n, at + PIECE)
        buf[at:end] = piece[:end - at]
    return buf


def set_value(path, name, buf, size):
    """0, or the errno of the first setxattr that failed."""
    pieces = range(0, size, PIECE) if name == FORK else [0]
    for at in pieces:
        n = min(PIECE, size - at) if name == FORK else size
        view = (ctypes.c_char * n).from_buffer(buf, at % PIECE if name == FORK else 0)
        if LIBC.setxattr(path, name, view, n, at, 0) != 0:
            return ctypes.get_errno()
        if size == 0:
            break
    return 0


def got_size(path, name):
    n = LIBC.getxattr(path, name, None, 0, 0, 0)
    return n if n >= 0 else -ctypes.get_errno()


def attempt(directory, name, buf, size, out):
    path = os.path.join(directory, "a%d" % size).encode()
    with open(path, "wb"):
        pass
    began = time.monotonic()
    errno = set_value(path, name, buf, size)
    record = {
        "name": name.decode(),
        "size": size,
        "errno": errno,
        "error": os.strerror(errno) if errno else None,
        "got": got_size(path, name),
        "seconds": round(time.monotonic() - began, 2),
    }
    os.unlink(path)
    out.write(json.dumps(record) + "\n")
    out.flush()
    print(json.dumps(record), flush=True)
    return errno == 0 and record["got"] == size


def ladder(directory, name, maxlog, out_path):
    name = name.encode()
    maxlog = int(maxlog)
    buf = pattern(PIECE if name == FORK else 1 << maxlog)
    taken, refused = None, None
    with open(out_path, "w") as out:
        for k in range(12, maxlog + 1):
            if attempt(directory, name, buf, 1 << k, out):
                taken = 1 << k
            else:
                refused = 1 << k
                break
        if taken is not None and refused is not None:
            lo, hi = taken, refused
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if attempt(directory, name, buf, mid, out):
                    lo = mid
                else:
                    hi = mid
            taken, refused = lo, hi
        out.write(json.dumps({"largest_taken": taken, "smallest_refused": refused}) + "\n")


def gap(path, out_path):
    path = path.encode()
    with open(path, "wb"):
        pass
    buf = pattern(4096)
    view = (ctypes.c_char * 4096).from_buffer(buf)
    first = LIBC.setxattr(path, FORK, view, 4096, 0, 0)
    first = 0 if first == 0 else ctypes.get_errno()
    second = LIBC.setxattr(path, FORK, view, 4096, 16 << 20, 0)
    second = 0 if second == 0 else ctypes.get_errno()
    with open(out_path, "w") as out:
        json.dump({"first": first, "second": second, "got": got_size(path, FORK),
                   "inode": os.stat(path).st_ino}, out)


def main(argv):
    if argv[1:2] == ["ladder"] and len(argv) == 6:
        ladder(*argv[2:])
    elif argv[1:2] == ["gap"] and len(argv) == 4:
        gap(*argv[2:])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
