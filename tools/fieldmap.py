#!/usr/bin/env python3
"""Read a mutate sweep back as a map of each swept block's fields.

    fieldmap.py DIR...           a report per swept block
    fieldmap.py --types DIR...   one report per object type and subtype,
                                 from every block of that kind

Each DIR is an output directory of probe/mutate.py. A change is noticed
when the checker's output differs from its output for the unchanged image,
and a noticed output is reduced to a signature: the lines the unchanged
output lacks, with every number written as # and a hex dump's character
column dropped, so that the same complaint about different values reads
the same. A change that ran past the checker's time limit has the
signature "timeout". Adjacent bytes of one block with the same signature
form a run, and so do adjacent bytes the checker did not notice. For each
noticed run the report gives the words in its first complaint (error or
warning line) that names anything, where names are words that look like
field names: lower-case words joined by underscores, with any index or
member that follows them. Names from the other complaints come after
"also", since a wrong value can set off complaints about the fields that
depend on it.

--types gathers the noticed runs of every block of one type and subtype,
and marks a run that not every block of that kind shares with how many do:
a block the checker never reads, such as an older copy, shares none.
"""

import collections
import json
import os
import re
import sys

NUMBER = re.compile(r"0x[0-9a-fA-F]+|\b[0-9a-fA-F]*[0-9][0-9a-fA-F]*\b")
DUMP_TEXT = re.compile(r"\|[^|]*\|\s*$")
NAME = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?:\[#\])?(?:\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*)*")


def normalize(line):
    return NUMBER.sub("#", DUMP_TEXT.sub("|", line)).rstrip()


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
        texts = {}
        with open(os.path.join(d, "outputs.jsonl")) as f:
            for line in f:
                o = json.loads(line)
                texts[o["id"]] = o["text"]
        base = set(normalize(l) for l in texts[0].splitlines())
        self.signatures = {}
        for i, text in texts.items():
            new = []
            for l in text.splitlines():
                n = normalize(l)
                if n not in base and n not in new:
                    new.append(n)
            self.signatures[i] = tuple(new)
        self.changes = collections.defaultdict(dict)
        with open(os.path.join(d, "results.tsv")) as f:
            next(f)
            for line in f:
                n, off, change, status, _, out, _ = line.rstrip("\n").split("\t")
                sig = ("timeout",) if status == "timeout" else self.signatures[int(out)]
                self.changes[int(n)][(int(off), change)] = sig

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


def lead(sig):
    """The first complaint of a signature that names a field, else its first
    error or warning line."""
    complaints = [l for l in sig if l.startswith(("error", "warning"))] or list(sig)
    return next((l for l in complaints if NAME.search(l)), complaints[0])


def names(lines):
    found = []
    for line in lines:
        for m in NAME.findall(line):
            if m not in found:
                found.append(m)
    return found


def describe(first, last, sig, note=""):
    span = "%d" % first if first == last else "%d..%d" % (first, last)
    if not sig:
        return "  %-11s unnoticed%s" % (span, note)
    if sig == ("timeout",):
        return "  %-11s the checker ran past its time limit%s" % (span, note)
    first_line = lead(sig)
    primary = names([first_line])
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


def main(argv):
    types = "--types" in argv
    dirs = [a for a in argv[1:] if a != "--types"]
    if not dirs:
        raise SystemExit(__doc__)
    sweeps = [Sweep(d) for d in dirs]
    (by_type if types else report)(sweeps)


if __name__ == "__main__":
    main(sys.argv)
