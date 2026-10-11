#!/usr/bin/env python3
"""LZBITMAP streams built by hand, each asking libcompression's decoder one question.

The block layout these streams are built in is the one libzbitmap (MIT, copyright 2022
Corellium LLC, https://github.com/eafer/libzbitmap) decodes, and which the `study` and `oracle`
steps of the lzbitmap probe hold against Apple's encoder and decoder. A stream is the signature
`ZBM\\x09`, then blocks, the last of which decodes to nothing. Each block opens with its own
length and its decoded length, 24-bit little-endian; one whose length is its decoded length
plus six is stored raw. Any other block carries three more 24-bit offsets, from the block's
first byte, to three areas: distances, masks, and a run of 4-bit selectors. Its literals run
from byte 15 to the first area, and its last 17 bytes are a table of twelve 10-bit entries.

Decoding a compressed block repeats one step until the block's decoded length is reached. A
selector picks a mask, from the table (selectors 3 to 14) or from the mask area (0 to 2, which
is also how many distance bytes the step reads). A following selector of 15 opens a count of
repetitions, unless at most 8 bytes of the block are left. Each 1 bit of the mask, lowest
first, takes the next literal; each 0 bit copies the byte the current distance back.

Every case is named for its question. `cases()` returns (name, stream, expected decoded
length) triples; the expected length only sizes the buffer the decoder is given.

The decoder refuses a compressed block longer than the block stored raw would be, so every
case below makes its blocks decode to more than they hold: after the steps that ask its
question, a block repeats one step of copies, from table entry 11 (selector 14), 30 times.
"""

END = b"\x06\x00\x00\x00\x00\x00"
MAGIC = b"ZBM\x09"
LIT8 = (0xFF, 0)
COPY8 = (0x00, 0)


def u24(n):
    return bytes((n & 0xFF, n >> 8 & 0xFF, n >> 16 & 0xFF))


def nibbles(values):
    out = bytearray()
    for i in range(0, len(values), 2):
        lo = values[i]
        hi = values[i + 1] if i + 1 < len(values) else 0
        out.append(lo | hi << 4)
    return bytes(out)


def table(entries, tail=b"\x00\x00"):
    entries = list(entries) + [(0, 0)] * (12 - len(entries))
    bits = 0
    for i, (mask, count) in enumerate(entries):
        bits |= (mask | count << 8) << (10 * i)
    return bits.to_bytes(15, "little") + tail


def block(dlen, lits=b"", dists=b"", masks=b"", sel=(), entries=(), offs=None, tail=b"\x00\x00",
          length=None, pad=b""):
    """A compressed block, its areas in order unless `offs` places them."""
    o1 = 15 + len(lits)
    o2 = o1 + len(dists)
    o3 = o2 + len(masks)
    if offs:
        o1, o2, o3 = offs
    body = lits + dists + masks + nibbles(sel) + pad
    blen = 15 + len(body) + 17
    return (u24(blen if length is None else length) + u24(dlen) + u24(o1) + u24(o2) + u24(o3)
            + body + table(entries, tail))


def raw(data):
    return u24(len(data) + 6) + u24(len(data)) + data


def stream(*blocks, end=END):
    return MAGIC + b"".join(blocks) + end


LETTERS = b"ABCDEFGHIJKLMNOP"
# Table entry 11, selector 14: a step that copies 8 bytes at the current distance.
FILL_SEL = 14
FILL_STEPS = 30
FILL = [FILL_SEL, 15, 15, FILL_STEPS - 4 - 15]


def entries_with_fill(entries):
    entries = list(entries) + [(0, 0)] * (11 - len(entries))
    return entries[:11] + [COPY8]


def fblock(dlen, sel, entries=(), **kw):
    """A block whose selectors end with the fill, decoding to `dlen` plus the fill's bytes."""
    return block(dlen + 8 * FILL_STEPS, sel=list(sel) + FILL, entries=entries_with_fill(entries),
                 **kw)


def count_nibbles(n):
    """The selectors after a step's own that repeat it n times in all, n of 4 or more."""
    n -= 4
    out = [15]
    while n >= 15:
        out.append(15)
        n -= 15
    return out + [n]


def cases():
    out = []

    def case(name, s, size):
        out.append((name, s, size))

    F = 8 * FILL_STEPS
    # Controls: the builder, held to the decoder on streams that leave nothing open.
    case("ctl-fill", stream(fblock(16, [3, 3], [LIT8], lits=LETTERS)), 16 + F)
    case("ctl-two-blocks", stream(fblock(16, [3, 3], [LIT8], lits=LETTERS),
                                  fblock(16, [3, 3], [LIT8], lits=LETTERS[::-1])), 32 + 2 * F)

    # The size rule's edge: one block of 8 literals and a count of 4 copy steps, 42 bytes long,
    # given decoded lengths that make it 2 and 1 bytes shorter than raw, the same length, and
    # 1 and 2 bytes longer.
    for dlen, name in ((38, "shorter-by-2"), (37, "shorter-by-1"), (36, "equal"),
                       (35, "longer-by-1"), (34, "longer-by-2")):
        case("size-" + name, stream(block(dlen, lits=LETTERS[:8], sel=[3, 4, 15, 0],
                                          entries=[LIT8, COPY8])), dlen)

    # A distance: 16 literals, then one step of mask 0 at distance d (selector 1 reads a mask
    # and one distance byte), then the fill at that distance. Below 8 a copy reads bytes of
    # its own step; 0 and past 16 reach outside what has been decoded.
    for d in (0, 1, 2, 4, 5, 7, 8, 9, 15, 16, 17, 100, 255):
        case("dist-%d" % d, stream(fblock(24, [3, 3, 1], [LIT8], lits=LETTERS, dists=bytes([d]),
                                          masks=b"\x00")), 24 + F)
    # A distance read and never used: a step of literals that sets distance 0.
    case("dist-0-unused", stream(fblock(24, [3, 3, 4], [LIT8, (0xFF, 1)],
                                        lits=LETTERS + b"QRSTUVWX", dists=b"\x00")), 24 + F)
    # Two distance bytes holding a value one byte would hold.
    case("dist-two-bytes-small", stream(fblock(24, [3, 3, 2], [LIT8], lits=LETTERS,
                                               dists=b"\x10\x00", masks=b"\x00")), 24 + F)
    # A distance into the block before.
    case("dist-previous-block", stream(fblock(16, [3, 3], [LIT8], lits=LETTERS),
                                       fblock(8, [1], dists=bytes([200]), masks=b"\x00")),
         24 + 2 * F)
    # Two distance bytes reaching 300 back, past a raw block.
    case("dist-two-bytes-far", stream(raw(bytes(range(256)) + LETTERS * 3),
                                      fblock(16, [3, 2], [LIT8], lits=LETTERS[:8],
                                             dists=b"\x2c\x01", masks=b"\x00")), 320 + F)

    # Whether a block's distance starts at 8 or carries over from the block before: the first
    # block ends at distance 16, and the second reads no distance.
    case("carry", stream(fblock(24, [3, 3, 1], [LIT8], lits=LETTERS, dists=b"\x10",
                                masks=b"\x00"),
                         fblock(0, [])), 24 + 2 * F)
    # The same across a raw block between the two.
    case("carry-past-raw", stream(fblock(24, [3, 3, 1], [LIT8], lits=LETTERS, dists=b"\x10",
                                         masks=b"\x00"),
                                  raw(LETTERS),
                                  fblock(0, [])), 40 + 2 * F)

    # The areas. Literals and distances left over are refused (observed); masks and
    # selectors left over are open.
    case("lits-left-over", stream(fblock(16, [3, 3], [LIT8], lits=LETTERS + b"Q")), 16 + F)
    case("dists-left-over", stream(fblock(24, [3, 3, 1], [LIT8], lits=LETTERS,
                                          dists=b"\x10\x10", masks=b"\x00")), 24 + F)
    case("masks-left-over", stream(fblock(24, [3, 3, 1], [LIT8], lits=LETTERS, dists=b"\x10",
                                          masks=b"\x00\x00")), 24 + F)
    case("selectors-left-over", stream(block(16 + F, lits=LETTERS,
                                             sel=[3, 3] + FILL + [3, 3, 3, 3],
                                             entries=entries_with_fill([LIT8]))), 16 + F)
    # Masks read from where the selectors begin: the mask area is empty, so selector 0 takes
    # the selector area's first byte, 0x33, as its mask: 4 literals and 4 copies.
    lits = LETTERS + b"wxyz"
    o1 = 15 + len(lits)
    case("mask-from-selectors", stream(fblock(24, [3, 3, 0], [LIT8], lits=lits,
                                              offs=(o1, o1, o1))), 24 + F)
    # Masks read on past the selectors into the table: eight steps of selector 0 after two of
    # literals, with the mask area where the selectors begin. Their selector bytes are 0x33,
    # then 0x00 four times, then the fill's 0xFE, 0xBF; the eighth mask is the table's first
    # byte, entry 0's mask 0xFF. Those masks take 4 + 7 + 7 + 8 literals after the first 16.
    sel = [3, 3] + [0] * 8
    mask_bytes = [0x33, 0x00, 0x00, 0x00, 0x00, 0xFE, 0xBF, 0xFF]
    lits = (LETTERS * 4)[:16 + sum(bin(m).count("1") for m in mask_bytes)]
    o1 = 15 + len(lits)
    case("mask-from-table", stream(fblock(80, sel, [LIT8], lits=lits, offs=(o1, o1, o1))),
         80 + F)
    # Selectors read from the table: a step of literals, the fill, one copy step; then the
    # selector area has ended, and the next selector is the table's first byte, 0x33: entry
    # 0, mask 0x33, which takes 4 literals and copies 4.
    case("selector-from-table", stream(block(8 + F + 8 + 8, lits=LETTERS[:12],
                                             sel=[4] + FILL + [14],
                                             entries=[(0x33, 0), LIT8] + [(0, 0)] * 9 + [COPY8])),
         8 + F + 16)
    # Selectors read from the literals: the selector area starts at the first literal.
    sel_bytes = nibbles([3] + FILL + [3])
    lits = sel_bytes + LETTERS[:16 - len(sel_bytes)]
    case("selectors-in-literals", stream(block(16 + F, lits=lits,
                                               entries=entries_with_fill([LIT8]),
                                               offs=(31, 31, 15))), 16 + F)
    # A first offset inside the header, in a block that takes no literals.
    case("first-offset-in-header", stream(raw(LETTERS), fblock(0, [], offs=(14, 14, 15))),
         16 + F)

    # Repetition counts. A step repeated past the block's end, whether the repetitions that
    # produce nothing read their distance byte: 16 literals, then ten steps of mask 0 at
    # distance 16 under a count of 13, with 10 or 13 distance bytes; from the mask area
    # (selector 1, a mask byte each as well) and from the table (entry (0, 1)).
    for n in (10, 13):
        case("rep-overshoot-masks-%d-distances" % n,
             stream(block(96, lits=LETTERS, dists=b"\x10" * n, masks=b"\x00" * n,
                          sel=[3, 3, 1] + count_nibbles(13), entries=[LIT8])), 96)
        case("rep-overshoot-table-%d-distances" % n,
             stream(block(96, lits=LETTERS, dists=b"\x10" * n,
                          sel=[3, 3, 4] + count_nibbles(13), entries=[LIT8, (0x00, 1)])), 96)
    # A selector of 15 after the last step, which a count would read when 8 or fewer bytes
    # remain: with 1 distance byte if it is not read, 6 if it is and each repetition reads one.
    for n in (1, 6):
        case("rep-count-in-last-step-%d-distances" % n,
             stream(block(16 + F + 8, lits=LETTERS, dists=b"\x10" * n,
                          sel=[3, 3] + FILL + [4, 15, 2],
                          entries=[LIT8, (0x00, 1)] + [(0, 0)] * 9 + [COPY8])), 16 + F + 8)
    # How far a count may run past its block's end: 8 literals, then 100 steps of copies
    # needed and 100 + k counted.
    for k in (0, 1, 7, 8, 9, 16, 100, 1000):
        case("rep-past-end-by-%d" % k, stream(block(808, lits=LETTERS[:8],
                                                    sel=[3, 4] + count_nibbles(100 + k),
                                                    entries=[LIT8, COPY8])), 808)
    # Counts either side of 65,535 in a full block, which needs 4,095.
    for total in (65535, 65536, 65539):
        case("rep-count-%d" % total, stream(block(32768, lits=LETTERS[:8],
                                                  sel=[3, 4] + count_nibbles(total),
                                                  entries=[LIT8, COPY8])), 32768)

    # The table.
    case("table-count-3-unused", stream(block(16 + F, lits=LETTERS, sel=[3, 3] + FILL,
                                              entries=[LIT8] + [(0, 0)] * 9 + [(0, 3), COPY8])),
         16 + F)
    case("table-count-3-used", stream(fblock(24, [3, 3, 4], [LIT8, (0, 3)], lits=LETTERS,
                                             dists=b"\x10")), 24 + F)
    case("table-tail-set", stream(fblock(16, [3, 3], [LIT8], lits=LETTERS, tail=b"\xff\xff")),
         16 + F)

    # The signature's fourth byte, and its first three.
    for b in (0x00, 0x01, 0x08, 0x0A, 0x10, 0xFF):
        case("magic-%02x" % b, b"ZBM" + bytes([b]) + raw(LETTERS) + END, 16)
    case("magic-zbn", b"ZBN\x09" + raw(LETTERS) + END, 16)
    case("magic-lower", b"zbm\x09" + raw(LETTERS) + END, 16)

    # Every value of the signature's fourth byte.
    for b in range(256):
        case("version-%02x" % b, b"ZBM" + bytes([b]) + raw(LETTERS) + END, 16)

    # Where the limit on a repetition count lies: counts from 4,096 to 65,534 in a full block,
    # and those up to 16,385 in a block of 808 bytes, which needs 100 (a longer count is more
    # selectors than that block may hold).
    for total in (4096, 4097, 4111, 4112, 5000, 8191, 8192, 8193, 16383, 16384, 16385, 32767,
                  32768, 32769, 65534):
        case("count-full-%d" % total, stream(block(32768, lits=LETTERS[:8],
                                                   sel=[3, 4] + count_nibbles(total),
                                                   entries=[LIT8, COPY8])), 32768)
        if total <= 16385:
            case("count-short-%d" % total, stream(block(808, lits=LETTERS[:8],
                                                        sel=[3, 4] + count_nibbles(total),
                                                        entries=[LIT8, COPY8])), 808)

    # The order of the three offsets. Selectors that start among the distances: 64 literals
    # in eight steps, then selector 1, whose distance byte is the selector area's first byte,
    # 0x33 (51), and whose mask is its second, 0x33, taking 4 more literals.
    sel = [3] * 8 + [1] + FILL
    lits = (LETTERS * 5)[:68]
    o1 = 15 + len(lits)
    case("selectors-start-at-distances", stream(block(72 + F, lits=lits, sel=sel,
                                                      entries=entries_with_fill([LIT8]),
                                                      offs=(o1, o1 + 1, o1))), 72 + F)
    # The same block with its selectors one byte later, where they would begin in order.
    case("selectors-start-after-distances", stream(block(72 + F, lits=lits, dists=b"\x33",
                                                         masks=b"\x33", sel=sel,
                                                         entries=entries_with_fill([LIT8]))),
         72 + F)
    # Selectors that start in the table, after a raw block: all three offsets are 15, the
    # table's first byte, 0xF4, is selector 4 and a count marker, and its second, 0, ends the
    # count at 4 steps of entry 1's copies.
    case("selectors-start-in-table", stream(raw(LETTERS), block(32, entries=[(0xF4, 0), COPY8])),
         48)
    # An end block whose length is not six: shorter, and longer with bytes after its header.
    case("end-length-5", stream(raw(LETTERS), end=u24(5) + u24(0) + b"\x00" * 10), 16)
    case("end-length-7", stream(raw(LETTERS), end=u24(7) + u24(0) + b"\x00" * 10), 16)
    return out


if __name__ == "__main__":
    for name, s, size in cases():
        print(name, size, len(s), s[:48].hex())
