#!/usr/bin/env python3
"""Change an image's metadata one byte at a time, and keep what fsck_apfs
says about each change.

    mutate.py blocks IMAGE
    mutate.py run IMAGE OUTDIR [--objects] [--shard K/N] [--budget SECONDS]
                               [--workers N] [--fsck PATH]

The image is read as 4096-byte blocks. A block is an object when its first
8 bytes are the Fletcher check words of the rest of the block: the two
32-bit words that, appended to the block's remaining bytes read as
little-endian 32-bit words, bring both running sums to zero modulo
2^32 - 1 (Kodis, "Fletcher's Checksum", Dr. Dobb's Journal, May 1992).
Plum and Dewald describe this checksum at the start of every APFS object,
with the object's type in the 16-bit word at byte 24 and its subtype in the
one at byte 28 ("Forensic APFS File Recovery", ARES 2018, section 2.1). A
block made of nothing but 0xFF bytes satisfies the same equation and is not
counted as an object. Block 0 must be an object holding "NXSB" at byte 32
and 4096 in the 32-bit word at byte 36, the container's block size (the same
paper, table 2), or the image is refused.

`blocks` lists every nonzero block: its number, whether it is an object,
and for an object the words at bytes 24 and 28.

`run` sweeps a shard of the nonzero blocks (with --objects, of the object
blocks), shard K of N holding the blocks whose place in that list leaves
K - 1 when divided by N. Each byte of a swept block in turn has all eight
bits inverted in a copy of the image, `fsck_apfs -n -W` checks the copy,
and the byte is put back. In an object the check words are recomputed after
the change, so the checker sees a well-formed object holding one wrong
value; bytes 0 to 7, the check words themselves, are instead changed once
per block, at byte 0, without recomputing them. Each worker owns one copy.
The sweep stops taking new bytes once --budget seconds have passed.

OUTDIR receives:

    blocks.tsv     the swept blocks, as `blocks` lists them
    results.tsv    one line per change: block, offset, change (flip or
                   check), exit status (or "timeout"), milliseconds, the
                   checker's output id, and 1 where the checker changed the
                   copy (which is then replaced)
    outputs.jsonl  each distinct output once, as {"id", "text"}: stdout,
                   then stderr after a "--- stderr" line, with the copy's
                   path written as IMAGE; id 0 is the unchanged image's
    summary.json   counts, times and the base image's sha256

A base image the checker does not pass is reported and not swept.
"""

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time

BS = 4096
MOD = 0xFFFFFFFF
TIMEOUT = 30
PATH_RE = re.compile(r"\S*/w\d+\.img")


def check_words(rest):
    """The Fletcher check words for the bytes after a block's first 8."""
    c0 = c1 = 0
    for (w,) in struct.iter_unpack("<I", rest):
        c0 = (c0 + w) % MOD
        c1 = (c1 + c0) % MOD
    k0 = MOD - (c0 + c1) % MOD
    k1 = MOD - (c0 + k0) % MOD
    return struct.pack("<II", k0, k1)


def is_object(block):
    return block != b"\xff" * BS and block[:8] == check_words(block[8:])


def read_blocks(path):
    """Yield (number, bytes) for every nonzero block of the image."""
    zero = bytes(BS)
    with open(path, "rb") as f:
        n = 0
        while True:
            b = f.read(BS)
            if not b:
                return
            if len(b) < BS:
                b += bytes(BS - len(b))
            if b != zero:
                yield n, b
            n += 1


def listing(path):
    """[(number, is object, type word, subtype word)] for the nonzero blocks."""
    with open(path, "rb") as f:
        b = f.read(BS)
    if not (len(b) == BS and is_object(b) and b[32:36] == b"NXSB"
            and struct.unpack_from("<I", b, 36)[0] == BS):
        raise SystemExit("%s: block 0 is not a container of %d-byte blocks" % (path, BS))
    out = []
    for n, b in read_blocks(path):
        if is_object(b):
            t, s = struct.unpack_from("<H", b, 24)[0], struct.unpack_from("<H", b, 28)[0]
            out.append((n, True, t, s))
        else:
            out.append((n, False, None, None))
    return out


def blocks_tsv(rows):
    lines = ["block\tobject\ttype\tsubtype"]
    for n, obj, t, s in rows:
        lines.append("%d\t%d\t%s\t%s" % (n, obj, "" if t is None else t, "" if s is None else s))
    return "\n".join(lines) + "\n"


def changes(rows):
    """Yield (block, offset, change) in sweep order."""
    for n, obj, _, _ in rows:
        if obj:
            yield n, 0, "check"
            for off in range(8, BS):
                yield n, off, "flip"
        else:
            for off in range(BS):
                yield n, off, "flip"


def mutated(block, obj, off, change):
    b = bytearray(block)
    b[off] ^= 0xFF
    if obj and change == "flip":
        b[:8] = check_words(bytes(b[8:]))
    return bytes(b)


class Sweep:
    def __init__(self, image, outdir, rows, fsck, budget):
        self.image = image
        self.outdir = outdir
        self.rows = {n: obj for n, obj, _, _ in rows}
        self.fsck = fsck
        self.deadline = time.monotonic() + budget
        self.lock = threading.Lock()
        self.todo = changes(rows)
        self.outputs = {}
        self.done = 0
        self.rewritten = 0
        self.results = open(os.path.join(outdir, "results.tsv"), "w", buffering=1)
        self.results.write("block\toffset\tchange\texit\tms\toutput\twrote\n")
        self.jsonl = open(os.path.join(outdir, "outputs.jsonl"), "w", buffering=1)
        with open(image, "rb") as f:
            self.original = {n: self._read(f, n) for n in self.rows}

    @staticmethod
    def _read(f, n):
        f.seek(n * BS)
        return f.read(BS)

    def output_id(self, text):
        with self.lock:
            if text not in self.outputs:
                self.outputs[text] = len(self.outputs)
                self.jsonl.write(json.dumps({"id": self.outputs[text], "text": text}) + "\n")
            return self.outputs[text]

    def check(self, copy):
        t0 = time.monotonic()
        try:
            p = subprocess.run([self.fsck, "-n", "-W", copy], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=TIMEOUT)
            status = str(p.returncode)
            text = (p.stdout.decode("utf-8", "replace") + "--- stderr\n"
                    + p.stderr.decode("utf-8", "replace"))
        except subprocess.TimeoutExpired as e:
            status = "timeout"
            text = ((e.stdout or b"").decode("utf-8", "replace") + "--- stderr\n"
                    + (e.stderr or b"").decode("utf-8", "replace"))
        ms = int((time.monotonic() - t0) * 1000)
        return status, ms, self.output_id(PATH_RE.sub("IMAGE", text))

    def take(self):
        with self.lock:
            if time.monotonic() >= self.deadline:
                return None
            return next(self.todo, None)

    def worker(self, copy):
        while True:
            item = self.take()
            if item is None:
                return
            n, off, change = item
            obj = self.rows[n]
            fd = os.open(copy, os.O_RDWR)
            try:
                os.pwrite(fd, mutated(self.original[n], obj, off, change), n * BS)
            finally:
                os.close(fd)
            before = os.stat(copy)
            status, ms, out = self.check(copy)
            after = os.stat(copy)
            wrote = (after.st_mtime_ns, after.st_size) != (before.st_mtime_ns, before.st_size)
            if wrote:
                copy_image(self.image, copy)
            else:
                fd = os.open(copy, os.O_RDWR)
                try:
                    os.pwrite(fd, self.original[n], n * BS)
                finally:
                    os.close(fd)
            with self.lock:
                self.results.write("%d\t%d\t%s\t%s\t%d\t%d\t%d\n"
                                   % (n, off, change, status, ms, out, wrote))
                self.done += 1
                self.rewritten += wrote


def copy_image(src, dst):
    if os.path.exists(dst):
        os.unlink(dst)
    if subprocess.run(["cp", "-c", src, dst], stderr=subprocess.DEVNULL).returncode != 0:
        shutil.copyfile(src, dst)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_shard(text):
    k, n = (int(x) for x in text.split("/"))
    if not 1 <= k <= n:
        raise SystemExit("shard %s: K must be between 1 and N" % text)
    return k, n


def run(image, outdir, objects_only, shard, budget, workers, fsck):
    os.makedirs(outdir, exist_ok=True)
    rows = [r for r in listing(image) if r[1] or not objects_only]
    k, n = shard
    rows = [r for i, r in enumerate(rows) if i % n == k - 1]
    with open(os.path.join(outdir, "blocks.tsv"), "w") as f:
        f.write(blocks_tsv(rows))
    work = tempfile.mkdtemp(prefix="mutate.", dir=os.environ.get("RUNNER_TEMP"))
    copies = [os.path.join(work, "w%d.img" % i) for i in range(workers)]
    for c in copies:
        copy_image(image, c)
    sweep = Sweep(image, outdir, rows, fsck, budget)
    t0 = time.monotonic()
    before = os.stat(copies[0])
    base_status, base_ms, base_out = sweep.check(copies[0])
    after = os.stat(copies[0])
    base_wrote = (after.st_mtime_ns, after.st_size) != (before.st_mtime_ns, before.st_size)
    if base_wrote:
        copy_image(image, copies[0])
    summary = {
        "image": os.path.basename(image),
        "image_sha256": sha256(image),
        "block_size": BS,
        "shard": "%d/%d" % shard,
        "objects_only": objects_only,
        "blocks": len(rows),
        "object_blocks": sum(1 for r in rows if r[1]),
        "changes": sum(1 for _ in changes(rows)),
        "workers": workers,
        "budget_seconds": budget,
        "base_exit": base_status,
        "base_ms": base_ms,
        "base_output": base_out,
        "base_wrote": base_wrote,
    }
    if base_status == "0":
        threads = [threading.Thread(target=sweep.worker, args=(c,)) for c in copies]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    summary.update({
        "done": sweep.done,
        "complete": sweep.done == summary["changes"],
        "rewritten_copies": sweep.rewritten,
        "distinct_outputs": len(sweep.outputs),
        "seconds": round(time.monotonic() - t0, 1),
    })
    sweep.results.close()
    sweep.jsonl.close()
    shutil.rmtree(work, ignore_errors=True)
    with open(os.path.join(outdir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1, sort_keys=True)
    print(json.dumps(summary, sort_keys=True))
    if base_status != "0":
        raise SystemExit("%s: the checker does not pass the unchanged image" % image)


def option(argv, name, default):
    if name in argv:
        i = argv.index(name)
        value = argv[i + 1]
        del argv[i:i + 2]
        return value
    return default


def main(argv):
    argv = list(argv)
    if len(argv) == 3 and argv[1] == "blocks":
        sys.stdout.write(blocks_tsv(listing(argv[2])))
    elif len(argv) >= 4 and argv[1] == "run":
        objects_only = "--objects" in argv
        if objects_only:
            argv.remove("--objects")
        shard = parse_shard(option(argv, "--shard", "1/1"))
        budget = float(option(argv, "--budget", "900"))
        workers = int(option(argv, "--workers", str(os.cpu_count() or 1)))
        fsck = option(argv, "--fsck", "fsck_apfs")
        if len(argv) != 4:
            raise SystemExit(__doc__)
        run(argv[2], argv[3], objects_only, shard, budget, workers, fsck)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
