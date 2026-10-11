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
inputs than `study`'s, of many blocks and of every kind of step.
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
                "verify": (verify, 2), "streams": (streams, 3), "study": (study, 3),
                "oracle": (oracle, 2), "rules": (rules, 2), "corpus": (corpus, 3)}
    if len(argv) < 2 or argv[1] not in commands or len(argv) != commands[argv[1]][1] + 2:
        raise SystemExit(__doc__)
    commands[argv[1]][0](*argv[2:])


if __name__ == "__main__":
    main(sys.argv)
