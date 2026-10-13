#!/usr/bin/env python3
"""Reads the traces observe.sh takes against the extents of the files it wrote.

    observeutil.py verify IMAGE L2P DIR OUT.json
    observeutil.py match-text TRACE L2P DISK FIELD OUT.json
    observeutil.py match-ktrace TRACE L2P OUT.json KEPT.ndjson|- [--all-codes]
    observeutil.py log-types LOG.ndjson

L2P is l2p's output: one line per extent holding the file's path, the offset
in the file, the device offset F_LOG2PHYS_EXT returned, and the contiguous
length, all in bytes. An extent's start is its device offset.

`verify` reads each extent's bytes from IMAGE at its device offset and
compares them with the same bytes of the file's copy in DIR, which tells
whether the device offsets are offsets in the image.

`match-text` reads a trace holding one I/O per line: fs_usage's, whose block
number follows D=0x in hex (FIELD D=), or DTrace's, whose follows blk= in
decimal (FIELD blk=). A line matches when its number, read as bytes,
512-byte sectors or 4 KiB blocks, is an extent's start. OUT.json counts the
lines naming each disk, the distinct starts matched in each unit by the lines
naming DISK and by the rest, and, for the unit with the most on DISK, the
second word of DISK's matching lines and of its others: fs_usage's
operation, or DTrace's first field after the timestamp.

`match-ktrace` reads ktrace's ndjson output. An event's code is its debugid
field. Every other number the event carries, a JSON number or a hex or
decimal string, is read in the same three units. An event starts at an
extent when one number is the extent's start in some unit and another is
its length, in bytes or in that unit. A code matches when its events start
at as many distinct extents in one unit as half the extents there are, and
at least four. OUT.json records the trace's size, the field names seen, and
each matching code with its counts per unit, the fields its matches were in
and the starts matched without a length beside them; of the codes that do
not match, only their number, unless --all-codes records each. KEPT.ndjson,
unless it is -, holds each matching code's first 200 events and up to 2,000
of its matching ones.

`log-types` counts the messages of log(1)'s ndjson output by process,
sender and message type, most first.

The filter ktrace takes for the matching codes is printed: S and each code's
high 16 bits, as ktrace(1) writes a subclass. That this is where a debugid
holds its class and subclass is a hypothesis, which a trace taken under the
filter tests: it holds when that trace carries the same codes.
"""

import collections
import json
import os
import re
import sys

UNITS = (1, 512, 4096)
SAMPLE = 200
MATCHED = 2000


def extents(path):
    """(path, file offset, device offset, length) per line of l2p's output."""
    out = []
    for line in open(path, encoding="utf-8", errors="replace"):
        parts = line.rstrip("\n").rsplit(" ", 3)
        if len(parts) != 4:
            continue
        try:
            out.append((parts[0], int(parts[1]), int(parts[2]), int(parts[3])))
        except ValueError:
            continue
    return out


def write_json(path, value):
    with open(path, "w") as f:
        json.dump(value, f, indent=1, sort_keys=True)
        f.write("\n")


def verify(argv):
    image, l2p, folder, out = argv
    rows = []
    with open(image, "rb") as img:
        for path, foff, doff, n in extents(l2p):
            copy = os.path.join(folder, os.path.basename(path))
            with open(copy, "rb") as f:
                f.seek(foff)
                want = f.read(n)
            img.seek(doff)
            got = img.read(len(want))
            rows.append({"path": path, "file_offset": foff, "device_offset": doff,
                         "length": n, "compared": len(want), "match": got == want})
    write_json(out, {"extents": rows, "matching": sum(r["match"] for r in rows)})
    print(f"{sum(r['match'] for r in rows)} of {len(rows)} extents match the image")


def starts(l2p):
    """Each extent's start, with its length."""
    return {doff: n for _, _, doff, n in extents(l2p)}


def hits(value, at):
    """The units in which value is one of the starts in at."""
    return [u for u in UNITS if value * u in at]


def match_text(argv):
    trace, l2p, disk, field, out = argv
    at = starts(l2p)
    if field == "D=":
        pattern, base = re.compile(r"\bD=0x([0-9A-Fa-f]+)"), 16
    elif field == "blk=":
        pattern, base = re.compile(r"\bblk=(\d+)"), 10
    else:
        raise SystemExit(f"no field {field}")
    on_disk = re.compile(r"\b" + re.escape(disk) + r"\b")
    any_disk = re.compile(r"\bdisk\d+(?:s\d+)*\b")
    lines = 0
    by_disk = collections.Counter()
    elsewhere = {u: set() for u in UNITS}
    per_unit = {u: set() for u in UNITS}
    words = []
    for line in open(trace, encoding="utf-8", errors="replace"):
        m = pattern.search(line)
        if not m:
            continue
        by_disk.update(set(any_disk.findall(line)) or {""})
        value = int(m.group(1), base)
        units = hits(value, at)
        if not on_disk.search(line):
            for u in units:
                elsewhere[u].add(value * u)
            continue
        lines += 1
        for u in units:
            per_unit[u].add(value * u)
        split = line.split()
        words.append((units, split[1] if len(split) > 1 else ""))
    best = max(UNITS, key=lambda u: len(per_unit[u]))
    matched, rest = collections.Counter(), collections.Counter()
    for units, word in words:
        (matched if best in units else rest)[word] += 1
    write_json(out, {
        "extents": len(at), "lines_on_disk": lines, "lines_by_disk": dict(by_disk),
        "distinct_starts": {str(u): len(per_unit[u]) for u in UNITS},
        "distinct_starts_on_other_disks": {str(u): len(elsewhere[u]) for u in UNITS},
        "best_unit": best if per_unit[best] else None,
        "matching_words": dict(matched), "other_words": dict(rest),
    })
    print(f"{lines} lines on {disk}; distinct starts by unit "
          f"{ {u: len(per_unit[u]) for u in UNITS} }")


def numbers(value, key, out):
    """Every number in value, with the key path it was found at."""
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        out.append((key, value))
    elif isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"0[xX][0-9A-Fa-f]+", s):
            out.append((key, int(s, 16)))
        elif re.fullmatch(r"\d+", s):
            out.append((key, int(s)))
    elif isinstance(value, dict):
        for k, v in value.items():
            numbers(v, f"{key}.{k}" if key else k, out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            numbers(v, f"{key}[{i}]", out)


def code_key(event):
    for k in event:
        if "debugid" in k.lower():
            return k
    return None


def code_of(event, key):
    if key is None:
        return None
    found = []
    numbers(event.get(key), "", found)
    return found[0][1] if found else None


def events(trace):
    for line in open(trace, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield line, event


def event_hits(found, at):
    """(path, unit, start, with_length) for each number in found that is a
    start in at, with_length telling whether another number is its length."""
    out = []
    for path, value in found:
        for u in hits(value, at):
            n = at[value * u]
            with_length = any(p != path and (v == n or v * u == n) for p, v in found)
            out.append((path, u, value * u, with_length))
    return out


def match_ktrace(argv):
    all_codes = "--all-codes" in argv
    argv = [a for a in argv if a != "--all-codes"]
    trace, l2p, out, kept = argv
    at = starts(l2p)
    need = max(4, len(at) // 2)
    total, unreadable_code = 0, 0
    fields = collections.Counter()
    count = collections.Counter()
    matched = collections.Counter()
    distinct = collections.defaultdict(lambda: {u: set() for u in UNITS})
    bare = collections.defaultdict(lambda: {u: set() for u in UNITS})
    where = collections.defaultdict(collections.Counter)
    for _, event in events(trace):
        total += 1
        fields.update(event.keys())
        key = code_key(event)
        code = code_of(event, key)
        if code is None:
            unreadable_code += 1
            continue
        count[code] += 1
        found = []
        for k, v in event.items():
            if k != key:
                numbers(v, k, found)
        any_hit = False
        for path, u, start, with_length in event_hits(found, at):
            if with_length:
                distinct[code][u].add(start)
                where[code][f"{path}@{u}"] += 1
                any_hit = True
            else:
                bare[code][u].add(start)
        if any_hit:
            matched[code] += 1
    match = sorted(c for c in distinct
                   if max(len(s) for s in distinct[c].values()) >= need)

    def record(c):
        return {"code": f"0x{c:08x}", "events": count[c], "matching_events": matched[c],
                "distinct_starts": {str(u): len(distinct[c][u]) for u in UNITS},
                "starts_without_length": {str(u): len(bare[c][u]) for u in UNITS},
                "fields": dict(where[c])}

    summary = {
        "trace_bytes": os.path.getsize(trace), "events": total,
        "events_without_code": unreadable_code, "fields": dict(fields),
        "extents": len(at), "starts_needed": need, "codes": len(count),
        "matching": [record(c) for c in match],
    }
    if all_codes:
        summary["all_codes"] = [record(c) for c in sorted(count)]
    else:
        summary["codes_not_matching"] = len(count) - len(match)
    filt = ",".join(sorted({f"S0x{(c >> 16) & 0xffff:04x}" for c in match}))
    summary["filter"] = filt
    write_json(out, summary)

    if kept != "-":
        want = set(match)
        first = collections.Counter()
        hit = collections.Counter()
        with open(kept, "w") as f:
            for line, event in events(trace):
                key = code_key(event)
                code = code_of(event, key)
                if code not in want:
                    continue
                found = []
                for k, v in event.items():
                    if k != key:
                        numbers(v, k, found)
                is_hit = any(h[3] for h in event_hits(found, at))
                if first[code] < SAMPLE:
                    first[code] += 1
                elif not (is_hit and hit[code] < MATCHED):
                    continue
                if is_hit:
                    hit[code] += 1
                f.write(line + "\n")
    print(filt)


def log_types(argv):
    (path,) = argv
    count = collections.Counter()
    for _, event in events(path):
        count[(os.path.basename(str(event.get("processImagePath", ""))),
               os.path.basename(str(event.get("senderImagePath", ""))),
               str(event.get("messageType", "")))] += 1
    for (process, sender, kind), n in count.most_common():
        print(n, process, sender, kind)


def main(argv):
    if len(argv) < 2:
        raise SystemExit(__doc__)
    cmd, rest = argv[1], argv[2:]
    if cmd == "verify":
        verify(rest)
    elif cmd == "match-text":
        match_text(rest)
    elif cmd == "match-ktrace":
        match_ktrace(rest)
    elif cmd == "log-types":
        log_types(rest)
    else:
        raise SystemExit(f"no command {cmd}")


if __name__ == "__main__":
    main(sys.argv)
