#!/usr/bin/env python3
"""How a firmlink is kept, read from the runner's own system without naming
any path, and the same kept on a directory of the probe's own.

    grouputil.py firmlinks OUT.json
    grouputil.py mark DIR TARGET RECIPE.json
    grouputil.py marks DIR

`firmlinks` reads /usr/share/firmlinks and the directories its lines name, and
writes to OUT.json the file's shape and how those directories are kept,
beside an ordinary directory of the system volume, /bin. The directories are
read twice: at /, and through the snapshot the system roots from, mounted
read-only in a directory of its own, where no firmlink is followed. Neither
the device nor the snapshot is named in what is written: only whether that
mount was made. Of the file: its
number of lines, of blank lines and of lines beginning "#", the number of
fields of each line split at a tab, how many first and second fields begin
"/", and whether it ends with a newline. Of the directories: how many there
are and how many lstat finds; each distinct st_flags value with how many hold
it, and /bin's; the names of their extended attributes beginning
"com.apple.fs.", with how many carry each, and how many carry any other; and,
for each such name, how its value relates to the line's second field: equal
to it, equal to it with a NUL after it, equal to the first field, or none of
these. No path, field or value is written. The recipe drawn from the
snapshot's reading, or from the one at / where the snapshot was not mounted,
is the flag bits every listed directory holds and /bin does not, and each attribute
name every listed directory carries, with the relation every one of them
shows; it is printed and kept in OUT.json as "recipe".

`mark` keeps that recipe on DIR as a firmlink to TARGET: each attribute set
with the value the relation gives, and the flag bits added to DIR's own, each
step reported with its errno where it fails.

`marks` prints DIR's st_flags and its extended attributes, values in hex.
"""

import ctypes
import errno
import json
import os
import plistlib
import subprocess
import sys
import tempfile

# listxattr(2), getxattr(2) and setxattr(2) as macOS declares them, each
# taking an options word; XATTR_NOFOLLOW acts on a symbolic link itself.
LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
LIBC.listxattr.restype = ctypes.c_ssize_t
LIBC.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                          ctypes.c_uint32, ctypes.c_int]
LIBC.getxattr.restype = ctypes.c_ssize_t
LIBC.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                          ctypes.c_uint32, ctypes.c_int]
LIBC.setxattr.restype = ctypes.c_int
XATTR_NOFOLLOW = 0x0001


def _fail():
    e = ctypes.get_errno()
    raise OSError(e, os.strerror(e))


def listxattr(path):
    p = os.fsencode(path)
    n = LIBC.listxattr(p, None, 0, XATTR_NOFOLLOW)
    if n < 0:
        _fail()
    buf = ctypes.create_string_buffer(n)
    n = LIBC.listxattr(p, buf, n, XATTR_NOFOLLOW)
    if n < 0:
        _fail()
    return [x.decode("utf-8", "surrogateescape") for x in buf.raw[:n].split(b"\0") if x]


def getxattr(path, name):
    p, a = os.fsencode(path), name.encode("utf-8", "surrogateescape")
    n = LIBC.getxattr(p, a, None, 0, 0, XATTR_NOFOLLOW)
    if n < 0:
        _fail()
    buf = ctypes.create_string_buffer(max(n, 1))
    n = LIBC.getxattr(p, a, buf, n, 0, XATTR_NOFOLLOW)
    if n < 0:
        _fail()
    return buf.raw[:n]


def setxattr(path, name, value):
    if LIBC.setxattr(os.fsencode(path), name.encode("utf-8", "surrogateescape"), value,
                     len(value), 0, XATTR_NOFOLLOW) != 0:
        _fail()


FIRMLINKS = "/usr/share/firmlinks"
CONTROL = "/bin"
PREFIX = "com.apple.fs."


def relation(value, first, second):
    if value == second:
        return "second"
    if value == second + b"\0":
        return "second+nul"
    if value == first:
        return "first"
    return "other"


def mount_root_snapshot():
    """Mount the snapshot the system roots from read-only in a new directory:
    (directory, whether it was mounted). Nothing about the device is kept."""
    try:
        r = subprocess.run(["diskutil", "info", "-plist", "/"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=60)
        info = plistlib.loads(r.stdout)
    except (subprocess.TimeoutExpired, ValueError, plistlib.InvalidFileException):
        return None, False
    dev = info.get("DeviceIdentifier")
    snap = info.get("APFSSnapshotName")
    parent = info.get("APFSSnapshotParentDevice") or info.get("ParentWholeDisk")
    if not dev:
        return None, False
    mnt = tempfile.mkdtemp(prefix="sysroot.")
    tries = [["mount_apfs", "-o", "rdonly", "/dev/" + dev, mnt]]
    if snap and parent:
        tries.append(["mount_apfs", "-o", "rdonly", "-s", snap, "/dev/" + parent, mnt])
    for cmd in tries:
        try:
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=120)
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0 and os.path.ismount(mnt):
            return mnt, True
    return mnt, False


def directories(lines, root):
    """How the directories LINES name are kept, read under ROOT."""
    flags = {}
    names = {}
    other = 0
    relations = {}
    found = 0
    for first, second in lines:
        try:
            st = os.lstat(under(root, first))
        except OSError:
            continue
        found += 1
        flags[st.st_flags] = flags.get(st.st_flags, 0) + 1
        try:
            attrs = listxattr(under(root, first))
        except OSError:
            attrs = []
        for a in attrs:
            if not a.startswith(PREFIX):
                other += 1
                continue
            names[a] = names.get(a, 0) + 1
            try:
                v = getxattr(under(root, first), a)
                r = relation(v, first, second)
            except OSError as e:
                r = "unreadable " + errno.errorcode.get(e.errno, str(e.errno))
            relations.setdefault(a, {})
            relations[a][r] = relations[a].get(r, 0) + 1
    try:
        control = os.lstat(under(root, CONTROL)).st_flags
    except OSError:
        control = None
    return {"listed": len(lines), "found": found,
            "st_flags": {"%#x" % k: v for k, v in sorted(flags.items())},
            "control_st_flags": None if control is None else "%#x" % control,
            "attribute_names": names, "other_attributes": other,
            "value_relations": relations}


def drawn(dirs):
    """The flag bits every listed directory holds and the control does not, and
    each attribute every one carries with the one relation all show."""
    recipe = {"flags": 0, "attributes": {}}
    found = dirs.get("found", 0)
    flags = [int(k, 16) for k in dirs.get("st_flags", {})]
    control = dirs.get("control_st_flags")
    if found and flags and control is not None:
        common = ~0
        for k in flags:
            common &= k
        recipe["flags"] = common & ~int(control, 16) & 0xFFFFFFFF
    for a, n in dirs.get("attribute_names", {}).items():
        rel = dirs.get("value_relations", {}).get(a, {})
        if n == found and len(rel) == 1:
            recipe["attributes"][a] = next(iter(rel))
    return recipe


def under(root, path):
    return os.path.join(root, path.lstrip(b"/").decode("utf-8", "surrogateescape"))


def firmlinks(out):
    shape = {"read": False}
    try:
        with open(FIRMLINKS, "rb") as f:
            data = f.read()
    except OSError as e:
        shape["error"] = errno.errorcode.get(e.errno, str(e.errno))
        data = None
    lines = []
    if data is not None:
        shape["read"] = True
        text = data.split(b"\n")
        shape["ends_with_newline"] = data.endswith(b"\n")
        if shape["ends_with_newline"]:
            text = text[:-1]
        shape["lines"] = len(text)
        shape["blank"] = sum(1 for t in text if not t.strip())
        shape["comment"] = sum(1 for t in text if t.startswith(b"#"))
        fields = {}
        first_abs = second_abs = 0
        for t in text:
            if not t.strip() or t.startswith(b"#"):
                continue
            parts = t.split(b"\t")
            fields[len(parts)] = fields.get(len(parts), 0) + 1
            if parts[0].startswith(b"/"):
                first_abs += 1
            if len(parts) > 1 and parts[1].startswith(b"/"):
                second_abs += 1
            if len(parts) == 2:
                lines.append((parts[0], parts[1]))
        shape["fields_per_line"] = {str(k): v for k, v in sorted(fields.items())}
        shape["first_field_absolute"] = first_abs
        shape["second_field_absolute"] = second_abs
        shape["any_space"] = any(b" " in t for t in text)

    views = {"root": directories(lines, "/")}
    mnt, mounted = mount_root_snapshot()
    views["snapshot"] = {"mounted": mounted}
    if mounted:
        views["snapshot"].update(directories(lines, mnt))
        subprocess.run(["umount", mnt], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if mnt:
        try:
            os.rmdir(mnt)
        except OSError:
            pass
    source = views["snapshot"] if mounted else views["root"]
    recipe = drawn(source)
    result = {"file": shape, "directories": views, "recipe_from": "snapshot" if mounted else "root",
              "recipe": recipe}
    with open(out, "w") as f:
        json.dump(result, f, indent=1)
    print(json.dumps(recipe))


def mark(d, target, recipe_path):
    with open(recipe_path) as f:
        recipe = json.load(f)["recipe"]
    t = target.encode()
    name = os.path.basename(d.rstrip("/")).encode()
    report = {}
    for a, rel in recipe["attributes"].items():
        value = {"second": t, "second+nul": t + b"\0", "first": name}.get(rel)
        if value is None:
            report[a] = "no value for relation %s" % rel
            continue
        try:
            setxattr(d, a, value)
            report[a] = "set"
        except OSError as e:
            report[a] = errno.errorcode.get(e.errno, str(e.errno))
    if recipe["flags"]:
        try:
            st = os.lstat(d)
            os.chflags(d, st.st_flags | recipe["flags"], follow_symlinks=False)
            report["flags"] = "set %#x" % recipe["flags"]
        except OSError as e:
            report["flags"] = errno.errorcode.get(e.errno, str(e.errno))
    print(json.dumps(report))
    if any(v not in ("set",) and not str(v).startswith("set ") for v in report.values()):
        raise SystemExit(1)


def marks(d):
    st = os.lstat(d)
    out = {"st_flags": "%#x" % st.st_flags, "attributes": {}}
    for a in listxattr(d):
        try:
            out["attributes"][a] = getxattr(d, a).hex()
        except OSError as e:
            out["attributes"][a] = errno.errorcode.get(e.errno, str(e.errno))
    print(json.dumps(out, indent=1))


def main(argv):
    if len(argv) == 3 and argv[1] == "firmlinks":
        firmlinks(argv[2])
    elif len(argv) == 5 and argv[1] == "mark":
        mark(argv[2], argv[3], argv[4])
    elif len(argv) == 3 and argv[1] == "marks":
        marks(argv[2])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
