#!/usr/bin/env python3
"""Files of known content stored compressed, and what the kernel reads back.

    compressutil.py algorithms SDK_HEADER OUT.json
    compressutil.py stage DIR OUT.json
    compressutil.py own ALGORITHMS.json DIR OUT.json
    compressutil.py verify MNT OUT.json
    compressutil.py streams ALGORITHMS.json DIR OUT.json

`algorithms` reads the algorithm names and numbers libcompression's public
header declares, so nothing here spells one from memory. `stage` writes the
contents every part of the probe compresses, one file per content. `own`
stores each content in DIR compressed by libcompression with LZVN, LZFSE and
LZBITMAP: the compressed form in the file's com.apple.decmpfs attribute
after its 16-byte header, where it fits, and in com.apple.ResourceFork as
the observed type 8 keeps it otherwise, a table of one 32-bit offset per
64 KiB chunk and one for the end, then the chunks. The compression type a
file records is a number this script is given as an argument of the layout,
never one it infers. `verify` reads every file under MNT through the kernel
and compares it with the content its name says it holds. `streams` keeps
each algorithm's encoding of a set of small inputs, by
compression_encode_buffer, with the inputs beside them.
"""

import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import re
import stat
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fsprobe import XATTR_NOFOLLOW, XATTR_SHOWCOMPRESSION, get_xattr, list_xattrs, pattern, text  # noqa: E402

CHUNK = 64 * 1024
DECMPFS = b"com.apple.decmpfs"
FORK = b"com.apple.ResourceFork"
# The largest attribute this probe embeds the compressed form in. A stored
# attribute larger than 3,804 bytes is kept in a stream of its own on the
# volumes the earlier probes made, so this stays below it.
INLINE_MAX = 3800


def contents():
    """Every content the probe stores, by name."""
    return {
        "empty": b"",
        "text-100": text(100, 11),
        "text-1000": text(1000, 12),
        "text-3000": text(3000, 13),
        "text-8192": text(8192, 14),
        "text-65536": text(CHUNK, 15),
        "text-65537": text(CHUNK + 1, 16),
        "text-204800": text(200 * 1024, 17),
        "text-1048699": text(1024 * 1024 + 123, 18),
        "random-65536": pattern(CHUNK, 19),
        "mixed-204800": text(CHUNK, 20) + pattern(CHUNK, 21) + text(72 * 1024, 22),
        "zeros-131072": bytes(128 * 1024),
    }


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    with open(path, "w") as f:
        json.dump(value, f, indent=1, ensure_ascii=True)


def algorithms(header, out):
    """The COMPRESSION_* names and numbers the header declares."""
    found = {}
    with open(header, encoding="utf-8", errors="replace") as f:
        for m in re.finditer(r"\b(COMPRESSION_[A-Z0-9_]+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)", f.read()):
            found[m.group(1)] = int(m.group(2), 0)
    write_json(out, found)
    if "COMPRESSION_LZBITMAP" not in found:
        raise SystemExit("the header declares no COMPRESSION_LZBITMAP")


def stage(directory, out):
    os.makedirs(directory, exist_ok=True)
    record = {}
    for name, data in contents().items():
        with open(os.path.join(directory, name), "wb") as f:
            f.write(data)
        record[name] = {"size": len(data), "sha256": sha(data)}
    write_json(out, record)


class Codec:
    """libcompression's buffer interface."""

    def __init__(self):
        self.lib = ctypes.CDLL(ctypes.util.find_library("compression"), use_errno=True)
        for fn in ("compression_encode_buffer", "compression_decode_buffer"):
            f = getattr(self.lib, fn)
            f.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
                          ctypes.c_void_p, ctypes.c_int]
            f.restype = ctypes.c_size_t

    def encode(self, data, algorithm):
        """`data` encoded, or None where the encoder writes nothing."""
        cap = 2 * len(data) + 4096
        dst = ctypes.create_string_buffer(cap)
        src = ctypes.create_string_buffer(data, len(data)) if data else None
        n = self.lib.compression_encode_buffer(dst, cap, src, len(data), None, algorithm)
        return dst.raw[:n] if n else None

    def decode(self, data, size, algorithm):
        dst = ctypes.create_string_buffer(max(size, 1))
        src = ctypes.create_string_buffer(data, len(data))
        n = self.lib.compression_decode_buffer(dst, size, src, len(data), None, algorithm)
        return dst.raw[:n]


def libc():
    lib = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    lib.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                             ctypes.c_uint32, ctypes.c_int]
    lib.setxattr.restype = ctypes.c_int
    return lib


def set_xattr(lib, path, name, value):
    """Set an attribute, with the option that shows compression attributes and,
    where that is refused, without it; say which was taken."""
    for options, label in ((XATTR_NOFOLLOW | XATTR_SHOWCOMPRESSION, "showcompression"),
                           (XATTR_NOFOLLOW, "plain")):
        if lib.setxattr(path, name, value, len(value), 0, options) == 0:
            return label
        e = ctypes.get_errno()
    raise OSError(e, os.strerror(e))


# Each layout this probe writes: a name, the algorithm, where the compressed
# form goes, and the type number the header records.
LAYOUTS = [
    ("lzvn-inline", "COMPRESSION_LZVN", "inline", 7),
    ("lzvn-fork", "COMPRESSION_LZVN", "fork", 8),
    ("lzfse-inline", "COMPRESSION_LZFSE", "inline", 11),
    ("lzfse-fork", "COMPRESSION_LZFSE", "fork", 12),
    ("lzbitmap-inline", "COMPRESSION_LZBITMAP", "inline", 13),
    ("lzbitmap-fork", "COMPRESSION_LZBITMAP", "fork", 14),
]


def header(kind, size):
    return b"fpmc" + struct.pack("<IQ", kind, size)


def fork(chunks):
    """The type 8 layout: one offset per chunk and one for the end, the first
    being the table's own length, then the chunks."""
    table = 4 * (len(chunks) + 1)
    offsets = [table]
    for c in chunks:
        offsets.append(offsets[-1] + len(c))
    return struct.pack("<%dI" % len(offsets), *offsets) + b"".join(chunks)


def own(algos_json, directory, out):
    with open(algos_json) as f:
        algos = json.load(f)
    codec = Codec()
    lib = libc()
    record = []
    for layout, algo_name, place, kind in LAYOUTS:
        algo = algos.get(algo_name)
        sub = os.path.join(directory, layout)
        os.makedirs(sub, exist_ok=True)
        for name, data in contents().items():
            entry = {"layout": layout, "name": name, "type": kind, "algorithm": algo}
            record.append(entry)
            if algo is None:
                entry["skipped"] = "the header declares no " + algo_name
                continue
            chunks = [codec.encode(data[i:i + CHUNK], algo) for i in range(0, len(data), CHUNK)]
            if any(c is None for c in chunks):
                entry["skipped"] = "the encoder wrote nothing for a chunk"
                continue
            entry["chunk_lengths"] = [len(c) for c in chunks]
            if place == "inline":
                if len(chunks) != 1:
                    entry["skipped"] = "%d chunks, where inline holds one" % len(chunks)
                    continue
                value = header(kind, len(data)) + chunks[0]
                if len(value) > INLINE_MAX:
                    entry["skipped"] = "longer than %d bytes inline" % INLINE_MAX
                    continue
                attrs = [(DECMPFS, value)]
            else:
                attrs = [(FORK, fork(chunks)), (DECMPFS, header(kind, len(data)))]
            path = os.path.join(sub, name).encode()
            try:
                os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
                entry["set"] = {}
                for attr, value in attrs:
                    entry["set"][attr.decode()] = {"len": len(value), "sha256": sha(value),
                                                   "how": set_xattr(lib, path, attr, value)}
                os.chflags(path, stat.UF_COMPRESSED)
                entry["ok"] = True
            except OSError as e:
                entry["ok"] = False
                entry["error"] = errno.errorcode.get(e.errno, str(e.errno))
    write_json(out, record)


def verify(mnt, out):
    """Every regular file under MNT read through the kernel, against the
    content its name names."""
    want = {name: sha(data) for name, data in contents().items()}
    record = []
    for top, _, files in os.walk(mnt):
        for name in sorted(files):
            if name not in want:
                continue
            path = os.path.join(top, name)
            entry = {"path": os.path.relpath(path, mnt)}
            try:
                st = os.lstat(path)
                entry["flags"] = "0x%x" % getattr(st, "st_flags", 0)
                entry["size"] = st.st_size
                entry["blocks"] = st.st_blocks
                names = list_xattrs(path.encode())
                if DECMPFS in names:
                    value = get_xattr(path.encode(), DECMPFS)
                    entry["decmpfs_len"] = len(value)
                    if len(value) >= 16 and value[:4] == b"fpmc":
                        entry["type"], entry["header_size"] = struct.unpack("<IQ", value[4:16])
                if FORK in names:
                    entry["fork_len"] = len(get_xattr(path.encode(), FORK))
                with open(path, "rb") as f:
                    got = f.read()
                entry["read"] = "match" if sha(got) == want[name] else "mismatch"
                if entry["read"] == "mismatch":
                    entry["read_len"] = len(got)
                    entry["read_sha256"] = sha(got)
            except OSError as e:
                entry["read"] = "error"
                entry["error"] = errno.errorcode.get(e.errno, str(e.errno))
            record.append(entry)
    record.sort(key=lambda e: e["path"])
    write_json(out, record)


def stream_inputs():
    """Small inputs, each showing the encoder one thing."""
    inputs = {
        "empty": b"",
        "one": b"a",
        "abc": b"abc",
        "run-16": b"a" * 16,
        "run-100": b"a" * 100,
        "run-1000": b"a" * 1000,
        "run-65536": b"a" * CHUNK,
        "alphabet": bytes(range(97, 123)),
        "bytes-256": bytes(range(256)),
        "text-200": text(200, 31),
        "text-4096": text(4096, 32),
        "text-65536": text(CHUNK, 33),
        "random-256": pattern(256, 34),
        "random-4096": pattern(4096, 35),
        "random-65536": pattern(CHUNK, 36),
        "zeros-4096": bytes(4096),
        "repeat-7-1000": (b"abcdefg" * 143)[:1000],
        "two-runs": b"x" * 300 + b"y" * 300,
    }
    for n in (31, 32, 33, 63, 64, 65, 127, 128, 129, 255, 256, 257, 511, 512, 513):
        inputs["text-%d" % n] = text(n, 40 + n)
    return inputs


def streams(algos_json, directory, out):
    with open(algos_json) as f:
        algos = json.load(f)
    codec = Codec()
    os.makedirs(os.path.join(directory, "in"), exist_ok=True)
    record = {}
    inputs = stream_inputs()
    for name, data in inputs.items():
        with open(os.path.join(directory, "in", name), "wb") as f:
            f.write(data)
    for algo_name, algo in sorted(algos.items()):
        sub = os.path.join(directory, algo_name.lower())
        os.makedirs(sub, exist_ok=True)
        for name, data in inputs.items():
            entry = {"algorithm": algo, "input": name, "input_len": len(data)}
            record["%s/%s" % (algo_name, name)] = entry
            try:
                encoded = codec.encode(data, algo)
            except OSError as e:
                entry["error"] = errno.errorcode.get(e.errno, str(e.errno))
                continue
            if encoded is None:
                entry["encoded"] = None
                continue
            with open(os.path.join(sub, name), "wb") as f:
                f.write(encoded)
            entry["encoded_len"] = len(encoded)
            entry["round_trip"] = codec.decode(encoded, len(data), algo) == data
    write_json(out, record)


def main(argv):
    commands = {"algorithms": (algorithms, 2), "stage": (stage, 2), "own": (own, 3),
                "verify": (verify, 2), "streams": (streams, 3)}
    if len(argv) < 2 or argv[1] not in commands or len(argv) != commands[argv[1]][1] + 2:
        raise SystemExit(__doc__)
    commands[argv[1]][0](*argv[2:])


if __name__ == "__main__":
    main(sys.argv)
