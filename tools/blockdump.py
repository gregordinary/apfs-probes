#!/usr/bin/env python3
"""Store an image's nonzero blocks compactly, and rebuild the image from them.

    blockdump.py dump IMAGE OUT [--block-size N]
    blockdump.py restore IN IMAGE
    blockdump.py info IN
    blockdump.py seek-support DIR

OUT is an xz stream holding an 8-byte magic, the image size and the block
size, then runs of consecutive nonzero blocks, each a (first block, count)
header followed by the blocks' bytes, ended by a run whose count is zero.
A block that reads as all zeros is left out, so an image restored from a dump
reads back byte for byte as the original did, holes included.

Where the host reports holes through SEEK_DATA and SEEK_HOLE, only the data
regions are read, which keeps a sparse terabyte-sized image cheap to dump.
"""

import errno
import hashlib
import json
import lzma
import os
import struct
import sys

MAGIC = b"BLKDUMP1"
HEADER = struct.Struct("<8sQI")
RUN = struct.Struct("<QI")
MAX_RUN = 4096
CHUNK = 1 << 20

if sys.platform == "darwin":
    SEEK_HOLE = getattr(os, "SEEK_HOLE", 3)
    SEEK_DATA = getattr(os, "SEEK_DATA", 4)
else:
    SEEK_DATA = getattr(os, "SEEK_DATA", 3)
    SEEK_HOLE = getattr(os, "SEEK_HOLE", 4)


def seek_supported(fd):
    """Whether this file descriptor answers SEEK_DATA."""
    try:
        os.lseek(fd, 0, SEEK_DATA)
    except OSError as e:
        if e.errno == errno.ENXIO:
            return True
        return False
    return True


def data_ranges(fd, size):
    """Yield the (start, end) byte ranges that may hold data."""
    if not seek_supported(fd):
        yield 0, size
        return
    off = 0
    while off < size:
        try:
            start = os.lseek(fd, off, SEEK_DATA)
        except OSError as e:
            if e.errno == errno.ENXIO:
                return
            raise
        end = os.lseek(fd, start, SEEK_HOLE)
        yield start, min(end, size)
        off = end


def nonzero_runs(fd, size, bs):
    """Yield (first block, [blocks]) for runs of consecutive nonzero blocks."""
    zero = bytes(bs)
    first, blocks = None, []
    for start, end in data_ranges(fd, size):
        b0 = start // bs
        b1 = (end + bs - 1) // bs
        os.lseek(fd, b0 * bs, os.SEEK_SET)
        index = b0
        while index < b1:
            want = min(CHUNK, (b1 - index) * bs)
            buf = os.read(fd, want)
            if not buf:
                break
            if len(buf) % bs:
                buf += bytes(bs - len(buf) % bs)
            for i in range(0, len(buf), bs):
                block = buf[i:i + bs]
                if block == zero:
                    if blocks:
                        yield first, blocks
                        first, blocks = None, []
                else:
                    if blocks and (first + len(blocks) != index or len(blocks) == MAX_RUN):
                        yield first, blocks
                        first, blocks = None, []
                    if not blocks:
                        first = index
                    blocks.append(block)
                index += 1
    if blocks:
        yield first, blocks


def dump(image, out, bs):
    fd = os.open(image, os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size
        seekable = seek_supported(fd)
        digest = hashlib.sha256()
        count = runs = 0
        with lzma.open(out, "wb", preset=6) as f:
            f.write(HEADER.pack(MAGIC, size, bs))
            for first, blocks in nonzero_runs(fd, size, bs):
                head = RUN.pack(first, len(blocks))
                f.write(head)
                digest.update(head)
                for block in blocks:
                    f.write(block)
                    digest.update(block)
                count += len(blocks)
                runs += 1
            f.write(RUN.pack(0, 0))
    finally:
        os.close(fd)
    return {
        "image_size": size,
        "block_size": bs,
        "nonzero_blocks": count,
        "runs": runs,
        "content_sha256": digest.hexdigest(),
        "seek_data": seekable,
    }


def read_dump(path):
    with lzma.open(path, "rb") as f:
        magic, size, bs = HEADER.unpack(f.read(HEADER.size))
        if magic != MAGIC:
            raise SystemExit("%s: not a block dump" % path)
        yield size, bs
        while True:
            first, n = RUN.unpack(f.read(RUN.size))
            if n == 0:
                return
            yield first, f.read(n * bs)


def restore(path, image):
    it = read_dump(path)
    size, bs = next(it)
    with open(image, "wb") as out:
        out.truncate(size)
        for first, data in it:
            out.seek(first * bs)
            out.write(data)


def info(path):
    it = read_dump(path)
    size, bs = next(it)
    runs = [(first, len(data) // bs) for first, data in it]
    return {
        "image_size": size,
        "block_size": bs,
        "nonzero_blocks": sum(n for _, n in runs),
        "runs": [[first, n] for first, n in runs],
    }


def main(argv):
    if len(argv) >= 3 and argv[1] == "dump":
        bs = 4096
        if "--block-size" in argv:
            bs = int(argv[argv.index("--block-size") + 1])
        meta = dump(argv[2], argv[3], bs)
        meta["source"] = os.path.basename(argv[2])
        with open(argv[3] + ".json", "w") as f:
            json.dump(meta, f, indent=1, sort_keys=True)
        print(json.dumps(meta, sort_keys=True))
    elif len(argv) == 4 and argv[1] == "restore":
        restore(argv[2], argv[3])
    elif len(argv) == 3 and argv[1] == "info":
        print(json.dumps(info(argv[2]), indent=1))
    elif len(argv) == 3 and argv[1] == "seek-support":
        probe = os.path.join(argv[2], "seek-probe.bin")
        with open(probe, "wb") as f:
            f.truncate(64 << 20)
            f.seek(32 << 20)
            f.write(b"x" * 4096)
        fd = os.open(probe, os.O_RDONLY)
        try:
            ranges = list(data_ranges(fd, 64 << 20)) if seek_supported(fd) else None
        finally:
            os.close(fd)
            os.unlink(probe)
        print(json.dumps({"seek_data": ranges is not None, "ranges": ranges}))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
