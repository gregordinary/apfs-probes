#!/usr/bin/env python3
"""Waiting for a change of a volume's encryption, and the cryptogen probe's
verdict.

    cryptogenutil.py wait VDEV STATE LIMIT OUT.json
    cryptogenutil.py check OUTDIR

`wait` lists the APFS containers with `diskutil apfs list -plist` every two
seconds until the volume VDEV is listed as STATE, `encrypted` or `decrypted`,
with no conversion under way, on three listings in a row, or until LIMIT
seconds have passed. OUT.json keeps every listing's entry for the volume and
the lines `diskutil apfs list` prints about it, so a change that never ends
is recorded as it went. It exits 0 when the volume reached STATE, 1 if not.

`check` writes summary.json: for every stage the probe dumped, whether macOS
read back every file the probe wrote as written, with its attributes, link
target and hard link, and the exit status of the stage's encryption change,
its wait, and fsck_apfs.
"""

import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fsprobe  # noqa: E402

POLL = 2
STEADY = 3


def write_json(path, value):
    with open(path, "w") as f:
        json.dump(value, f, indent=1, sort_keys=True, ensure_ascii=True)
        f.write("\n")


def read_json(path):
    with open(path) as f:
        return json.load(f)


def listed_volume(vdev):
    """VDEV's entry in `diskutil apfs list -plist`, or None."""
    out = subprocess.run(["diskutil", "apfs", "list", "-plist"], capture_output=True,
                         timeout=60).stdout
    start = out.find(b"<?xml")
    if start < 0:
        return None
    for c in plistlib.loads(out[start:]).get("Containers", []):
        for v in c.get("Volumes", []):
            if v.get("DeviceIdentifier") == vdev:
                return v
    return None


def listed_lines(vdev):
    """The lines `diskutil apfs list` prints under volume VDEV."""
    out = subprocess.run(["diskutil", "apfs", "list"], capture_output=True, text=True,
                         timeout=60).stdout
    lines, inside = [], False
    for line in out.splitlines():
        head = re.search(r"\+-[-<>] (.*)", line)
        if head:
            inside = head.group(1).split()[:2] == ["Volume", vdev]
            continue
        if inside:
            text = line.strip(" |")
            if text and not text.startswith("---"):
                lines.append(text)
    return lines


def plain(value):
    """A plist value as JSON holds it."""
    if isinstance(value, dict):
        return dict((k, plain(v)) for k, v in value.items())
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)


def reached(volume, state):
    if volume is None or volume.get("CryptoMigrationOn"):
        return False
    return bool(volume.get("Encryption")) == (state == "encrypted")


def wait(vdev, state, limit, out):
    if state not in ("encrypted", "decrypted"):
        raise SystemExit("STATE is encrypted or decrypted")
    limit = int(limit)
    t0 = time.time()
    polls, steady, done = [], 0, False
    try:
        while True:
            entry = {"t": round(time.time() - t0, 1)}
            try:
                volume = listed_volume(vdev)
                entry["volume"] = plain(volume)
                entry["lines"] = listed_lines(vdev)
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                volume = None
                entry["error"] = "%s: %s" % (type(e).__name__, e)
            polls.append(entry)
            steady = steady + 1 if reached(volume, state) else 0
            if steady >= STEADY:
                done = True
                break
            if time.time() - t0 > limit:
                break
            time.sleep(POLL)
    finally:
        write_json(out, {"volume": vdev, "state": state, "limit": limit, "reached": done,
                         "seconds": round(time.time() - t0, 1), "polls": polls})
    print("%s %s after %.0f s, %d listings" % (vdev, state if done else "not " + state,
                                                time.time() - t0, len(polls)))
    raise SystemExit(0 if done else 1)


# -- the verdict ----------------------------------------------------------------

def sha(data):
    return hashlib.sha256(data).hexdigest()


# What the probe writes, as opsutil.py tree and opsutil.py write make it: the
# tree under tree/, a 1 MiB file, and from the fourth conversion stage on, one
# more file written after the volume was decrypted.
TREE_FILES = {
    "tree/a": b"",
    "tree/b": fsprobe.pattern(3 * 4096, 2),
    "tree/d/e": fsprobe.pattern(4096, 3),
    "tree/h": fsprobe.pattern(3 * 4096, 2),
    "data": fsprobe.pattern(1 << 20, 7),
}
LATER = ("later", fsprobe.pattern(65536, 8))
XATTRS = {
    "tree/b": ("user.small", fsprobe.pattern(16, 4)),
    "tree/d/e": ("user.big", fsprobe.pattern(8000, 5)),
}


def expected(stage):
    files = dict(TREE_FILES)
    parts = stage.split("-")
    if parts[0] == "conv" and parts[1].isdigit() and int(parts[1]) >= 4:
        files[LATER[0]] = LATER[1]
    return files


def content_problems(manifest_path, stage):
    m = read_json(manifest_path)
    by_path = dict((e["path"], e) for e in m.get("entries", []))
    problems = ["%s: %s" % (e.get("path_hex"), e.get("error")) for e in m.get("errors", [])]
    for path, data in sorted(expected(stage).items()):
        e = by_path.get(path)
        if e is None:
            problems.append("%s is missing" % path)
        elif e.get("sha256") != sha(data):
            problems.append("%s does not read as written" % path)
    for path, (name, value) in sorted(XATTRS.items()):
        x = (by_path.get(path) or {}).get("xattrs", {}).get(name)
        if not x or x.get("sha256") != sha(value):
            problems.append("%s's %s does not read as written" % (path, name))
    s = by_path.get("tree/s") or {}
    if s.get("target_hex") != b"b".hex():
        problems.append("tree/s does not point at b")
    b, h = by_path.get("tree/b") or {}, by_path.get("tree/h") or {}
    if not b.get("ino") or b.get("ino") != h.get("ino"):
        problems.append("tree/h is not a hard link to tree/b")
    return problems


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
    for name in [k[:-len("-dump")] for k in rcs if k.endswith("-dump")]:
        entry = {"stage": name}
        for k in ("encryptVolume", "decryptVolume", "wait", "fsck", "fsck-warnings"):
            if "%s-%s" % (name, k) in rcs:
                entry[k] = int(rcs["%s-%s" % (name, k)])
        path = os.path.join(outdir, "manifests", name + ".json")
        entry["manifest"] = os.path.exists(path)
        entry["problems"] = content_problems(path, name) if entry["manifest"] else []
        waited = os.path.join(outdir, "waits", name + ".json")
        if os.path.exists(waited):
            entry["reached"] = read_json(waited).get("reached")
        entry["image"] = os.path.exists(os.path.join(outdir, "images", name + ".bd"))
        entry["ok"] = bool(entry["manifest"] and not entry["problems"] and entry["image"]
                           and entry.get("fsck") == 0 and entry.get("reached", True)
                           and entry.get("encryptVolume", 0) == 0
                           and entry.get("decryptVolume", 0) == 0)
        summary.append(entry)
    write_json(os.path.join(outdir, "summary.json"), summary)
    for e in summary:
        print("%-20s %s%s" % (e["stage"], "ok" if e["ok"] else "FAILED",
                              "".join("; " + p for p in e["problems"])))
    print("%d of %d stages ok" % (sum(1 for e in summary if e["ok"]), len(summary)))


def main(argv):
    if len(argv) == 6 and argv[1] == "wait":
        wait(*argv[2:])
    elif len(argv) == 3 and argv[1] == "check":
        check(argv[2])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
