#!/usr/bin/env python3
"""Read the plists hdiutil and diskutil print.

    disks.py attach-dev ATTACH.plist
        the whole-disk device an `hdiutil attach -plist` created
    disks.py container-of APFS-LIST.plist DEV
        the APFS container whose physical store is DEV or a slice of it
    disks.py volume-named APFS-LIST.plist CONTAINER NAME
        the device of the volume called NAME in CONTAINER
    disks.py volumes APFS-LIST.plist CONTAINER
        every volume of CONTAINER, one "device<TAB>name" line each
    disks.py key PLIST KEY
        the top-level value KEY, as `diskutil info -plist` prints MountPoint
    disks.py snapshots SNAPSHOTS.plist
        the name of each snapshot `diskutil apfs listSnapshots -plist` lists
    disks.py tm-dest DESTINATIONS.plist MOUNT
        the ID of the Time Machine destination mounted at MOUNT, read from
        `tmutil destinationinfo -X`
"""

import os
import plistlib
import re
import sys

# The content hint of the container disk APFS synthesizes over its store.
APFS_CONTAINER = "EF57347C-0000-11AA-AA11-00306543ECAC"


def load(path):
    with open(path, "rb") as f:
        data = f.read()
    start = data.find(b"<?xml")
    if start < 0:
        raise SystemExit("%s: no plist" % path)
    return plistlib.loads(data[start:])


def bare(dev):
    return os.path.basename(dev)


def containers(listing):
    return listing.get("Containers", [])


def main(argv):
    if len(argv) == 3 and argv[1] == "attach-dev":
        # The image's whole disk, which is listed in no fixed order beside
        # its slices and the container disk APFS synthesizes over a slice.
        entities = load(argv[2]).get("system-entities", [])
        whole = [e for e in entities if re.fullmatch(r"/dev/disk[0-9]+", e.get("dev-entry", ""))]
        image = [e for e in whole if e.get("content-hint") != APFS_CONTAINER]
        if not image:
            raise SystemExit("no image disk in attach output")
        print(image[0]["dev-entry"])
    elif len(argv) == 4 and argv[1] == "container-of":
        dev = bare(argv[3])
        for c in containers(load(argv[2])):
            stores = [s.get("DeviceIdentifier", "") for s in c.get("PhysicalStores", [])]
            if any(s == dev or s.startswith(dev + "s") for s in stores):
                print(c["ContainerReference"])
                return
        raise SystemExit("no container on %s" % dev)
    elif len(argv) == 5 and argv[1] == "volume-named":
        for c in containers(load(argv[2])):
            if c.get("ContainerReference") == bare(argv[3]):
                for v in c.get("Volumes", []):
                    if v.get("Name") == argv[4]:
                        print(v["DeviceIdentifier"])
                        return
        raise SystemExit("no volume %s in %s" % (argv[4], argv[3]))
    elif len(argv) == 4 and argv[1] == "volumes":
        for c in containers(load(argv[2])):
            if c.get("ContainerReference") == bare(argv[3]):
                for v in c.get("Volumes", []):
                    print("%s\t%s" % (v.get("DeviceIdentifier"), v.get("Name")))
    elif len(argv) == 4 and argv[1] == "key":
        value = load(argv[2]).get(argv[3])
        if value is None:
            raise SystemExit("no %s in %s" % (argv[3], argv[2]))
        print(value)
    elif len(argv) == 3 and argv[1] == "snapshots":
        for s in load(argv[2]).get("Snapshots", []):
            name = s.get("SnapshotName", s.get("Name"))
            if name:
                print(name)
    elif len(argv) == 4 and argv[1] == "tm-dest":
        dests = load(argv[2]).get("Destinations", [])
        ids = [d["ID"] for d in dests if "ID" in d and d.get("MountPoint") == argv[3]]
        if not ids and len(dests) == 1 and "ID" in dests[0]:
            ids = [dests[0]["ID"]]
        if not ids:
            raise SystemExit("no destination at %s" % argv[3])
        print(ids[0])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
