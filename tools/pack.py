#!/usr/bin/env python3
"""Pack one probe's run artifacts into the archive a release attaches.

    pack.py OUT.tar.xz DIR...

Each DIR is one runner's artifact from a run of the probe workflow, as
`gh run download` writes it, and becomes a top-level directory of the
archive under its own name. Each block dump is checked against the
content_sha256 in the .json beside it and stored decompressed, so the
archive's single xz stream compresses a block that several images share
once; blockdump.py restore reads a dump in either form. Members are sorted
and carry zero times and owners, so the same artifacts pack to the same
bytes. Unpacking takes about 256 MiB of memory for the xz dictionary.
"""

import json
import lzma
import os
import shutil
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blockdump  # noqa: E402

FILTERS = [{
    "id": lzma.FILTER_LZMA2,
    "preset": 9,
    "dict_size": 256 << 20,
}]
CHECKED = ("image_size", "block_size", "nonzero_blocks", "content_sha256")


def top_name(path):
    return os.path.basename(os.path.normpath(path))


def walk(top):
    """Yield (archive name, path, is a directory) for top and all under it, sorted."""
    base = top_name(top)
    for root, dirs, files in os.walk(top):
        for d in dirs:
            if os.path.islink(os.path.join(root, d)):
                raise SystemExit("%s: a symbolic link" % os.path.join(root, d))
        dirs.sort()
        rel = os.path.relpath(root, top)
        name = base if rel == os.curdir else base + "/" + rel.replace(os.sep, "/")
        yield name, root, True
        for f in sorted(files):
            yield name + "/" + f, os.path.join(root, f), False


def entry(name, size=0, directory=False):
    info = tarfile.TarInfo(name)
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    if directory:
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
    else:
        info.size = size
        info.mode = 0o644
    return info


def add_dump(tar, name, path):
    """Check the dump at path against its .json and add it decompressed."""
    meta = path + ".json"
    if not os.path.isfile(meta):
        raise SystemExit("%s: no %s beside it" % (path, os.path.basename(meta)))
    with open(meta) as f:
        want = json.load(f)
    got = blockdump.info(path)
    for key in CHECKED:
        if got[key] != want[key]:
            raise SystemExit("%s: %s is %s, and its .json records %s"
                             % (path, key, got[key], want[key]))
    with tempfile.TemporaryFile() as tmp:
        with blockdump.open_dump(path) as f:
            shutil.copyfileobj(f, tmp, blockdump.CHUNK)
        size = tmp.tell()
        tmp.seek(0)
        tar.addfile(entry(name, size), tmp)


def pack(out, dirs):
    for d in dirs:
        if not os.path.isdir(d):
            raise SystemExit("%s: not a directory" % d)
    names = [top_name(d) for d in dirs]
    if len(set(names)) != len(names):
        raise SystemExit("two directories have the same name")
    part = out + ".part"
    files = dumps = 0
    try:
        with lzma.open(part, "wb", format=lzma.FORMAT_XZ,
                       check=lzma.CHECK_CRC64, filters=FILTERS) as xz:
            with tarfile.open(fileobj=xz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for d in sorted(dirs, key=top_name):
                    for name, path, is_dir in walk(d):
                        if is_dir:
                            tar.addfile(entry(name, directory=True))
                        elif os.path.islink(path) or not os.path.isfile(path):
                            raise SystemExit("%s: not a regular file" % path)
                        elif path.endswith(".bd"):
                            add_dump(tar, name, path)
                            dumps += 1
                        else:
                            with open(path, "rb") as f:
                                tar.addfile(entry(name, os.fstat(f.fileno()).st_size), f)
                            files += 1
        os.replace(part, out)
    except BaseException:
        if os.path.exists(part):
            os.unlink(part)
        raise
    print("%s: %d dumps and %d other files from %s, %d bytes"
          % (out, dumps, files, " ".join(sorted(names)), os.path.getsize(out)))


def main(argv):
    if len(argv) < 3 or not argv[1].endswith(".tar.xz"):
        raise SystemExit(__doc__)
    pack(argv[1], argv[2:])


if __name__ == "__main__":
    main(sys.argv)
