#!/usr/bin/env python3
"""The corpus's tree, its changes, and macOS's reading of each volume.

    corpusutil.py tree MNT AFSCTOOL ALGORITHMS.json OUT.json
    corpusutil.py change MNT N BEFORE.json OUT.json
    corpusutil.py manifest MNT OUT.json
    corpusutil.py snapshots LISTSNAPSHOTS.plist OUT.json
    corpusutil.py row OUT.json NAME BLOCK-SIZE KIND VOLUMES SNAPSHOTS
    corpusutil.py check OUTDIR ROW
    corpusutil.py index OUTDIR

`tree` builds the corpus's tree at the root of a mounted volume: every kind
of file a volume holds, every way it holds attributes, names that test its
equivalence, a directory large enough for a tree of several levels, clones,
and a file of every compression type the reader decodes. Every content is
text, so an image compresses to little. Its result records each operation's
outcome and the SHA-256 of every regular file it wrote, which is the content
macOS must read back. `change` makes change N of the two a volume with
snapshots receives between and after them, and records the contents after it.

`manifest` records every entry under MNT as macOS's system calls report it:
the tree a reader of the image must reproduce. Attributes are listed the way
any program lists them, so a compressed file's two compression attributes
are not among them; its compression type and size are recorded beside, from
the header. A device node's attributes are null: macOS refuses to list them.
Creation times come from getattrlist, whole to the nanosecond.
`snapshots` lists a volume's snapshots in the order of their transactions.

`row` records what one row of the covering array asks for. `check` holds a
row to what the corpus promises: macOS read every regular file it wrote as
written, the two read-only passes reported the same tree, and the image did
not change from the moment it was written to the end of the second pass;
the images and manifests that only repeat the first pass's are removed.
`index` writes corpus.json, which names every image, its macOS version, its
volumes and their snapshots, each with the manifest it is held to.
"""

import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import platform
import plistlib
import shutil
import stat
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compressutil  # noqa: E402
import fsprobe  # noqa: E402
from fsprobe import text  # noqa: E402

DARWIN = sys.platform == "darwin"
XATTR_NOFOLLOW = 0x0001
UF_COMPRESSED = 0x20
# Files macOS writes on its own at the root of a volume it mounts.
HOUSEKEEPING = (b".fseventsd", b".Spotlight-V100")

if DARWIN:
    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    _libc.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
    _libc.listxattr.restype = ctypes.c_ssize_t
    _libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                               ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    _libc.getxattr.restype = ctypes.c_ssize_t
    _libc.getattrlist.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.c_ulong]
    _libc.getattrlist.restype = ctypes.c_int


def macos_major():
    version = platform.mac_ver()[0]
    return int(version.split(".")[0]) if version else 0


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    with open(path, "w") as f:
        json.dump(value, f, indent=1, sort_keys=True, ensure_ascii=True)
        f.write("\n")


def read_json(path):
    with open(path) as f:
        return json.load(f)


# -- the tree ---------------------------------------------------------------

class Tree:
    """Operations on a volume, each logged, with the content of every regular
    file kept by its path relative to the root."""

    def __init__(self, mnt, files=None):
        self.root = os.fsencode(mnt)
        self.log = fsprobe.Log()
        self.files = dict(files or {})
        self.times = {}

    def p(self, rel):
        return os.path.join(self.root, os.fsencode(rel) if isinstance(rel, str) else rel)

    def run(self, desc, fn, *args, **kwargs):
        return self.log.run(desc, fn, *args, **kwargs)

    def mkdir(self, rel):
        return self.run("mkdir " + rel, os.mkdir, self.p(rel))

    def put(self, rel, data, mode=0o644):
        ok = self.run("write " + _show(rel), fsprobe.write_file, self.p(rel), data, mode)
        if ok:
            self.files[_key(rel)] = sha(data)
        return ok

    def expect(self, rel, data):
        self.files[_key(rel)] = sha(data)

    def forget(self, rel):
        self.files.pop(_key(rel), None)

    def save(self, out):
        write_json(out, {"ops": self.log.ops, "files": self.files, "times": self.times})


def _key(rel):
    return (os.fsencode(rel) if isinstance(rel, str) else rel).hex()


def _show(rel):
    return rel if isinstance(rel, str) else rel.decode("utf-8", "backslashreplace")


def sparse(path, offset, data, size):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        if data:
            os.lseek(fd, offset, os.SEEK_SET)
            os.write(fd, data)
        os.ftruncate(fd, size)
    finally:
        os.close(fd)


def sparse_content(offset, data, size):
    out = bytearray(size)
    out[offset:offset + len(data)] = data
    return bytes(out)


def overwrite(path, offset, data):
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(data)


def append(path, data):
    with open(path, "ab") as f:
        f.write(data)


def make_socket(directory, name):
    import socket
    cwd = os.getcwd()
    os.chdir(directory)
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(name)
        s.close()
    finally:
        os.chdir(cwd)


def clone(src, dst):
    proc = subprocess.run(["cp", "-c", src, dst], capture_output=True)
    if proc.returncode:
        raise OSError(errno.EIO, proc.stderr.decode("utf-8", "replace").strip())


# Every content a compressed file holds: one that fits the attribute, one a
# byte past a chunk, one of several chunks, and one with a chunk of random
# bytes in the middle. afsctool leaves two of them uncompressed on every
# version seen, mixed-204800 under zlib and text-65537 under LZVN, so each
# compressor is given the contents it compresses.
COMPRESSED = ["text-1000", "text-65537", "text-204800", "mixed-204800"]
AFSCTOOL = [("zlib", "ZLIB", ["text-1000", "text-65537", "text-204800"]),
            ("lzvn", "LZVN", ["text-1000", "text-204800", "mixed-204800"]),
            ("lzfse", "LZFSE", COMPRESSED)]
STORED = 0xCC
PLAIN = [("empty", 0), ("small", 6), ("under-4k", 4095), ("block-4k", 4096), ("over-4k", 4097),
         ("multi", 3 * 4096 + 17), ("block-64k", 65536), ("large", (1 << 20) + 3)]
LARGE = dict(PLAIN)["large"]
# What the first change writes over the head of plain/large.
LARGE_HEAD = 4096


def plain(n):
    return text(n, 100 + n % 97)


def build(t, afsctool, algos_json):
    """The corpus's tree, under t's root."""
    for d in ("plain", "sparse", "links", "links/other", "special", "times", "xattr", "names",
              "bigdir", "flags", "owner", "perm", "deep", "clones", "compress"):
        t.mkdir(d)

    # Regular files about the block sizes the corpus uses, and one of many.
    for name, n in PLAIN:
        t.put("plain/" + name, plain(n))

    # Sparse files: a hole before data, a hole after it, and only a hole.
    for name, offset, data, size in (("middle", 1 << 20, text(10, 2), 2 << 20),
                                     ("tail", 0, text(4, 3), 8 << 20),
                                     ("hole", 0, b"", 8 << 20)):
        if t.run("sparse/" + name, sparse, t.p("sparse/" + name), offset, data, size):
            t.expect("sparse/" + name, sparse_content(offset, data, size))

    # Hard links in two directories, and symlinks of every kind of target.
    t.put("links/target", text(40, 4))
    for rel in ("links/hard1", "links/other/hard2"):
        if t.run(rel, os.link, t.p("links/target"), t.p(rel)):
            t.expect(rel, text(40, 4))
    for name, target in (("sym-rel", b"target"), ("sym-abs", b"/nonexistent/absolute/target"),
                         ("sym-long", b"d/" * 500 + b"end"), ("sym-dangling", b"missing"),
                         ("sym-dir", b"other")):
        t.run("links/" + name, os.symlink, target, t.p("links/" + name))

    # Special files, the device numbers at the edges of their fields.
    t.run("special/fifo", os.mkfifo, t.p("special/fifo"), 0o644)
    for name, kind, major, minor in (("cdev-1-2", stat.S_IFCHR, 1, 2),
                                     ("bdev-3-4", stat.S_IFBLK, 3, 4),
                                     ("cdev-255-max", stat.S_IFCHR, 255, 0xFFFFFF),
                                     ("cdev-0-0", stat.S_IFCHR, 0, 0)):
        t.run("special/" + name, os.mknod, t.p("special/" + name), kind | 0o644,
              os.makedev(major, minor))
    t.run("special/sock", make_socket, t.p("special"), "sock")

    # Times before 1970, at it, past 2038 and past 2106, the largest the
    # field holds, and every nanosecond digit. A time is a signed count of
    # nanoseconds, so the largest is in 2262; a later one set reads back as
    # that largest. macOS 14's stat reports a time before 1970 with a nonzero
    # fraction as though it were unsigned, so its manifest could not state the
    # stored value, and those two files are left out there.
    signed_fraction = macos_major() >= 15
    times = {
        "pre1900": -2208988800 * 10**9 - 5 * 10**9,
        "epoch": 0,
        "y2038": 2**31 * 10**9,
        "y2200": 7258118400 * 10**9 + 987654321,
        "max": 2**63 - 1,
        "nsec": 1700000000 * 10**9 + 123456789,
    }
    if signed_fraction:
        times["pre1970"] = -10 * 365 * 86400 * 10**9 + 123
        times["minus1ns"] = -1
    for name, ns in sorted(times.items()):
        if t.put("times/" + name, name.encode()):
            if t.run("utime times/" + name, os.utime, t.p("times/" + name), ns=(ns, ns)):
                t.times[_key("times/" + name)] = ns
    if t.run("times/symlink", os.symlink, b"epoch", t.p("times/symlink")):
        atime = times["pre1970"] if signed_fraction else times["pre1900"]
        t.run("utime times/symlink", os.utime, t.p("times/symlink"),
              ns=(atime, times["nsec"]), follow_symlinks=False)

    # Attributes: small, at and past the largest kept in the record, kept in
    # streams of their own, many on one file, on a directory and a symlink.
    for name, size in (("small", 5), ("inline-3803", 3803), ("inline-3804", 3804),
                       ("stream-3805", 3805), ("stream-64k", 64 * 1024), ("stream-200k", 200 * 1024)):
        if t.put("xattr/" + name, name.encode()):
            t.run("setxattr xattr/" + name, fsprobe.set_xattr, t.p("xattr/" + name),
                  b"corpus.value", text(size, 10 + size % 89))
    if t.put("xattr/many", b"many"):
        for i in range(50):
            t.run("setxattr xattr/many %d" % i, fsprobe.set_xattr, t.p("xattr/many"),
                  b"corpus.attr%02d" % i, b"value %d" % i)
    t.run("setxattr xattr", fsprobe.set_xattr, t.p("xattr"), b"corpus.dir", b"on a directory")
    if t.run("xattr/symlink", os.symlink, b"small", t.p("xattr/symlink")):
        t.run("setxattr xattr/symlink", fsprobe.set_xattr, t.p("xattr/symlink"),
              b"corpus.link", b"on a symlink")

    # Names, in order: on a volume that folds case or normalization, a later
    # name equal to an earlier one is refused, and what remains is the record.
    for name in fsprobe.NAMES:
        t.put("names/" + name, b"")

    # A directory of enough entries for a tree of several levels at 64 KiB.
    for i in range(2500):
        t.put(bigdir_name(i), b"")
    t.log.ops = [op for op in t.log.ops if not (op["op"].startswith("write bigdir/") and op["ok"])]

    # Flags, owners and permission bits.
    for name, flag in (("hidden", "UF_HIDDEN"), ("nodump", "UF_NODUMP"),
                       ("uchg", "UF_IMMUTABLE"), ("opaque", "UF_OPAQUE")):
        if t.put("flags/" + name, name.encode()) and hasattr(os, "chflags") and hasattr(stat, flag):
            t.run("chflags flags/" + name, os.chflags, t.p("flags/" + name), getattr(stat, flag))
    if t.put("owner/u1234", b"owned"):
        t.run("chown owner/u1234", os.chown, t.p("owner/u1234"), 1234, 5678)
    if t.put("owner/u0", b"root"):
        t.run("chown owner/u0", os.chown, t.p("owner/u0"), 0, 0)
    for name, mode in (("0000", 0o000), ("4755", 0o4755), ("2755", 0o2755), ("0777", 0o777)):
        if t.put("perm/" + name, name.encode()):
            t.run("chmod perm/" + name, os.chmod, t.p("perm/" + name), mode)
    if t.mkdir("perm/sticky-dir"):
        t.run("chmod perm/sticky-dir", os.chmod, t.p("perm/sticky-dir"), 0o1777)

    # Twenty directories deep.
    rel = "deep"
    for i in range(20):
        rel += "/d%02d" % i
        t.mkdir(rel)
    t.put(DEEP + "/leaf", b"leaf\n")

    # Clones: a whole clone, and a clone that has since diverged by a block.
    source = text(256 * 1024, 5)
    t.put("clones/source", source)
    if t.run("clones/copy", clone, t.p("clones/source"), t.p("clones/copy")):
        t.expect("clones/copy", source)
    if t.run("clones/changed", clone, t.p("clones/source"), t.p("clones/changed")):
        if t.run("overwrite clones/changed", overwrite, t.p("clones/changed"), 8192, text(4096, 6)):
            t.expect("clones/changed", source[:8192] + text(4096, 6) + source[12288:])

    # Compression: zlib, LZVN and LZFSE as afsctool writes them, and LZBITMAP
    # and content kept after the marker byte as this script stores them.
    contents = compressutil.contents()
    for label, name, kept in AFSCTOOL:
        d = "compress/afsctool-" + label
        t.mkdir(d)
        for c in kept:
            t.put(d + "/" + c, contents[c])
        if afsctool:
            proc = subprocess.run([afsctool, "-c", "-T", name, t.p(d)], capture_output=True)
            t.log.ops.append({"op": "afsctool -c -T %s %s" % (name, d), "ok": proc.returncode == 0,
                              "returncode": proc.returncode,
                              "stderr": proc.stderr.decode("utf-8", "replace")})
    t.mkdir("compress/own")
    try:
        algos = read_json(algos_json)
        codec = compressutil.Codec()
        lib = compressutil.libc()
    except (OSError, AttributeError, ValueError) as e:
        t.log.ops.append({"op": "compress/own", "ok": False, "error": "%s: %s" % (type(e).__name__, e)})
        return
    lzbitmap = algos.get("COMPRESSION_LZBITMAP")
    for name, kind, place, c in (("lzbitmap-inline", 13, "inline", "text-1000"),
                                 ("lzbitmap-fork", 14, "fork", "text-204800"),
                                 ("stored-inline", 9, "inline", "text-1000"),
                                 ("stored-fork", 10, "fork", "text-204800")):
        data = contents[c]
        chunks = [data[i:i + compressutil.CHUNK] for i in range(0, len(data), compressutil.CHUNK)]
        if kind in (9, 10):
            chunks = [bytes([STORED]) + x for x in chunks]
        elif lzbitmap is None:
            t.log.ops.append({"op": "compress/own/" + name, "ok": False,
                              "error": "the header declares no COMPRESSION_LZBITMAP"})
            continue
        else:
            chunks = [codec.encode(x, lzbitmap) for x in chunks]
        rel = "compress/own/" + name
        done = compressutil.store(lib, t.p(rel), place, kind, len(data), chunks)
        ok = "error" not in done and done.get("chflags") == "ok"
        t.log.ops.append({"op": rel, "ok": ok, "stored": done})
        if ok:
            t.expect(rel, data)


def tree(mnt, afsctool, algos_json, out):
    t = Tree(mnt)
    build(t, afsctool, algos_json)
    t.save(out)


AFTER_FIRST = b"after the first snapshot\n"
TRUNCATED = 512 * 1024
DEEP = "deep/" + "/".join("d%02d" % i for i in range(20))


def bigdir_name(i):
    return "bigdir/entry-%05d-%s" % (i, hashlib.sha256(b"%d" % i).hexdigest()[:12])


def change(mnt, n, before, out):
    """Change N of the two a snapshotted volume receives: the first between
    its snapshots, the second after the last."""
    t = Tree(mnt, read_json(before)["files"])
    large = plain(LARGE)
    changed_large = text(LARGE_HEAD, 91) + large[LARGE_HEAD:]
    if n == 1:
        if t.run("append plain/small", append, t.p("plain/small"), AFTER_FIRST):
            t.expect("plain/small", plain(6) + AFTER_FIRST)
        if t.run("overwrite plain/large", overwrite, t.p("plain/large"), 0, changed_large[:LARGE_HEAD]):
            t.expect("plain/large", changed_large)
        if t.run("unlink plain/multi", os.unlink, t.p("plain/multi")):
            t.forget("plain/multi")
        t.put("plain/after-1", text(5000, 92))
        if t.run("rename links/target", os.rename, t.p("links/target"), t.p("links/target-renamed")):
            t.files[_key("links/target-renamed")] = t.files.pop(_key("links/target"))
        t.run("setxattr xattr/small", fsprobe.set_xattr, t.p("xattr/small"), b"corpus.added",
              AFTER_FIRST)
        for i in range(10):
            rel = bigdir_name(i)
            if t.run("unlink " + rel, os.unlink, t.p(rel)):
                t.forget(rel)
    elif n == 2:
        t.put("plain/after-2", text(3000, 93))
        head = changed_large if t.files.get(_key("plain/large")) == sha(changed_large) else large
        if t.run("truncate plain/large", os.truncate, t.p("plain/large"), TRUNCATED):
            t.expect("plain/large", head[:TRUNCATED])
        if t.run("unlink clones/copy", os.unlink, t.p("clones/copy")):
            t.forget("clones/copy")
        t.run("chmod perm/0777", os.chmod, t.p("perm/0777"), 0o755)
        leaf = DEEP + "/leaf"
        if t.run("unlink " + leaf, os.unlink, t.p(leaf)):
            t.forget(leaf)
    else:
        raise SystemExit("no change %d" % n)
    t.save(out)


# -- macOS's reading --------------------------------------------------------

def list_xattrs(path):
    """Attribute names as any program lists them."""
    if not DARWIN:
        return sorted(os.fsencode(n) for n in os.listxattr(path, follow_symlinks=False))
    size = _libc.listxattr(path, None, 0, XATTR_NOFOLLOW)
    if size < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    if size == 0:
        return []
    buf = ctypes.create_string_buffer(size)
    size = _libc.listxattr(path, buf, size, XATTR_NOFOLLOW)
    if size < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return sorted(n for n in buf.raw[:size].split(b"\0") if n)


def get_xattr(path, name):
    if not DARWIN:
        return os.getxattr(path, name, follow_symlinks=False)
    size = _libc.getxattr(path, name, None, 0, 0, XATTR_NOFOLLOW)
    if size < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    buf = ctypes.create_string_buffer(max(size, 1))
    size = _libc.getxattr(path, name, buf, size, 0, XATTR_NOFOLLOW)
    if size < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return buf.raw[:size]


ATTR_BIT_MAP_COUNT = 5
ATTR_CMN_CRTIME = 0x00000200
FSOPT_NOFOLLOW = 0x00000001


class AttrList(ctypes.Structure):
    _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_uint16),
                ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32),
                ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
                ("forkattr", ctypes.c_uint32)]


def birthtime_ns(path):
    """The creation time getattrlist reports: a length, then a timespec of two
    64-bit fields packed at 4-byte alignment. Off macOS, where this script is
    only tried, the change time stands in."""
    if not DARWIN:
        return os.lstat(path).st_ctime_ns
    al = AttrList(ATTR_BIT_MAP_COUNT, 0, ATTR_CMN_CRTIME, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(64)
    if _libc.getattrlist(path, ctypes.byref(al), buf, len(buf), FSOPT_NOFOLLOW) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    sec = int.from_bytes(buf.raw[4:12], "little", signed=True)
    nsec = int.from_bytes(buf.raw[12:20], "little", signed=True)
    return sec * 10**9 + nsec


def describe(full, rel, st):
    entry = {
        "path_hex": rel.hex(),
        "ino": st.st_ino,
        "mode": st.st_mode,
        "nlink": st.st_nlink,
        "uid": st.st_uid,
        "gid": st.st_gid,
        "size": st.st_size,
        "blocks": st.st_blocks,
        "flags": getattr(st, "st_flags", 0),
        "atime_ns": st.st_atime_ns,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
        "birthtime_ns": birthtime_ns(full),
    }
    # stat's own creation time is a float to the microsecond at best; the two
    # agreeing to the second is what says getattrlist's buffer was read right.
    if DARWIN and abs(entry["birthtime_ns"] - int(st.st_birthtime * 10**9)) >= 10**9:
        raise OSError(errno.EBADMSG, "getattrlist's creation time disagrees with stat's")
    if stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
        # The field's bits: Darwin's dev_t is signed, and prints all ones as -1.
        entry["rdev"] = st.st_rdev & 0xFFFFFFFF
    return entry


def manifest(mnt, out):
    root = os.fsencode(mnt)
    entries, errors = [], []

    def fail(rel, what, e):
        errors.append({"path_hex": rel.hex(), "what": what,
                       "error": errno.errorcode.get(e.errno, str(e.errno))})

    def visit(rel):
        full = os.path.join(root, rel) if rel else root
        try:
            st = os.lstat(full)
            entry = describe(full, rel, st)
        except OSError as e:
            fail(rel, "stat", e)
            return
        entries.append(entry)
        try:
            entry["xattrs"] = [{"name_hex": n.hex(), "len": len(v), "sha256": sha(v)}
                               for n, v in ((n, get_xattr(full, n)) for n in list_xattrs(full))]
        except OSError as e:
            # macOS refuses to list a device node's attributes, and the tree
            # sets none on one; its entry says the list is unknown.
            if e.errno == errno.EPERM and (stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode)):
                entry["xattrs"] = None
            else:
                fail(rel, "xattrs", e)
        if stat.S_ISREG(st.st_mode):
            if entry["flags"] & UF_COMPRESSED:
                entry["compression"] = compressutil.compression_of(full)
            try:
                h, n = hashlib.sha256(), 0
                with open(full, "rb") as f:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        h.update(chunk)
                        n += len(chunk)
                entry["sha256"] = h.hexdigest()
                entry["read_len"] = n
            except OSError as e:
                fail(rel, "read", e)
        elif stat.S_ISLNK(st.st_mode):
            entry["target_hex"] = os.readlink(full).hex()
        elif stat.S_ISDIR(st.st_mode):
            try:
                names = os.listdir(full)
            except OSError as e:
                fail(rel, "listdir", e)
                names = []
            entry["children_hex"] = [n.hex() for n in names]
            for n in sorted(names):
                visit(os.path.join(rel, n) if rel else n)

    visit(b"")
    write_json(out, {"entries": entries, "errors": errors})


def snapshots(listing, out):
    """Each snapshot's name, transaction and UUID, oldest first; the names are
    printed one a line for the probe to mount."""
    with open(listing, "rb") as f:
        data = f.read()
    start = data.find(b"<?xml")
    found = plistlib.loads(data[start:]).get("Snapshots", []) if start >= 0 else []
    found = [{"name": s.get("SnapshotName"), "xid": s.get("SnapshotXID"), "uuid": s.get("SnapshotUUID")}
             for s in found]
    found.sort(key=lambda s: (s["xid"] is None, s["xid"] or 0))
    write_json(out, found)
    for s in found:
        print(s["name"])


# -- the row's verdict and the index ----------------------------------------

def row(out, name, bs, kind, volumes, snaps):
    write_json(out, {"name": name, "block_size": int(bs), "kind": kind, "volumes": volumes,
                     "snapshots": snaps})


def steps(outdir):
    """Each step's exit status, by name."""
    found = {}
    with open(os.path.join(outdir, "steps.tsv")) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[0] != "#":
                found[parts[0]] = int(parts[1])
    return found


def recorded(voldir, k):
    """What pass K recorded of one volume: a manifest of the live tree and of
    each snapshot, and the snapshot list, by file name."""
    d = os.path.join(voldir, "pass%d" % k)
    if not os.path.isdir(d):
        return {}
    return {f: os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".json")}


def is_view(f):
    return f == "live.json" or f.startswith("snap-")


# The compression types each directory of compressed files is written to
# hold, inline and in the resource fork.
INTENDED = {
    "compress/afsctool-zlib/": {3, 4},
    "compress/afsctool-lzvn/": {7, 8},
    "compress/afsctool-lzfse/": {11, 12},
    "compress/own/lzbitmap-inline": {13},
    "compress/own/lzbitmap-fork": {14},
    "compress/own/stored-inline": {9},
    "compress/own/stored-fork": {10},
}


def compression_problems(manifest_path):
    """Each file under compress/ that macOS does not report compressed with a
    type it was written to have."""
    problems = []
    seen = set()
    for e in read_json(manifest_path)["entries"]:
        path = bytes.fromhex(e["path_hex"]).decode("utf-8", "replace")
        for prefix, kinds in INTENDED.items():
            if path.startswith(prefix):
                seen.add(prefix)
                got = (e.get("compression") or {}).get("type")
                if got not in kinds:
                    problems.append({"path": path, "want": sorted(kinds), "got": got})
    for prefix in sorted(set(INTENDED) - seen):
        problems.append({"path": prefix, "want": sorted(INTENDED[prefix]), "got": "absent"})
    return problems


def expected(voldir):
    """What the tree and each change left, oldest first."""
    found = []
    while os.path.exists(os.path.join(voldir, "expect-%d.json" % len(found))):
        found.append(read_json(os.path.join(voldir, "expect-%d.json" % len(found))))
    return found


# What the corpus's tree sets that macOS honours only when asked: ownership,
# which an external volume ignores until `diskutil enableOwnership`.
OWNED = {_key("owner/u1234"): (1234, 5678)}


def setting_problems(manifest_path, times):
    """Each owner and time the tree set that the manifest does not report."""
    by_path = {e["path_hex"]: e for e in read_json(manifest_path)["entries"]}
    problems = []
    for path_hex, (uid, gid) in OWNED.items():
        e = by_path.get(path_hex)
        if e is None or (e["uid"], e["gid"]) != (uid, gid):
            problems.append({"path_hex": path_hex, "want": [uid, gid],
                             "got": e and [e["uid"], e["gid"]]})
    for path_hex, ns in sorted(times.items()):
        e = by_path.get(path_hex)
        if e is None or (e["atime_ns"], e["mtime_ns"]) != (ns, ns):
            problems.append({"path_hex": path_hex, "want": [ns, ns],
                             "got": e and [e["atime_ns"], e["mtime_ns"]]})
    return problems


def content_problems(manifest_path, files):
    by_path = {e["path_hex"]: e for e in read_json(manifest_path)["entries"]}
    problems = []
    for path_hex, want in sorted(files.items()):
        e = by_path.get(path_hex)
        if e is None:
            problems.append({"path_hex": path_hex, "problem": "missing"})
        elif e.get("sha256") != want:
            problems.append({"path_hex": path_hex, "problem": "content",
                             "want": want, "got": e.get("sha256")})
    for path_hex, e in sorted(by_path.items()):
        path = bytes.fromhex(path_hex)
        if (stat.S_ISREG(e["mode"]) and path_hex not in files
                and not path.startswith(HOUSEKEEPING)):
            problems.append({"path_hex": path_hex, "problem": "unexpected"})
    return problems


def check(outdir, name):
    rowdir = os.path.join(outdir, "manifests", name)
    record = read_json(os.path.join(rowdir, "row.json"))
    status = steps(outdir)
    verdict = {"row": name, "problems": []}

    def problem(what, **detail):
        verdict["problems"].append(dict(what=what, **detail))

    # The image, as written and after each read-only pass.
    images = os.path.join(outdir, "images")
    digests = {}
    for label, stem in (("written", name + "-written"), ("pass1", name), ("pass2", name + "-again")):
        meta = os.path.join(images, stem + ".bd.json")
        digests[label] = read_json(meta)["content_sha256"] if os.path.exists(meta) else None
    verdict["image_sha256"] = digests
    if None in digests.values():
        problem("an image was not dumped")
    elif len(set(digests.values())) != 1:
        problem("the image changed after it was written", digests=digests)

    for step_name in (name + "-fsck-written", name + "-fsck-read"):
        verdict[step_name] = status.get(step_name)
        if status.get(step_name) != 0:
            problem("fsck_apfs -n did not pass", step=step_name, exit=status.get(step_name))

    order_path = os.path.join(rowdir, "volumes.txt")
    vols = open(order_path).read().split() if os.path.exists(order_path) else []
    want_vols = 2 if record["volumes"] == "two" else 1
    if len(vols) != want_vols:
        problem("volumes", want=want_vols, got=vols)
    verdict["volumes"] = {}
    for slot, vol in enumerate(vols):
        voldir = os.path.join(rowdir, vol)
        one, two = recorded(voldir, 1), recorded(voldir, 2)
        files = expected(voldir)
        snaps = sorted(f for f in one if f.startswith("snap-"))
        # The row's snapshots are taken of its first volume, after two changes.
        want_changes = 2 if record["snapshots"] == "snap" and slot == 0 else 0
        v = verdict["volumes"][vol] = {"views": sorted(f for f in one if is_view(f)),
                                       "changes": len(files) - 1}
        if set(one) != set(two):
            problem("the passes recorded different things", volume=vol, pass1=sorted(one),
                    pass2=sorted(two))
        for f in sorted(set(one) & set(two)):
            with open(one[f], "rb") as a, open(two[f], "rb") as b:
                if a.read() != b.read():
                    problem("the passes read it differently", volume=vol, file=f)
        if "live.json" not in one:
            problem("no live view", volume=vol)
            continue
        if len(files) - 1 != want_changes:
            problem("changes", volume=vol, want=want_changes, got=len(files) - 1)
        if not files:
            continue
        if len(snaps) != want_changes:
            problem("snapshots", volume=vol, want=want_changes, got=snaps)
        pairs = [("live.json", files[-1])] + [
            ("snap-%d.json" % (i + 1), files[i]) for i in range(min(len(snaps), len(files) - 1))]
        for view, want in pairs:
            if view not in one:
                continue
            for p in setting_problems(one[view], files[0]["times"]):
                problem("macOS reports a setting other than as set", volume=vol, view=view, **p)
            found = content_problems(one[view], want["files"])
            v[view] = len(found)
            for p in found:
                problem("macOS read a file other than as written", volume=vol, view=view, **p)
            errors = read_json(one[view])["errors"]
            if errors:
                problem("the manifest met errors", volume=vol, view=view, errors=errors)
        for p in compression_problems(one["live.json"]):
            problem("a file is not compressed as intended", volume=vol, **p)

    verdict["ok"] = not verdict["problems"]
    if verdict["ok"]:
        # Everything after the first pass repeated it.
        for stem in (name + "-written", name + "-again"):
            path = os.path.join(images, stem + ".bd")
            if os.path.exists(path):
                os.unlink(path)
        for vol in vols:
            shutil.rmtree(os.path.join(rowdir, vol, "pass2"), ignore_errors=True)
    write_json(os.path.join(rowdir, "check.json"), verdict)
    print(json.dumps({"row": name, "ok": verdict["ok"], "problems": len(verdict["problems"])}))
    return 0 if verdict["ok"] else 1


def index(outdir):
    """corpus.json: every image, its macOS version, its volumes in container
    order and each one's snapshots, each with the manifest it is held to."""
    def log(name):
        path = os.path.join(outdir, "log", name + ".out")
        return open(path).read().strip() if os.path.exists(path) else None

    env = {}
    for line in (log("sw_vers") or "").splitlines():
        key, _, value = line.partition(":")
        env[key.strip()] = value.strip()
    mandir = os.path.join(outdir, "manifests")
    rows = []
    for name in sorted(os.listdir(mandir)) if os.path.isdir(mandir) else []:
        rowdir = os.path.join(mandir, name)
        record = read_json(os.path.join(rowdir, "row.json"))
        verdict_path = os.path.join(rowdir, "check.json")
        verdict = read_json(verdict_path) if os.path.exists(verdict_path) else {"ok": False}
        order_path = os.path.join(rowdir, "volumes.txt")
        order = open(order_path).read().split() if os.path.exists(order_path) else []
        vols = []
        for slot, vol in enumerate(order):
            voldir = os.path.join(rowdir, vol)
            snaps_path = os.path.join(voldir, "pass1", "snapshots.json")
            snaps = read_json(snaps_path) if os.path.exists(snaps_path) else []
            rel = lambda f: os.path.relpath(os.path.join(voldir, "pass1", f), outdir)  # noqa: E731
            vols.append({
                "slot": slot,
                "name": vol,
                "case_sensitive": vol == "CaseKeep",
                "live": rel("live.json"),
                "snapshots": [dict(s, manifest=rel("snap-%d.json" % (i + 1))) for i, s in enumerate(snaps)],
            })
        meta_path = os.path.join(outdir, "images", name + ".bd.json")
        meta = read_json(meta_path) if os.path.exists(meta_path) else {}
        rows.append(dict(record, ok=verdict.get("ok", False), image="images/%s.bd" % name,
                         image_size=meta.get("image_size"), content_sha256=meta.get("content_sha256"),
                         volume_list=vols))
    write_json(os.path.join(outdir, "corpus.json"), {
        "macos": {"product_version": env.get("ProductVersion"), "build": env.get("BuildVersion")},
        "runner": log("runner-env"),
        "rows": rows,
    })


def main(argv):
    if len(argv) == 6 and argv[1] == "tree":
        tree(argv[2], argv[3], argv[4], argv[5])
    elif len(argv) == 6 and argv[1] == "change":
        change(argv[2], int(argv[3]), argv[4], argv[5])
    elif len(argv) == 4 and argv[1] == "manifest":
        manifest(argv[2], argv[3])
    elif len(argv) == 4 and argv[1] == "snapshots":
        snapshots(argv[2], argv[3])
    elif len(argv) == 8 and argv[1] == "row":
        row(*argv[2:])
    elif len(argv) == 4 and argv[1] == "check":
        sys.exit(check(argv[2], argv[3]))
    elif len(argv) == 3 and argv[1] == "index":
        index(argv[2])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
