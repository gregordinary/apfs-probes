#!/usr/bin/env python3
"""The scenes of the clonerefs probe, and macOS's reading after each stage.

    clonerefsutil.py scenes
    clonerefsutil.py stages SCENE
    clonerefsutil.py stage MNT VDEV BLOCK-SIZE SCENE K OUT.json
    clonerefsutil.py check OUTDIR

A scene is a list of stages, each a list of operations on files at the root
of one volume: write a file of whole blocks, keep one compressed in its
resource fork, clone one with `cp -c`, overwrite one block, truncate, remove,
take a Time Machine snapshot, delete a snapshot. Every content is text fixed
by a seed, so the content each file must hold after any stage is known
without reading the volume.

`stage` makes stage K of SCENE on the volume mounted at MNT, after stages 1
to K-1 were made by earlier calls: it replays those in memory to know every
file's content and every snapshot's name, then makes stage K on the volume,
and records each operation's outcome, the snapshots macOS lists, and, for
every file, its inode number, size, flags, allocated blocks and whether macOS
reads it back as the content it must hold.

`check` writes summary.json: for every stage, whether every operation
succeeded, every file read back as expected, and fsck_apfs accepted the
image.
"""

import errno
import glob
import hashlib
import json
import os
import plistlib
import stat
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compressutil  # noqa: E402
import fsprobe  # noqa: E402
from fsprobe import text  # noqa: E402

STORED = 0xCC
# Files macOS writes on its own at the root of a volume it mounts.
HOUSEKEEPING = (".fseventsd", ".Spotlight-V100")

# Operations, by the stage that makes them. Sizes and positions are in the
# container's blocks; a seed fixes a content.
#   ("write", NAME, BLOCKS, SEED)      a new file of BLOCKS blocks of text
#   ("stored", NAME, BLOCKS, SEED)     a new file compressed as type 10, its
#                                      chunks stored in the resource fork
#   ("clone", SRC, DST)                cp -c SRC DST
#   ("over", NAME, BLOCK, SEED)        overwrite block BLOCK with one of text
#   ("trunc", NAME, BLOCKS)            truncate to BLOCKS blocks
#   ("rm", NAME)                       unlink
#   ("snapshot",)                      one Time Machine local snapshot
#   ("delete", K)                      delete the Kth snapshot taken
SCENES = [
    # A clone written since it was made, then its source removed: the blocks
    # the two shared are left to the clone alone.
    ("src-deleted", [
        [("write", "f", 8, 1), ("clone", "f", "g"), ("over", "g", 2, 2)],
        [("rm", "f")],
    ]),
    # The same, with the source overwritten and then emptied instead, so the
    # clone alone uses blocks its source wrote while the source still exists.
    ("src-overwritten", [
        [("write", "f", 8, 1), ("clone", "f", "g"), ("over", "g", 2, 2)],
        [("over", "f", 5, 3)],
        [("trunc", "f", 0)],
    ]),
    # A source written while its clone is not, so the two stop sharing one
    # data stream from the source's side; then the clone written, and the
    # source removed.
    ("src-first", [
        [("write", "f", 8, 1), ("clone", "f", "g"), ("over", "f", 5, 3)],
        [("over", "g", 2, 2)],
        [("rm", "f")],
    ]),
    # A clone made after a snapshot of blocks the snapshot holds, then both
    # files changed and removed while the snapshot keeps the blocks.
    ("after-snapshot", [
        [("write", "f", 8, 1), ("snapshot",)],
        [("clone", "f", "g")],
        [("over", "g", 2, 2)],
        [("over", "f", 5, 3)],
        [("rm", "f")],
        [("rm", "g")],
    ]),
    # A clone of a written clone, then the middle and the first removed.
    ("chain", [
        [("write", "f", 8, 1), ("clone", "f", "g"), ("over", "g", 2, 2), ("clone", "g", "h"),
         ("over", "h", 4, 3)],
        [("rm", "g")],
        [("rm", "f")],
    ]),
    # Two written clones of one source, the source removed, then one clone
    # written again.
    ("fan-out", [
        [("write", "f", 8, 1), ("clone", "f", "g"), ("clone", "f", "h"), ("over", "g", 2, 2),
         ("over", "h", 4, 3)],
        [("rm", "f")],
        [("over", "g", 5, 4)],
    ]),
    # Clones across two snapshots, then each snapshot deleted, oldest first.
    ("snapshot-delete", [
        [("write", "f", 8, 1), ("snapshot",)],
        [("clone", "f", "g"), ("over", "g", 2, 2), ("snapshot",)],
        [("rm", "f"), ("over", "g", 6, 3)],
        [("delete", 1)],
        [("delete", 2)],
    ]),
    # A clone of a file kept in its resource fork, the source removed, then
    # the clone written, which leaves it uncompressed.
    ("fork", [
        [("stored", "f", 50, 1), ("clone", "f", "g")],
        [("rm", "f")],
        [("over", "g", 1, 2)],
    ]),
]


def scene(name):
    for n, stages in SCENES:
        if n == name:
            return stages
    raise SystemExit("no scene named %s" % name)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    with open(path, "w") as f:
        json.dump(value, f, indent=1, sort_keys=True, ensure_ascii=True)
        f.write("\n")


def read_json(path):
    with open(path) as f:
        return json.load(f)


# -- the model: every file's content, from the operations alone --------------

def apply_model(files, op, bs):
    """Apply OP to FILES (name -> content). Snapshot operations change no file."""
    kind = op[0]
    if kind in ("write", "stored"):
        files[op[1]] = text(op[2] * bs, op[3])
    elif kind == "clone":
        files[op[2]] = files[op[1]]
    elif kind == "over":
        data = bytearray(files[op[1]])
        at = op[2] * bs
        data[at:at + bs] = text(bs, op[3])
        files[op[1]] = bytes(data)
    elif kind == "trunc":
        files[op[1]] = files[op[1]][:op[2] * bs]
    elif kind == "rm":
        del files[op[1]]


# -- the operations on the volume ---------------------------------------------

def clone(src, dst):
    proc = subprocess.run(["cp", "-c", src, dst], capture_output=True)
    if proc.returncode:
        raise OSError(errno.EIO, proc.stderr.decode("utf-8", "replace").strip())


def overwrite(path, at, data):
    with open(path, "r+b") as f:
        f.seek(at)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def stored(lib, path, data):
    chunks = [bytes([STORED]) + data[i:i + compressutil.CHUNK]
              for i in range(0, len(data), compressutil.CHUNK)]
    done = compressutil.store(lib, path, "fork", 10, len(data), chunks)
    if "error" in done or done.get("chflags") != "ok":
        raise OSError(errno.EIO, json.dumps(done, sort_keys=True))
    return done


def command(args):
    proc = subprocess.run(args, capture_output=True)
    out = {"args": args, "returncode": proc.returncode,
           "stdout": proc.stdout.decode("utf-8", "replace"),
           "stderr": proc.stderr.decode("utf-8", "replace")}
    if proc.returncode:
        raise OSError(errno.EIO, json.dumps(out, sort_keys=True))
    return out


def listed_snapshots(vdev):
    """The names of VDEV's snapshots, oldest transaction first."""
    proc = subprocess.run(["diskutil", "apfs", "listSnapshots", "-plist", vdev], capture_output=True)
    data = proc.stdout
    start = data.find(b"<?xml")
    found = plistlib.loads(data[start:]).get("Snapshots", []) if start >= 0 else []
    found.sort(key=lambda s: s.get("SnapshotXID") or 0)
    return [{"name": s.get("SnapshotName"), "xid": s.get("SnapshotXID")} for s in found]


def take_snapshot(vdev, taken):
    before = {s["name"] for s in listed_snapshots(vdev)}
    out = command(["tmutil", "localsnapshot"])
    # A snapshot's name carries its time to the second.
    time.sleep(1)
    new = [s["name"] for s in listed_snapshots(vdev) if s["name"] not in before]
    if len(new) != 1:
        raise OSError(errno.EIO, "the volume lists %d new snapshots" % len(new))
    taken.append(new[0])
    out["name"] = new[0]
    return out


def make(root, vdev, bs, op, taken, lib):
    """Make OP on the volume at ROOT."""
    p = lambda name: os.path.join(root, name)  # noqa: E731
    kind = op[0]
    if kind == "write":
        fsprobe.write_file(p(op[1]), text(op[2] * bs, op[3]))
    elif kind == "stored":
        return stored(lib, p(op[1]).encode(), text(op[2] * bs, op[3]))
    elif kind == "clone":
        clone(p(op[1]), p(op[2]))
    elif kind == "over":
        overwrite(p(op[1]), op[2] * bs, text(bs, op[3]))
    elif kind == "trunc":
        os.truncate(p(op[1]), op[2] * bs)
    elif kind == "rm":
        os.unlink(p(op[1]))
    elif kind == "snapshot":
        return take_snapshot(vdev, taken)
    elif kind == "delete":
        name = taken[op[1] - 1]
        out = command(["diskutil", "apfs", "deleteSnapshot", vdev, "-name", name])
        out["name"] = name
        return out
    else:
        raise ValueError("no operation %r" % (kind,))
    return None


def read_all(path):
    with open(path, "rb") as f:
        return f.read()


def model(files, op, bs, problems):
    """Apply OP to the model, noting an operation the model cannot follow."""
    try:
        apply_model(files, op, bs)
    except KeyError as e:
        problems.append("the model holds no %s for %s" % (e, " ".join(str(x) for x in op)))


def replay(directory, stages, k, bs, problems):
    """The files and snapshot names after stages 1 to K-1, from the operations
    each stage's record says were made."""
    files, taken = {}, []
    for j in range(1, k):
        try:
            prior = read_json(os.path.join(directory, "s%d.json" % j))
        except (OSError, ValueError):
            problems.append("stage %d left no record, so its operations are taken as made" % j)
            prior = None
        for i, op in enumerate(stages[j - 1]):
            if prior is None or prior["ops"][i]["ok"]:
                model(files, op, bs, problems)
        if prior is not None:
            taken = prior["taken"]
    return files, taken


def read_back(mnt, files, problems):
    """Every entry at the root as macOS reports it, held to FILES."""
    seen = {}
    for entry in sorted(os.listdir(mnt)):
        if entry in HOUSEKEEPING:
            continue
        full = os.path.join(mnt, entry)
        st = os.lstat(full)
        flags = getattr(st, "st_flags", 0)
        seen[entry] = {"ino": st.st_ino, "mode": st.st_mode, "size": st.st_size,
                       "flags": flags, "blocks": st.st_blocks,
                       "compressed": bool(flags & stat.UF_COMPRESSED)}
        if stat.S_ISREG(st.st_mode):
            try:
                seen[entry]["sha256"] = sha(read_all(full))
            except OSError as e:
                seen[entry]["read_error"] = errno.errorcode.get(e.errno, str(e.errno))
    for f, data in sorted(files.items()):
        if f not in seen:
            problems.append("%s is missing" % f)
        elif seen[f].get("sha256") != sha(data):
            problems.append("%s does not read as written" % f)
        else:
            seen[f]["expected"] = True
    for f in sorted(set(seen) - set(files)):
        problems.append("%s is not one of the scene's files" % f)
    return seen


def stage(mnt, vdev, bs, name, k, out):
    bs, k = int(bs), int(k)
    stages = scene(name)
    if not 1 <= k <= len(stages):
        raise SystemExit("%s has no stage %d" % (name, k))
    problems = []
    files, taken = replay(os.path.dirname(out), stages, k, bs, problems)
    log = fsprobe.Log()
    record = {"scene": name, "stage": k, "block_size": bs, "ops": log.ops, "taken": taken,
              "problems": problems}
    try:
        lib = compressutil.libc()
        for op in stages[k - 1]:
            if log.run(" ".join(str(x) for x in op), make, mnt, vdev, bs, op, taken, lib):
                model(files, op, bs, problems)
        os.sync()
        record["files"] = read_back(mnt, files, problems)
        record["snapshots"] = listed_snapshots(vdev)
    except Exception as e:  # recorded, so the stage always leaves its record
        problems.append("the stage stopped: %s: %s" % (type(e).__name__, e))
    finally:
        write_json(out, record)


# -- the verdict ----------------------------------------------------------------

def steps(outdir):
    out = {}
    with open(os.path.join(outdir, "steps.tsv")) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[0] != "#":
                out[parts[0]] = parts[1]
    return out


def check(outdir):
    rcs = steps(outdir)
    summary = []
    for name, stages in SCENES:
        for k in range(1, len(stages) + 1):
            p = "%s-s%d" % (name, k)
            path = os.path.join(outdir, "scenes", name, "s%d.json" % k)
            entry = {"scene": name, "stage": k, "ran": os.path.exists(path)}
            if entry["ran"]:
                s = read_json(path)
                entry["ops_ok"] = all(op["ok"] for op in s["ops"])
                entry["problems"] = s["problems"]
                entry["snapshots"] = len(s["snapshots"])
            entry["fsck"] = rcs.get(p + "-fsck")
            entry["fsck_warnings"] = rcs.get(p + "-fsck-warnings")
            entry["image"] = os.path.exists(os.path.join(outdir, "images", p + ".bd"))
            entry["ok"] = bool(entry["ran"] and entry["ops_ok"] and not entry["problems"]
                               and entry["fsck"] == "0" and entry["image"])
            if entry["ran"] or p + "-fsck" in rcs:
                summary.append(entry)
    write_json(os.path.join(outdir, "summary.json"), summary)
    bad = [e for e in summary if not e["ok"]]
    for e in summary:
        print("%-24s s%d %s" % (e["scene"], e["stage"], "ok" if e["ok"] else "FAILED"))
    print("%d of %d stages ok" % (len(summary) - len(bad), len(summary)))


def main(argv):
    if len(argv) == 2 and argv[1] == "scenes":
        for name, _ in SCENES:
            print(name)
    elif len(argv) == 3 and argv[1] == "stages":
        print(len(scene(argv[2])))
    elif len(argv) == 8 and argv[1] == "stage":
        stage(*argv[2:])
    elif len(argv) == 3 and argv[1] == "check":
        check(argv[2])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
