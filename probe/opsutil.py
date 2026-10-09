#!/usr/bin/env python3
"""Small, fixed operations on a mounted volume, for probes that compare the
image before and after one of them.

    opsutil.py write PATH SIZE SEED     a new file of SIZE patterned bytes
    opsutil.py extend PATH SIZE         set PATH's length to SIZE
    opsutil.py setxattr PATH NAME SIZE  an extended attribute of SIZE bytes
    opsutil.py tree DIR                 a small fixed tree
    opsutil.py srctree DIR              a source tree with fixed times
    opsutil.py srctree-reverse DIR      the same tree, created in reverse order
    opsutil.py sweep DIR RESULT.json    one file per code point (see below)

Contents come from fsprobe.pattern, so the same operation writes the same
bytes on every machine.
"""

import errno
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fsprobe  # noqa: E402

# 2020-09-13 12:26:40 UTC: a fixed time for every entry of the source tree.
FIXED_NS = 1600000000 * 10**9


def write(path, size, seed):
    fsprobe.write_file(path, fsprobe.pattern(size, seed))


def tree(d):
    """a: empty; b: three blocks with a small xattr; d/e: one block with a
    large xattr; s: symlink to b; h: hard link to b."""
    j = lambda *p: os.path.join(d, *p)  # noqa: E731
    write(j("a"), 0, 1)
    write(j("b"), 3 * 4096, 2)
    os.mkdir(j("d"))
    write(j("d", "e"), 4096, 3)
    os.symlink("b", j("s"))
    os.link(j("b"), j("h"))
    fsprobe.set_xattr(j("b").encode(), b"user.small", fsprobe.pattern(16, 4))
    fsprobe.set_xattr(j("d", "e").encode(), b"user.big", fsprobe.pattern(8000, 5))


SRC_FILES = [
    ("top/empty", 0), ("top/one", 1), ("top/hundred", 100), ("top/under", 4095),
    ("top/block", 4096), ("top/over", 4097), ("top/three", 12288),
    ("top/sub1/sixtyfour", 65536), ("top/sub1/mega", (1 << 20) + 3),
    ("top/sub2/deep/a", 10), ("top/sub2/deep/b", 20000), ("top/sub2/école", 300),
]


def srctree(d, reverse=False):
    """A tree whose every entry has fixed contents and fixed times, so two
    copies of it differ only where the copying filesystem chooses. With
    reverse, the same entries are created in the opposite order, which
    changes the source's inode numbers and directory order and nothing
    else."""
    for rel in ("top", "top/sub1", "top/sub2", "top/sub2/deep", "top/sub2/empty-dir"):
        os.makedirs(os.path.join(d, rel), exist_ok=True)
    files = [(rel, size, 100 + i) for i, (rel, size) in enumerate(SRC_FILES)]
    files += [("top/sub1/n%02d" % i, 50 * i, 200 + i) for i in range(30)]
    for rel, size, seed in (reversed(files) if reverse else files):
        write(os.path.join(d, rel), size, seed)
    os.symlink("block", os.path.join(d, "top/link-rel"))
    os.symlink("/nowhere/at/all", os.path.join(d, "top/link-abs"))
    os.link(os.path.join(d, "top/three"), os.path.join(d, "top/sub1/three-again"))
    fsprobe.set_xattr(os.path.join(d, "top/block").encode(), b"user.small", fsprobe.pattern(16, 300))
    fsprobe.set_xattr(os.path.join(d, "top/over").encode(), b"user.big", fsprobe.pattern(8000, 301))
    # Deepest entries first, so setting a directory's times comes after its
    # children are in place.
    entries = []
    for root, dirs, files in os.walk(d):
        for name in dirs + files:
            entries.append(os.path.join(root, name))
    for path in sorted(entries, key=lambda p: -p.count(os.sep)):
        os.utime(path, ns=(FIXED_NS, FIXED_NS), follow_symlinks=False)
    os.utime(d, ns=(FIXED_NS, FIXED_NS))


# The sweep: planes 0 to 3 in full, plane 14's assigned block, and the first
# 256 code points of every other plane, as a control that unassigned planes
# are refused. Each code point is one name, "x" followed by it, created in
# ascending order, so on an insensitive volume the first of an equivalent
# pair is the one that exists and the second fails with EEXIST. SWEEP_PART
# in the environment selects one part: "bmp" is plane 0, "rest" the others.
SWEEP_PARTS = {
    "bmp": [(0x0, 0x10000)],
    "rest": [(0x10000, 0x40000), (0xE0000, 0xE1000)] +
            [(p << 16, (p << 16) + 0x100) for p in list(range(4, 14)) + [15, 16]],
}
SWEEP_RANGES = SWEEP_PARTS["bmp"] + SWEEP_PARTS["rest"]
SKIP = {0x00, 0x2F}

# Byte strings that are not UTF-8: a surrogate, an overlong slash, a stray
# byte, a code point past U+10FFFF, and a truncated sequence.
SWEEP_RAW = [b"x\xed\xa0\x80", b"x\xed\xbf\xbf", b"x\xc0\xaf", b"x\xff",
             b"x\xf4\x90\x80\x80", b"x\xe2\x82"]


def sweep(d, result):
    """Create the sweep's names under d, writing RESULT every 10,000 names so
    a run cut short still leaves what it did."""
    os.mkdir(d)
    base = os.fsencode(d)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    errors = {}
    created = 0
    t0 = time.time()
    ranges = SWEEP_PARTS.get(os.environ.get("SWEEP_PART", ""), SWEEP_RANGES)
    done = [0]

    def save(last):
        tmp = result + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"ranges": ranges, "skipped": sorted(SKIP) + ["surrogates"],
                       "created": created, "seconds": round(time.time() - t0, 1),
                       "complete": last, "through": done[0], "errors": errors}, f)
        os.replace(tmp, result)

    def attempt(name, label):
        nonlocal created
        try:
            os.close(os.open(os.path.join(base, name), flags, 0o644))
            created += 1
        except OSError as e:
            errors.setdefault(errno.errorcode.get(e.errno, str(e.errno)), []).append(label)

    for lo, hi in ranges:
        for cp in range(lo, hi):
            if cp in SKIP or 0xD800 <= cp <= 0xDFFF:
                continue
            attempt(b"x" + chr(cp).encode("utf-8"), cp)
            done[0] = cp
            if cp % 10000 == 0:
                save(False)
    for raw in SWEEP_RAW:
        attempt(raw, raw.hex())
    save(True)


def main(argv):
    if len(argv) == 5 and argv[1] == "write":
        write(argv[2], int(argv[3]), int(argv[4]))
    elif len(argv) == 4 and argv[1] == "extend":
        os.truncate(argv[2], int(argv[3]))
    elif len(argv) == 5 and argv[1] == "setxattr":
        fsprobe.set_xattr(argv[2].encode(), argv[3].encode(), fsprobe.pattern(int(argv[4]), 9))
    elif len(argv) == 3 and argv[1] == "tree":
        tree(argv[2])
    elif len(argv) == 3 and argv[1] == "srctree":
        srctree(argv[2])
    elif len(argv) == 3 and argv[1] == "srctree-reverse":
        srctree(argv[2], reverse=True)
    elif len(argv) == 4 and argv[1] == "sweep":
        sweep(argv[2], argv[3])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
