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


def cases():
    out = []

    def case(name, s, size):
        out.append((name, s, size))

    # Controls: the builder itself, held to the decoder on streams that leave nothing open.
    case("ctl-empty", stream(), 0)
    case("ctl-raw", stream(raw(LETTERS)), 16)
    case("ctl-literals", stream(block(16, lits=LETTERS, sel=[3, 3], entries=[LIT8])), 16)
    case("ctl-copy", stream(block(64, lits=LETTERS[:8], sel=[3, 4, 15, 3], entries=[LIT8, COPY8])), 64)
    case("ctl-two-blocks", stream(block(16, lits=LETTERS, sel=[3, 3], entries=[LIT8]),
                                  block(16, lits=LETTERS[::-1], sel=[3, 3], entries=[LIT8])), 32)

    # A distance: 16 literals, then one step of mask 0 at distance d (selector 1 reads a mask
    # and one distance byte). Below 8 the copy reads bytes of its own step; 0 and past 16
    # reach outside what has been decoded.
    for d in (0, 1, 2, 4, 5, 7, 8, 9, 15, 16, 17, 100, 255):
        case("dist-%d" % d, stream(block(24, lits=LETTERS, dists=bytes([d]), masks=b"\x00",
                                         sel=[3, 3, 1], entries=[LIT8])), 24)
    # Two distance bytes holding a value one byte would hold.
    case("dist-two-bytes-small", stream(block(24, lits=LETTERS, dists=b"\x10\x00", masks=b"\x00",
                                              sel=[3, 3, 2], entries=[LIT8])), 24)
    # A distance into the block before.
    case("dist-previous-block", stream(block(16, lits=LETTERS, sel=[3, 3], entries=[LIT8]),
                                       block(8, dists=b"\x10", masks=b"\x00", sel=[1])), 24)
    # A distance two bytes long reaching 300 back, across two earlier blocks.
    case("dist-two-bytes-far", stream(raw(bytes(range(256)) + LETTERS * 3),
                                      block(16, lits=LETTERS[:8], dists=b"\x2c\x01", masks=b"\x00",
                                            sel=[3, 2], entries=[LIT8])), 320)

    # Whether a block's distance starts at 8 or carries over from the block before: the first
    # block ends at distance 16, and the second's only step reads no distance.
    case("carry", stream(block(24, lits=LETTERS, dists=b"\x10", masks=b"\x00", sel=[3, 3, 1],
                               entries=[LIT8]),
                         block(8, sel=[3], entries=[COPY8])), 32)

    # The areas. Literals and distances are used up exactly in every stream Apple's encoder
    # wrote; whether masks and selectors must be is open.
    case("lits-left-over", stream(block(16, lits=LETTERS + b"Q", sel=[3, 3], entries=[LIT8])), 16)
    case("dists-left-over", stream(block(24, lits=LETTERS, dists=b"\x10\x10", masks=b"\x00",
                                         sel=[3, 3, 1], entries=[LIT8])), 24)
    case("masks-left-over", stream(block(24, lits=LETTERS, dists=b"\x10", masks=b"\x00\x00",
                                         sel=[3, 3, 1], entries=[LIT8])), 24)
    case("selectors-left-over", stream(block(16, lits=LETTERS, sel=[3, 3, 3, 3, 3, 3],
                                             entries=[LIT8])), 16)
    # A mask read from where the selectors begin: the mask area is empty, so selector 0 takes
    # the selector area's first byte, 0x33, as its mask. With 16 decoded, that is 4 literals
    # and 4 copies at distance 8.
    lits = LETTERS + b"wxyz"
    o1 = 15 + len(lits)
    case("mask-from-selectors", stream(block(24, lits=lits, sel=[3, 3, 0, 0],
                                             entries=[LIT8], offs=(o1, o1, o1))), 24)
    # A mask read from the table: the mask and selector areas start together after 28
    # literals, so the four steps of selector 0 read masks 0x33 and 0x00 from the selector
    # bytes, 0x00 from the byte after, and then entry 0's mask, 0xFF, from the table.
    o1 = 15 + 28
    case("mask-from-table", stream(block(48, lits=LETTERS + b"wxyz12345678", sel=[3, 3, 0, 0, 0, 0],
                                         entries=[LIT8], offs=(o1, o1, o1))), 48)
    # Selectors read from the table: one selector byte gives two steps of 8 literals, and
    # the third step's selector is the table's first byte, entry 0's mask 0x33: selector 3,
    # whose mask takes 4 literals and copies 4 at distance 8.
    case("selector-from-table", stream(block(24, lits=LETTERS + b"wxyz", sel=[4, 4],
                                             entries=[(0x33, 0), LIT8])), 24)
    # Selectors read from the literals: the selector area starts at the first literal, '3'.
    case("selectors-in-literals", stream(block(16, lits=b"33ABCDEFGHIJKLMN", entries=[LIT8],
                                               offs=(31, 31, 15))), 16)
    # A first offset inside the header, in a block that takes no literals.
    case("first-offset-in-header", stream(raw(LETTERS), block(8, sel=[3], entries=[COPY8],
                                                          offs=(14, 14, 15))), 24)

    # Repetition counts. A step repeated past the block's end, from a selector that reads a
    # mask and a distance byte: whether the repetitions that produce nothing read them.
    for n in (2, 4):
        case("rep-overshoot-%d-distances" % n, stream(block(32, lits=LETTERS, dists=b"\x10" * n,
                                                            masks=b"\x00" * n,
                                                            sel=[3, 3, 1, 15, 0],
                                                            entries=[LIT8])), 32)
    # A selector of 15 after the last step, which a count would read when 8 or fewer remain.
    case("rep-count-in-last-step", stream(block(24, lits=LETTERS, dists=b"\x10", masks=b"\x00",
                                                sel=[3, 3, 1, 15, 2], entries=[LIT8])), 24)
    # A count that fills a whole 32 KiB block: 8 literals, then 4,095 steps of mask 0.
    count = 4095 - 4
    sel = [3, 4, 15] + [15] * (count // 15) + [count % 15]
    case("rep-full-block", stream(block(32768, lits=LETTERS[:8], sel=sel, entries=[LIT8, COPY8])),
         32768)
    # A count past 65,535, more steps than a block holds.
    count = 70000 - 4
    sel = [3, 4, 15] + [15] * (count // 15) + [count % 15]
    case("rep-past-16-bits", stream(block(32768, lits=LETTERS[:8], sel=sel, entries=[LIT8, COPY8])),
         32768)
    # A selector of 15 where a step begins: after a count ends, with 8 bytes left.
    case("selector-15", stream(block(48, lits=LETTERS[:8], sel=[3, 4, 15, 0, 15],
                                     entries=[LIT8, COPY8])), 48)

    # The table.
    case("table-count-3-unused", stream(block(16, lits=LETTERS, sel=[3, 3],
                                              entries=[LIT8] + [(0, 0)] * 10 + [(0, 3)])), 16)
    case("table-count-3-used", stream(block(24, lits=LETTERS, dists=b"\x10", sel=[3, 3, 4],
                                            entries=[LIT8, (0, 3)])), 24)
    case("table-tail-set", stream(block(16, lits=LETTERS, sel=[3, 3], entries=[LIT8],
                                        tail=b"\xff\xff")), 16)

    # Block framing.
    case("raw-32768", stream(raw(bytes(32768))), 32768)
    case("raw-32769", stream(raw(bytes(32769))), 32769)
    sel = [3, 4, 15] + [15] * ((4096 - 4) // 15) + [(4096 - 4) % 15]
    case("compressed-32769", stream(block(32769, lits=LETTERS[:8], sel=sel,
                                          entries=[LIT8, COPY8])), 32769)
    case("end-compressed-form", stream(raw(LETTERS), end=block(0)), 16)
    case("end-then-bytes", stream(raw(LETTERS), end=END + b"\x00\x00\x00\x00"), 16)
    case("end-then-block", stream(raw(LETTERS), end=END + raw(LETTERS) + END), 32)
    case("no-end", stream(raw(LETTERS), end=b""), 16)
    case("block-length-0", stream(u24(0) + u24(16) + LETTERS), 16)
    case("block-shorter-than-table", stream(u24(20) + u24(16) + u24(15) + u24(15) + u24(15)
                                            + b"\x33\x33\x00\x00\x00"), 16)
    case("magic-08", b"ZBM\x08" + raw(LETTERS) + END, 16)
    return out


if __name__ == "__main__":
    for name, s, size in cases():
        print(name, size, len(s), s[:48].hex())
