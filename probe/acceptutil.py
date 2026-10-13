#!/usr/bin/env python3
"""Hold each container under accept/ to what macOS makes of it.

    acceptutil.py select ACCEPT WORDS...
    acceptutil.py verify ACCEPT NAME
    acceptutil.py control OUT NAME BLOCK-SIZE BYTES KIND VOLUME-NAME
    acceptutil.py listing ACCEPT OUT NAME LIST.plist CONTAINER
    acceptutil.py write MNT NAME VOLUME-UUID OUT.json
    acceptutil.py read MNT NAME VOLUME-UUID OUT.json
    acceptutil.py verdict ACCEPT OUT NAME
    acceptutil.py index ACCEPT OUT

ACCEPT is the accept/ directory: images.json, which names every container and
states what macOS should report of it, and one block dump per container.
OUT is the probe's output directory.

`select` prints the names of the containers WORDS asks for: `few`, those of
at most five volumes; `many`, those of more; `controls`, the containers
`newfs_apfs` formats on the runner beside them; or names. `verify` checks a
dump against the digests images.json gives it. `control` records what a
container `newfs_apfs` formatted should report, which is less than an image
of accept/ states: its identifiers and times are the runner's.

`listing` holds the container `diskutil apfs list` reports to the one
images.json states: its identifier, its capacity, and its volumes, each found
by its identifier, with its name and no role. It prints each volume's
device, in the order listed, for the probe to mount. `write` writes a file
to a volume mounted read-write, its content named by the container and the
volume's identifier, and `read` reads it back after the volume is mounted
again.

`verdict` gathers one container's steps into verdicts/NAME.json. A
container is accepted where every check passes:

- `fsck_apfs -n -W` exits 0 on the container as sent;
- the listing is the one images.json states;
- every volume mounts read-only, `diskutil info` reports its name, its
  identifier, its kind of name and its block size, and its root directory's
  owner, mode, flags and four times, with nothing in it, are as stated;
- `apfs_checkseal -q -v` lists the entries images.json states, which for an
  empty volume is none;
- every volume mounts read-write and takes a file, and after it is
  unmounted, `fsck_apfs -n -W` exits 0 again;
- mounted again, every volume reads the file back as written.

`index` writes verdicts.json: the release, and each container's verdict
beside what images.json expects of it.
"""

import hashlib
import json
import os
import platform
import plistlib
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import blockdump  # noqa: E402

CONTROLS = ["control-b4k-mid", "control-b16k-far-keep", "control-b64k-floor"]
FEW = 5
FILE = "accept.txt"


def read_json(path):
    with open(path) as f:
        return json.load(f)


def write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f, indent=1, sort_keys=True, ensure_ascii=True)
        f.write("\n")


def plist(path):
    with open(path, "rb") as f:
        data = f.read()
    start = data.find(b"<?xml")
    if start < 0:
        raise ValueError("%s: no plist" % path)
    return plistlib.loads(data[start:])


def images(acc):
    return read_json(os.path.join(acc, "images.json"))["images"]


def stated(acc, out, name):
    """What images.json, or the control's record, states of NAME."""
    if name.startswith("control-"):
        return read_json(os.path.join(out, "controls", name + ".json"))
    for image in images(acc):
        if image["name"] == name:
            return image
    raise SystemExit("no image named %s" % name)


def steps(out):
    found = {}
    with open(os.path.join(out, "steps.tsv")) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[0] != "#":
                found[parts[0]] = int(parts[1])
    return found


# -- choosing and checking the containers -----------------------------------

def select(acc, words):
    names = [i["name"] for i in images(acc)]
    want = []
    for w in words or ["controls", "few", "many"]:
        if w == "few":
            want += [i["name"] for i in images(acc) if len(i["volumes"]) <= FEW]
        elif w == "many":
            want += [i["name"] for i in images(acc) if len(i["volumes"]) > FEW]
        elif w == "controls":
            want += CONTROLS
        elif w in names or w in CONTROLS:
            want.append(w)
        else:
            raise SystemExit("no container named %s" % w)
    seen = set()
    for n in want:
        if n not in seen:
            seen.add(n)
            print(n)


def digest_of(path):
    """The first 64 bits of the SHA-256 of a dump's nonzero blocks, each
    preceded by its index as a little-endian 64-bit number, in blocks of the
    dump's block size: the digest images.json records."""
    it = blockdump.read_dump(path)
    _, bs = next(it)
    zero = bytes(bs)
    sha = hashlib.sha256()
    for first, data in it:
        for i in range(len(data) // bs):
            block = data[i * bs:(i + 1) * bs]
            if block != zero:
                sha.update(struct.pack("<Q", first + i))
                sha.update(block)
    return sha.hexdigest()[:16]


def verify(acc, name):
    image = stated(acc, None, name)
    path = os.path.join(acc, image["dump"])
    info = blockdump.info(path)
    found = {"content_sha256": info["content_sha256"], "image_size": info["image_size"],
             "block_size": info["block_size"], "digest": digest_of(path)}
    want = {k: image[k] for k in ("content_sha256", "digest")}
    want["image_size"] = image["bytes"]
    want["block_size"] = image["block_size"]
    print(json.dumps({"found": found, "want": want}, sort_keys=True))
    if found != want:
        raise SystemExit("%s: the dump is not the image images.json states" % name)


def control(out, name, bs, size, kind, volume):
    """A container newfs_apfs formats: its volume's name and kind, its size
    and block size are known; its identifiers, owner and times are not."""
    write_json(os.path.join(out, "controls", name + ".json"), {
        "name": name, "expect": "accept", "control": True, "block_size": int(bs),
        "bytes": int(size), "container_uuid": None,
        "volumes": [{"slot": 0, "name": volume, "name_hex": volume.encode().hex(), "uuid": None,
                     "case_sensitive": kind == "keep", "root": None}],
    })


# -- the listing --------------------------------------------------------------

def listing(acc, out, name, listing_path, container):
    image = stated(acc, out, name)
    problems, accounting = [], {}
    found = None
    for c in plist(listing_path).get("Containers", []):
        if c.get("ContainerReference") == os.path.basename(container):
            found = c
    devices = []
    if found is None:
        problems.append("no container %s listed" % container)
    else:
        if image["container_uuid"] is not None and found.get("APFSContainerUUID") != image["container_uuid"]:
            problems.append("container identifier %s, stated %s"
                            % (found.get("APFSContainerUUID"), image["container_uuid"]))
        if found.get("CapacityCeiling") != image["bytes"]:
            problems.append("capacity %s, stated %s" % (found.get("CapacityCeiling"), image["bytes"]))
        listed = found.get("Volumes", [])
        if len(listed) != len(image["volumes"]):
            problems.append("%d volumes listed, %d stated" % (len(listed), len(image["volumes"])))
        order = []
        for want in image["volumes"]:
            slot = want["slot"]
            if want["uuid"] is None:
                at = slot if slot < len(listed) else None
            else:
                at = next((i for i, v in enumerate(listed) if v.get("APFSVolumeUUID") == want["uuid"]), None)
            if at is None:
                problems.append("slot %d: not listed" % slot)
                continue
            order.append((at, slot))
            got = listed[at]
            name_bytes = (got.get("Name") or "").encode()
            if name_bytes.hex() != want["name_hex"]:
                problems.append("slot %d: name %s, stated %s" % (slot, name_bytes.hex(), want["name_hex"]))
            if got.get("Roles"):
                problems.append("slot %d: roles %s, stated none" % (slot, got.get("Roles")))
            if got.get("Encryption") or got.get("FileVault") or got.get("Locked"):
                problems.append("slot %d: encrypted or locked" % slot)
        stated_uuids = {v["uuid"] for v in image["volumes"]}
        for v in listed:
            if None not in stated_uuids and v.get("APFSVolumeUUID") not in stated_uuids:
                problems.append("volume %s listed, none stated" % v.get("APFSVolumeUUID"))
        for got in listed:
            devices.append(got.get("DeviceIdentifier"))
        accounting = {
            "slots_in_listed_order": [slot for _, slot in sorted(order)],
            "capacity_free": found.get("CapacityFree"),
            "stated_free": (image.get("free_blocks") * image["block_size"]
                            if image.get("free_blocks") is not None else None),
            "in_use": [v.get("CapacityInUse") for v in listed],
            "stated_in_use": [v.get("allocated_blocks") * image["block_size"]
                              if v.get("allocated_blocks") is not None else None
                              for v in image["volumes"]],
        }
    write_json(os.path.join(out, "listing", name + ".json"),
               {"devices": devices, "problems": problems, "accounting": accounting})
    for d in devices:
        print(d)


# -- the file each volume takes -----------------------------------------------

def content(name, key):
    line = ("%s, volume %s: written by the accept probe.\n" % (name, key)).encode()
    return line * (10000 // len(line) + 1)


def write(mnt, name, key, out):
    data = content(name, key)
    result = {"path": FILE, "len": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    try:
        fd = os.open(os.path.join(mnt, FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        result["written"] = True
    except OSError as e:
        result["written"] = False
        result["error"] = str(e)
    write_json(out, result)
    if not result["written"]:
        raise SystemExit(result["error"])


def read(mnt, name, key, out):
    want = hashlib.sha256(content(name, key)).hexdigest()
    result = {"path": FILE, "sha256_stated": want}
    try:
        with open(os.path.join(mnt, FILE), "rb") as f:
            data = f.read()
        result["sha256"] = hashlib.sha256(data).hexdigest()
        result["len"] = len(data)
        result["same"] = result["sha256"] == want
    except OSError as e:
        result["same"] = False
        result["error"] = str(e)
    write_json(out, result)
    if not result["same"]:
        raise SystemExit("%s volume %s: the file reads back otherwise" % (name, key))


# -- the verdict --------------------------------------------------------------

def checkseal_entries(path):
    """The entries `apfs_checkseal -q -v` lists: every line that names a
    path, which it begins with."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        return [line.rstrip("\n") for line in f if line.startswith("/")]


def volume_checks(image, out, name, k, problems):
    """The read-only pass's checks of the K-th volume listed; returns its slot."""
    p = "%s-ro-v%d" % (name, k)
    status = steps(out)
    if status.get(p + "-mount") != 0:
        problems.append("%s: the read-only mount failed" % p)
        return None
    try:
        info = plist(os.path.join(out, "log", p + "-info.out"))
    except (OSError, ValueError):
        problems.append("%s: no diskutil info" % p)
        return None
    want = None
    for v in image["volumes"]:
        if v["uuid"] is not None and v["uuid"] == info.get("VolumeUUID"):
            want = v
    if want is None:
        if image.get("control") and len(image["volumes"]) == 1:
            want = image["volumes"][0]
        else:
            problems.append("%s: identifier %s is no volume stated" % (p, info.get("VolumeUUID")))
            return None
    slot = want["slot"]
    tag = "%s slot %d" % (p, slot)
    if (info.get("VolumeName") or "").encode().hex() != want["name_hex"]:
        problems.append("%s: diskutil names it %s" % (tag, (info.get("VolumeName") or "").encode().hex()))
    kind = info.get("FilesystemName")
    if kind != ("Case-sensitive APFS" if want["case_sensitive"] else "APFS"):
        problems.append("%s: kind %s" % (tag, kind))
    if info.get("VolumeAllocationBlockSize") != image["block_size"]:
        problems.append("%s: block size %s" % (tag, info.get("VolumeAllocationBlockSize")))
    try:
        entries = read_json(os.path.join(out, "volumes", p + ".json"))
    except OSError:
        problems.append("%s: no listing of the mounted volume" % tag)
        entries = None
    if entries is not None:
        if entries.get("errors"):
            problems.append("%s: listing errors %s" % (tag, entries["errors"]))
        root = next((e for e in entries["entries"] if e["path_hex"] == ""), None)
        others = [e["path_hex"] for e in entries["entries"] if e["path_hex"] != ""]
        if root is None:
            problems.append("%s: no root directory" % tag)
        else:
            if root.get("children_hex"):
                problems.append("%s: root holds %s" % (tag, root["children_hex"]))
            if others:
                problems.append("%s: entries %s" % (tag, others))
            stated_root = want["root"]
            if stated_root is not None:
                for field in ("uid", "gid", "mode", "flags", "atime_ns", "mtime_ns", "ctime_ns",
                              "birthtime_ns"):
                    if root.get(field) != stated_root[field]:
                        problems.append("%s: root %s %s, stated %s"
                                        % (tag, field, root.get(field), stated_root[field]))
    listed = checkseal_entries(os.path.join(out, "log", p + "-checkseal.out"))
    if listed is None:
        problems.append("%s: apfs_checkseal did not run" % tag)
    elif listed:
        problems.append("%s: apfs_checkseal lists %d entries, stated none" % (tag, len(listed)))
    return slot


def verdict(acc, out, name):
    image = stated(acc, out, name)
    status = steps(out)
    problems = []
    if status.get(name + "-fsck") != 0:
        problems.append("fsck_apfs -n -W exits %s on the container as sent" % status.get(name + "-fsck"))
    try:
        listed = read_json(os.path.join(out, "listing", name + ".json"))
    except OSError:
        listed = {"devices": [], "problems": ["no listing"], "accounting": {}}
    problems += ["listing: " + p for p in listed["problems"]]
    n = len(listed["devices"])
    slots = [volume_checks(image, out, name, k, problems) for k in range(1, n + 1)]
    for k in range(1, n + 1):
        p = "%s-rw-v%d" % (name, k)
        if status.get(p + "-mount") != 0:
            problems.append("%s: the read-write mount failed" % p)
        elif status.get(p + "-write") != 0:
            problems.append("%s: the file was not written" % p)
        r = "%s-back-v%d" % (name, k)
        if status.get(r + "-mount") != 0:
            problems.append("%s: the mount after the write failed" % r)
        elif status.get(r + "-read") != 0:
            problems.append("%s: the file did not read back" % r)
    if status.get(name + "-fsck-written") != 0:
        problems.append("fsck_apfs -n -W exits %s after the write" % status.get(name + "-fsck-written"))
    result = {
        "name": name,
        "expect": image["expect"],
        "control": bool(image.get("control")),
        "verdict": "accepted" if not problems else "refused",
        "problems": problems,
        "slots_listed": slots,
        "accounting": listed.get("accounting", {}),
    }
    result["as_expected"] = (result["verdict"] == "accepted") == (image["expect"] == "accept")
    write_json(os.path.join(out, "verdicts", name + ".json"), result)
    print(json.dumps({"verdict": result["verdict"], "as_expected": result["as_expected"],
                      "problems": len(problems)}))


def release(out):
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


def index(acc, out):
    verdicts = {}
    vdir = os.path.join(out, "verdicts")
    for f in sorted(os.listdir(vdir)) if os.path.isdir(vdir) else []:
        v = read_json(os.path.join(vdir, f))
        verdicts[v["name"]] = v
    write_json(os.path.join(out, "verdicts.json"), {
        "release": release(out),
        "images": verdicts,
        "accepted": sorted(n for n, v in verdicts.items() if v["verdict"] == "accepted"),
        "refused": sorted(n for n, v in verdicts.items() if v["verdict"] == "refused"),
        "unexpected": sorted(n for n, v in verdicts.items() if not v["as_expected"]),
    })
    bad = [n for n, v in verdicts.items() if not v["as_expected"]]
    print("%d containers, %d as expected" % (len(verdicts), len(verdicts) - len(bad)))
    for n in sorted(bad):
        print("unexpected: %s %s" % (n, verdicts[n]["verdict"]))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "select" and len(argv) >= 3:
        select(argv[2], argv[3:])
    elif cmd == "verify" and len(argv) == 4:
        verify(argv[2], argv[3])
    elif cmd == "control" and len(argv) == 8:
        control(*argv[2:])
    elif cmd == "listing" and len(argv) == 7:
        listing(*argv[2:])
    elif cmd == "write" and len(argv) == 6:
        write(*argv[2:])
    elif cmd == "read" and len(argv) == 6:
        read(*argv[2:])
    elif cmd == "verdict" and len(argv) == 5:
        verdict(*argv[2:])
    elif cmd == "index" and len(argv) == 4:
        index(argv[2], argv[3])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
