#!/usr/bin/env python3
"""Fill the containers under reserve/ until macOS refuses to write to them.

    reserveutil.py select RESERVE WORDS...
    reserveutil.py fill MNT TRIAL BLOCK-SIZE NAME OUT.json
    reserveutil.py one MNT BLOCK-SIZE NAME OUT.json
    reserveutil.py index RESERVE OUT

RESERVE is the reserve/ directory: images.json, which names every container,
its block count and the blocks its space manager records free, and one block
dump per container. OUT is the probe's output directory.

`select` prints one `NAME TRIAL` line for each trial WORDS asks for: with no
word, every trial images.json names; a block size word such as `b4k` keeps
the containers of that block size, a trial word (`block`, `mib`, `files`,
`user`) keeps that trial, a container's name keeps that container, and a
last word `K/N` keeps the Kth of N equal shares of what the others kept.

`fill` writes to the volume mounted at MNT until a call fails:

- `block`: one block of the volume's block size a call, each followed by
  fsync(2), into one file;
- `user`: the same, as whoever runs it, where `block` runs as root;
- `mib`: 1 MiB a call into one file with no fsync until a call fails, then
  fsync, then on into a second file a block a call as `block` does;
- `files`: empty files, one a call, until one cannot be created.

Each records the bytes or files taken, the call that failed and its errno,
statvfs(3) before the first call, every 32 calls and after the failure, and
then whether an empty file can still be created and a block still written
and synced. A trial stops on its own after 150 seconds or 400,000 calls.

`one` writes one block into a new file and syncs it, as the second volume
of a container another trial filled is asked to.

`index` gathers every trial's record into results.json with the release.
Every file's content is a repeating pattern with no zero bytes, named by
the container.
"""

import errno
import json
import os
import platform
import subprocess
import sys
import time

MIB = 1 << 20
LIMIT_SECS = 150
LIMIT_CALLS = 400000
EVERY = 32


def read_json(path):
    with open(path) as f:
        return json.load(f)


def write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f, indent=1, sort_keys=True, ensure_ascii=True)
        f.write("\n")


def images(reserve):
    return read_json(os.path.join(reserve, "images.json"))["images"]


TRIALS = ("block", "user", "mib", "files")


def select(reserve, words):
    shard = None
    if words and "/" in words[-1]:
        k, n = (int(x) for x in words[-1].split("/"))
        if not 1 <= k <= n:
            raise SystemExit("no share %d of %d" % (k, n))
        shard, words = (k, n), words[:-1]
    sizes = {w for w in words if w[0] == "b" and w.endswith("k") and w[1:-1].isdigit()}
    trials = {w for w in words if w in TRIALS}
    names = {i["name"] for i in images(reserve)}
    named = {w for w in words if w in names}
    unknown = set(words) - sizes - trials - named
    if unknown:
        raise SystemExit("no container or trial named %s" % " ".join(sorted(unknown)))
    chosen = []
    for image in images(reserve):
        if sizes and "b%dk" % (image["block_size"] // 1024) not in sizes:
            continue
        if named and image["name"] not in named:
            continue
        for trial in image["trials"]:
            if trials and trial not in trials:
                continue
            chosen.append((image["name"], trial))
    if shard:
        k, n = shard
        chosen = chosen[(k - 1) * len(chosen) // n:k * len(chosen) // n]
    for name, trial in chosen:
        print(name, trial)


def pattern(name, n):
    unit = ("%s: written by the reserve probe. " % name).encode()
    return (unit * (n // len(unit) + 1))[:n]


def space(path):
    s = os.statvfs(path)
    return {"bsize": s.f_bsize, "frsize": s.f_frsize, "blocks": s.f_blocks,
            "bfree": s.f_bfree, "bavail": s.f_bavail, "files": s.f_files,
            "ffree": s.f_ffree}


def errname(e):
    return errno.errorcode.get(e.errno, str(e.errno))


def try_create(mnt, name):
    """Whether an empty file can be created at MNT/NAME: 0, or the errno."""
    try:
        fd = os.open(os.path.join(mnt, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        os.close(fd)
        return 0
    except OSError as e:
        return errname(e)


def try_block(mnt, name, data):
    """Whether one block can be written to a new file MNT/NAME and synced: 0,
    or the call that failed and its errno."""
    stage = "open"
    try:
        fd = os.open(os.path.join(mnt, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            stage = "write"
            if os.write(fd, data) != len(data):
                return "write short"
            stage = "fsync"
            os.fsync(fd)
        finally:
            os.close(fd)
        return 0
    except OSError as e:
        return "%s %s" % (stage, errname(e))


def blocks_into(fd, data, rec, mnt, counter):
    """Write DATA a call, each synced, until a call fails or a limit is met;
    COUNTER names the record's tally."""
    t0 = time.time()
    while rec[counter] < LIMIT_CALLS and time.time() - t0 < LIMIT_SECS:
        stage = "write"
        try:
            n = os.write(fd, data)
            if n != len(data):
                rec["stopped"] = {"call": "write", "short": n, "at": rec[counter]}
                return
            stage = "fsync"
            os.fsync(fd)
        except OSError as e:
            rec["stopped"] = {"call": stage, "errno": errname(e), "at": rec[counter]}
            return
        rec[counter] += 1
        if rec[counter] % EVERY == 0:
            rec["space"].append([rec[counter], space(mnt)])
    rec["stopped"] = {"call": None, "limit": True, "at": rec[counter]}


def fill(mnt, trial, bs, name, out):
    bs = int(bs)
    block = pattern(name, bs)
    rec = {"trial": trial, "block_size": bs, "uid": os.getuid(), "before": space(mnt),
           "space": [], "started": time.time()}
    if trial in ("block", "user"):
        rec["blocks"] = 0
        fd = os.open(os.path.join(mnt, "fill-a"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            blocks_into(fd, block, rec, mnt, "blocks")
        finally:
            os.close(fd)
        rec["bytes"] = rec["blocks"] * bs
    elif trial == "mib":
        rec["mibs"] = 0
        rec["mib_bytes"] = 0
        chunk = pattern(name, MIB)
        fd = os.open(os.path.join(mnt, "fill-a"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            t0 = time.time()
            while rec["mibs"] < LIMIT_CALLS and time.time() - t0 < LIMIT_SECS:
                try:
                    n = os.write(fd, chunk)
                except OSError as e:
                    rec["mib_stopped"] = {"call": "write", "errno": errname(e), "at": rec["mibs"]}
                    break
                rec["mib_bytes"] += n
                if n != len(chunk):
                    rec["mib_stopped"] = {"call": "write", "short": n, "at": rec["mibs"]}
                    break
                rec["mibs"] += 1
            else:
                rec["mib_stopped"] = {"call": None, "limit": True, "at": rec["mibs"]}
            try:
                os.fsync(fd)
                rec["mib_fsync"] = 0
            except OSError as e:
                rec["mib_fsync"] = errname(e)
        finally:
            os.close(fd)
        rec["mib_space"] = space(mnt)
        rec["blocks"] = 0
        fd = os.open(os.path.join(mnt, "fill-b"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            blocks_into(fd, block, rec, mnt, "blocks")
        finally:
            os.close(fd)
        rec["bytes"] = rec["mib_bytes"] + rec["blocks"] * bs
    elif trial == "files":
        rec["files"] = 0
        os.mkdir(os.path.join(mnt, "many"))
        t0 = time.time()
        while rec["files"] < LIMIT_CALLS and time.time() - t0 < LIMIT_SECS:
            got = try_create(os.path.join(mnt, "many"), "f%06d" % rec["files"])
            if got != 0:
                rec["stopped"] = {"call": "open", "errno": got, "at": rec["files"]}
                break
            rec["files"] += 1
            if rec["files"] % (EVERY * 16) == 0:
                rec["space"].append([rec["files"], space(mnt)])
        else:
            rec["stopped"] = {"call": None, "limit": True, "at": rec["files"]}
    else:
        raise SystemExit("no trial %s" % trial)
    rec["after"] = space(mnt)
    rec["after_create"] = try_create(mnt, "after-empty")
    rec["after_block"] = try_block(mnt, "after-block", block)
    rec["final"] = space(mnt)
    rec["seconds"] = round(time.time() - rec.pop("started"), 3)
    write_json(out, rec)
    print(json.dumps({k: rec.get(k) for k in ("bytes", "files", "stopped", "after_create",
                                               "after_block")}, sort_keys=True))


def one(mnt, bs, name, out):
    rec = {"before": space(mnt), "block": try_block(mnt, "one-block", pattern(name, int(bs)))}
    rec["after"] = space(mnt)
    write_json(out, rec)
    print(json.dumps(rec["block"]))


def release():
    found = {"product": platform.mac_ver()[0] or None}
    try:
        found["build"] = subprocess.run(["sw_vers", "-buildVersion"], capture_output=True,
                                        text=True).stdout.strip() or None
    except OSError:
        found["build"] = None
    found["runner"] = os.environ.get("RUNNER_LABEL")
    found["image_version"] = os.environ.get("ImageVersion")
    found["arch"] = platform.machine()
    return found


def index(reserve, out):
    fills = {}
    fdir = os.path.join(out, "fills")
    for f in sorted(os.listdir(fdir)) if os.path.isdir(fdir) else []:
        fills[f[:-len(".json")]] = read_json(os.path.join(fdir, f))
    write_json(os.path.join(out, "results.json"), {
        "release": release(), "images": {i["name"]: i for i in images(reserve)},
        "fills": fills,
    })
    print("%d records" % len(fills))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "select" and len(argv) >= 3:
        select(argv[2], argv[3:])
    elif cmd == "fill" and len(argv) == 7:
        fill(*argv[2:])
    elif cmd == "one" and len(argv) == 6:
        one(*argv[2:])
    elif cmd == "index" and len(argv) == 4:
        index(argv[2], argv[3])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
