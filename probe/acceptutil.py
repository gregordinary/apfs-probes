#!/usr/bin/env python3
"""Hold each container under accept/ to what macOS makes of it.

    acceptutil.py select ACCEPT WORDS...
    acceptutil.py verify ACCEPT NAME
    acceptutil.py control OUT NAME BLOCK-SIZE BYTES KIND VOLUME-NAME
    acceptutil.py listing ACCEPT OUT NAME LIST.plist CONTAINER
    acceptutil.py write MNT NAME VOLUME-UUID OUT.json
    acceptutil.py read MNT NAME VOLUME-UUID OUT.json
    acceptutil.py room-bytes ACCEPT NAME
    acceptutil.py room MNT NAME BYTES OUT.json
    acceptutil.py room-read MNT NAME BYTES OUT.json
    acceptutil.py snapshot-name ACCEPT NAME INFO.plist
    acceptutil.py verdict ACCEPT OUT NAME
    acceptutil.py index ACCEPT OUT

ACCEPT is the accept/ directory: images.json, which names every container and
states what macOS should report of it, and one block dump per container.
OUT is the probe's output directory.

`select` prints the names of the containers WORDS asks for: `few`, those of
at most five volumes; `many`, those of more; `controls`, the containers
`newfs_apfs` formats on the runner beside them; `limits`, those whose names
begin `limit-`; `trees`, those whose first volume holds a tree, whose names
begin `tree-`; `corpus`, those whose first volume holds a corpus volume,
whose names begin `corpus-`; `fit`, those sized to the tree they hold,
whose names begin `fit-`; `root`, those whose volumes have a root snapshot,
whose names begin `root-`; or names. `verify` checks a
dump against the digests images.json gives it. `control` records what a
container `newfs_apfs` formatted should report, which is less than an image
of accept/ states: its identifiers and times are the runner's.

`listing` holds the container `diskutil apfs list` reports to the one
images.json states: its identifier, its capacity, and its volumes, each found
by its identifier, with its name and no role. A name longer than 127 bytes
is listed shortened, which is held to the rule `listed_short` states. It prints each volume's
device, in the order listed, for the probe to mount. `write` writes a file
to a volume mounted read-write, its content named by the container and the
volume's identifier, and records how many bytes it took and, where it
stopped, at which call and with which error. `read` reads it back after the
volume is mounted again, and records whether it is there and whether what is
there begins the content written.

`verdict` gathers one container's steps into verdicts/NAME.json. A
container is accepted where every check passes:

- `fsck_apfs -n -W` exits 0 on the container as sent, or warns only of what
  images.json says it holds: that its time is later than the runner's clock,
  for a container expected to warn of that, and that a device node's number
  is zero, once for each device node it states numbered zero, which
  `fsck_apfs` warns of on the containers macOS writes too;
- the listing is the one images.json states, each volume with the role it is
  stated with, or none;
- every volume mounts read-only, `diskutil info` reports its name, its
  identifier, its kind of name and its block size, and its root directory's
  owner, mode, flags and four times are as stated. A volume images.json
  states no entries of holds nothing else. A volume it states entries of
  holds those and no others, each by path with its inode number, mode,
  owner, group, flags and four times as stated; a file's link count, size
  and content, a link's target, a device's number, a directory's names and
  every entry's attributes by name, length and content as stated too.
  What macOS reports of a directory's link count and size, and of a file's
  blocks, is recorded beside the verdict and not held;
- `apfs_checkseal -q -v` lists the entries images.json states, which for an
  empty volume is none, and for each the owner, group, mode, flags, size,
  allocation and attributes the writer recorded;
- every volume mounts read-write and takes a file, and after it is
  unmounted, `fsck_apfs -n -W` passes again as above;
- mounted again, every volume reads the file back as written;
- each volume images.json states a root snapshot of is listed by `diskutil
  apfs listSnapshots` with that snapshot alone, by its name, identifier and
  transaction, as the one the volume roots to (`RootTo`); and the snapshot,
  mounted read-only beside the volume, holds what the volume was stated to
  hold, root directory and entries alike. Both hold in the read-only pass
  and again after the file is written, when the volume holds more and the
  snapshot is to hold the same. A volume stated with none lists none. What
  `diskutil` reports of a snapshot's other words, `Purgeable` among them, is
  recorded beside the verdict and not held.
- `diskutil apfs listVolumeGroups` lists the container with the volume group
  images.json states of it: one group, under the identifier its volumes
  record, of exactly the volumes stated in it, each by its identifier with the
  role stated (`System` for 0x1, `Data` for 0x40). A container stated with no
  group lists none. What `diskutil info` reports of each volume's role is
  recorded beside the verdict and not held.

`snapshot-name` prints the name of the root snapshot images.json states for
the volume `diskutil info` reported in INFO.plist, and exits 1 for a volume
it states none of.

A container images.json expects to take `room:BYTES` is held to every check
`accept` holds a container to, and before the probe's file the runner's own
user writes BYTES into the first volume, a mebibyte a write and synced at
the end (`room`), every write taken; mounted again, the volume reads them
back (`room-read`). `room-bytes` prints BYTES for such a container, and
nothing for any other.

A container images.json expects to be `full` is one macOS finds too little
room in to take the file: one with no block free, or with fewer than macOS
sets aside before a change. It is held to the same checks but the file:
every volume mounts read-write and refuses it for want of room (ENOSPC), and
mounted again holds no file, or a file that begins the content written.

`index` writes verdicts.json: the release, and each container's verdict
beside what images.json expects of it.
"""

import errno
import hashlib
import json
import os
import platform
import plistlib
import re
import stat
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import blockdump  # noqa: E402

CONTROLS = ["control-b4k-mid", "control-b16k-far-keep", "control-b64k-floor"]
FEW = 5
FILE = "accept.txt"
ROOM_FILE = "room.bin"
MIB = 1 << 20


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
        elif w == "limits":
            want += [n for n in names if n.startswith("limit-")]
        elif w == "trees":
            want += [n for n in names if n.startswith("tree-")]
        elif w == "corpus":
            want += [n for n in names if n.startswith("corpus-")]
        elif w == "fit":
            want += [n for n in names if n.startswith("fit-")]
        elif w == "root":
            want += [n for n in names if n.startswith("root-")]
        elif w == "group":
            want += [n for n in names if n.startswith("group-")]
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

# `diskutil apfs list` gives a name longer than this as its leading whole
# characters: of 85 three-byte characters, the first 42. `diskutil info` gives
# the whole name, and the read-only pass holds it to the statement.
LISTED_NAME_BYTES = 127


def listed_short(listed, stated):
    """Whether LISTED is STATED as `diskutil apfs list` shortens a long name:
    its leading whole characters, within LISTED_NAME_BYTES and no more than
    one character short of them."""
    if len(stated) <= LISTED_NAME_BYTES or not stated.startswith(listed):
        return False
    try:
        whole = stated.decode("utf-8")
        listed.decode("utf-8")
    except UnicodeDecodeError:
        return False
    fit = b""
    for ch in whole:
        if len(fit) + len(ch.encode()) > LISTED_NAME_BYTES:
            break
        fit += ch.encode()
    return listed == fit

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
        order, shortened = [], []
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
                if listed_short(name_bytes, bytes.fromhex(want["name_hex"])):
                    shortened.append(slot)
                else:
                    problems.append("slot %d: name %s, stated %s" % (slot, name_bytes.hex(), want["name_hex"]))
            role = want.get("role") or 0
            stated_roles = [ROLE_WORDS.get(role, str(role))] if role else []
            if (got.get("Roles") or []) != stated_roles:
                problems.append("slot %d: roles %s, stated %s"
                                % (slot, got.get("Roles"), stated_roles or "none"))
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
            "slots_listed_shortened": shortened,
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
    result = {"path": FILE, "len": len(data), "sha256": hashlib.sha256(data).hexdigest(),
              "taken": 0}
    stage = "open"
    try:
        fd = os.open(os.path.join(mnt, FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            stage = "write"
            while result["taken"] < len(data):
                n = os.write(fd, data[result["taken"]:])
                if n == 0:
                    break
                result["taken"] += n
            stage = "fsync"
            os.fsync(fd)
        finally:
            os.close(fd)
        result["written"] = result["taken"] == len(data)
        if not result["written"]:
            result["stage"] = "write"
            result["error"] = "a write took no bytes after %d" % result["taken"]
    except OSError as e:
        result["written"] = False
        result["stage"] = stage
        result["errno"] = errno.errorcode.get(e.errno, e.errno)
        result["error"] = str(e)
    write_json(out, result)
    if not result["written"]:
        raise SystemExit(result["error"])


def read(mnt, name, key, out):
    written = content(name, key)
    want = hashlib.sha256(written).hexdigest()
    result = {"path": FILE, "sha256_stated": want, "absent": False}
    try:
        with open(os.path.join(mnt, FILE), "rb") as f:
            data = f.read()
        result["sha256"] = hashlib.sha256(data).hexdigest()
        result["len"] = len(data)
        result["same"] = result["sha256"] == want
        result["prefix"] = written.startswith(data)
    except OSError as e:
        result["same"] = False
        result["absent"] = e.errno == errno.ENOENT
        result["error"] = str(e)
    write_json(out, result)
    if not result["same"]:
        raise SystemExit("%s volume %s: the file reads back otherwise" % (name, key))


def room_of(image):
    """The bytes a container images.json expects to take as room, or None."""
    expect = image.get("expect", "")
    return int(expect[len("room:"):]) if expect.startswith("room:") else None


def room_bytes(acc, name):
    if name in CONTROLS:
        return
    room = room_of(stated(acc, None, name))
    if room is not None:
        print(room)


def room_chunks(name, nbytes):
    """The room file's content, a mebibyte at a time."""
    unit = ("%s: room the accept probe writes.\n" % name).encode()
    chunk = (unit * (MIB // len(unit) + 1))[:MIB]
    left = nbytes
    while left > 0:
        yield chunk[:min(MIB, left)]
        left -= MIB


def statvfs(path):
    s = os.statvfs(path)
    return {"bavail": s.f_bavail, "bfree": s.f_bfree, "blocks": s.f_blocks}


def room(mnt, name, nbytes, out):
    nbytes = int(nbytes)
    result = {"path": ROOM_FILE, "len": nbytes, "taken": 0, "uid": os.getuid(),
              "before": statvfs(mnt)}
    stage = "open"
    try:
        fd = os.open(os.path.join(mnt, ROOM_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            stage = "write"
            for chunk in room_chunks(name, nbytes):
                n = os.write(fd, chunk)
                result["taken"] += n
                if n != len(chunk):
                    raise OSError(errno.ENOSPC, "a write took %d of %d bytes" % (n, len(chunk)))
            stage = "fsync"
            os.fsync(fd)
        finally:
            os.close(fd)
        result["written"] = result["taken"] == nbytes
    except OSError as e:
        result["written"] = False
        result["stage"] = stage
        result["errno"] = errno.errorcode.get(e.errno, e.errno)
        result["error"] = str(e)
    result["after"] = statvfs(mnt)
    write_json(out, result)
    if not result["written"]:
        raise SystemExit("the room was not written: %s" % result.get("error"))


def room_read(mnt, name, nbytes, out):
    nbytes = int(nbytes)
    want = hashlib.sha256()
    for chunk in room_chunks(name, nbytes):
        want.update(chunk)
    result = {"path": ROOM_FILE, "len_stated": nbytes, "sha256_stated": want.hexdigest()}
    try:
        got, n = hashlib.sha256(), 0
        with open(os.path.join(mnt, ROOM_FILE), "rb") as f:
            while True:
                block = f.read(MIB)
                if not block:
                    break
                got.update(block)
                n += len(block)
        result["len"] = n
        result["sha256"] = got.hexdigest()
        result["same"] = result["sha256"] == result["sha256_stated"]
    except OSError as e:
        result["same"] = False
        result["error"] = str(e)
    write_json(out, result)
    if not result["same"]:
        raise SystemExit("the room reads back otherwise")


# -- the verdict --------------------------------------------------------------

def checkseal_entries(path):
    """The entries `apfs_checkseal -q -v` lists. Each begins a line with its
    path and ends with its fields in braces; a name is printed as it is, so
    one holding a line feed carries its entry onto the lines after."""
    if not os.path.exists(path):
        return None
    entries = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("/"):
                entries.append(line)
            elif entries and not CHECKSEAL_END.search(entries[-1]):
                entries[-1] += "\n" + line
    return entries


CHECKSEAL_END = re.compile(r" \{[^{}]*\}$")

CHECKSEAL_FIELD = re.compile(
    r"(uid/gid/mode|bsd_flags|internal_flags|file-size|alloced-size|def-prot-class|"
    r"embedded data size|data-len|flags|[A-Za-z_-]+): (\S+)")


def checkseal_fields(text):
    """The `key: value` pairs between a checkseal line's braces."""
    return {k: v for k, v in CHECKSEAL_FIELD.findall(text)}


def checkseal_parsed(lines):
    """Each entry line's fields by its path's bytes in hex, and each attribute
    line's by its path's and name's."""
    entries, xattrs = {}, {}
    for line in lines:
        head, brace, rest = line.rpartition(" {")
        if not brace:
            continue
        fields = checkseal_fields(rest.rstrip("}"))
        path, sep, name = head.partition(" :: ")
        if sep:
            xattrs[(path.encode().hex(), name.encode().hex())] = fields
        else:
            entries[path.encode().hex()] = fields
    return entries, xattrs


def compare_checkseal(tag, lines, stated, problems):
    """Hold what `apfs_checkseal -q -v` lists to what the writer recorded."""
    found, found_xattrs = checkseal_parsed(lines)
    want = {e["path_hex"]: e for e in stated}
    missing = sorted(set(want) - set(found))
    extra = sorted(set(found) - set(want))
    if missing:
        problems.append("%s: apfs_checkseal lists %d stated entries not, the first %s"
                        % (tag, len(missing), missing[0]))
    if extra:
        problems.append("%s: apfs_checkseal lists %d entries not stated, the first %s"
                        % (tag, len(extra), extra[0]))
    wrong = []
    for path in sorted(set(want) & set(found)):
        w, f = want[path], found[path]
        held = {"uid/gid/mode": "%d/%d/%#x" % (w["uid"], w["gid"], w["mode"]),
                "bsd_flags": "%#x" % w["bsd_flags"],
                "internal_flags": "%#x" % w["internal_flags"]}
        if "file_size" in w:
            held["file-size"] = str(w["file_size"])
            held["alloced-size"] = str(w["alloced_size"])
        for k, v in held.items():
            if f.get(k) != v:
                wrong.append("%s %s %s, stated %s" % (path, k, f.get(k), v))
    # An inode's attributes are listed once, under one of its names, so they are held to the
    # inode: the attributes listed under any of its names against those stated of it.
    ino_of = {e["path_hex"]: e["ino"] for e in stated}
    listed_by_ino = {}
    for (path, name), fields in found_xattrs.items():
        if path in ino_of:
            listed_by_ino.setdefault(ino_of[path], {})[name] = fields
    seen = set()
    for e in stated:
        if e["ino"] in seen:
            continue
        seen.add(e["ino"])
        got_all = listed_by_ino.get(e["ino"], {})
        for x in e["xattrs"]:
            got = got_all.get(x["name_hex"])
            if got is None:
                wrong.append("%s attribute %s not listed" % (e["path_hex"], x["name_hex"]))
                continue
            if got.get("flags") != "%#x" % x["flags"]:
                wrong.append("%s attribute %s flags %s, stated %#x"
                             % (e["path_hex"], x["name_hex"], got.get("flags"), x["flags"]))
            if "data_len" in x and got.get("data-len") != str(x["data_len"]):
                wrong.append("%s attribute %s data-len %s, stated %d"
                             % (e["path_hex"], x["name_hex"], got.get("data-len"), x["data_len"]))
        extra = set(got_all) - {x["name_hex"] for x in e["xattrs"]}
        if extra:
            wrong.append("%s attributes listed and not stated %s" % (e["path_hex"], sorted(extra)))
    if wrong:
        problems.append("%s: apfs_checkseal differs in %d places, the first %s"
                        % (tag, len(wrong), "; ".join(wrong[:3])))


# The fields every entry of a manifest is held to, and those held only where
# images.json states them.
HELD = ("ino", "mode", "uid", "gid", "flags", "atime_ns", "mtime_ns", "ctime_ns", "birthtime_ns")
HELD_WHERE_STATED = ("nlink", "size", "rdev", "sha256", "read_len", "target_hex")


def compare_entries(tag, found, stated, problems, observed):
    """Hold a mounted volume's manifest to the entries images.json states."""
    have = {e["path_hex"]: e for e in found}
    want = {e["path_hex"]: e for e in stated}
    missing = sorted(set(want) - set(have))
    extra = sorted(set(have) - set(want))
    if missing:
        problems.append("%s: %d stated entries are missing, the first %s"
                        % (tag, len(missing), missing[0]))
    if extra:
        problems.append("%s: %d entries are not stated, the first %s"
                        % (tag, len(extra), extra[0]))
    wrong, by_field = [], {}
    for path in sorted(set(want) & set(have)):
        w, f = want[path], have[path]
        fields = list(HELD) + [k for k in HELD_WHERE_STATED if k in w]
        for k in fields:
            if f.get(k) != w[k]:
                by_field[k] = by_field.get(k, 0) + 1
                wrong.append("%s %s %s, stated %s" % (path, k, f.get(k), w[k]))
        if "children_hex" in w and sorted(f.get("children_hex") or []) != sorted(w["children_hex"]):
            by_field["children_hex"] = by_field.get("children_hex", 0) + 1
            wrong.append("%s children differ" % path)
        if f.get("xattrs") is not None:
            got = sorted((x["name_hex"], x["len"], x["sha256"]) for x in f["xattrs"])
            exp = sorted((x["name_hex"], x["len"], x["sha256"]) for x in w["xattrs"])
            if got != exp:
                by_field["xattrs"] = by_field.get("xattrs", 0) + 1
                wrong.append("%s attributes %s, stated %s" % (path, got, exp))
        # What is recorded and not held: a directory's link count and size, a file's blocks.
        kind = "dir" if "children_hex" in w else ("file" if "sha256" in w else "other")
        for k in ("nlink", "size", "blocks"):
            if k in f and k not in w:
                observed.setdefault("%s %s" % (kind, k), []).append(
                    [path, f[k], len(w.get("children_hex", []))])
    if wrong:
        problems.append("%s: %d fields differ (%s), the first %s"
                        % (tag, len(wrong), ", ".join("%s %d" % kv for kv in sorted(by_field.items())),
                           "; ".join(wrong[:3])))


def held_tree(tag, path, want, problems, observed):
    """Hold the manifest at PATH of a mounted volume, or of a snapshot of one,
    to the tree images.json states of the volume WANT: its root directory, and
    the entries it states or, where it states none, nothing beside the root."""
    try:
        entries = read_json(path)
    except OSError:
        problems.append("%s: no listing of the mounted volume" % tag)
        return
    if entries.get("errors"):
        problems.append("%s: listing errors %s" % (tag, entries["errors"]))
    root = next((e for e in entries["entries"] if e["path_hex"] == ""), None)
    others = [e["path_hex"] for e in entries["entries"] if e["path_hex"] != ""]
    if want.get("entries") is not None:
        compare_entries(tag, entries["entries"], want["entries"], problems, observed)
    if root is None:
        problems.append("%s: no root directory" % tag)
        return
    if want.get("entries") is None and root.get("children_hex"):
        problems.append("%s: root holds %s" % (tag, root["children_hex"]))
    if want.get("entries") is None and others:
        problems.append("%s: entries %s" % (tag, others))
    stated_root = want["root"]
    if stated_root is not None:
        for field in ("uid", "gid", "mode", "flags", "atime_ns", "mtime_ns", "ctime_ns",
                      "birthtime_ns"):
            if root.get(field) != stated_root[field]:
                problems.append("%s: root %s %s, stated %s"
                                % (tag, field, root.get(field), stated_root[field]))


def stated_volume(image, info_path):
    """The volume images.json states whose identifier `diskutil info` reported
    in INFO_PATH, or None."""
    try:
        info = plist(info_path)
    except (OSError, ValueError):
        return None
    return next((v for v in image["volumes"]
                 if v["uuid"] is not None and v["uuid"] == info.get("VolumeUUID")), None)


def snapshot_name(acc, name, info_path):
    want = stated_volume(stated(acc, None, name), info_path)
    if want is None or want.get("snapshot") is None:
        raise SystemExit(1)
    print(want["snapshot"]["name"])


# The words `diskutil apfs listSnapshots` reports of a snapshot that are
# recorded and not held.
SNAPSHOT_WORDS = ("Purgeable", "LimitingContainerShrink", "RevertTo")


def snapshot_checks(image, out, name, phase, k, problems, observed):
    """The checks of PHASE's pass of the K-th volume: the snapshots it lists,
    and the root snapshot images.json states of it, mounted beside it."""
    p = "%s-%s-v%d" % (name, phase, k)
    status = steps(out)
    want = stated_volume(image, os.path.join(out, "log", p + "-info.out"))
    if want is None:
        return
    snap = want.get("snapshot")
    tag = "%s slot %d snapshots" % (p, want["slot"])
    try:
        listed = plist(os.path.join(out, "log", p + "-snapshots.out")).get("Snapshots", [])
    except (OSError, ValueError):
        problems.append("%s: not listed" % tag)
        return
    if snap is None:
        if listed:
            problems.append("%s: %d listed, stated none" % (tag, len(listed)))
        return
    if len(listed) != 1:
        problems.append("%s: %d listed, stated one" % (tag, len(listed)))
    for got in listed[:1]:
        found = (got.get("SnapshotName"), got.get("SnapshotUUID"), got.get("SnapshotXID"),
                 got.get("RootTo"))
        if found != (snap["name"], snap["uuid"], snap["xid"], True):
            problems.append("%s: listed as %s, stated %s" % (
                tag, found, (snap["name"], snap["uuid"], snap["xid"], True)))
        observed.setdefault("snapshot words", {})[p] = {w: got.get(w) for w in SNAPSHOT_WORDS}
    if status.get(p + "-snap-mount") != 0:
        problems.append("%s: the snapshot's read-only mount failed" % tag)
        return
    held_tree("%s slot %d snapshot" % (p, want["slot"]),
              os.path.join(out, "volumes", p + "-snap.json"), want, problems,
              observed.setdefault("%d snapshot %s" % (want["slot"], phase), {})
              if want.get("entries") is not None else {})


NO_GROUP = "00000000-0000-0000-0000-000000000000"
ROLE_WORDS = {0x1: "System", 0x40: "Data"}


def group_checks(image, out, name, problems, observed):
    """The container's volume groups as `diskutil apfs listVolumeGroups` lists
    them in the ro pass, held to the group images.json states."""
    stated_groups = {}
    for v in image["volumes"]:
        g = v.get("volume_group") or NO_GROUP
        if g != NO_GROUP:
            stated_groups.setdefault(g, []).append(v)
    try:
        found = plist(os.path.join(out, "log", name + "-ro-groups.out"))
    except (OSError, ValueError):
        problems.append("volume groups: not listed")
        return
    mine = [c for c in found.get("Containers", [])
            if c.get("APFSContainerUUID") == image["container_uuid"]]
    if len(mine) != 1:
        problems.append("volume groups: the container is listed %d times" % len(mine))
        return
    groups = mine[0].get("VolumeGroups", [])
    observed["volume groups"] = groups
    listed = {g.get("APFSVolumeGroupUUID"): g for g in groups}
    if set(listed) != set(stated_groups):
        problems.append("volume groups: listed %s, stated %s"
                        % (sorted(listed), sorted(stated_groups)))
        return
    for uuid, members in stated_groups.items():
        got = sorted((m.get("DiskUUID"), m.get("Role")) for m in listed[uuid].get("Volumes", []))
        want = sorted((v["uuid"], ROLE_WORDS.get(v.get("role"), str(v.get("role"))))
                      for v in members)
        if got != want:
            problems.append("volume group %s: lists %s, stated %s" % (uuid, got, want))


def volume_checks(image, out, name, k, problems, observed):
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
    roles = {key: value for key, value in info.items() if "Role" in key}
    if roles:
        observed.setdefault("roles", {})[str(slot)] = roles
    held_tree(tag, os.path.join(out, "volumes", p + ".json"), want, problems,
              observed.setdefault(str(slot), {}) if want.get("entries") is not None else {})
    listed = checkseal_entries(os.path.join(out, "log", p + "-checkseal.out"))
    if listed is None:
        problems.append("%s: apfs_checkseal did not run" % tag)
    elif want.get("checkseal") is not None:
        compare_checkseal(tag, listed, want["checkseal"], problems)
    elif listed:
        problems.append("%s: apfs_checkseal lists %d entries, stated none" % (tag, len(listed)))
    return slot


def fsck_warnings(out, step):
    """The warnings fsck_apfs gave in STEP about the container, and its count
    of failures; the note that an image file is no disk is not one of them."""
    lines, failures = [], None
    for suffix in (".out", ".err"):
        path = os.path.join(out, "log", step + suffix)
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8", errors="replace"):
            line = line.rstrip("\n")
            if line.startswith("warning:") and "not a valid disk name/path" not in line:
                lines.append(line)
            if " failures and " in line:
                failures = int(line.split()[0])
    return lines, failures


ZERO_DEVICE = re.compile(r"warning: inode \(id (\d+)\): .*found zero rdev on char/block device")


def zero_devices(image):
    """The inode numbers of the device nodes images.json states numbered zero."""
    return sorted({e["ino"] for v in image.get("volumes", []) for e in v.get("entries") or []
                   if (stat.S_ISCHR(e["mode"]) or stat.S_ISBLK(e["mode"])) and e.get("rdev") == 0})


def fsck_passes(image, out, step, status):
    """Whether fsck_apfs -n -W passed STEP: it exits 0, or it exits 8 on the
    warnings the image is expected to draw and no others. Those are that a
    volume superblock's time is greater than the current time, for an image
    expected to warn of that, and that a device node's number is zero, once
    for each device node the image states numbered zero, by its inode, each
    of which -W counts as a failure."""
    rc = status.get(step)
    if rc == 0:
        return True
    warnings, failures = fsck_warnings(out, step)
    if rc != 8 or not warnings:
        return False
    zero = sorted(int(m.group(1)) for m in map(ZERO_DEVICE.match, warnings) if m)
    late = [w for w in warnings if "is greater than current time" in w]
    return (len(zero) + len(late) == len(warnings)
            and zero == zero_devices(image) and failures == len(zero)
            and bool(late) == (image["expect"] == "warn-future-time"))


def record(out, step):
    """What `write` or `read` recorded in STEP, or None where it did not run."""
    try:
        return read_json(os.path.join(out, "files", step + ".json"))
    except OSError:
        return None


def verdict(acc, out, name):
    image = stated(acc, out, name)
    status = steps(out)
    problems = []
    if not fsck_passes(image, out, name + "-fsck", status):
        problems.append("fsck_apfs -n -W exits %s on the container as sent" % status.get(name + "-fsck"))
    try:
        listed = read_json(os.path.join(out, "listing", name + ".json"))
    except OSError:
        listed = {"devices": [], "problems": ["no listing"], "accounting": {}}
    problems += ["listing: " + p for p in listed["problems"]]
    n = len(listed["devices"])
    observed = {}
    full = image["expect"] == "full"
    room = room_of(image)
    slots = [volume_checks(image, out, name, k, problems, observed) for k in range(1, n + 1)]
    if not image.get("control"):
        group_checks(image, out, name, problems, observed)
    if any(v.get("snapshot") is not None for v in image["volumes"]):
        for phase in ("ro", "back"):
            for k in range(1, n + 1):
                snapshot_checks(image, out, name, phase, k, problems, observed)
    if room is not None:
        took = record(out, "%s-rw-v1-room" % name)
        back = record(out, "%s-back-v1-room" % name)
        if took is None or not took["written"] or took["taken"] != room:
            problems.append("the room of %d bytes was not taken: %s" % (
                room, took and (took["taken"], took.get("stage"), took.get("error"))))
        if back is None or not back["same"]:
            problems.append("the room did not read back: %s" % (back and back.get("error")))
    for k in range(1, n + 1):
        p = "%s-rw-v%d" % (name, k)
        if status.get(p + "-mount") != 0:
            problems.append("%s: the read-write mount failed" % p)
        elif full:
            took = record(out, p)
            if took is None:
                problems.append("%s: no record of the write" % p)
            elif took["written"]:
                problems.append("%s: the file was written, to a container stated full" % p)
            elif took.get("errno") != "ENOSPC":
                problems.append("%s: the %s failed otherwise than for want of room: %s"
                                % (p, took.get("stage"), took.get("error")))
        elif status.get(p + "-write") != 0:
            problems.append("%s: the file was not written" % p)
        r = "%s-back-v%d" % (name, k)
        if status.get(r + "-mount") != 0:
            problems.append("%s: the mount after the write failed" % r)
        elif full:
            back = record(out, r)
            if back is None:
                problems.append("%s: no record of the read" % r)
            elif not back["absent"] and "error" in back:
                problems.append("%s: the file did not read: %s" % (r, back["error"]))
            elif not (back["absent"] or back["prefix"]):
                problems.append("%s: the file holds what was not written" % r)
        elif status.get(r + "-read") != 0:
            problems.append("%s: the file did not read back" % r)
    if not fsck_passes(image, out, name + "-fsck-written", status):
        problems.append("fsck_apfs -n -W exits %s after the write" % status.get(name + "-fsck-written"))
    result = {
        "name": name,
        "expect": image["expect"],
        "control": bool(image.get("control")),
        "verdict": "accepted" if not problems else "refused",
        "problems": problems,
        "slots_listed": slots,
        "accounting": listed.get("accounting", {}),
        "observed": observed,
    }
    result["fsck_warnings"] = {k: fsck_warnings(out, name + k)[0] for k in ("-fsck", "-fsck-written")}
    result["as_expected"] = (result["verdict"] == "accepted") == (
        image["expect"] in ("accept", "warn-future-time", "full") or room is not None)
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
    elif cmd == "room-bytes" and len(argv) == 4:
        room_bytes(argv[2], argv[3])
    elif cmd == "room" and len(argv) == 6:
        room(*argv[2:])
    elif cmd == "room-read" and len(argv) == 6:
        room_read(*argv[2:])
    elif cmd == "snapshot-name" and len(argv) == 5:
        snapshot_name(*argv[2:])
    elif cmd == "verdict" and len(argv) == 5:
        verdict(*argv[2:])
    elif cmd == "index" and len(argv) == 4:
        index(argv[2], argv[3])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
