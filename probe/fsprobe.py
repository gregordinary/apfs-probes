#!/usr/bin/env python3
"""Build a fixed tree on a mounted volume, and record what the host sees.

    fsprobe.py populate MNT RESULT.json
    fsprobe.py mutate MNT RESULT.json
    fsprobe.py snapshot MNT NAME RESULT.json
    fsprobe.py manifest MNT OUT.json

`populate` records the outcome of every operation, so a name the volume
refuses or folds into an earlier one is a result rather than a failure.
`mutate` changes the tree after a snapshot, so the snapshot and the live
tree differ in known ways. `manifest` records every entry as the host's
system calls report it: the tree a reader of the image must reproduce.

The tree is deterministic: file contents come from fixed patterns, so the
same tree built on two machines differs only where the filesystem does.
"""

import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile

DARWIN = sys.platform == "darwin"
XATTR_NOFOLLOW = 0x0001
XATTR_SHOWCOMPRESSION = 0x0020

if DARWIN:
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
    libc.listxattr.restype = ctypes.c_ssize_t
    libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                              ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    libc.getxattr.restype = ctypes.c_ssize_t
    libc.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                              ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    libc.setxattr.restype = ctypes.c_int
    libc.fs_snapshot_create.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    libc.fs_snapshot_create.restype = ctypes.c_int


def _check(rc):
    if rc < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return rc


def _linux_name(name):
    return b"user." + name


def list_xattrs(path):
    if not DARWIN:
        return [n.encode() for n in os.listxattr(path, follow_symlinks=False)]
    opts = XATTR_NOFOLLOW | XATTR_SHOWCOMPRESSION
    size = _check(libc.listxattr(path, None, 0, opts))
    if size == 0:
        return []
    buf = ctypes.create_string_buffer(size)
    size = _check(libc.listxattr(path, buf, size, opts))
    return [n for n in buf.raw[:size].split(b"\0") if n]


def get_xattr(path, name):
    if not DARWIN:
        return os.getxattr(path, name, follow_symlinks=False)
    opts = XATTR_NOFOLLOW | XATTR_SHOWCOMPRESSION
    size = _check(libc.getxattr(path, name, None, 0, 0, opts))
    buf = ctypes.create_string_buffer(max(size, 1))
    size = _check(libc.getxattr(path, name, buf, size, 0, opts))
    return buf.raw[:size]


def set_xattr(path, name, value):
    if not DARWIN:
        os.setxattr(path, _linux_name(name), value, follow_symlinks=False)
        return
    _check(libc.setxattr(path, name, value, len(value), 0, XATTR_NOFOLLOW))


def pattern(n, seed):
    """n incompressible bytes, fixed by seed."""
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(b"%d:%d" % (seed, counter)).digest()
        counter += 1
    return bytes(out[:n])


def text(n, seed):
    """n compressible bytes, fixed by seed."""
    out = bytearray()
    line = 0
    while len(out) < n:
        out += b"line %06d of seed %d: the quick brown fox jumps over the lazy dog\n" % (line, seed)
        line += 1
    return bytes(out[:n])


# Names created in order in one directory. On a case- or normalization-
# insensitive volume a later name equal to an earlier one fails with EEXIST,
# which records the volume's equivalence of the two.
NAMES = [
    "ascii", "MixedCase", "mixedcase", "MIXEDCASE",
    "école", "école", "ÉCOLE", "ÉCOLE",
    "straße", "STRASSE", "strasse", "STRAẞE",
    "K", "K", "k",
    "Å", "Å", "Å", "å",
    "Ω", "Ω", "ω",
    "İ", "i̇", "I", "i", "ı",
    "ﬁ", "fi", "FI",
    "Σα", "σα", "ςα",
    "ᾈ", "ᾀ", "ᾀ", "ἀι",
    "가", "가",
    "豈", "豈",
    "Ꭰ", "ꭰ",
    "ა", "Ა",
    "Ⱟ", "ⱟ",
    "Ꟁ", "ꟁ",
    "Ɤ", "ɤ",
    "\U00010570", "\U00010597",
    "\U00016e40", "\U00016e60",
    "\U0001e900", "\U0001e922",
    "\U000104b0", "\U000104d8",
    "\U00010d50", "\U00010d70",
    "\U0001f600", "", "\U000e0001", "͸", "\U000e0080",
    "colon:name", "back\\slash", "new\nline", "tab\tname", " leading-space",
    "trailing-dot.", "​zero-width",
    "L" * 255, "L" * 256,
    "€" * 85, "€" * 86, "€" * 255, "€" * 256,
]

RAW_NAMES = [b"bad-\xff", b"bad-\xc0\xaf", b"bad-\xed\xa0\x80"]


class Log:
    def __init__(self):
        self.ops = []

    def run(self, desc, fn, *args, **kwargs):
        entry = {"op": desc}
        try:
            value = fn(*args, **kwargs)
            entry["ok"] = True
            if value is not None:
                entry["value"] = value
        except OSError as e:
            entry["ok"] = False
            entry["errno"] = e.errno
            entry["error"] = errno.errorcode.get(e.errno, str(e.errno))
        except Exception as e:  # recorded, never fatal
            entry["ok"] = False
            entry["error"] = "%s: %s" % (type(e).__name__, e)
        self.ops.append(entry)
        return entry["ok"]

    def save(self, path):
        with open(path, "w") as f:
            json.dump(self.ops, f, indent=1, ensure_ascii=True)


def write_file(path, data, mode=0o644):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def populate(mnt, result):
    log = Log()
    root = os.fsencode(mnt)

    def p(*parts):
        return os.path.join(root, *[os.fsencode(x) if isinstance(x, str) else x for x in parts])

    for d in ("plain", "links", "links/other", "special", "times", "xattr", "names",
              "names-raw", "bigdir", "flags", "owner", "perm", "compress", "deep"):
        log.run("mkdir " + d, os.mkdir, p(d))

    # Regular files: empty, inline-sized, one block, several blocks, sparse.
    log.run("plain/empty", write_file, p("plain/empty"), b"")
    log.run("plain/small", write_file, p("plain/small"), b"hello\n")
    log.run("plain/block", write_file, p("plain/block"), pattern(4096, 1))
    log.run("plain/multi", write_file, p("plain/multi"), pattern(3 * 4096 + 17, 2))
    log.run("plain/large", write_file, p("plain/large"), pattern(5 * 1024 * 1024 + 3, 3))

    def sparse(path, offset, data, size):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            if data:
                os.lseek(fd, offset, os.SEEK_SET)
                os.write(fd, data)
            os.ftruncate(fd, size)
        finally:
            os.close(fd)

    log.run("plain/sparse-middle", sparse, p("plain/sparse-middle"), 1 << 20, b"x" * 10, 2 << 20)
    log.run("plain/sparse-tail", sparse, p("plain/sparse-tail"), 0, b"head", 8 << 20)
    log.run("plain/sparse-empty", sparse, p("plain/sparse-empty"), 0, b"", 8 << 20)

    # Links: hard links in two directories, symlinks of several lengths.
    log.run("links/target", write_file, p("links/target"), b"target contents\n")
    log.run("links/hard1", os.link, p("links/target"), p("links/hard1"))
    log.run("links/other/hard2", os.link, p("links/target"), p("links/other/hard2"))
    log.run("links/sym-rel", os.symlink, b"target", p("links/sym-rel"))
    log.run("links/sym-abs", os.symlink, b"/nonexistent/absolute/target", p("links/sym-abs"))
    log.run("links/sym-long", os.symlink, b"d/" * 500 + b"end", p("links/sym-long"))
    log.run("links/sym-dangling", os.symlink, b"missing", p("links/sym-dangling"))
    log.run("links/dir-link", os.symlink, b"other", p("links/dir-link"))

    # Special files. Device numbers probe how rdev is packed.
    log.run("special/fifo", os.mkfifo, p("special/fifo"), 0o644)
    for name, kind, major, minor in (
            ("cdev-1-2", stat.S_IFCHR, 1, 2),
            ("bdev-3-4", stat.S_IFBLK, 3, 4),
            ("cdev-255-max", stat.S_IFCHR, 255, 0xFFFFFF),
            ("cdev-0-0", stat.S_IFCHR, 0, 0)):
        log.run("special/" + name, lambda path=p("special", name), kind=kind, major=major, minor=minor:
                os.mknod(path, kind | 0o644, os.makedev(major, minor)))

    def make_socket(directory, name):
        cwd = os.getcwd()
        os.chdir(directory)
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(name)
            s.close()
        finally:
            os.chdir(cwd)

    log.run("special/sock", make_socket, p("special"), "sock")

    # Timestamps: pre-1970, the nanosecond before the epoch, the epoch,
    # 2038, far future, and a value with every nanosecond digit set.
    times = {
        "pre1970": -10 * 365 * 86400 * 10**9 + 123,
        "minus1ns": -1,
        "epoch": 0,
        "y2038": 2**31 * 10**9,
        "y2400": 13569465600 * 10**9 + 987654321,
        "nsec": 1700000000 * 10**9 + 123456789,
        "pre1900": -2208988800 * 10**9 - 5 * 10**9,
    }
    for name, ns in sorted(times.items()):
        path = p("times", name)
        if log.run("times/" + name, write_file, path, name.encode()):
            log.run("utime times/" + name, os.utime, path, ns=(ns, ns))
    if log.run("times/symlink", os.symlink, b"epoch", p("times/symlink")):
        log.run("utime times/symlink", os.utime, p("times/symlink"),
                ns=(times["pre1970"], times["nsec"]), follow_symlinks=False)

    # Extended attributes: small, one past the inline limit, large, many,
    # on a directory and on a symlink.
    for name, size in (("small", 5), ("inline-max", 3803), ("inline-edge", 3804), ("past-inline", 3805),
                       ("big", 64 * 1024), ("huge", 200 * 1024)):
        path = p("xattr", name)
        if log.run("xattr/" + name, write_file, path, name.encode()):
            log.run("setxattr xattr/" + name, set_xattr, path, b"probe.value", pattern(size, 10 + size))
    if log.run("xattr/many", write_file, p("xattr/many"), b"many"):
        for i in range(50):
            log.run("setxattr xattr/many %d" % i, set_xattr, p("xattr/many"),
                    b"probe.attr%02d" % i, b"value %d" % i)
    log.run("setxattr xattr (dir)", set_xattr, p("xattr"), b"probe.dir", b"on a directory")
    if log.run("xattr/symlink", os.symlink, b"small", p("xattr/symlink")):
        log.run("setxattr xattr/symlink", set_xattr, p("xattr/symlink"), b"probe.link", b"on a symlink")

    # Names, in order; the outcome of each create is the observation.
    for name in NAMES:
        log.run("name " + name.encode("utf-8").hex(), write_file, p("names", name), b"")
    for raw in RAW_NAMES:
        log.run("raw-name " + raw.hex(), write_file, p("names-raw", raw), b"")

    # A directory large enough to need several tree levels.
    for i in range(2500):
        log.run("bigdir %d" % i, write_file,
                p("bigdir", "entry-%05d-%s" % (i, hashlib.sha256(b"%d" % i).hexdigest()[:12])), b"")
    log.ops = [op for op in log.ops if not (op["op"].startswith("bigdir ") and op["ok"])]

    # BSD flags.
    for name, flags in (("hidden", "UF_HIDDEN"), ("nodump", "UF_NODUMP"),
                        ("uchg", "UF_IMMUTABLE"), ("opaque", "UF_OPAQUE")):
        path = p("flags", name)
        if log.run("flags/" + name, write_file, path, name.encode()):
            if hasattr(os, "chflags") and hasattr(stat, flags):
                log.run("chflags flags/" + name, os.chflags, path, getattr(stat, flags))

    # Ownership and permission bits.
    if log.run("owner/u1234", write_file, p("owner/u1234"), b"owned"):
        log.run("chown owner/u1234", os.chown, p("owner/u1234"), 1234, 5678)
    if log.run("owner/u0", write_file, p("owner/u0"), b"root"):
        log.run("chown owner/u0", os.chown, p("owner/u0"), 0, 0)
    for name, mode in (("0000", 0o000), ("4755", 0o4755), ("2755", 0o2755), ("0777", 0o777)):
        path = p("perm", name)
        if log.run("perm/" + name, write_file, path, name.encode()):
            log.run("chmod perm/" + name, os.chmod, path, mode)
    if log.run("perm/sticky-dir", os.mkdir, p("perm/sticky-dir")):
        log.run("chmod perm/sticky-dir", os.chmod, p("perm/sticky-dir"), 0o1777)

    # Deep nesting.
    path = p("deep")
    for i in range(20):
        path = os.path.join(path, b"d%02d" % i)
        log.run("deep %d" % i, os.mkdir, path)
    log.run("deep/leaf", write_file, os.path.join(path, b"leaf"), b"leaf\n")

    # Compression through ditto, from a staging tree on the host.
    if DARWIN:
        stage = tempfile.mkdtemp(prefix="fsprobe-stage-")
        files = {
            "text-small": text(1000, 1),
            "text-edge": text(3000, 2),
            "text-big": text(200 * 1024, 3),
            "text-huge": text(3 * 1024 * 1024, 4),
            "random": pattern(64 * 1024, 5),
            "zeros": bytes(128 * 1024),
        }
        for name, data in files.items():
            with open(os.path.join(stage, name), "wb") as f:
                f.write(data)
        proc = subprocess.run(["ditto", "--hfsCompression", stage, os.path.join(mnt, "compress")],
                              capture_output=True)
        log.ops.append({"op": "ditto --hfsCompression", "ok": proc.returncode == 0,
                        "returncode": proc.returncode,
                        "stderr": proc.stderr.decode("utf-8", "replace")})

    log.save(result)


def mutate(mnt, result):
    log = Log()
    root = os.fsencode(mnt)

    def append(path, data):
        with open(path, "ab") as f:
            f.write(data)

    log.run("append plain/small", append, os.path.join(root, b"plain/small"), b"after snapshot\n")
    def overwrite(path, data):
        with open(path, "r+b") as f:
            f.write(data)

    log.run("rewrite plain/block", overwrite, os.path.join(root, b"plain/block"), pattern(4096, 99))
    log.run("unlink plain/multi", os.unlink, os.path.join(root, b"plain/multi"))
    log.run("create plain/after-snapshot", write_file, os.path.join(root, b"plain/after-snapshot"),
            b"created after the snapshot\n")
    log.run("rename links/target", os.rename, os.path.join(root, b"links/target"),
            os.path.join(root, b"links/target-renamed"))
    log.save(result)


def snapshot(mnt, name, result):
    log = Log()
    if DARWIN:
        def create():
            fd = os.open(mnt, os.O_RDONLY)
            try:
                _check(libc.fs_snapshot_create(fd, name.encode(), 0))
            finally:
                os.close(fd)
        log.run("fs_snapshot_create " + name, create)
    log.save(result)


def describe(path, st):
    entry = {
        "path": path.decode("utf-8", "surrogateescape"),
        "path_hex": path.hex(),
        "ino": st.st_ino,
        "mode": "%07o" % st.st_mode,
        "nlink": st.st_nlink,
        "uid": st.st_uid,
        "gid": st.st_gid,
        "size": st.st_size,
        "blocks": st.st_blocks,
        "atime_ns": st.st_atime_ns,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
    }
    if hasattr(st, "st_birthtime_ns"):
        entry["birthtime_ns"] = st.st_birthtime_ns
    elif hasattr(st, "st_birthtime"):
        entry["birthtime"] = st.st_birthtime
    if hasattr(st, "st_flags"):
        entry["flags"] = "0x%x" % st.st_flags
    if stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
        entry["rdev"] = st.st_rdev
        entry["rdev_major"] = os.major(st.st_rdev)
        entry["rdev_minor"] = os.minor(st.st_rdev)
    return entry


def manifest(mnt, out):
    root = os.fsencode(mnt)
    entries = []
    errors = []

    def visit(rel):
        full = os.path.join(root, rel) if rel else root
        try:
            st = os.lstat(full)
        except OSError as e:
            errors.append({"path_hex": rel.hex(), "error": errno.errorcode.get(e.errno)})
            return
        entry = describe(rel or b".", st)
        try:
            names = list_xattrs(full)
            entry["xattrs"] = {}
            for name in names:
                try:
                    value = get_xattr(full, name)
                    x = {"len": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                    if len(value) <= 256:
                        x["hex"] = value.hex()
                    entry["xattrs"][name.decode("utf-8", "surrogateescape")] = x
                except OSError as e:
                    entry["xattrs"][name.decode("utf-8", "surrogateescape")] = {
                        "error": errno.errorcode.get(e.errno)}
        except OSError as e:
            entry["xattrs_error"] = errno.errorcode.get(e.errno)
        if stat.S_ISREG(st.st_mode):
            try:
                h = hashlib.sha256()
                with open(full, "rb") as f:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        h.update(chunk)
                entry["sha256"] = h.hexdigest()
            except OSError as e:
                entry["read_error"] = errno.errorcode.get(e.errno)
        elif stat.S_ISLNK(st.st_mode):
            entry["target_hex"] = os.readlink(full).hex()
        elif stat.S_ISDIR(st.st_mode):
            try:
                names = os.listdir(full)
            except OSError as e:
                entry["listdir_error"] = errno.errorcode.get(e.errno)
                names = []
            entry["listdir_order_hex"] = [n.hex() for n in names]
            entries.append(entry)
            for n in sorted(names):
                visit(os.path.join(rel, n) if rel else n)
            return
        entries.append(entry)

    visit(b"")
    with open(out, "w") as f:
        json.dump({"entries": entries, "errors": errors}, f, indent=1, ensure_ascii=True)


def main(argv):
    if len(argv) == 4 and argv[1] == "populate":
        populate(argv[2], argv[3])
    elif len(argv) == 4 and argv[1] == "mutate":
        mutate(argv[2], argv[3])
    elif len(argv) == 5 and argv[1] == "snapshot":
        snapshot(argv[2], argv[3], argv[4])
    elif len(argv) == 4 and argv[1] == "manifest":
        manifest(argv[2], argv[3])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
