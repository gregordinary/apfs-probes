#!/usr/bin/env python3
"""Small readings for the reformat probe, from the image and from what Apple's
tools printed.

    reformatutil.py layout IMAGE
        gpt if IMAGE holds a GUID partition table's header at its second
        512-byte sector, raw if not
    reformatutil.py xid FSCK.out...
        the transaction of the checkpoint fsck_apfs says it checks, from the
        first of the logs that names one
    reformatutil.py summary OUTDIR
        summary.json: for the aged images and every route, the container
        diskutil listed and its volumes, and what fsck_apfs checked

`summary` reads listings.tsv, where the probe names, for each image it
examines, the listing it took and the container on the image, and the
fsck_apfs logs of the same name. A route's container is the old one again
when diskutil lists the old container's UUID for it.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disks  # noqa: E402

# The steps that make a route's container, by the suffix the probe gives them.
ROUTE_STEPS = ("newfs", "newfs-root", "erasevolume", "erasedisk", "partitiondisk", "delete",
               "create", "addvol")
CHECKPOINT = re.compile(r"Checking the checkpoint with transaction ID (\d+)")
FORMATTED = re.compile(r"The volume (.*) was formatted by ")
VERDICT = re.compile(r"^\*\* The container .* (appears to be OK|could not be verified.*|was repaired.*|is invalid.*)")


def layout(image):
    with open(image, "rb") as f:
        f.seek(512)
        print("gpt" if f.read(8) == b"EFI PART" else "raw")


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def xid_of(text):
    m = CHECKPOINT.search(text or "")
    return int(m.group(1)) if m else None


def xid(paths):
    for p in paths:
        x = xid_of(read(p))
        if x is not None:
            print(x)
            return
    raise SystemExit(1)


def steps(outdir):
    out = {}
    with open(os.path.join(outdir, "steps.tsv")) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[0] != "#":
                out[parts[0]] = parts[1]
    return out


def fsck(outdir, rcs, name):
    """What fsck_apfs said in step NAME, or in NAME-root where the step was
    taken again as root after it failed."""
    n = name if name in rcs else None
    if (n is None or rcs[n] != "0") and name + "-root" in rcs:
        n = name + "-root"
    if n is None:
        return None
    text = (read(os.path.join(outdir, "log", n + ".out")) or "") + \
        (read(os.path.join(outdir, "log", n + ".err")) or "")
    verdict = None
    for line in text.splitlines():
        m = VERDICT.match(line.strip())
        if m:
            verdict = m.group(1)
    return {"step": n, "exit": int(rcs[n]), "xid": xid_of(text),
            "volumes": FORMATTED.findall(text), "verdict": verdict}


def listed(outdir, listing, cont):
    path = os.path.join(outdir, "log", listing + ".out")
    if not cont or not os.path.exists(path):
        return None
    for c in disks.containers(disks.load(path)):
        if c.get("ContainerReference") == cont:
            return {"uuid": c.get("APFSContainerUUID"),
                    "volumes": [v.get("Name") for v in c.get("Volumes", [])]}
    return None


def summary(outdir):
    rcs = steps(outdir)
    seen = {}
    path = os.path.join(outdir, "listings.tsv")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) == 3:
                    seen[parts[0]] = listed(outdir, parts[1], parts[2])
    out = {}
    for layout_name in ("raw", "gpt"):
        n = "old-" + layout_name
        if n in seen or n + "-fsck" in rcs:
            out[n] = {"listed": seen.get(n), "fsck": fsck(outdir, rcs, n + "-fsck"),
                      "image": os.path.exists(os.path.join(outdir, "images", n + ".bd"))}
    # Every route dumps its image as ROUTE-made, whatever else it reached.
    names = [k[:-len("-made-dump")] for k in rcs if k.endswith("-made-dump")]
    for n in names:
        old = out.get("old-" + n.split("-")[0], {}).get("listed") or {}
        entry = {
            "route": dict((k, int(rcs[n + "-" + k])) for k in ROUTE_STEPS if n + "-" + k in rcs),
            "before": seen.get(n + "-before"),
            "made": seen.get(n + "-after"),
            "attached": seen.get(n),
            "fsck_file": fsck(outdir, rcs, n + "-fsck-file"),
            "fsck": fsck(outdir, rcs, n + "-fsck"),
            "images": [k for k in ("made", "mounted")
                       if os.path.exists(os.path.join(outdir, "images", "%s-%s.bd" % (n, k)))],
        }
        a = entry["attached"] or {}
        entry["attached_is_old"] = bool(a.get("uuid")) and a.get("uuid") == old.get("uuid")
        out[n] = entry
    with open(os.path.join(outdir, "summary.json"), "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
        f.write("\n")
    for n, e in out.items():
        if n.startswith("old-"):
            lst, ck = e["listed"] or {}, e["fsck"] or {}
            print("%-20s uuid %s volumes %s xid %s" % (n, lst.get("uuid"), lst.get("volumes"),
                                                       ck.get("xid")))
            continue
        a, ck = e["attached"] or {}, e["fsck"] or e["fsck_file"] or {}
        print("%-20s route %s; attached %s%s volumes %s; fsck xid %s volumes %s %s" % (
            n, e["route"], a.get("uuid"), " (the old container)" if e["attached_is_old"] else "",
            a.get("volumes"), ck.get("xid"), ck.get("volumes"), ck.get("verdict")))


def main(argv):
    if len(argv) == 3 and argv[1] == "layout":
        layout(argv[2])
    elif len(argv) >= 3 and argv[1] == "xid":
        xid(argv[2:])
    elif len(argv) == 3 and argv[1] == "summary":
        summary(argv[2])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
