#!/usr/bin/env python3
"""Files of known content stored compressed, and what the kernel reads back.

    compressutil.py algorithms SDK_HEADER OUT.json
    compressutil.py stage DIR OUT.json
    compressutil.py own ALGORITHMS.json DIR OUT.json
    compressutil.py verify MNT OUT.json
    compressutil.py streams ALGORITHMS.json DIR OUT.json
    compressutil.py study ALGORITHMS.json DIR OUT.json
    compressutil.py oracle ALGORITHMS.json OUT.jsonl
    compressutil.py rules ALGORITHMS.json OUT.jsonl
    compressutil.py corpus ALGORITHMS.json DIR OUT.json
    compressutil.py flips ALGORITHMS.json OUT.jsonl
    compressutil.py types ALGORITHMS.json DIR OUT.json
    compressutil.py typeread MNT OUT.jsonl
    compressutil.py survey ROOT OUT.json SECONDS
    compressutil.py typesof DIR OUT.json
    compressutil.py writers DIR OUT.json
    compressutil.py inline DIR OUT.json
    compressutil.py inlineread MNT RECORD.json OUT.jsonl

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
compression_encode_buffer, with the inputs beside them. `study` does the same
for LZBITMAP alone over a larger set, each family of inputs varying one thing
a step at a time: where the encoder stops storing raw, the length of a run of
one byte, the distance of one match, the length of one match at a fixed
distance, and the literals between two copies of a run. `oracle` encodes a
few of those inputs, changes every bit of each stream after its first block
header in turn, and keeps what libcompression decodes each changed stream to.
`rules` hands the decoder the streams zbmcases.py builds by hand, each into
three buffers: zeroed, filled with 0xA5, and filled with 0xC3 behind 64 bytes
of 0x5A that are not part of it, so that an output taken from the buffer
rather than the stream shows. `corpus` keeps LZBITMAP's encoding of larger
inputs than `study`'s, of many blocks and of every kind of step. `flips` does
what `oracle` does for more streams, among them streams of two blocks and
streams whose steps read masks and two-byte distances, and also decodes every
prefix of each; it keeps a digest of each output rather than the output.
`types` stores, under every compression type number from 0 to 63, inline and
in the resource fork, LZRAVEN's, LZMESH's and LZ4's encoding of known content in the
layout type 8 has, and a payload no algorithm wrote; beside them, LZFSE and
LZBITMAP at the type numbers the kernel reads them under. `typeread` reads
each of those files back through the kernel, each read in a process of its own
under a time limit, so that a read that never returns costs one file.
`survey` walks ROOT, without following symlinks or entering another volume
through /System/Volumes, and tallies the compression type of every compressed
regular file, until SECONDS run out. `typesof` lists every file under DIR
with its compression type. `writers` stages the contents a compressing copy
is given: `stage`'s, a 20 MiB text, and copies of three of Apple's
executables. `inline` stores content after the header under types 3, 7, 11,
13 and 9, and zlib's raw chunk under type 3, at attribute lengths a byte apart
across 3,802, each with content that does not compress and, for the encoded
types, content of more than twice the encoding; `inlineread` reads each back
against the content it was made from.
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


def study_inputs():
    """Inputs that each show the LZBITMAP encoder one thing, a step at a time."""
    inputs = {}
    # Where the encoder stops storing a block raw.
    for n in range(120, 301):
        inputs["text-%d" % n] = text(n, 100)
    # One byte repeated: a literal or few, then one match at distance one, of every length.
    for n in list(range(1, 401)) + [511, 512, 513, 1000, 4095, 4096, 4097, 16384, 65535, 65536]:
        inputs["run-%d" % n] = b"a" * n
    # K random bytes, then the same K again: one match of length K at distance K.
    for k in list(range(2, 300)) + [511, 512, 1000, 2047, 2048, 4095, 4096, 8192, 16383,
                                   16384, 32767, 32768]:
        p = pattern(k, 200)
        inputs["repeat-%d" % k] = p + p
    # 64 random bytes, then L more bytes copied from 64 back: one match of length L at
    # distance 64, overlapping itself once L passes 64.
    p = pattern(64, 300)
    for length in range(1, 257):
        inputs["dist64-%d" % length] = (p * 5)[:64 + length]
    # 32 random bytes, G random bytes, then the first 32 again: G literals between the two.
    a = pattern(32, 301)
    for gap in range(0, 200):
        inputs["gap-%d" % gap] = a + pattern(gap, 1000 + gap) + a
    # A run, other bytes, the same run: the second at a distance past the first.
    for n in (16, 64, 256):
        for gap in (1, 7, 100):
            inputs["runs-%d-%d" % (n, gap)] = b"x" * n + pattern(gap, 2000 + gap) + b"x" * n
    return inputs


def study(algos_json, directory, out):
    with open(algos_json) as f:
        algo = json.load(f)["COMPRESSION_LZBITMAP"]
    codec = Codec()
    for sub in ("in", "lzbitmap"):
        os.makedirs(os.path.join(directory, sub), exist_ok=True)
    record = {}
    for name, data in study_inputs().items():
        with open(os.path.join(directory, "in", name), "wb") as f:
            f.write(data)
        entry = {"input_len": len(data)}
        record[name] = entry
        encoded = codec.encode(data, algo)
        if encoded is None:
            entry["encoded"] = None
            continue
        with open(os.path.join(directory, "lzbitmap", name), "wb") as f:
            f.write(encoded)
        entry["encoded_len"] = len(encoded)
        entry["round_trip"] = codec.decode(encoded, len(data), algo) == data
    write_json(out, record)


# The study inputs the oracle changes: each small, each holding one match or a few.
ORACLE_BASES = ["run-145", "run-200", "run-300", "run-400", "dist64-100", "dist64-150",
                "repeat-100", "repeat-140", "repeat-160", "gap-10", "gap-40", "runs-64-7",
                "text-145", "text-200"]


def oracle(algos_json, out):
    with open(algos_json) as f:
        algo = json.load(f)["COMPRESSION_LZBITMAP"]
    codec = Codec()
    inputs = study_inputs()
    # A line at a time, so that a decoder fault partway through keeps every line before it.
    with open(out, "w", buffering=1) as f:
        for base in ORACLE_BASES:
            data = inputs[base]
            encoded = codec.encode(data, algo)
            f.write(json.dumps({"base": base, "input": data.hex(), "stream": encoded.hex()}) + "\n")
            # Everything after the four-byte signature and the first block's two lengths.
            for at in range(10, len(encoded)):
                for bit in range(8):
                    changed = bytearray(encoded)
                    changed[at] ^= 1 << bit
                    room = 2 * len(data) + 64
                    dst = ctypes.create_string_buffer(room)
                    src = ctypes.create_string_buffer(bytes(changed), len(changed))
                    n = codec.lib.compression_decode_buffer(dst, room, src, len(changed), None,
                                                           algo)
                    f.write(json.dumps({"base": base, "at": at, "bit": bit, "produced": n,
                                        "output": dst.raw[:n].hex()}) + "\n")


# The buffers `rules` decodes into: a name, the byte the buffer is filled with, and how
# many bytes of 0x5A sit before the part the decoder is given.
FILLS = [("zero", 0x00, 0), ("a5", 0xA5, 0), ("c3-after-5a", 0xC3, 64)]


def rules(algos_json, out):
    import zbmcases
    with open(algos_json) as f:
        algo = json.load(f)["COMPRESSION_LZBITMAP"]
    codec = Codec()
    with open(out, "w", buffering=1) as f:
        for name, stream, size in zbmcases.cases():
            room = 2 * size + 64
            results = []
            for fill, byte, before in FILLS:
                whole = ctypes.create_string_buffer(bytes([0x5A]) * before + bytes([byte]) * room,
                                                    before + room)
                dst = ctypes.addressof(whole) + before
                src = ctypes.create_string_buffer(stream, len(stream))
                n = codec.lib.compression_decode_buffer(dst, room, src, len(stream), None, algo)
                raw = whole.raw
                results.append({"fill": fill, "produced": n,
                                "output": raw[before:before + n].hex(),
                                "before_intact": raw[:before] == bytes([0x5A]) * before,
                                "after_intact": raw[before + n:before + room]
                                == bytes([byte]) * (room - n)})
            f.write(json.dumps({"case": name, "size": size, "stream": stream.hex(),
                                "results": results}) + "\n")


def corpus_inputs():
    """Inputs of many blocks, each kind of data asking the encoder for other steps."""
    inputs = {}
    for n, seed in ((1000, 1), (8191, 2), (32767, 3), (32768, 4), (32769, 5), (65535, 6),
                    (65536, 7), (100000, 8), (262144, 9)):
        inputs["text-%d" % n] = text(n, 500 + seed)
    # Records whose fields change at different rates, for masks of every shape.
    rec = bytearray()
    for i in range(6000):
        rec += struct.pack("<IIHH8s", i, i * 7919 % 65536, i % 13, 0xBEEF, b"name%04d" % (i % 1000))
    inputs["records-120000"] = bytes(rec)
    # One 8-byte pattern with a byte changed every 37 bytes.
    sparse = bytearray(b"pattern!" * 8192)
    for i in range(0, len(sparse), 37):
        sparse[i] ^= 0x55
    inputs["sparse-65536"] = bytes(sparse)
    # Runs long enough for counts of many nibbles, in more than one block.
    inputs["zeros-131072"] = bytes(131072)
    inputs["zeros-random-zeros"] = bytes(40000) + pattern(20000, 501) + bytes(40000)
    # Repeats at one distance per input: across a block, past what one byte holds, and past
    # what two bytes hold.
    a = pattern(40000, 502)
    inputs["repeat-40000"] = a + a
    b = pattern(70000, 503)
    inputs["repeat-70000"] = b + b
    inputs["cycle-255"] = bytes(range(255)) * 400
    inputs["cycle-256"] = bytes(range(256)) * 400
    inputs["cycle-257"] = (bytes(range(256)) + b"\x00") * 400
    # Text and random bytes in pieces of every length, for literals and distances of both
    # widths side by side.
    mixed = bytearray()
    for i in range(1, 400):
        mixed += text(i % 300 + 1, 600 + i % 7) if i % 2 else pattern(i % 200 + 1, 700 + i)
    inputs["mixed"] = bytes(mixed)
    inputs["random-100000"] = pattern(100000, 504)
    return inputs


def corpus(algos_json, directory, out):
    with open(algos_json) as f:
        algo = json.load(f)["COMPRESSION_LZBITMAP"]
    codec = Codec()
    for sub in ("in", "lzbitmap"):
        os.makedirs(os.path.join(directory, sub), exist_ok=True)
    record = {}
    for name, data in corpus_inputs().items():
        with open(os.path.join(directory, "in", name), "wb") as f:
            f.write(data)
        entry = {"input_len": len(data), "sha256": sha(data)}
        record[name] = entry
        encoded = codec.encode(data, algo)
        if encoded is None:
            entry["encoded"] = None
            continue
        with open(os.path.join(directory, "lzbitmap", name), "wb") as f:
            f.write(encoded)
        entry["encoded_len"] = len(encoded)
        entry["round_trip"] = codec.decode(encoded, len(data), algo) == data
    write_json(out, record)


def flip_inputs():
    """The inputs `flips` encodes, changes and cuts: the `oracle` bases and more."""
    study = study_inputs()
    inputs = {name: study[name] for name in ORACLE_BASES}
    inputs["text-600"] = text(600, 800)
    inputs["text-1500"] = text(1500, 801)
    rec = bytearray()
    for i in range(60):
        rec += struct.pack("<IIHH8s", i, i * 7919 % 65536, i % 13, 0xBEEF, b"name%04d" % i)
    inputs["records-1200"] = bytes(rec)
    mixed = bytearray()
    for i in range(1, 40):
        mixed += text(i % 30 + 1, 810 + i % 3) if i % 2 else pattern(i % 20 + 1, 820 + i)
    inputs["mixed"] = bytes(mixed)
    p = pattern(300, 830)
    inputs["distance-300"] = p + text(40, 831) + p
    sparse = bytearray(b"pattern!" * 128)
    for i in range(0, len(sparse), 37):
        sparse[i] ^= 0x55
    inputs["sparse-1024"] = bytes(sparse)
    inputs["two-blocks"] = text(33000, 832)
    inputs["zeros-text"] = bytes(3000) + text(300, 833)
    return inputs


def flips(algos_json, out):
    with open(algos_json) as f:
        algo = json.load(f)["COMPRESSION_LZBITMAP"]
    codec = Codec()

    def run(stream, room):
        dst = ctypes.create_string_buffer(room)
        src = ctypes.create_string_buffer(stream, len(stream))
        n = codec.lib.compression_decode_buffer(dst, room, src, len(stream), None, algo)
        return n, hashlib.sha256(dst.raw[:n]).hexdigest()[:24]

    with open(out, "w", buffering=1 << 16) as f:
        for base, data in flip_inputs().items():
            encoded = codec.encode(data, algo)
            room = 2 * len(data) + 64
            f.write(json.dumps({"base": base, "input": data.hex(), "stream": encoded.hex()}) + "\n")
            for at in range(4, len(encoded)):
                for bit in range(8):
                    changed = bytearray(encoded)
                    changed[at] ^= 1 << bit
                    n, digest = run(bytes(changed), room)
                    f.write(json.dumps({"base": base, "at": at, "bit": bit, "produced": n,
                                        "digest": digest}) + "\n")
            for length in range(len(encoded)):
                n, digest = run(encoded[:length], room)
                f.write(json.dumps({"base": base, "cut": length, "produced": n,
                                    "digest": digest}) + "\n")


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


# The compression type numbers `types` records, every one in both places.
TYPE_NUMBERS = range(64)
# The algorithms `types` stores under every type number, and the content each
# place holds: one chunk inline, several chunks in the resource fork.
TYPE_ALGORITHMS = ["COMPRESSION_LZRAVEN", "COMPRESSION_LZMESH", "COMPRESSION_LZ4"]
TYPE_CONTENTS = {"inline": "text-1000", "fork": "text-204800"}
# A payload no algorithm wrote, so that a type the kernel does not know and a
# type it knows but cannot decode this payload with can answer differently.
GARBAGE = bytes([0xA5]) * 64
GARBAGE_SIZE = 4096
# Layouts the earlier probes found the kernel reads, written again beside the
# sweep as controls on the harness.
TYPE_CONTROLS = [
    ("control-lzfse", "COMPRESSION_LZFSE", "inline", 11),
    ("control-lzfse", "COMPRESSION_LZFSE", "fork", 12),
    ("control-lzbitmap", "COMPRESSION_LZBITMAP", "inline", 13),
    ("control-lzbitmap", "COMPRESSION_LZBITMAP", "fork", 14),
]


def store(lib, path, place, kind, size, chunks):
    """Create PATH holding CHUNKS as compression type KIND: the one chunk in
    the decmpfs attribute when PLACE is inline, the type 8 table and chunks in
    the resource fork otherwise. Returns what each call did."""
    if place == "inline":
        attrs = [(DECMPFS, header(kind, size) + chunks[0])]
    else:
        attrs = [(FORK, fork(chunks)), (DECMPFS, header(kind, size))]
    done = {}
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
        for attr, value in attrs:
            done[attr.decode()] = {"len": len(value), "how": set_xattr(lib, path, attr, value)}
    except OSError as e:
        done["error"] = errno.errorcode.get(e.errno, str(e.errno))
        return done
    try:
        os.chflags(path, stat.UF_COMPRESSED)
        done["chflags"] = "ok"
    except OSError as e:
        done["chflags"] = errno.errorcode.get(e.errno, str(e.errno))
    return done


def types(algos_json, directory, out):
    """Every type number in both places, holding each algorithm's encoding of
    known content and holding the garbage payload, beside the controls."""
    with open(algos_json) as f:
        algos = json.load(f)
    codec = Codec()
    lib = libc()
    data = contents()
    plan = []
    for kind in TYPE_NUMBERS:
        for place in ("inline", "fork"):
            plan.append(("garbage", None, place, kind))
            for algo_name in TYPE_ALGORITHMS:
                plan.append((algo_name[len("COMPRESSION_"):].lower(), algo_name, place, kind))
    plan += TYPE_CONTROLS
    record = []
    for label, algo_name, place, kind in plan:
        content = TYPE_CONTENTS[place] if algo_name else None
        name = "t%02d-%s-%s" % (kind, place, content or "garbage")
        sub = os.path.join(directory, label)
        os.makedirs(sub, exist_ok=True)
        entry = {"label": label, "place": place, "type": kind, "file": os.path.join(label, name)}
        record.append(entry)
        if algo_name is None:
            size, chunks = GARBAGE_SIZE, [GARBAGE]
        else:
            entry["algorithm"] = algos.get(algo_name)
            if entry["algorithm"] is None:
                entry["skipped"] = "the header declares no " + algo_name
                continue
            raw = data[content]
            size = len(raw)
            chunks = [codec.encode(raw[i:i + CHUNK], entry["algorithm"])
                      for i in range(0, len(raw), CHUNK)]
            if any(c is None for c in chunks):
                entry["skipped"] = "the encoder wrote nothing for a chunk"
                continue
            entry["chunk_lengths"] = [len(c) for c in chunks]
        entry["stored"] = store(lib, os.path.join(sub, name).encode(), place, kind, size, chunks)
    write_json(out, record)


READ_CHILD = r"""
import hashlib, json, os, sys
try:
    with open(sys.argv[1], "rb") as f:
        got = f.read(4 * 1024 * 1024)
    print(json.dumps({"len": len(got), "sha256": hashlib.sha256(got).hexdigest(),
                      "head": got[:32].hex()}))
except OSError as e:
    print(json.dumps({"errno": e.errno}))
"""


def typeread(mnt, out):
    """Every file `types` wrote, read through the kernel by a process of its
    own under a time limit, against the content its name names."""
    want = {name: sha(data) for name, data in contents().items()}
    paths = []
    for top, dirs, files in os.walk(mnt):
        dirs.sort()
        paths += [os.path.join(top, name) for name in sorted(files)]
    # Controls first, then by type number, so the files most likely to answer
    # are read before any that might not; one line per file, written as it is
    # read, so a run cut short keeps what it read.
    paths.sort(key=lambda p: ("control" not in p, os.path.basename(p)))
    log = open(out, "w")
    for path in paths:
        name = os.path.basename(path)
        m = re.match(r"t(\d\d)-(inline|fork)-(.+)$", name)
        if not m:
            continue
        entry = {"file": os.path.relpath(path, mnt), "type": int(m.group(1)),
                 "place": m.group(2), "content": m.group(3)}
        try:
            entry.update(read_one(path, want))
        finally:
            log.write(json.dumps(entry, ensure_ascii=True) + "\n")
            log.flush()
    log.close()


def read_one(path, want):
    """What the kernel gives for one file of `types`."""
    import subprocess
    entry = {}
    content = re.match(r"t\d\d-(?:inline|fork)-(.+)$", os.path.basename(path)).group(1)
    try:
        st = os.lstat(path)
        entry["flags"] = "0x%x" % getattr(st, "st_flags", 0)
        entry["size"] = st.st_size
    except OSError as e:
        entry["stat"] = errno.errorcode.get(e.errno, str(e.errno))
    try:
        child = subprocess.run([sys.executable, "-c", READ_CHILD, path],
                               capture_output=True, text=True, timeout=10)
        got = json.loads(child.stdout or "{}")
    except subprocess.TimeoutExpired:
        entry["read"] = "timeout"
        return entry
    except ValueError:
        entry["read"] = "no answer"
        return entry
    if "errno" in got:
        entry["read"] = "error"
        entry["error"] = errno.errorcode.get(got["errno"], str(got["errno"]))
    elif "len" not in got:
        entry["read"] = "no answer"
    elif content in want:
        entry["read"] = "match" if got["sha256"] == want[content] else "mismatch"
    else:
        entry["read"] = "read"
    if entry["read"] in ("mismatch", "read"):
        entry.update(read_len=got["len"], read_sha256=got["sha256"], head=got["head"])
    return entry


def compression_of(path):
    """The compression type and size a compressed file's header records, or
    the error reading it."""
    try:
        value = get_xattr(path, DECMPFS)
    except OSError as e:
        return {"error": errno.errorcode.get(e.errno, str(e.errno))}
    if len(value) < 16 or value[:4] != b"fpmc":
        return {"error": "no fpmc header", "decmpfs_len": len(value)}
    kind, size = struct.unpack("<IQ", value[4:16])
    return {"type": kind, "header_size": size, "decmpfs_len": len(value)}


# Subtrees a survey of ROOT does not enter: other volumes reached through
# /System/Volumes, which the survey of the root they are mounted at covers.
SURVEY_SKIP = {"/System/Volumes"}
SURVEY_SAMPLES = 25


def survey(root, out, seconds):
    """Every regular file under ROOT, without following symlinks, until the
    time budget runs out: how many there are, and for each compression type
    found, how many files record it, their sizes, the device they are on, the
    directories three levels down that hold them, and the first few paths."""
    import time
    start = time.monotonic()
    deadline = start + float(seconds)
    files = compressed = unreadable = 0
    kinds = {}
    stack = [root]
    complete = True
    while stack:
        if time.monotonic() > deadline:
            complete = False
            break
        top = stack.pop()
        try:
            entries = sorted(os.scandir(top), key=lambda e: e.name)
        except OSError:
            unreadable += 1
            continue
        for entry in entries:
            path = entry.path
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                unreadable += 1
                continue
            if stat.S_ISDIR(st.st_mode):
                if path not in SURVEY_SKIP:
                    stack.append(path)
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            files += 1
            if not getattr(st, "st_flags", 0) & stat.UF_COMPRESSED:
                continue
            compressed += 1
            got = compression_of(path.encode())
            key = str(got.get("type", got.get("error")))
            k = kinds.setdefault(key, {"files": 0, "bytes": 0, "devices": {}, "dirs": {},
                                       "samples": []})
            k["files"] += 1
            k["bytes"] += got.get("header_size", 0)
            dev = str(st.st_dev)
            k["devices"][dev] = k["devices"].get(dev, 0) + 1
            d = "/".join(path.split("/")[:4])
            k["dirs"][d] = k["dirs"].get(d, 0) + 1
            if len(k["samples"]) < SURVEY_SAMPLES:
                k["samples"].append(path)
    write_json(out, {"root": root, "complete": complete,
                     "seconds": round(time.monotonic() - start, 1), "files": files,
                     "compressed": compressed, "unreadable": unreadable, "types": kinds})


def typesof(directory, out):
    """Every regular file under DIRECTORY: its size, flags and, where it is
    compressed, the type its header records."""
    record = []
    for top, dirs, files in os.walk(directory):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(top, name)
            st = os.lstat(path)
            entry = {"file": os.path.relpath(path, directory), "size": st.st_size,
                     "flags": "0x%x" % getattr(st, "st_flags", 0)}
            if getattr(st, "st_flags", 0) & stat.UF_COMPRESSED:
                entry.update(compression_of(path.encode()))
            record.append(entry)
    write_json(out, record)


def writers(directory, out):
    """Contents for a compressing copy to choose a type for: those `stage`
    writes, a large text, and copies of three of Apple's executables."""
    import shutil
    stage(directory, out)
    with open(out) as f:
        record = json.load(f)
    big = text(20 * 1024 * 1024, 30)
    with open(os.path.join(directory, "text-20971520"), "wb") as f:
        f.write(big)
    record["text-20971520"] = {"size": len(big), "sha256": sha(big)}
    for src in ("/bin/ls", "/bin/zsh", "/usr/lib/dyld"):
        name = "copy-" + src.strip("/").replace("/", "-")
        try:
            shutil.copyfile(src, os.path.join(directory, name))
            record[name] = {"size": os.path.getsize(os.path.join(directory, name)), "from": src}
        except OSError as e:
            record[name] = {"error": errno.errorcode.get(e.errno, str(e.errno))}
    write_json(out, record)


# Where the kernel stops reading content kept after the header, asked of every
# type that keeps it there: attribute lengths a byte apart across the length
# at which it stops reading type 9.
INLINE_LENGTHS = range(3790, 3816)
HEADER_LEN = 16
# The algorithm numbers of the types kept inline: the public header's LZFSE and
# LZBITMAP, and 0x900, which the kernel names for types 7 and 8.
INLINE_ENCODERS = {7: 0x900, 11: 0x801, 13: 0x702}


def mixed(n, seed):
    """n bytes that compress to less than half: lines of a 16-digit token."""
    out = bytearray()
    i = 0
    while len(out) < n:
        token = hashlib.sha256(b"%d:%d" % (seed, i)).hexdigest()[:16].encode()
        out += b"%s entry %06d\n" % (token, i)
        i += 1
    return bytes(out[:n])


INLINE_CONTENTS = {"random": lambda n: pattern(n, 70), "mixed": lambda n: mixed(n, 71)}


def inline_encoder(kind, codec):
    import zlib
    if kind == 3:
        return lambda data: zlib.compress(data, 9)
    if kind == 9:
        return lambda data: b"\xcc" + data
    if kind == "3-raw":
        return lambda data: b"\xff" + data
    algo = INLINE_ENCODERS[kind]
    return lambda data: codec.encode(data, algo) or b""


def fit(encode, make, target):
    """The content size, at most one chunk, whose encoding is TARGET bytes
    long, or None."""
    lo, hi = 1, CHUNK
    while lo < hi:
        mid = (lo + hi) // 2
        if len(encode(make(mid))) < target:
            lo = mid + 1
        else:
            hi = mid
    for d in range(400):
        for n in (lo - d, lo + d):
            if 1 <= n <= CHUNK and len(encode(make(n))) == target:
                return n
    return None


def inline_cases(codec):
    """Each file `inline` writes: name, type, the content's family and size,
    and the encoding kept after the header."""
    cases = []
    kinds = [3, 7, 11, 13, "3-raw", 9]
    for kind in kinds:
        encode = inline_encoder(kind, codec)
        families = ["random", "mixed"] if kind not in ("3-raw", 9) else ["random"]
        for family in families:
            make = INLINE_CONTENTS[family]
            controls = [("control", 500)]
            for label, target in controls + [("len-%d" % L, L) for L in INLINE_LENGTHS]:
                n = fit(encode, make, target - HEADER_LEN)
                name = "t%s-%s-%s" % (kind, family, label)
                cases.append({"name": name, "type": 3 if kind == "3-raw" else kind, "family": family,
                              "content_len": n, "stream": encode(make(n)) if n else None})
    return cases


def inline(directory, out):
    codec = Codec()
    lib = libc()
    os.makedirs(directory, exist_ok=True)
    record = []
    for case in inline_cases(codec):
        entry = {k: v for k, v in case.items() if k != "stream"}
        record.append(entry)
        if case["stream"] is None:
            entry["skipped"] = "no content size encodes to that length"
            continue
        value = header(case["type"], case["content_len"]) + case["stream"]
        entry["attr_len"] = len(value)
        path = os.path.join(directory, case["name"]).encode()
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
            entry["how"] = set_xattr(lib, path, DECMPFS, value)
            os.chflags(path, stat.UF_COMPRESSED)
        except OSError as e:
            entry["error"] = errno.errorcode.get(e.errno, str(e.errno))
    write_json(out, record)


def inlineread(mnt, record_json, out):
    """Each file `inline` wrote, read through the kernel in a process of its
    own, against the content it was made from."""
    import subprocess
    with open(record_json) as f:
        record = json.load(f)
    with open(out, "w") as log:
        for entry in record:
            if "attr_len" not in entry or "error" in entry:
                continue
            want = INLINE_CONTENTS[entry["family"]](entry["content_len"])
            got = {"file": entry["name"], "type": entry["type"], "attr_len": entry["attr_len"],
                   "content_len": entry["content_len"]}
            try:
                child = subprocess.run([sys.executable, "-c", READ_CHILD, os.path.join(mnt, entry["name"])],
                                       capture_output=True, text=True, timeout=10)
                answer = json.loads(child.stdout or "{}")
            except (subprocess.TimeoutExpired, ValueError):
                answer = {"errno": -1}
            if "errno" in answer:
                got["read"] = "error %s" % errno.errorcode.get(answer["errno"], str(answer["errno"]))
            elif answer["sha256"] == sha(want):
                got["read"] = "content"
            elif answer["len"] == 0:
                got["read"] = "nothing"
            elif answer["head"] == "00" * min(32, answer["len"]):
                got["read"] = "zeros"
            else:
                got["read"] = "other"
                got["len"] = answer["len"]
            log.write(json.dumps(got) + "\n")
            log.flush()


def main(argv):
    commands = {"algorithms": (algorithms, 2), "stage": (stage, 2), "own": (own, 3),
                "verify": (verify, 2), "streams": (streams, 3), "study": (study, 3),
                "oracle": (oracle, 2), "rules": (rules, 2), "corpus": (corpus, 3),
                "flips": (flips, 2), "types": (types, 3), "typeread": (typeread, 2),
                "survey": (survey, 3), "typesof": (typesof, 2), "writers": (writers, 2),
                "inline": (inline, 2), "inlineread": (inlineread, 3)}
    if len(argv) < 2 or argv[1] not in commands or len(argv) != commands[argv[1]][1] + 2:
        raise SystemExit(__doc__)
    commands[argv[1]][0](*argv[2:])


if __name__ == "__main__":
    main(sys.argv)
