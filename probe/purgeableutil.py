#!/usr/bin/env python3
"""Change the objects that describe a volume's snapshots, a few bits at a time,
and keep what `diskutil apfs listSnapshots` reports of each snapshot.

    purgeableutil.py words IMAGE
    purgeableutil.py sweep IMAGE OUTDIR [--budget SECONDS] [--slice K/N] [--words FILE]
    purgeableutil.py words-of LIST.plist

The image is read as 4096-byte blocks, and an object is recognised as
probe/mutate.py recognises one: by its Fletcher check words, with its type in
the 16-bit word at byte 24 and its subtype in the one at byte 28. The swept
objects are those of the newest transaction among objects of their kind, of
these kinds: an object map (type 0x0B), a volume superblock (0x0D, the
volume's own and a snapshot's copy alike), the object holding a snapshot's
identifier (0x1D), and the root node (type 2) of a tree of extent references
(subtype 0x0F), of snapshots (0x10) or of an object map's snapshots (0x13).
Bytes 0 to 31, the check words and the object's identifier, transaction and
type, are left alone.

`words` lists the 32-bit words swept, as block and offset. In a volume
superblock that is every word from byte 32 to 64 bytes past the later of its
last nonzero byte and byte 1024. Elsewhere it is every word that is nonzero
or lies within 16 bytes of a nonzero word, so that a node's free space is
passed over and the fields between a record's values are not.

`sweep` makes tries. A try clones the image (`cp -c`), inverts the bits of a
mask in one word of the clone, writes the object's check words again, attaches
the clone, mounts the first volume of its container read-only, and records
what `diskutil apfs listSnapshots -plist` lists for that volume. The first
pass inverts all 32 bits of each word, the identifier object, the snapshot
tree, the object map's snapshot tree and the snapshot's superblock first. The second pass
splits the mask of every word whose listing differed from the unchanged
image's into two halves, tries each, and splits again each half whose listing
differs, down to single bits. It takes first the words whose snapshots were
listed differently, then those where nothing could be listed, a word that held
less than 65536 before one that held more. Neither pass takes a new word once
--budget seconds have passed.

--words FILE sweeps only the words FILE lists, a block and an offset a line
after a header, such as those an earlier sweep found but did not split.

--slice K/N sweeps the Kth of N runs of consecutive words, counted from 1, in
the order words lists them, so the whole sweep is N jobs and a runner lost to
one of them loses that run alone. Every 25 tries, and at the start and end,
the sweep records what the runner holds: images attached, disk nodes, mounts,
the free space where the tries are made, and the load.

OUTDIR receives:

    words.tsv       the swept words
    tries.jsonl     one line per try: block, offset, mask, seconds, and what
                    was listed, as {"snapshots": [...]} or {"error": STAGE}
    health.jsonl    what the runner held, every 25 tries
    summary.json    the unchanged image's listing, the number of words swept
                    and of tries, and every single-bit try whose listing
                    differs from the unchanged image's

`words-of` prints the listing of LIST.plist as the sweep records one.
"""

import json
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mutate  # noqa: E402

BS = mutate.BS
OBJECT_MAP = 0x0B
VOLUME_SUPERBLOCK = 0x0D
SNAPSHOT_EXTENSION = 0x1D
TREE_ROOT = 0x02
TREE_SUBTYPES = (0x0F, 0x10, 0x13)
APFS_CONTAINER = "EF57347C-0000-11AA-AA11-00306543ECAC"


def block_at(path, n):
    with open(path, "rb") as f:
        f.seek(n * BS)
        return f.read(BS)


def swept_blocks(path):
    """[(block, kind)] of the objects swept, kind being "type/subtype"."""
    found = {}
    for n, obj, t, s in mutate.listing(path):
        if not obj:
            continue
        if t in (OBJECT_MAP, VOLUME_SUPERBLOCK, SNAPSHOT_EXTENSION):
            kind = (t, 0)
        elif t == TREE_ROOT and s in TREE_SUBTYPES:
            kind = (t, s)
        else:
            continue
        xid = struct.unpack_from("<Q", block_at(path, n), 16)[0]
        found.setdefault(kind, []).append((xid, n))
    out = []
    for kind, blocks in sorted(found.items()):
        newest = max(x for x, _ in blocks)
        out += [(n, "%#x/%#x" % kind) for x, n in blocks if x == newest]
    return sorted(out)


def words(path):
    """[(block, offset, kind)] of the words swept."""
    out = []
    for n, kind in swept_blocks(path):
        b = block_at(path, n)
        last = max(i for i in range(BS) if b[i])
        if kind.startswith("%#x/" % VOLUME_SUPERBLOCK):
            end = min(BS, (max(last, 1024) + 64) // 4 * 4)
            offs = range(32, end, 4)
        else:
            nonzero = [o for o in range(32, BS, 4) if b[o:o + 4] != bytes(4)]
            near = set()
            for o in nonzero:
                near.update(range(max(32, o - 16), min(BS, o + 20), 4))
            offs = sorted(near)
        out += [(n, o, kind) for o in offs]
    return out


def run(cmd, timeout=60):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout)


def load_plist(data):
    start = data.find(b"<?xml")
    if start < 0:
        raise ValueError("no plist")
    return plistlib.loads(data[start:])


def normal(value):
    """A plist value as JSON holds it."""
    if isinstance(value, dict):
        return {k: normal(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [normal(v) for v in value]
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def listed(snapshots_plist):
    return {"snapshots": [normal(s) for s in snapshots_plist.get("Snapshots", [])]}


def detach(dev):
    for _ in range(5):
        try:
            if run(["hdiutil", "detach", dev]).returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(2)
    try:
        run(["hdiutil", "detach", "-force", dev])
    except subprocess.TimeoutExpired:
        pass


def listing_of(img, mnt):
    """What is listed for the first volume of the container in IMG, mounted
    read-only at MNT."""
    try:
        r = run(["hdiutil", "attach", "-plist", "-nomount",
                 "-imagekey", "diskimage-class=CRawDiskImage", img], 120)
    except subprocess.TimeoutExpired:
        return {"error": "attach timeout"}
    if r.returncode != 0:
        return {"error": "attach"}
    entities = load_plist(r.stdout).get("system-entities", [])
    whole = [e["dev-entry"] for e in entities
             if re.fullmatch(r"/dev/disk[0-9]+", e.get("dev-entry", ""))
             and e.get("content-hint") != APFS_CONTAINER]
    if not whole:
        return {"error": "no image disk"}
    dev = whole[0]
    try:
        vdev = None
        for _ in range(10):
            r = run(["diskutil", "apfs", "list", "-plist"])
            if r.returncode == 0:
                for c in load_plist(r.stdout).get("Containers", []):
                    stores = [s.get("DeviceIdentifier", "") for s in c.get("PhysicalStores", [])]
                    base = os.path.basename(dev)
                    if any(s == base or s.startswith(base + "s") for s in stores):
                        vols = c.get("Volumes", [])
                        if vols:
                            vdev = vols[0]["DeviceIdentifier"]
            if vdev:
                break
            time.sleep(1)
        if vdev is None:
            return {"error": "no volume"}
        os.makedirs(mnt, exist_ok=True)
        r = run(["sudo", "-n", "diskutil", "mount", "readOnly", "nobrowse", "-mountPoint", mnt,
                 vdev], 120)
        if r.returncode != 0:
            return {"error": "mount"}
        r = run(["diskutil", "apfs", "listSnapshots", "-plist", vdev])
        if r.returncode != 0:
            return {"error": "listSnapshots",
                    "text": (r.stdout + r.stderr).decode("utf-8", "replace")[-400:]}
        return listed(load_plist(r.stdout))
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    finally:
        detach(dev)


def leftovers(img):
    """Force every disk still attached from IMG off: a try whose volume would not
    mount can leave its image attached past `hdiutil detach`, and attached
    images that pile up slow every later try until the runner is lost."""
    try:
        r = run(["hdiutil", "info", "-plist"])
        found = load_plist(r.stdout).get("images", [])
    except (subprocess.TimeoutExpired, ValueError):
        return
    for image in found:
        if image.get("image-path") not in (img, os.path.realpath(img)):
            continue
        devs = sorted((e.get("dev-entry", "") for e in image.get("system-entities", [])),
                      key=len)
        for dev in devs[:1]:
            try:
                run(["hdiutil", "detach", "-force", dev])
            except subprocess.TimeoutExpired:
                pass


def health(work, count, started):
    """What the runner holds after COUNT tries: a growing count across a run is
    something the tries do not give back."""
    out = {"tries": count, "seconds": round(time.time() - started)}
    try:
        r = run(["hdiutil", "info"])
        out["attached"] = r.stdout.count(b"image-path")
    except subprocess.TimeoutExpired:
        out["attached"] = "timeout"
    out["disk_nodes"] = len([d for d in os.listdir("/dev") if d.startswith("disk")])
    try:
        out["mounts"] = len(run(["mount"]).stdout.splitlines())
    except subprocess.TimeoutExpired:
        out["mounts"] = "timeout"
    st = os.statvfs(work)
    out["free_mb"] = st.f_bavail * st.f_frsize // (1 << 20)
    out["load"] = round(os.getloadavg()[0], 2)
    return out


def changed_copy(base, img, n, off, mask):
    if os.path.exists(img):
        os.remove(img)
    if run(["cp", "-c", base, img]).returncode != 0:
        shutil.copyfile(base, img)
    if mask:
        b = bytearray(block_at(base, n))
        w = struct.unpack_from("<I", b, off)[0] ^ mask
        struct.pack_into("<I", b, off, w)
        b[:8] = mutate.check_words(bytes(b[8:]))
        with open(img, "r+b") as f:
            f.seek(n * BS)
            f.write(b)


def sweep(base, out, budget, part=None, only=None):
    os.makedirs(out, exist_ok=True)
    ws = words(base)
    with open(os.path.join(out, "words.tsv"), "w") as f:
        f.write("block\toffset\tkind\n")
        for n, o, k in ws:
            f.write("%d\t%d\t%s\n" % (n, o, k))
    if only is not None:
        ws = [w for w in ws if (w[0], w[1]) in only]
    if part:
        k, parts = part
        ws = ws[len(ws) * (k - 1) // parts:len(ws) * k // parts]
    work = tempfile.mkdtemp(prefix="purgeable.", dir=os.environ.get("RUNNER_TEMP"))
    img = os.path.join(work, "try.img")
    tries = open(os.path.join(out, "tries.jsonl"), "w")
    health_log = open(os.path.join(out, "health.jsonl"), "w")
    count = 0
    began = time.time()

    def record():
        h = health(work, count, began)
        health_log.write(json.dumps(h) + "\n")
        health_log.flush()
        print("health " + json.dumps(h), flush=True)

    def attempt(n, off, mask):
        nonlocal count
        t0 = time.time()
        changed_copy(base, img, n, off, mask)
        got = listing_of(img, os.path.join(work, "mnt"))
        leftovers(img)
        count += 1
        tries.write(json.dumps({"block": n, "offset": off, "mask": mask,
                                "seconds": round(time.time() - t0, 2), "listed": got}) + "\n")
        tries.flush()
        if count % 25 == 0:
            record()
        return got

    record()

    unchanged = attempt(0, 0, 0)
    again = attempt(0, 0, 0)
    differing = []
    started = time.time()
    stopped = None
    snapshot_kinds = ("%#x/0" % SNAPSHOT_EXTENSION, "%#x/0x10" % TREE_ROOT, "%#x/0x13" % TREE_ROOT)
    copy_type = struct.pack("<I", 0x40000000 | VOLUME_SUPERBLOCK)
    order = sorted(ws, key=lambda w: (
        0 if w[2] in snapshot_kinds else 1 if block_at(base, w[0])[24:28] == copy_type else 2,
        w[0], w[1]))
    whole = []
    for n, off, _ in order:
        if time.time() - started > budget:
            stopped = "budget in the first pass"
            break
        got = attempt(n, off, 0xFFFFFFFF)
        if got != unchanged:
            held = struct.unpack_from("<I", block_at(base, n), off)[0]
            whole.append(("error" in got, held >= 0x10000, n, off))
    whole.sort()
    split = 0
    for _, _, n, off in whole:
        if stopped:
            break
        if time.time() - started > budget:
            stopped = "budget in the second pass"
            break
        split += 1
        bits = list(range(32))
        pending = [bits[16:], bits[:16]]
        while pending:
            group = pending.pop()
            mask = sum(1 << i for i in group)
            got = attempt(n, off, mask)
            if got == unchanged:
                continue
            if len(group) == 1:
                differing.append({"block": n, "offset": off, "bit": group[0], "listed": got})
                continue
            half = len(group) // 2
            pending += [group[half:], group[:half]]
    record()
    tries.close()
    health_log.close()
    shutil.rmtree(work, ignore_errors=True)
    summary = {"unchanged": unchanged, "unchanged_again_same": unchanged == again,
               "slice": "%d/%d" % part if part else None,
               "words": len(ws), "words_differing_whole": len(whole), "words_split": split,
               "tries": count, "stopped": stopped,
               "seconds": round(time.time() - started), "differing_bits": differing}
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps({k: v for k, v in summary.items() if k != "differing_bits"}))
    print("%d single bits change what is listed" % len(differing))


def main(argv):
    if len(argv) == 3 and argv[1] == "words":
        for n, o, k in words(argv[2]):
            print("%d\t%d\t%s" % (n, o, k))
    elif len(argv) >= 4 and argv[1] == "sweep":
        budget = 3600.0
        part = None
        only = None
        rest = argv[4:]
        while rest:
            if len(rest) < 2:
                raise SystemExit(__doc__)
            if rest[0] == "--budget":
                budget = float(rest[1])
            elif rest[0] == "--slice":
                m = re.fullmatch(r"([0-9]+)/([0-9]+)", rest[1])
                if not m or not 1 <= int(m.group(1)) <= int(m.group(2)):
                    raise SystemExit("--slice is K/N, with K from 1 to N")
                part = (int(m.group(1)), int(m.group(2)))
            elif rest[0] == "--words":
                with open(rest[1]) as f:
                    only = {tuple(int(v) for v in line.split("\t")[:2])
                            for line in f.read().splitlines()[1:] if line.strip()}
            else:
                raise SystemExit(__doc__)
            rest = rest[2:]
        sweep(argv[2], argv[3], budget, part, only)
    elif len(argv) == 3 and argv[1] == "words-of":
        with open(argv[2], "rb") as f:
            print(json.dumps(listed(load_plist(f.read())), indent=1))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
