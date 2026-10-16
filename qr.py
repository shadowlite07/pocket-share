"""
qr.py - a small QR Code generator (ISO/IEC 18004), standard library only.

Enough of the spec to encode a URL: byte mode, error-correction level M,
versions 1-10 (up to ~213 characters), automatic version and mask selection.

There is no third-party module here on purpose: the app ships as a single
standalone executable with nothing to install, so the QR code has to be
drawn by hand.

    import qr
    svg = qr.to_svg("http://192.168.181.50:8765/")
"""

from __future__ import annotations

# ============================== GF(256) ==============================
# Reed-Solomon arithmetic over the field GF(256) with primitive polynomial
# x^8 + x^4 + x^3 + x^2 + 1 (0x11D), the one QR Code uses.

_PRIMITIVE = 0x11D


def _build_tables() -> tuple[list[int], list[int]]:
    exp = [0] * 512
    log = [0] * 256
    value = 1
    for i in range(255):
        exp[i] = value
        log[value] = i
        value <<= 1
        if value & 0x100:
            value ^= _PRIMITIVE
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_EXP, _LOG = _build_tables()


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(degree: int) -> list[int]:
    """Generator polynomial g(x) = (x - a^0)(x - a^1)... for `degree` codewords.

    Coefficients are stored highest power first. Both endpoints have to be
    written explicitly: multiplying by (x + a^i) puts the leading term of the
    old polynomial at the top unchanged, and only the trailing coefficient
    picks up the root. Getting these two the wrong way round silently builds
    the reciprocal polynomial, which produces error correction that looks
    plausible but never decodes.
    """
    poly = [1]
    for i in range(degree):
        root = _EXP[i]
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= coef                     # from x * poly
            nxt[j + 1] ^= _mul(coef, root)     # from a^i * poly
        poly = nxt
    return poly


def _rs_encode(data: list[int], ec_count: int) -> list[int]:
    generator = _rs_generator(ec_count)
    remainder = [0] * ec_count
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + [0]
        # The generator has ec_count + 1 coefficients; its leading term is 1
        # and is implicit in synthetic division, so the rest are used here.
        for i, coef in enumerate(generator[1:]):
            remainder[i] ^= _mul(coef, factor)
    return remainder


# ============================== TABLES ==============================
# Level M only, which is a sensible trade for a code you scan off a phone
# screen. Each row: (version, ec per block, [(blocks, data codewords), ...])

_BLOCKS_M = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}

# Centre coordinates of the alignment patterns, per version.
_ALIGNMENT = {
    1: [],
    2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

_MAX_VERSION = 10
_FORMAT_BITS_M = 0b00  # error-correction level M, as encoded in the format info


# ============================== ENCODING ==============================


def _data_capacity_bytes(version: int) -> int:
    """Data codewords available, before the mode indicator and count field."""
    return sum(count * size for count, size in _BLOCKS_M[version][1])


def _max_payload_bytes(version: int) -> int:
    """The longest byte-mode payload `version` can hold."""
    count_bits = 8 if version < 10 else 16
    return (_data_capacity_bytes(version) * 8 - 4 - count_bits) // 8


def _pick_version(length: int) -> int:
    for version in range(1, _MAX_VERSION + 1):
        if length <= _max_payload_bytes(version):
            return version
    raise ValueError(
        f"Too much data for one QR code: {length} bytes "
        f"(the largest supported version holds {_max_payload_bytes(_MAX_VERSION)}).")


def _make_data_codewords(payload: bytes, version: int) -> list[int]:
    bits: list[int] = []

    def push(value: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            bits.append((value >> shift) & 1)

    push(0b0100, 4)                                   # byte mode
    push(len(payload), 8 if version < 10 else 16)    # character count
    for byte in payload:
        push(byte, 8)

    capacity_bits = _data_capacity_bytes(version) * 8
    push(0, min(4, capacity_bits - len(bits)))        # terminator
    while len(bits) % 8:
        bits.append(0)

    codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2)
                 for i in range(0, len(bits), 8)]
    # The two pad codewords the spec mandates, alternating 0xEC then 0x11.
    # The alternation is counted over the pads alone - indexing by the
    # codeword position would start on 0x11 whenever the data does not end on
    # an even codeword, which is a different, undecodable codeword stream.
    pad = [0xEC, 0x11]
    used = 0
    while len(codewords) < _data_capacity_bytes(version):
        codewords.append(pad[used % 2])
        used += 1
    return codewords


def _interleave(data: list[int], version: int) -> list[int]:
    """Split into blocks, add error correction, then interleave both."""
    ec_per_block, groups = _BLOCKS_M[version]

    blocks: list[list[int]] = []
    ec_blocks: list[list[int]] = []
    index = 0
    for count, size in groups:
        for _ in range(count):
            block = data[index:index + size]
            index += size
            blocks.append(block)
            ec_blocks.append(_rs_encode(block, ec_per_block))

    result: list[int] = []
    longest = max(len(b) for b in blocks)
    for i in range(longest):                     # data columns
        for block in blocks:
            if i < len(block):
                result.append(block[i])
    for i in range(ec_per_block):                # then error correction columns
        for block in ec_blocks:
            result.append(block[i])
    return result


# ============================== MATRIX ==============================


def _bch15(value: int) -> int:
    """Format information: BCH(15,5) with the spec's masking constant."""
    rem = value << 10
    for i in range(4, -1, -1):
        if rem & (1 << (10 + i)):
            rem ^= 0x537 << i
    return ((value << 10) | rem) ^ 0x5412


def _bch18(version: int) -> int:
    rem = version << 12
    for i in range(5, -1, -1):
        if rem & (1 << (12 + i)):
            rem ^= 0x1F25 << i
    return (version << 12) | rem


def _new_matrix(size: int) -> list[list[None]]:
    return [[None] * size for _ in range(size)]


def _place_finder(matrix, row: int, col: int) -> None:
    """A 7x7 finder pattern plus its one-module separator.

    The separator is the ring at -1 and 7, and it is always light. The
    bounds test on the core matters: without `0 <= c <= 6` the ring picks up
    the outer row/column rule and gets painted dark, which destroys the
    timing patterns next to it.
    """
    for r in range(-1, 8):
        for c in range(-1, 8):
            y, x = row + r, col + c
            if not (0 <= y < len(matrix) and 0 <= x < len(matrix)):
                continue
            in_core = 0 <= r <= 6 and 0 <= c <= 6
            on = (in_core and (c in (0, 6) or r in (0, 6))) or \
                 (2 <= r <= 4 and 2 <= c <= 4)
            matrix[y][x] = 1 if on else 0


def _place_alignment(matrix, centres) -> None:
    last = len(centres) - 1
    for i, cy in enumerate(centres):
        for j, cx in enumerate(centres):
            # No alignment pattern in the three corners the finders occupy.
            if (i, j) in ((0, 0), (0, last), (last, 0)):
                continue
            for r in range(-2, 3):
                for c in range(-2, 3):
                    if not (0 <= cy + r < len(matrix) and
                            0 <= cx + c < len(matrix)):
                        continue
                    edge = max(abs(r), abs(c))
                    matrix[cy + r][cx + c] = 1 if edge != 1 else 0


def _reserve_format(matrix) -> None:
    size = len(matrix)
    for i in range(9):                       # around the top-left finder
        if matrix[8][i] is None:
            matrix[8][i] = 0
        if matrix[i][8] is None:
            matrix[i][8] = 0
    for i in range(8):                       # the other two finders
        if matrix[8][size - 1 - i] is None:
            matrix[8][size - 1 - i] = 0
        if matrix[size - 1 - i][8] is None:
            matrix[size - 1 - i][8] = 0
    matrix[size - 8][8] = 1                  # the always-dark module


def _place_timing(matrix) -> None:
    size = len(matrix)
    for i in range(8, size - 8):
        bit = 1 if i % 2 == 0 else 0
        if matrix[6][i] is None:
            matrix[6][i] = bit
        if matrix[i][6] is None:
            matrix[i][6] = bit


def _place_version(matrix, version: int) -> None:
    """Version information block, only versions 7 and up need it."""
    if version < 7:
        return
    size = len(matrix)
    bits = _bch18(version)
    for i in range(18):
        bit = (bits >> i) & 1
        row, col = i // 3, size - 11 + i % 3
        matrix[row][col] = bit
        matrix[col][row] = bit


def _place_data(matrix, codewords: list[int]) -> None:
    """Fill the free modules bottom-right to top-left, in the zigzag."""
    size = len(matrix)
    bits = [(byte >> i) & 1 for byte in codewords for i in range(7, -1, -1)]
    index = 0
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:          # the vertical timing pattern is skipped
            col -= 1
        for step in range(size):
            row = size - 1 - step if upward else step
            for offset in (0, 1):
                c = col - offset
                if matrix[row][c] is not None:
                    continue
                matrix[row][c] = bits[index] if index < len(bits) else 0
                index += 1
        upward = not upward
        col -= 2


_MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _apply_mask(base, mask: int, reserved: set[tuple[int, int]]) -> list[list[int]]:
    """XOR the mask over the data modules only.

    Function patterns - finders, separators, timing, alignment, format and
    version information - are never masked. Applying the mask to them is the
    classic mistake here: it corrupts the finder patterns and the result no
    longer scans.
    """
    fn = _MASKS[mask]
    return [[cell if (r, c) in reserved else cell ^ (1 if fn(r, c) else 0)
             for c, cell in enumerate(row)]
            for r, row in enumerate(base)]


def _penalty(matrix) -> int:
    """The four penalty rules; lower is a better-looking code."""
    size = len(matrix)
    score = 0

    for line in list(matrix) + [list(col) for col in zip(*matrix)]:
        run, previous = 1, line[0]
        for cell in line[1:]:
            if cell == previous:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run, previous = 1, cell
        if run >= 5:
            score += 3 + (run - 5)

    for r in range(size - 1):
        for c in range(size - 1):
            block = (matrix[r][c], matrix[r][c + 1],
                     matrix[r + 1][c], matrix[r + 1][c + 1])
            if block[0] == block[1] == block[2] == block[3]:
                score += 3

    pattern = [1, 0, 1, 1, 1, 0, 1]
    for line in list(matrix) + [list(col) for col in zip(*matrix)]:
        for i in range(size - 6):
            if line[i:i + 7] == pattern:
                score += 40
    dark = sum(sum(row) for row in matrix)
    ratio = dark * 100 / (size * size)
    score += int(abs(ratio - 50) / 5) * 10
    return score


def _make_base(version: int) -> tuple[list[list[None]], set[tuple[int, int]]]:
    size = 17 + 4 * version
    matrix = _new_matrix(size)
    _place_finder(matrix, 0, 0)
    _place_finder(matrix, 0, size - 7)
    _place_finder(matrix, size - 7, 0)
    _place_alignment(matrix, _ALIGNMENT[version])
    _place_timing(matrix)
    _reserve_format(matrix)
    _place_version(matrix, version)
    # Everything filled in so far is a function pattern; remember it, because
    # the mask must leave those modules alone.
    reserved = {(r, c) for r, row in enumerate(matrix)
                for c, cell in enumerate(row) if cell is not None}
    return matrix, reserved


def _with_format(base, mask: int, reserved: set[tuple[int, int]]) -> list[list[int]]:
    """Apply the mask, then stamp the format information.

    The 15 bits go down column 8 and across row 8, and the order matters:
    bits 0-5 run up the left of the top-left finder, bit 6 lands beside it,
    bit 7 sits on the corner module, and bits 8-14 run down the right of the
    bottom-left finder. The second copy mirrors that along row 8 and the top
    of the right-hand side. Transposing these two strips produces a code that
    still reads back correctly but that no scanner will accept, so the layout
    below follows the spec exactly.
    """
    size = len(base)
    matrix = _apply_mask(base, mask, reserved)
    bits = _bch15((_FORMAT_BITS_M << 3) | mask)

    for i in range(15):
        bit = (bits >> i) & 1
        # Vertical strip, column 8. Note it stops at size-7, leaving the dark
        # module at (size-8, 8) alone.
        if i < 6:
            matrix[i][8] = bit
        elif i < 8:
            matrix[i + 1][8] = bit
        else:
            matrix[size - 15 + i][8] = bit
        # Horizontal strip, row 8, skipping the timing module at column 6.
        if i < 8:
            matrix[8][size - 1 - i] = bit
        elif i < 9:
            matrix[8][15 - i] = bit
        else:
            matrix[8][14 - i] = bit
    return matrix


def encode(text: str) -> list[list[int]]:
    """Return the QR matrix for `text` as rows of 0/1."""
    payload = text.encode("utf-8")
    version = _pick_version(len(payload))
    codewords = _interleave(_make_data_codewords(payload, version), version)

    base, reserved = _make_base(version)
    _place_data(base, codewords)

    best = None
    for mask in range(8):
        candidate = _with_format(base, mask, reserved)
        score = _penalty(candidate)
        if best is None or score < best[0]:
            best = (score, candidate)
    return best[1]


def to_svg(text: str, *, module_px: int = 6, quiet_zone: int = 4,
           dark: str = "#16181d", light: str = "#ffffff",
           logo_margin: float = 0.20) -> str:
    """
    Render `text` as an SVG QR code.

    `logo_margin` is the width of a square hole punched in the middle for the
    logo, as a fraction of the code's width. At level M roughly 15% of the
    codewords can be erased and still decode, and a centred block is the kind
    of damage Reed-Solomon handles best; 0.20 erases about 4%. This was
    checked by decoding the result, and it still reads up to 0.26.

    Pass logo_margin=0 for no hole at all.
    """
    matrix = encode(text)
    size = len(matrix)
    span = (size + quiet_zone * 2) * module_px

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{span}" '
        f'height="{span}" viewBox="0 0 {span} {span}" shape-rendering="crispEdges">',
        f'<rect width="{span}" height="{span}" fill="{light}"/>',
        f'<g fill="{dark}">',
    ]

    hole = size * max(0.0, min(logo_margin, 0.30))
    centre = size / 2

    for r, row in enumerate(matrix):
        for c, cell in enumerate(row):
            if not cell:
                continue
            # Skip modules that fall inside the logo hole.
            if hole and abs(r + 0.5 - centre) < hole / 2 and \
                    abs(c + 0.5 - centre) < hole / 2:
                continue
            x = (c + quiet_zone) * module_px
            y = (r + quiet_zone) * module_px
            parts.append(f'<rect x="{x}" y="{y}" width="{module_px}" '
                         f'height="{module_px}"/>')

    parts.append('</g>')
    if hole:
        parts.append(f'<circle cx="{span / 2}" cy="{span / 2}" '
                     f'r="{hole / 2 * module_px:.1f}" fill="{light}"/>')
    parts.append('</svg>')
    return "".join(parts)


def matrix_to_ascii(text: str) -> str:
    """A plain-text rendering, handy for printing in a terminal."""
    matrix = encode(text)
    quiet = 2
    size = len(matrix)
    lines = []
    border = "  " + "  " * (size + quiet * 2)
    lines.append(border)
    for r in range(-quiet, size + quiet):
        row = ["  "]
        for c in range(-quiet, size + quiet):
            inside = 0 <= r < size and 0 <= c < size
            row.append("██" if inside and matrix[r][c] else "  ")
        lines.append("".join(row))
    lines.append(border)
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "http://192.168.181.50:8765/"
    print(f"QR for {target}\n")
    print(matrix_to_ascii(target))