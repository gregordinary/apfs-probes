#!/usr/bin/env python3
"""Compression type 9: what the kernel reads it as, asked of files this script
writes and of the files macOS ships under it.

    type9util.py codecs OUT.json
    type9util.py own CODECS.json DIR OUT.json
    type9util.py ownread MNT OUT.jsonl
    type9util.py ownlog LOG READS.jsonl OUT.jsonl
    type9util.py layout DIR OUT.json
    type9util.py layoutread MNT OUT.jsonl
    type9util.py apple CODECS.json OUT.jsonl SUMMARY.json SECONDS ROOT...
    type9util.py guessworker CODECS.json
    type9util.py copies APPLE.jsonl DIR PLAN.json PRIVATE.json
    type9util.py copyread MNT PLAN.json OUT.jsonl
    type9util.py copylog LOG READS.jsonl PRIVATE.json OUT.json
    type9util.py schemes APPLE.jsonl COPYLOG.json OUT.json

`codecs` asks libcompression, for every algorithm number from 0 to 65535, the
scratch sizes its encoder and decoder want, and encodes a known text with the
number and decodes it back. `own` writes files of type 9 and 10 whose
attribute and resource fork hold this script's bytes, varied one field at a
time: the payload's length and bytes, the header's size and type fields, and
each algorithm's encoding of a known text. `ownread` reads each back through
the kernel, and `ownlog` keeps what the system log says during each read.
`layout` writes files that hold the probe's own content after the marker byte
0xCC, which is how the files macOS ships under type 9 hold theirs: of sizes
from 0 to 1 MiB and every size where reading stops, with every other marker
value, with a header size that cuts or extends the content, and in the
resource fork under type 10, alone and in type 8's table. `layoutread` reads each back against that content.

`apple`, `copies`, `copyread`, `copylog` and `schemes` ask the same of the
files macOS ships under type 9, and keep nothing of their content. `apple`
walks each ROOT, as the typesurvey probe does, and for every file of type 9
or 10 reads its compression attribute and, through the kernel, its content.
`guessworker`, a process of its own, tries each codec `codecs` found, and
Python's zlib, LZMA and bzip2, on the payload from each of its first 33
offsets, against the content. `copies` stores, on an image of the probe's
own, the compression attribute of a sample of those files unchanged, and of
a smaller sample with one byte of the payload changed at a time, or the
header's size changed. `copyread` reads each copy through the kernel and
compares it with what the kernel reads from the file it was copied from, and
`copylog` keeps the system log's messages during each read, with every
number and everything quoted in them replaced, and names the field of the
payload whose changed value the kernel logs as an unknown scheme. `schemes`
counts the values the files hold in that field.

Of a file macOS ships, the records keep its path, the names of its extended
attributes, its sizes, the SHA-256 of its content and of its payload where
either is 32 bytes or longer (a shorter one could be recovered from its
digest), and verdicts: which guesses decode it, which integers at the
payload's start equal one of its lengths, how far the content's first and
last bytes appear verbatim, and how the kernel reads each copy. Of the bytes
at each of the payloads' first 32 and last 8 positions, the summary keeps how
many values occur and the share of the commonest three, not the values. A
value leaves only from a field the kernel names as the scheme, and only where
10 distinct payloads or more hold it. PRIVATE.json, which holds the copies'
payloads, belongs in the runner's work directory and never in the output.
"""

import bisect
import bz2
import collections
import ctypes
import ctypes.util
import datetime
import errno
import json
import lzma
import os
import re
import select
import stat
import struct
import subprocess
import sys
import time
import traceback
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compressutil import DECMPFS, FORK, fork, header, libc, set_xattr, sha, write_json  # noqa: E402
from fsprobe import get_xattr, list_xattrs, pattern, text  # noqa: E402

# The shortest content or payload whose digest a record keeps.
DIGEST_MIN = 32
# The payload offsets each guess starts decoding from.
OFFSETS = range(33)
# Positions the summary counts byte values at: from the payload's start, and
# from its end.
HEAD = 32
TAIL = 8
# A position's values are kept when no more than this many occur, and a value
# only where this many files share it.
FEW_VALUES = 16
SHARED = 10
# The most algorithm numbers the guesses try.
MAX_GUESS_ALGORITHMS = 40
# The number the kernel logs for type 9 and 10 in the decmpfs probe, whatever
# the payload.
DECMPFS_SCHEME = 460


# -- libcompression ---------------------------------------------------------

def compression():
    lib = ctypes.CDLL(ctypes.util.find_library("compression"))
    for fn in ("compression_encode_scratch_buffer_size", "compression_decode_scratch_buffer_size"):
        getattr(lib, fn).argtypes = [ctypes.c_int]
        getattr(lib, fn).restype = ctypes.c_size_t
    for fn in ("compression_encode_buffer", "compression_decode_buffer"):
        f = getattr(lib, fn)
        f.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
                      ctypes.c_void_p, ctypes.c_int]
        f.restype = ctypes.c_size_t
    return lib


SCAN_BLOCK = 256
CODEC_TEXT = text(1000, 40)

# One range of algorithm numbers, in a process of its own, so that a number
# that ends the process costs a rescan of its range one number at a time.
SCAN_CHILD = r"""
import ctypes, hashlib, json, sys
sys.path.insert(0, sys.argv[1])
from type9util import compression
lib = compression()
data = bytes.fromhex(sys.argv[4])
cap = 2 * len(data) + 4096
dst = ctypes.create_string_buffer(cap)
back = ctypes.create_string_buffer(len(data) + 64)
for alg in range(int(sys.argv[2]), int(sys.argv[3])):
    es = lib.compression_encode_scratch_buffer_size(alg)
    ds = lib.compression_decode_scratch_buffer_size(alg)
    n = lib.compression_encode_buffer(dst, cap, data, len(data), None, alg)
    if es or ds or n:
        m = lib.compression_decode_buffer(back, len(data) + 64, dst, n, None, alg) if n else 0
        print(json.dumps({"algorithm": alg, "encode_scratch": es, "decode_scratch": ds,
                          "encoded": n, "encoded_sha256": hashlib.sha256(dst.raw[:n]).hexdigest(),
                          "round_trip": bool(n) and back.raw[:m] == data}), flush=True)
"""


def codecs(out):
    here = os.path.dirname(os.path.abspath(__file__))

    def run(lo, hi):
        p = subprocess.run([sys.executable, "-c", SCAN_CHILD, here, str(lo), str(hi), CODEC_TEXT.hex()],
                           capture_output=True, text=True, timeout=600)
        return p.returncode, [json.loads(line) for line in p.stdout.splitlines() if line.startswith("{")]

    found, ended = [], []
    for lo in range(0, 0x10000, SCAN_BLOCK):
        rc, got = run(lo, lo + SCAN_BLOCK)
        if rc == 0:
            found += got
            continue
        for alg in range(lo, lo + SCAN_BLOCK):
            rc, got = run(alg, alg + 1)
            found += got
            if rc:
                ended.append({"algorithm": alg, "status": rc})
    write_json(out, {"text_len": len(CODEC_TEXT), "found": found, "ended": ended})


def guess_algorithms(codecs_json):
    """The algorithm numbers the guesses try: one for each distinct encoding
    of the codec text that decodes back, every number that names a decoder
    scratch size without encoding, and 0x900, which the kernel names for
    types 7 and 8."""
    with open(codecs_json) as f:
        found = json.load(f)["found"]
    picked, seen = [], set()
    for c in found:
        if c["round_trip"] and c["encoded_sha256"] not in seen:
            seen.add(c["encoded_sha256"])
            picked.append(c["algorithm"])
    for c in found:
        if not c["encoded"] and 0 < c["decode_scratch"] < 1 << 32:
            picked.append(c["algorithm"])
    if 0x900 not in picked:
        picked.append(0x900)
    return picked[:MAX_GUESS_ALGORITHMS]


# -- files of the probe's own -----------------------------------------------

def own_cases(lib, algorithms):
    """Each file `own` writes: its name, type, where its payload goes, the
    size its header records, and the payload."""
    cases = []

    def add(name, payload, size=4096, kind=9, place="inline"):
        cases.append({"name": name, "type": kind, "place": place, "size": size, "payload": payload})

    for n in list(range(65)) + [128, 256, 1024, 3000]:
        add("zeros-%04d" % n, bytes(n))
    for n in (1, 4, 16, 64):
        add("ff-%04d" % n, b"\xff" * n)
    add("a5-0064", b"\xa5" * 64)
    add("pattern-0064", pattern(64, 41))
    add("ascending-0064", bytes(range(64)))
    add("descending-0064", bytes(255 - i for i in range(64)))
    for v in range(256):
        add("first-%02x" % v, bytes([v]) + bytes(31))
    for p in range(1, 32):
        b = bytearray(32)
        b[p] = 0xFF
        add("position-%02d" % p, bytes(b))
    for s in (0, 1, 32, 460, 4095, 65536, 65537, 1 << 20, 1 << 31, 1 << 32, 1 << 48, (1 << 64) - 1):
        add("size-%x" % s, bytes(32), size=s)
    for kind in (0x109, 0x10009, 0x1000009, 0x80000009):
        add("type-%x" % kind, bytes(32), kind=kind)
    for n in (100, 1000):
        add("raw-%04d" % n, text(n, 42), size=n)
    content = text(1000, 43)
    cap = 2 * len(content) + 4096
    dst = ctypes.create_string_buffer(cap)
    for alg in algorithms:
        n = lib.compression_encode_buffer(dst, cap, content, len(content), None, alg)
        if n:
            add("stream-%04x" % alg, dst.raw[:n], size=len(content))
    add("fork-empty", b"", kind=10, place="fork")
    add("fork-table", fork([]), kind=10, place="fork")
    add("fork-zeros-0064", bytes(64), kind=10, place="fork")
    add("fork-a5-0064", fork([b"\xa5" * 64]), kind=10, place="fork")
    return cases


def store_raw(lib, path, decmpfs, resource=None):
    """Create PATH with DECMPFS as its compression attribute and, when given,
    RESOURCE as its resource fork, and mark it compressed. Returns what each
    call did."""
    done = {}
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
        attrs = ([(FORK, resource)] if resource is not None else []) + [(DECMPFS, decmpfs)]
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


def own(codecs_json, directory, out):
    lib = compression()
    c = libc()
    os.makedirs(directory, exist_ok=True)
    record = []
    for case in own_cases(lib, guess_algorithms(codecs_json)):
        hdr = header(case["type"], case["size"])
        if case["place"] == "inline":
            decmpfs, resource = hdr + case["payload"], None
        else:
            decmpfs, resource = hdr, case["payload"]
        entry = {k: v for k, v in case.items() if k != "payload"}
        entry["payload_len"] = len(case["payload"])
        entry["payload"] = case["payload"].hex()
        entry["stored"] = store_raw(c, os.path.join(directory, case["name"]).encode(), decmpfs, resource)
        record.append(entry)
    write_json(out, record)


# -- reading through the kernel ---------------------------------------------

READER = r"""
import hashlib, json, sys
for line in sys.stdin:
    mode, path = line.rstrip("\n").split("\t", 1)
    try:
        with open(path, "rb") as f:
            got = f.read(4 * 1024 * 1024)
        answer = {"len": len(got), "sha256": hashlib.sha256(got).hexdigest()}
        if mode == "head":
            answer["head"] = got[:64].hex()
        elif mode == "data":
            answer["data"] = got.hex()
    except OSError as e:
        answer = {"errno": e.errno}
    print(json.dumps(answer), flush=True)
"""


class Worker:
    """A process of its own answering one JSON line per request line, which is
    abandoned and replaced when it outlasts the time limit or ends. What it
    prints to standard error is dropped, since a worker that ends unexpectedly
    may hold a file macOS ships."""

    def __init__(self, argv, limit):
        self.argv = argv
        self.limit = limit
        self.child = None

    def ask(self, request):
        """The answer to REQUEST, None when there was none, and the wall-clock
        times around it."""
        if self.child is None or self.child.poll() is not None:
            self.child = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, text=True, bufsize=1)
        t0 = time.time()
        try:
            self.child.stdin.write(request + "\n")
            self.child.stdin.flush()
        except BrokenPipeError:
            self.child = None
            return None, t0, time.time()
        ready, _, _ = select.select([self.child.stdout], [], [], self.limit)
        if not ready:
            self.child.kill()
            self.child = None
            return "timeout", t0, time.time()
        line = self.child.stdout.readline()
        t1 = time.time()
        if not line:
            self.child = None
            return None, t0, t1
        return json.loads(line), t0, t1

    def close(self):
        if self.child is not None and self.child.poll() is None:
            self.child.stdin.close()
            self.child.wait(timeout=30)


class Reader(Worker):
    """Reads files through the kernel, one process of its own at a time."""

    def __init__(self, limit=10.0):
        super().__init__([sys.executable, "-c", READER], limit)

    def read(self, path, mode="plain"):
        """What one read of PATH gives, and the wall-clock times around it."""
        got, t0, t1 = self.ask("%s\t%s" % (mode, path))
        if got is None:
            return {"read": "no answer"}, t0, t1
        if got == "timeout":
            return {"read": "timeout"}, t0, t1
        if "errno" in got:
            return {"read": "error", "error": errno.errorcode.get(got["errno"], str(got["errno"]))}, t0, t1
        got["read"] = "ok"
        return got, t0, t1


# Between two reads, so that the system log's messages fall in one read's
# window or the other's.
READ_GAP = 0.005


def ownread(mnt, out):
    want = {"raw-%04d" % n: sha(text(n, 42)) for n in (100, 1000)}
    stream = sha(text(1000, 43))
    reader = Reader()
    with open(out, "w") as log:
        for name in sorted(os.listdir(mnt)):
            path = os.path.join(mnt, name)
            entry = {"file": name}
            try:
                st = os.lstat(path)
                entry["size"] = st.st_size
                entry["flags"] = "0x%x" % getattr(st, "st_flags", 0)
            except OSError as e:
                entry["stat"] = errno.errorcode.get(e.errno, str(e.errno))
            got, t0, t1 = reader.read(path, "head")
            entry.update(got)
            entry["t0"], entry["t1"] = t0, t1
            expect = want.get(name, stream if name.startswith("stream-") else None)
            if expect and got.get("read") == "ok":
                entry["match"] = got["sha256"] == expect
            log.write(json.dumps(entry, ensure_ascii=True) + "\n")
            log.flush()
            time.sleep(READ_GAP)
    reader.close()


# -- the layout, asked of the probe's own files -----------------------------

MARK = 0xCC


def layout_cases():
    """Each file `layout` writes: name, type, place, header size, payload, and
    the content a read that takes the payload after the marker as the file's
    content gives, cut or extended to the header's size."""
    cases = []

    def add(name, payload, size, kind=9, place="inline", content=b""):
        cases.append({"name": name, "type": kind, "place": place, "size": size, "payload": payload,
                      "content": content})

    c40 = text(40, 50)
    for v in range(256):
        add("mark-%02x" % v, bytes([v]) + c40, 40, content=c40)
    for n in (0, 1, 2, 15, 16, 17, 100, 111, 112, 1000, 3000, 3787, 3788, 4000, 8192,
              65535, 65536, 65537, 100000, 200000, 1 << 20):
        c = text(n, 51)
        add("raw-%07d" % n, bytes([MARK]) + c, n, content=c)
    for k in (1, 10, 39, 40):
        add("under-%02d" % k, bytes([MARK]) + c40, 40 - k, content=c40)
    for k in (1, 100, 4056, 4096, 65536, 1 << 20, 1 << 24):
        add("over-%08d" % k, bytes([MARK]) + c40, 40 + k, content=c40)
    # Where the content stops being read, by one byte, and where a header
    # size past the content stops being read.
    for n in range(3001, 3787):
        c = text(n, 54)
        add("limit-raw-%04d" % n, bytes([MARK]) + c, n, content=c)
    for size in (8192, 16384, 32768, 65535, 65536, 65537):
        add("limit-size-%05d" % size, bytes([MARK]) + c40, size, content=c40)
    add("marker-size0", bytes([MARK]), 0)
    add("marker-size5", bytes([MARK]), 5)
    add("marker-twice", bytes([MARK, MARK]) + c40, 41, content=bytes([MARK]) + c40)
    c = text(1000, 52)
    big = text(200 * 1024, 53)
    chunks = [big[i:i + 65536] for i in range(0, len(big), 65536)]
    add("t10-raw", bytes([MARK]) + c, len(c), kind=10, place="fork", content=c)
    add("t10-table", fork([bytes([MARK]) + c]), len(c), kind=10, place="fork", content=c)
    add("t10-table-multi", fork([bytes([MARK]) + x for x in chunks]), len(big), kind=10, place="fork",
        content=big)
    add("t10-table-plain", fork([c]), len(c), kind=10, place="fork", content=c)
    add("t9-table", fork([bytes([MARK]) + c]), len(c), content=c)
    return cases


def layout(directory, out):
    c = libc()
    os.makedirs(directory, exist_ok=True)
    record = []
    for case in layout_cases():
        hdr = header(case["type"], case["size"])
        if case["place"] == "inline":
            decmpfs, resource = hdr + case["payload"], None
        else:
            decmpfs, resource = hdr, case["payload"]
        record.append({"name": case["name"], "type": case["type"], "place": case["place"],
                       "size": case["size"], "payload_len": len(case["payload"]),
                       "stored": store_raw(c, os.path.join(directory, case["name"]).encode(), decmpfs, resource)})
    write_json(out, record)


def layoutread(mnt, out):
    """Each file `layout` wrote, read through the kernel, against its content
    taken as the payload after the marker: the same, a prefix, the content and
    then what bytes, or something else, shown by its first and last bytes."""
    reader = Reader()
    with open(out, "w") as log:
        for case in layout_cases():
            entry = {"file": case["name"], "size": case["size"]}
            got, t0, t1 = reader.read(os.path.join(mnt, case["name"]), "data")
            entry["t0"], entry["t1"] = t0, t1
            entry["read"] = got["read"]
            if "error" in got:
                entry["error"] = got["error"]
            if got["read"] == "ok":
                data = bytes.fromhex(got["data"])
                want = case["content"]
                entry["len"] = len(data)
                if data == want[:case["size"]] and len(data) == min(case["size"], len(want)):
                    entry["verdict"] = "content, cut to the size" if case["size"] < len(want) else "content"
                elif data.startswith(want) and want:
                    rest = data[len(want):]
                    entry["verdict"] = "content, then more"
                    entry["more"] = len(rest)
                    entry["more_values"] = sorted(set(rest))[:16]
                elif not data:
                    entry["verdict"] = "nothing"
                else:
                    entry["verdict"] = "other"
                    entry["first_difference"] = common_prefix(data, want)
                    entry["head"] = data[:64].hex()
                    entry["tail"] = data[-64:].hex()
                    entry["values"] = sorted(set(data))[:16]
            log.write(json.dumps(entry, ensure_ascii=True) + "\n")
            log.flush()
            time.sleep(READ_GAP)
    reader.close()


# -- the system log ---------------------------------------------------------

TIME_FORMATS = ("%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z")


def log_time(value):
    for fmt in TIME_FORMATS:
        try:
            return datetime.datetime.strptime(value, fmt).timestamp()
        except ValueError:
            pass
    return None


def log_entries(path):
    """The kernel's messages in a `log show --style ndjson` file, as (time,
    message) in time order, and how many lines were read and skipped."""
    entries = []
    counts = collections.Counter()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.startswith("{"):
                counts["not json"] += 1
                continue
            try:
                e = json.loads(line)
            except ValueError:
                counts["not json"] += 1
                continue
            ts = log_time(e.get("timestamp", ""))
            if ts is None:
                counts["no time"] += 1
            elif e.get("processID") != 0:
                counts["not the kernel"] += 1
            else:
                counts["kernel"] += 1
                entries.append((ts, e.get("eventMessage", "")))
    entries.sort()
    return entries, dict(counts)


def by_read(entries, reads):
    """Each read's messages: those logged between its start and a little
    after its end."""
    times = [ts for ts, _ in entries]
    out = {}
    for r in reads:
        if "t0" not in r:
            continue
        lo = bisect.bisect_left(times, r["t0"] - 0.0005)
        hi = bisect.bisect_right(times, r["t1"] + 0.002)
        out[r["key"]] = [entries[i][1] for i in range(lo, hi)]
    return out


BUILD_PATH = re.compile(r"/AppleInternal/\S*?/Sources/")


def short(message):
    return BUILD_PATH.sub("", message)[:400]


def ownlog(log, reads_jsonl, out):
    with open(reads_jsonl) as f:
        reads = [dict(json.loads(line), key=json.loads(line)["file"]) for line in f]
    entries, counts = log_entries(log)
    messages = by_read(entries, reads)
    with open(out, "w") as f:
        f.write(json.dumps({"log_lines": counts}) + "\n")
        for r in reads:
            f.write(json.dumps({"file": r["key"], "messages": [short(m) for m in messages.get(r["key"], [])]},
                               ensure_ascii=True) + "\n")


SCHEME = re.compile(r"unknown compression scheme (\d+)")


def template(message, mnt):
    """A message with the mount point, every number and everything quoted
    replaced, so that a message about a copy of a file macOS ships carries
    nothing the file holds."""
    m = short(message).replace(mnt, "MNT")
    m = re.sub(r"'[^']*'|\"[^\"]*\"|<[^<>]*>|\[[^\[\]]*\]", "<q>", m)
    m = re.sub(r"0x[0-9a-fA-F]+", "\0", m)
    return re.sub(r"\d+", "#", m).replace("\0", "0x#")


# -- the files macOS ships --------------------------------------------------

def common_prefix(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def uleb128(data, at):
    value = shift = 0
    for i in range(at, min(len(data), at + 10)):
        value |= (data[i] & 0x7F) << shift
        if not data[i] & 0x80:
            return value, i + 1 - at
        shift += 7
    return None, 0


def readings(payload, at):
    """Each integer reading of the payload at offset AT: name, value, width."""
    out = []
    for name, fmt in (("u8", "<B"), ("u16le", "<H"), ("u16be", ">H"), ("u32le", "<I"), ("u32be", ">I")):
        w = struct.calcsize(fmt)
        if at + w <= len(payload):
            out.append((name, struct.unpack_from(fmt, payload, at)[0], w))
    if at + 3 <= len(payload):
        out.append(("u24le", int.from_bytes(payload[at:at + 3], "little"), 3))
    v, w = uleb128(payload, at)
    if w:
        out.append(("uleb", v, w))
    return out


def relations(payload, clen):
    """Names of the integer readings at the payload's first 16 offsets that
    equal the content's length, the payload's, or what of the payload follows
    the reading."""
    found = []
    for at in range(16):
        for name, value, w in readings(payload, at):
            for target, t in (("content_len", clen), ("payload_len", len(payload)),
                              ("payload_after", len(payload) - at - w)):
                if value == t and t > 0:
                    found.append("%s@%d==%s" % (name, at, target))
    return found


def py_decoders():
    def zl(wbits):
        return lambda src, n: zlib.decompressobj(wbits).decompress(src, n)
    return [("zlib-raw", zl(-15)), ("zlib", zl(15)), ("gzip", zl(31)),
            ("lzma", lambda src, n: lzma.LZMADecompressor().decompress(src, n)),
            ("bzip2", lambda src, n: bz2.BZ2Decompressor().decompress(src, n))]


class Guesser:
    """Each guess at the payload's layout: a decoder started at an offset."""

    def __init__(self, algorithms, lib):
        self.algorithms = algorithms
        self.lib = lib
        self.cap = 1 << 20
        self.dst = ctypes.create_string_buffer(self.cap) if lib else None
        self.py = py_decoders()

    def decoders(self):
        for alg in self.algorithms:
            yield "%04x" % alg, alg
        for name, fn in self.py:
            yield name, fn

    def decode(self, decoder, src, room):
        if isinstance(decoder, int):
            n = self.lib.compression_decode_buffer(self.dst, room, src, len(src), None, decoder)
            return self.dst.raw[:n]
        try:
            return decoder(src, room)
        except Exception:
            return b""

    def guess(self, payload, content):
        """Every (decoder, offset) that decodes the payload to the content,
        each that decodes it to the content and more, and the one that
        decodes the longest prefix of it otherwise."""
        exact, longer, best = [], [], (0, None, None)
        room = min(len(content) + 64, self.cap)
        for k in OFFSETS:
            if k >= len(payload):
                break
            src = payload[k:]
            for name, decoder in self.decoders():
                got = self.decode(decoder, src, room)
                if got == content:
                    exact.append([name, k])
                elif len(got) > len(content) and got.startswith(content) and content:
                    longer.append([name, k])
                elif got:
                    n = common_prefix(got, content)
                    if n > best[0]:
                        best = (n, name, k)
        return exact, longer, best


def printable(data):
    if not data:
        return None
    return round(sum(1 for b in data if 32 <= b < 127 or b in (9, 10, 13)) / len(data), 2)


def analyze(payload, content):
    """The verdicts about one payload and the content the kernel reads for
    it, but for the guesses."""
    v = {"payload_printable": printable(payload), "content_printable": printable(content),
         "relations": relations(payload, len(content))}
    best_k, best_n = None, 0
    for k in OFFSETS:
        if k >= len(payload):
            break
        n = common_prefix(payload[k:], content)
        if n > best_n:
            best_k, best_n = k, n
    v["verbatim"] = {"offset": best_k, "len": min(best_n, 64)} if best_n else None
    v["verbatim_tail"] = min(common_prefix(payload[::-1], content[::-1]), 64)
    v["content_at"] = payload.find(content) if content else None
    if len(content) >= 8:
        v["head8_at"] = payload.find(content[:8])
        i = payload.rfind(content[-8:])
        v["tail8_at_from_end"] = len(payload) - i if i >= 0 else -1
    return v


def guess_verdicts(guesser, payload, content):
    exact, longer, best = guesser.guess(payload, content)
    return {"exact": exact, "longer": longer,
            "partial": {"len": best[0], "decoder": best[1], "offset": best[2]} if best[0] else None}


def guessworker(codecs_json):
    """Answer each path on standard input with the guesses' verdicts about
    that file, in a process of its own, so that a decoder that ends the
    process costs one file."""
    guesser = Guesser(guess_algorithms(codecs_json), compression())
    for line in sys.stdin:
        path = line.rstrip("\n").encode()
        try:
            payload = get_xattr(path, DECMPFS)[16:]
            with open(path, "rb") as f:
                content = f.read()
            answer = guess_verdicts(guesser, payload, content)
        except Exception as e:
            answer = {"failed": type(e).__name__}
        print(json.dumps(answer), flush=True)


def candidates(root, deadline):
    """Every compressed regular file under ROOT whose header records type 9
    or 10, until the deadline, and whether the walk finished."""
    skip = {"/System/Volumes"}
    stack = [root]
    found = []
    while stack:
        if time.monotonic() > deadline:
            return found, False
        top = stack.pop()
        try:
            entries = sorted(os.scandir(top), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                if entry.path not in skip:
                    stack.append(entry.path)
            elif stat.S_ISREG(st.st_mode) and getattr(st, "st_flags", 0) & stat.UF_COMPRESSED:
                try:
                    value = get_xattr(entry.path.encode(), DECMPFS)
                except OSError:
                    continue
                if len(value) >= 16 and value[:4] == b"fpmc" and struct.unpack_from("<I", value, 4)[0] in (9, 10):
                    found.append((entry.path, st))
    return found, True


def collect(path, st):
    """One file's record, and its payload and content, which stay here."""
    p = path.encode()
    r = {"path": path, "st_size": st.st_size, "flags": "0x%x" % getattr(st, "st_flags", 0)}
    value = get_xattr(p, DECMPFS)
    r["type"], r["header_size"] = struct.unpack_from("<IQ", value, 4)
    r["xattr_len"] = len(value)
    payload = value[16:]
    r["payload_len"] = len(payload)
    try:
        r["xattrs"] = sorted(n.decode("utf-8", "replace") for n in list_xattrs(p))
    except OSError as e:
        r["xattrs"] = errno.errorcode.get(e.errno, str(e.errno))
    resource = None
    if FORK.decode() in r["xattrs"]:
        try:
            resource = get_xattr(p, FORK)
            r["fork_len"] = len(resource)
        except OSError as e:
            r["fork_len"] = errno.errorcode.get(e.errno, str(e.errno))
    try:
        with open(p, "rb") as f:
            content = f.read()
        r["read_len"] = len(content)
    except OSError as e:
        r["read_error"] = errno.errorcode.get(e.errno, str(e.errno))
        content = None
    if len(payload) >= DIGEST_MIN:
        r["payload_sha256"] = sha(payload)
    if content is not None and len(content) >= DIGEST_MIN:
        r["content_sha256"] = sha(content)
    return r, payload, resource, content


def positions(rows, data):
    """For each counted position of the payloads in DATA, how many have it,
    how many values occur there, and the share of the three commonest, but
    not which values they are. A byte the content holds verbatim there, as
    the start of what the payload holds from some offset or as the payload's
    end, is content rather than layout, and is not counted; nor is a payload
    the same as one counted already, so that copies of one file count as one."""
    out = {}
    unique, seen = [], set()
    for r, d in zip(rows, data):
        if d not in seen:
            seen.add(d)
            unique.append((r, d))
    for label, at in [("head", i) for i in range(HEAD)] + [("tail", i) for i in range(1, TAIL + 1)]:
        counts = collections.Counter()
        for r, d in unique:
            v = r["verdicts"]
            if label == "head" and at < len(d):
                if v["verbatim"] and v["verbatim"]["offset"] <= at < v["verbatim"]["offset"] + v["verbatim"]["len"]:
                    continue
                counts[d[at]] += 1
            elif label == "tail" and at <= len(d):
                if at <= v["verbatim_tail"]:
                    continue
                counts[d[-at]] += 1
        n = sum(counts.values())
        out["%s%+d" % (label, at if label == "head" else -at)] = {
            "payloads": n, "values": len(counts),
            "top_shares": [round(c / n, 3) for _, c in counts.most_common(3)] if n else []}
    return out


def tally(rows):
    """The counts the summary keeps of a group of records."""
    t = {"files": len(rows), "relations": collections.Counter(), "exact": collections.Counter(),
         "longer": collections.Counter(), "partial": collections.Counter(),
         "guess_outcomes": collections.Counter(),
         "verbatim_offset": collections.Counter(), "content_at": collections.Counter(),
         "head8_at": collections.Counter(), "tail8_at_from_end": collections.Counter(),
         "payload_len": collections.Counter(), "ratio": collections.Counter(),
         "sizes_agree": collections.Counter()}
    for r in rows:
        v = r.get("verdicts")
        if not v:
            continue
        t["relations"].update(v["relations"])
        g = r.get("guesses")
        if isinstance(g, dict) and "exact" in g:
            t["guess_outcomes"]["guessed"] += 1
            t["exact"].update("%s@%d" % (a, k) for a, k in g["exact"])
            t["longer"].update("%s@%d" % (a, k) for a, k in g["longer"])
            if g["partial"]:
                t["partial"]["%s@%d len>=%d" % (g["partial"]["decoder"], g["partial"]["offset"],
                                                min(g["partial"]["len"], 16) // 4 * 4)] += 1
        elif g is None:
            t["guess_outcomes"]["not reached"] += 1
        else:
            t["guess_outcomes"][str(g.get("failed") if isinstance(g, dict) else g)] += 1
        if v["verbatim"]:
            t["verbatim_offset"]["%d len>=%d" % (v["verbatim"]["offset"],
                                                 min(v["verbatim"]["len"], 16) // 4 * 4)] += 1
        t["content_at"][str(v["content_at"])] += 1
        t["head8_at"][str(v.get("head8_at"))] += 1
        t["tail8_at_from_end"][str(v.get("tail8_at_from_end"))] += 1
        t["payload_len"][bucket(r["payload_len"])] += 1
        if r.get("read_len"):
            t["ratio"]["%.1f" % min(r["payload_len"] / r["read_len"], 9.9)] += 1
        t["sizes_agree"]["header==st_size:%s read==header:%s" % (
            r["header_size"] == r["st_size"], r.get("read_len") == r["header_size"])] += 1
    return {k: (dict(v.most_common()) if isinstance(v, collections.Counter) else v) for k, v in t.items()}


def bucket(n):
    edge = 1
    while edge < n:
        edge *= 2
    return "<=%d" % edge


def summarize(rows, payloads, walks, algorithms):
    """ROWS and PAYLOADS in step, a payload None where none was read."""
    readable = [i for i, r in enumerate(rows) if "verdicts" in r]
    s = {"walks": walks, "algorithms": ["%04x" % a for a in algorithms],
         "files": len(rows), "by_type": dict(collections.Counter(str(r.get("type")) for r in rows)),
         "not_collected": dict(collections.Counter(r["collect"] for r in rows if "collect" in r)),
         "unreadable": dict(collections.Counter(r["read_error"] for r in rows if "read_error" in r)),
         "with_fork": sum(1 for r in rows if "fork_len" in r),
         "xattr_names": dict(collections.Counter(n for r in rows if isinstance(r.get("xattrs"), list)
                                                 for n in r["xattrs"])),
         "positions": positions([rows[i] for i in readable], [payloads[i] for i in readable]),
         "all": tally([rows[i] for i in readable])}
    return s


# The longest each root's walk may take, and the longest one file's guesses may.
WALK_SECONDS = 300
GUESS_SECONDS = 30


def apple(codecs_json, out, summary, seconds, *roots):
    algorithms = guess_algorithms(codecs_json)
    walks = {}
    found = []
    for root in roots:
        t = time.monotonic()
        got, complete = candidates(root, t + WALK_SECONDS)
        walks[root] = {"files": len(got), "complete": complete, "seconds": round(time.monotonic() - t, 1)}
        found += got
    rows, payloads = [], []
    for path, st in found:
        try:
            r, payload, _, content = collect(path, st)
            if content is not None:
                r["verdicts"] = analyze(payload, content)
        except Exception as e:
            r, payload = {"path": path, "collect": type(e).__name__}, None
        rows.append(r)
        payloads.append(payload)
    # The guesses, in an order fixed by the path's digest until the time
    # runs out, so the files guessed are a sample of all of them.
    worker = Worker([sys.executable, os.path.abspath(__file__), "guessworker", codecs_json], GUESS_SECONDS)
    deadline = time.monotonic() + float(seconds)
    order = sorted(range(len(rows)), key=lambda i: sha(rows[i]["path"].encode()))
    for i in order:
        r = rows[i]
        if "verdicts" not in r or r["read_len"] > 256 * 1024:
            continue
        if time.monotonic() > deadline:
            break
        got, _, _ = worker.ask(r["path"])
        r["guesses"] = got if got is not None else "ended"
    worker.close()
    with open(out, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=True) + "\n")
    write_json(summary, summarize(rows, payloads, walks, algorithms))


# -- copies of the files macOS ships ----------------------------------------

VERBATIM = 400
MUTATED = 24


def spread(rows, n):
    """N rows spread evenly over ROWS, which are in payload-length order."""
    if len(rows) <= n:
        return list(rows)
    return [rows[i * len(rows) // n] for i in range(n)]


def mutation_positions(n):
    return list(range(n)) if n <= 64 else list(range(48)) + list(range(n - 16, n))


def copies(apple_jsonl, directory, plan_out, private_out):
    with open(apple_jsonl) as f:
        rows = [json.loads(line) for line in f]
    rows = [r for r in rows if r.get("type") == 9 and "read_len" in r and "collect" not in r]
    rows.sort(key=lambda r: (r["payload_len"], r["path"]))
    c = libc()
    os.makedirs(directory, exist_ok=True)
    plan, private = [], {}

    def add(case, source, decmpfs, resource, **what):
        name = "%05d" % len(plan)
        entry = {"copy": name, "case": case, "source": source, **what}
        entry["stored"] = store_raw(c, os.path.join(directory, name).encode(), decmpfs, resource)
        plan.append(entry)
        private[name] = {"payload": decmpfs[16:].hex(), "source": source}

    def original(r):
        value = get_xattr(r["path"].encode(), DECMPFS)
        resource = get_xattr(r["path"].encode(), FORK) if "fork_len" in r else None
        return value, resource

    for r in spread(rows, VERBATIM):
        try:
            value, resource = original(r)
        except OSError:
            continue
        add("verbatim", r["path"], value, resource)
    for r in spread(rows, MUTATED):
        try:
            value, resource = original(r)
        except OSError:
            continue
        for p in mutation_positions(len(value) - 16):
            changed = bytearray(value)
            changed[16 + p] ^= 0xFF
            add("byte", r["path"], bytes(changed), resource, position=p)
        size = struct.unpack_from("<Q", value, 8)[0]
        for label, s in (("zero", 0), ("minus1", size - 1), ("plus1", size + 1),
                         ("plus4096", size + 4096), ("high", size | 1 << 40)):
            changed = bytearray(value)
            struct.pack_into("<Q", changed, 8, s % (1 << 64))
            add("size", r["path"], bytes(changed), resource, variant=label)
    write_json(plan_out, plan)
    write_json(private_out, private)


def copyread(mnt, plan_json, out):
    with open(plan_json) as f:
        plan = json.load(f)
    reader = Reader()
    originals = {}
    with open(out, "w") as log:
        for c in plan:
            entry = {"copy": c["copy"], "case": c["case"]}
            for k in ("position", "variant"):
                if k in c:
                    entry[k] = c[k]
            if c["source"] not in originals:
                try:
                    with open(c["source"], "rb") as f:
                        originals[c["source"]] = f.read()
                except OSError:
                    originals[c["source"]] = None
            want = originals[c["source"]]
            got, t0, t1 = reader.read(os.path.join(mnt, c["copy"]), "data")
            entry["t0"], entry["t1"] = t0, t1
            entry["read"] = got["read"]
            if "error" in got:
                entry["error"] = got["error"]
            if got["read"] == "ok" and want is not None:
                data = bytes.fromhex(got["data"])
                entry["len"] = len(data)
                entry["same"] = data == want
                if data != want:
                    entry["first_difference"] = common_prefix(data, want)
                    entry["prefix_of_original"] = want.startswith(data)
            log.write(json.dumps(entry, ensure_ascii=True) + "\n")
            log.flush()
            time.sleep(READ_GAP)
    reader.close()


def scheme_relations(n, payload, at):
    """How a logged scheme number relates to the payload a byte of which was
    changed at AT, without saying what any byte holds."""
    found = []
    if n == DECMPFS_SCHEME:
        found.append("decmpfs-probe-number")
    if at is None:
        return found
    for start in range(max(0, at - 7), at + 1):
        for name, value, w in readings(payload, start):
            if start + w > at and value == n:
                found.append("%s@%d" % (name, start - at))
    return found


def copylog(log, reads_jsonl, private_json, out):
    with open(reads_jsonl) as f:
        reads = [dict(json.loads(line), key=json.loads(line)["copy"]) for line in f]
    with open(private_json) as f:
        private = json.load(f)
    entries, counts = log_entries(log)
    messages = by_read(entries, reads)
    mnt_re = re.compile(r"/\S*/copy-mnt")
    per_copy = {}
    by_case = collections.defaultdict(collections.Counter)
    fields = collections.defaultdict(set)
    for r in reads:
        msgs = messages.get(r["key"], [])
        templates = sorted(set(template(mnt_re.sub("MNT", m), "MNT") for m in msgs))
        rel = set()
        for m in msgs:
            for s in SCHEME.findall(m):
                payload = bytes.fromhex(private[r["key"]]["payload"])
                rel.update(scheme_relations(int(s), payload, r.get("position")))
        # A reading the logged number equals, placed in the payload rather
        # than against the changed byte: the field the kernel took the
        # scheme from.
        for name in rel:
            if "@" in name:
                reading, offset = name.split("@")
                fields["%s@%d" % (reading, r["position"] + int(offset))].add(private[r["key"]]["source"])
        per_copy[r["key"]] = {"templates": templates, "scheme_relations": sorted(rel)}
        outcome = r["read"] if r["read"] != "ok" else ("same" if r.get("same") else "differs")
        if r["case"] == "byte":
            plen = len(private[r["key"]]["payload"]) // 2
            where = "byte %d" % r["position"] if r["position"] < 48 else "byte end-%d" % (plen - r["position"])
        elif r["case"] == "size":
            where = "size " + r["variant"]
        else:
            where = "verbatim"
        by_case[where][" | ".join([outcome] + templates + sorted(rel))] += 1
    write_json(out, {"log_lines": counts, "copies": per_copy,
                     "by_case": {k: dict(v.most_common()) for k, v in by_case.items()},
                     "scheme_fields": {k: len(v) for k, v in sorted(fields.items())}})


# A field is taken as the scheme where the logged number equals it in copies
# of at least this many files.
SCHEME_SOURCES = 2
WIDTHS = {"u8": 1, "u16le": 2, "u16be": 2, "u24le": 3, "u32le": 4, "u32be": 4, "uleb": 5}


def scheme_field(fields):
    """The one field the kernel took the scheme from: the one that equals the
    logged number in copies of the most files, the narrowest where several
    do. A wider reading that also equals it does so because the bytes it adds
    happen to be zero, and those bytes may be content."""
    ranked = sorted(fields.items(), key=lambda kv: (-kv[1], WIDTHS[kv[0].split("@")[0]], kv[0]))
    if not ranked or ranked[0][1] < SCHEME_SOURCES:
        return None
    return ranked[0][0]


def schemes(apple_jsonl, copylog_json, out):
    """For the field the kernel took a scheme number from in `copylog`, the
    values the files macOS ships hold there, each shared by enough distinct
    payloads, and the summary's counts again for the files of each value."""
    with open(copylog_json) as f:
        fields = json.load(f)["scheme_fields"]
    with open(apple_jsonl) as f:
        rows = [json.loads(line) for line in f]
    rows = [r for r in rows if "verdicts" in r]
    payloads = []
    for r in rows:
        try:
            payloads.append(get_xattr(r["path"].encode(), DECMPFS)[16:])
        except OSError:
            payloads.append(None)
    result = {}
    for field in filter(None, [scheme_field(fields)]):
        reading, at = field.split("@")
        members = collections.defaultdict(list)
        for i, d in enumerate(payloads):
            if d is None:
                continue
            got = [v for name, v, _ in readings(d, int(at)) if name == reading]
            members[got[0] if got else None].append(i)
        values = {}
        unshared = 0
        for value, idx in sorted(members.items(), key=lambda kv: -len(kv[1])):
            distinct = len(set(payloads[i] for i in idx))
            if value is None or distinct < SHARED:
                unshared += distinct
                continue
            values[str(value)] = {"distinct_payloads": distinct,
                                  "tally": tally([rows[i] for i in idx]),
                                  "positions": positions([rows[i] for i in idx], [payloads[i] for i in idx])}
        result[field] = {"values": values, "unshared_payloads": unshared}
    write_json(out, {"fields": fields, "schemes": result})


# -- the command line -------------------------------------------------------

# The commands that read the files macOS ships. An error in one of them
# reports its kind and line alone, so no message carries a byte of a file.
GUARDED = {"apple", "guessworker", "copies", "copyread", "copylog", "schemes"}


def main(argv):
    commands = {"codecs": (codecs, 1), "own": (own, 3), "ownread": (ownread, 2),
                "layout": (layout, 2), "layoutread": (layoutread, 2),
                "ownlog": (ownlog, 3), "apple": (apple, None), "guessworker": (guessworker, 1),
                "copies": (copies, 4),
                "copyread": (copyread, 3), "copylog": (copylog, 4), "schemes": (schemes, 3)}
    if len(argv) < 2 or argv[1] not in commands:
        raise SystemExit(__doc__)
    fn, n = commands[argv[1]]
    if (n is not None and len(argv) != n + 2) or (n is None and len(argv) < 7):
        raise SystemExit(__doc__)
    if argv[1] not in GUARDED:
        fn(*argv[2:])
        return
    try:
        fn(*argv[2:])
    except BaseException as e:
        frame = traceback.extract_tb(e.__traceback__)[-1] if e.__traceback__ else None
        print("%s: %s at line %s" % (argv[1], type(e).__name__, frame.lineno if frame else "?"),
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv)
