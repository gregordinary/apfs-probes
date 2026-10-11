#!/usr/bin/env python3
"""Read a mutate sweep back as a map of each swept block's fields.

    fieldmap.py DIR...             a report per swept block
    fieldmap.py --types DIR...     one report per object type and subtype,
                                   from every block of that kind
    fieldmap.py --records DIR...   one report per kind of B-tree record,
                                   from every record of that kind

Each DIR is an output directory of probe/mutate.py. A change is noticed
when the checker's output differs from its output for the unchanged image,
and a noticed output is reduced to a signature: the lines the unchanged
output lacks, with every number written as #, a hex dump's character
column and a dumped key's bytes dropped, and a quoted file or attribute
name written as *, so that the same complaint about different values reads
the same. A change that ran past the checker's time limit has the
signature "timeout". Adjacent bytes of one block with the same signature
form a run, and so do adjacent bytes the checker did not notice.

For each noticed run the report gives the names in its first complaint
(error or warning line) that names anything. A name is a word that looks
like a field name: lower-case words joined by underscores, with any index
or member that follows them. A complaint holding none is named, in quotes,
by what it calls invalid or unknown ('key' for "invalid key (#, #)") or
says does not match. A line that only lists values, such as the "btn: oid
(#), xid (#), ..." line the checker prints before a complaint about a node,
is not a complaint. Names from the other complaints come after "also",
since a wrong value can set off complaints about the fields that depend on
it.

--types gathers the noticed runs of every block of one type and subtype,
and marks a run that not every block of that kind shares with how many do:
a block the checker never reads, such as an older copy, shares none.

--records reads the B-tree nodes among the swept blocks from the base
image, which mutate.sh keeps as images/NAME.bd beside the sweep's
mutate/NAME, and gives each byte of a node's table, keys and values its
place in a record, so that one byte of every record of a kind is read
together. A node is read with the layout the sweep finds in the blocks of
object type 2, which carry both a node header and a footer:

    36       btn_nkeys, the number of entries in the table, 32 bits
    40, 42   btn_table_space: the table's offset from the end of the
             header, and its length, 16 bits each
    44, 46   btn_free_space: the free space's offset from the end of the
             table, and its length
    48, 52   btn_key_free_list and btn_val_free_list, an offset and a
             length each
    56       the end of the header, whose last named field ends at 55
    4056     the footer, to the end of the block, with bt_key_size at 4064
             and bt_val_size at 4068, 32 bits each

The size of a table entry is the toc_entry_size the checker prints beside
btn_nkeys when btn_nkeys or btn_table_space is changed. An 8-byte entry holds a key's offset and
length and then a value's, 16 bits each, in the order the checker prints
them in "invalid key (#, #)" and "invalid value (#, #)" when a byte of the
entry is changed. A 4-byte entry holds the two offsets, and every key and
value has the footer's fixed size. A key's offset counts forward from the
end of the table, a value's back from the footer, and a value offset of
0xffff, which would place the value outside the block, is read as no value.
A node is read this way only if every entry fits the table, every key lies
before the free space and every value after it, no two keys or values
overlap, and the bytes before the free space that are in no key add up to
the key free list's length, as the bytes after it in no value add up to
the value free list's; a block that breaks one of these rules is reported
with it. A block where the checker noticed no change is left out, since it
holds no complaint to place.

Records are grouped by the tree's subtype and their key's eighth byte:
changing that byte of a file-system record makes the checker call the
record by another name, a directory record a dstream id, an inode a
sibling map. A kind is labelled with the name the checker most often gives
the records it complains about: the "NAME (id #" or "NAME (oid #" that
opens a complaint, or the "of NAME object (id #" within one, leaving out
the complaints about the eighth byte itself. Each byte of a key, a value or
a table entry is named from the most frequent named signature among the
records that have that byte, counting first the complaints about a record
of the kind's own name, since a changed value can also break a count kept
elsewhere (an identifier past apfs_next_obj_id). The report gives how many
of those records the checker noticed and the names the other signatures
give the byte; adjacent bytes with the same name form a run.
The free space is reported by offset from its start, and the bytes the
free lists account for by offset from the start of each gap they fill.
"""

import collections
import hashlib
import json
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blockdump  # noqa: E402

NUMBER = re.compile(r"0x[0-9a-fA-F]+|\b[0-9a-fA-F]*[0-9][0-9a-fA-F]*\b")
DUMP_TEXT = re.compile(r"\|[^|]*\|\s*$")
KEY_DUMP = re.compile(r"^(\s*index # key\s*:).*$")
QUOTED_NAME = re.compile(r"\bname \(.*?\)(?=, given| mismatch|$)")
LISTED_NAME = re.compile(r"(\(id #, name )[^)]*\)")
NAME = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?:\[#\])?(?:\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*)*")
PHRASE = re.compile(r"\b(?:invalid|unknown) ([a-z][a-z0-9 ]*?)(?= \(|:|,| given| of | on | for |$)"
                    r"|\b([a-z][a-z_]*) \(#\) (?:does not|doesn't) match")
CONTEXT = re.compile(r"^(?:error|warning): (?:[a-z]+: )*[a-z_]+ \(#\)(?:,? [a-z_]+ \(#\))*$")
SUBJECT = re.compile(r"^(?:error|warning): (?:\(oid #\) )?([a-z][a-z ]*?) \((?:id|oid) #"
                     r"|\bof ([a-z][a-z ]*?) object \(id #")
ENTRY_SIZE = re.compile(r"toc_entry_size \((\d+)\)")

BS = 4096
NODE_TYPE = 2
HEADER_END = 56
FOOTER = 4056
NO_VALUE = 0xFFFF


def normalize(line):
    line = NUMBER.sub("#", DUMP_TEXT.sub("|", line)).rstrip()
    line = QUOTED_NAME.sub("name (*)", line)
    line = LISTED_NAME.sub(r"\1*)", line)
    return KEY_DUMP.sub(r"\1 *", line)


class Sweep:
    """One mutate.py output directory."""

    def __init__(self, d):
        self.dir = d
        self.label = os.path.relpath(d)
        self.blocks = {}
        with open(os.path.join(d, "blocks.tsv")) as f:
            next(f)
            for line in f:
                n, obj, t, s = line.rstrip("\n").split("\t")
                self.blocks[int(n)] = (obj == "1", t and int(t), s and int(s))
        self.texts = {}
        with open(os.path.join(d, "outputs.jsonl")) as f:
            for line in f:
                o = json.loads(line)
                self.texts[o["id"]] = o["text"]
        base = set(normalize(l) for l in self.texts[0].splitlines())
        self.signatures = {}
        for i, text in self.texts.items():
            new = []
            for l in text.splitlines():
                n = normalize(l)
                if n not in base and n not in new:
                    new.append(n)
            self.signatures[i] = tuple(new)
        self.changes = collections.defaultdict(dict)
        self.outputs = collections.defaultdict(dict)
        with open(os.path.join(d, "results.tsv")) as f:
            next(f)
            for line in f:
                n, off, change, status, _, out, _ = line.rstrip("\n").split("\t")
                sig = ("timeout",) if status == "timeout" else self.signatures[int(out)]
                self.changes[int(n)][(int(off), change)] = sig
                self.outputs[int(n)][(int(off), change)] = int(out)

    def runs(self, n):
        """[(first, last, signature)] over the block's flipped bytes, in order."""
        flips = sorted((off, sig) for (off, change), sig in self.changes[n].items()
                       if change == "flip")
        out = []
        for off, sig in flips:
            if out and out[-1][1] == off - 1 and out[-1][2] == sig:
                out[-1] = (out[-1][0], off, sig)
            else:
                out.append((off, off, sig))
        return out

    def dump(self):
        """The dump of the swept image, which mutate.sh keeps beside the sweep."""
        name = os.path.basename(os.path.normpath(self.dir))
        return os.path.join(self.dir, os.pardir, os.pardir, "images", name + ".bd")

    def image(self):
        """The swept image's blocks as {number: bytes}, from its dump, checked
        against the digest the sweep recorded."""
        path = self.dump()
        it = blockdump.read_dump(path)
        size, bs = next(it)
        blocks = {}
        for first, data in it:
            for i in range(0, len(data), bs):
                blocks[first + i // bs] = data[i:i + bs]
        digest = hashlib.sha256()
        zero = bytes(bs)
        for n in range((size + bs - 1) // bs):
            digest.update(blocks.get(n, zero)[:size - n * bs])
        with open(os.path.join(self.dir, "summary.json")) as f:
            want = json.load(f)["image_sha256"]
        if bs != BS or digest.hexdigest() != want:
            raise SystemExit("%s: not the image %s swept" % (os.path.relpath(path), self.label))
        return blocks

    def entry_size(self, n):
        """The table entry size the checker printed for block n, if it did."""
        for off in range(36, 44):
            out = self.outputs[n].get((off, "flip"))
            m = out is not None and ENTRY_SIZE.search(self.texts[out])
            if m:
                return int(m.group(1))
        return None


def complaints(sig):
    lines = [l for l in sig if l.startswith(("error", "warning"))]
    return [l for l in lines if not CONTEXT.match(l)] or lines or list(sig)


def naming(line):
    """The names in one complaint, or the quoted phrase that stands for them."""
    found = []
    for m in NAME.findall(line):
        if m not in found:
            found.append(m)
    if not found:
        m = PHRASE.search(line)
        if m:
            found.append("'%s'" % (m.group(1) or m.group(2)))
    return found


def lead(sig):
    """The first complaint of a signature that names anything, else its first."""
    cs = complaints(sig)
    return next((l for l in cs if naming(l)), cs[0])


def names(lines):
    found = []
    for line in lines:
        for m in NAME.findall(line):
            if m not in found:
                found.append(m)
    return found


def label(sig):
    """What a signature calls the byte it was set off by."""
    if sig == ("timeout",):
        return "the checker ran past its time limit"
    return ", ".join(naming(lead(sig))) or "-"


def describe(first, last, sig, note=""):
    span = "%d" % first if first == last else "%d..%d" % (first, last)
    if not sig:
        return "  %-11s unnoticed%s" % (span, note)
    if sig == ("timeout",):
        return "  %-11s the checker ran past its time limit%s" % (span, note)
    first_line = lead(sig)
    primary = naming(first_line)
    others = [n for n in names(sig) if n not in primary]
    text = ", ".join(primary) or "-"
    if others:
        text += "  (also " + ", ".join(others) + ")"
    return "  %-11s %s%s\n  %-11s   %s" % (span, text, note, "", first_line[:110])


def kind(sweep, n):
    obj, t, s = sweep.blocks[n]
    return "object type %d subtype %d" % (t, s) if obj else "not an object"


def report(sweeps):
    for sw in sweeps:
        for n in sorted(sw.changes):
            print("%s block %d: %s" % (sw.label, n, kind(sw, n)))
            check = sw.changes[n].get((0, "check"))
            if check is not None:
                print("  %-11s %s" % ("check", "noticed" if check else "unnoticed"))
            for first, last, sig in sw.runs(n):
                print(describe(first, last, sig))
            print()


def by_type(sweeps):
    kinds = collections.defaultdict(list)
    for sw in sweeps:
        for n in sw.changes:
            obj, t, s = sw.blocks[n]
            if obj and len(sw.changes[n]) == 4089:
                kinds[(t, s)].append(sw.runs(n))
    for (t, s), blocks in sorted(kinds.items()):
        counts = collections.Counter(r for runs in blocks for r in runs)
        print("type %d subtype %d: %d complete blocks" % (t, s, len(blocks)))
        for (first, last, sig), c in sorted(counts.items()):
            if sig:
                note = "" if c == len(blocks) else "  [%d of %d blocks]" % (c, len(blocks))
                print(describe(first, last, sig, note))
        print()


def u16(b, off):
    return struct.unpack_from("<H", b, off)[0]


def u32(b, off):
    return struct.unpack_from("<I", b, off)[0]


class Node:
    """A node block read with the layout --records describes."""

    def __init__(self, block, entry_size):
        if entry_size not in (4, 8):
            raise ValueError("the checker gives its table entries %d bytes" % entry_size)
        self.nkeys = u32(block, 36)
        self.table = HEADER_END + u16(block, 40)
        self.entry_size = entry_size
        keys = self.table + u16(block, 42)
        free = keys + u16(block, 44)
        values = free + u16(block, 46)
        self.free = (free, values)
        if self.table + self.nkeys * entry_size > keys:
            raise ValueError("%d entries of %d bytes overrun the table, which ends at %d"
                             % (self.nkeys, entry_size, keys))
        self.records = []
        for i in range(self.nkeys):
            e = self.table + i * entry_size
            if entry_size == 8:
                ko, kl, vo, vl = struct.unpack_from("<4H", block, e)
            else:
                ko, vo = struct.unpack_from("<2H", block, e)
                kl, vl = u32(block, FOOTER + 8), u32(block, FOOTER + 12)
            k = (keys + ko, keys + ko + kl)
            v = (FOOTER, FOOTER) if vo == NO_VALUE else (FOOTER - vo, FOOTER - vo + vl)
            if not keys <= k[0] <= k[1] <= free:
                raise ValueError("key %d at %d..%d is not between the table's end %d and the "
                                 "free space at %d" % (i, k[0], k[1], keys, free))
            if not values <= v[0] <= v[1] <= FOOTER:
                raise ValueError("value %d at %d..%d is not between the free space's end %d "
                                 "and the footer" % (i, v[0], v[1], values))
            self.records.append((k, v))
        self.gaps = {}
        areas = (("key", keys, free, 50), ("value", values, FOOTER, 54))
        for part, (what, lo, hi, listed) in enumerate(areas):
            spans = sorted(r[part] for r in self.records)
            for a, b in zip(spans, spans[1:]):
                if a[1] > b[0]:
                    raise ValueError("two %ss overlap at %d" % (what, b[0]))
            gaps = []
            at = lo
            for a, b in spans + [(hi, hi)]:
                if a > at:
                    gaps.append((at, a))
                at = max(at, b)
            unused = sum(b - a for a, b in gaps)
            if unused != u16(block, listed):
                raise ValueError("%d bytes are in no %s, and the %s free list holds %d"
                                 % (unused, what, what, u16(block, listed)))
            self.gaps[what] = gaps
        self.places = {}
        for off in range(self.table, self.table + self.nkeys * entry_size):
            self.places[off] = ("entry", None, (off - self.table) % entry_size)
        for i, (k, v) in enumerate(self.records):
            for off in range(*k):
                self.places[off] = ("key", i, off - k[0])
            for off in range(*v):
                self.places[off] = ("value", i, off - v[0])
        for off in range(*self.free):
            self.places[off] = ("free", None, off - free)
        for what, gaps in self.gaps.items():
            for a, b in gaps:
                for off in range(a, b):
                    self.places[off] = ("%s free list" % what, None, off - a)


RECORD_PARTS = ("key", "value")
NODE_PARTS = ("entry", "free", "key free list", "value free list")


def record_kind(subtype, part, key):
    """The group a byte falls in: its tree's subtype and its record's key's
    eighth byte, or the subtype and the part of the node outside records."""
    if part in NODE_PARTS:
        return (subtype, part)
    return (subtype, key[7] if len(key) >= 8 else None)


def kind_order(k):
    subtype, tag = k
    if tag in NODE_PARTS:
        return (subtype, 0 if tag == "entry" else 2, NODE_PARTS.index(tag))
    return (subtype, 1, -1 if tag is None else tag)


def subject(sig):
    """What kind of record a signature's first complaint is about, if it says."""
    m = SUBJECT.search(lead(sig))
    return m and (m.group(1) or m.group(2))


def merged(sigs, what):
    """(name, example line, noticed count, the other records' names) for the
    signatures one byte drew across the records that have it, where what is
    the name the checker gives records of this kind."""
    noticed = [s for s in sigs if s]
    if not noticed:
        return "unnoticed", None, 0, []
    labels = collections.Counter(label(s) for s in noticed)
    ranked = [l for l, _ in labels.most_common()]
    own = collections.Counter(label(s) for s in noticed if subject(s) == what)
    name = next((l for l, _ in own.most_common() if l != "-"),
                next((l for l in ranked if l != "-"), "-"))
    example = next(s for s in noticed if label(s) == name)
    return name, example, len(noticed), [l for l in ranked if l not in (name, "-")]


def by_record(sweeps):
    places = collections.defaultdict(lambda: collections.defaultdict(list))
    records = collections.Counter()
    blocks = collections.defaultdict(set)
    subjects = collections.defaultdict(collections.Counter)
    notes = []
    for sw in sweeps:
        image = None
        for n in sorted(sw.changes):
            obj, t, s = sw.blocks[n]
            if not obj or t != NODE_TYPE:
                continue
            flips = {off: sig for (off, change), sig in sw.changes[n].items() if change == "flip"}
            if not any(flips.values()):
                notes.append("%s block %d: left out, no change noticed" % (sw.label, n))
                continue
            size = sw.entry_size(n)
            if size is None:
                notes.append("%s block %d: not read, the checker printed no entry size"
                             % (sw.label, n))
                continue
            if image is None:
                image = sw.image()
            block = image[n]
            try:
                node = Node(block, size)
            except ValueError as e:
                notes.append("%s block %d: not read, %s" % (sw.label, n, e))
                continue
            keys = [block[k[0]:k[1]] for k, _ in node.records]
            for key in keys:
                group = record_kind(s, "key", key)
                records[group] += 1
                blocks[group].add((sw.label, n))
            for part in {p for p, _, _ in node.places.values()} & set(NODE_PARTS):
                blocks[(s, part)].add((sw.label, n))
            records[(s, "entry")] += node.nkeys
            for off, sig in flips.items():
                p = node.places.get(off)
                if p is None:
                    continue
                part, i, j = p
                group = record_kind(s, part, keys[i] if i is not None else b"")
                places[group][(part, j)].append(sig)
                if sig and part in RECORD_PARTS and (part, j) != ("key", 7):
                    about = subject(sig)
                    if about:
                        subjects[group][about] += 1
    tally = collections.Counter()
    for group in sorted(places, key=kind_order):
        subtype, tag = group
        nblocks = len(blocks[group])
        known = subjects[group].most_common(1)
        what = known[0][0] if known else None
        if tag == "entry":
            print("subtype %d, table entries: %d in %d blocks" % (subtype, records[group], nblocks))
        elif tag in NODE_PARTS:
            print("subtype %d, %s: in %d blocks"
                  % (subtype, "free space" if tag == "free" else tag, nblocks))
        else:
            byte = "none" if tag is None else "0x%02x" % tag
            print("subtype %d, key byte 7 %s: %s, %d records in %d blocks"
                  % (subtype, byte, what or "-", records[group], nblocks))
        for part in ("entry",) + RECORD_PARTS + NODE_PARTS[1:]:
            offsets = sorted(j for p, j in places[group] if p == part)
            runs = []
            for j in offsets:
                sigs = places[group][(part, j)]
                name, example, c, others = merged(sigs, what)
                if runs and runs[-1]["name"] == name and runs[-1]["last"] == j - 1:
                    r = runs[-1]
                    r["last"] = j
                    r["noticed"].add(c)
                    r["have"].add(len(sigs))
                    r["others"] += [o for o in others if o not in r["others"]]
                else:
                    runs.append({"first": j, "last": j, "name": name, "example": example,
                                 "noticed": {c}, "have": {len(sigs)}, "others": others})
            for r in runs:
                print(describe_merged(part, r))
                if tag not in NODE_PARTS:
                    tally[(subtype, part, "unnoticed" if r["name"] == "unnoticed"
                           else "unnamed" if r["name"] == "-"
                           else "phrase" if r["name"].startswith("'")
                           else "named")] += 1
        print()
    for note in notes:
        print(note)
    if notes:
        print()
    for subtype in sorted({k[0] for k in tally}):
        for part in ("key", "value"):
            c = {w: tally[(subtype, part, w)] for w in ("named", "phrase", "unnamed", "unnoticed")}
            if any(c.values()):
                print("subtype %d %s runs: %d named, %d by a phrase, %d unnamed, %d unnoticed"
                      % (subtype, part, c["named"], c["phrase"], c["unnamed"], c["unnoticed"]))


def span(lo, hi):
    return "%d" % lo if lo == hi else "%d-%d" % (lo, hi)


def describe_merged(part, r):
    where = "%s %s" % (part, r["first"] if r["first"] == r["last"]
                       else "%d..%d" % (r["first"], r["last"]))
    have = span(min(r["have"]), max(r["have"]))
    if r["name"] == "unnoticed":
        return "  %-15s unnoticed  [%s records]" % (where, have)
    text = "%s  [%s of %s]" % (r["name"], span(min(r["noticed"]), max(r["noticed"])), have)
    if r["others"]:
        text += "  (others: " + ", ".join(r["others"][:4]) + ("" if len(r["others"]) <= 4
                                                              else ", ...") + ")"
    example = "" if r["example"] == ("timeout",) else "\n  %-15s   %s" % (
        "", lead(r["example"])[:110])
    return "  %-15s %s%s" % (where, text, example)


def main(argv):
    modes = {"--types": by_type, "--records": by_record}
    chosen = [a for a in argv[1:] if a in modes]
    dirs = [a for a in argv[1:] if a not in modes]
    if not dirs or len(chosen) > 1:
        raise SystemExit(__doc__)
    sweeps = [Sweep(d) for d in dirs]
    (modes[chosen[0]] if chosen else report)(sweeps)


if __name__ == "__main__":
    main(sys.argv)
