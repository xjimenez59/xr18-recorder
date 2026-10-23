"""Générateur de QR code minimal (mode octets, correction d'erreur M, versions 1 à 10), sortie SVG.

Algorithme standard (ISO/IEC 18004), assez pour une URL de moins de 200 caractères.
"""

from __future__ import annotations

# Niveau M, versions 1..10 : octets de correction par bloc, nombre de blocs.
_ECC_PER_BLOCK = [None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26]
_NUM_BLOCKS = [None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5]
_FORMAT_BITS_M = 0
MAX_VERSION = 10


def _raw_modules(ver: int) -> int:
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        a = ver // 7 + 2
        n -= (25 * a - 10) * a - 55
        if ver >= 7:
            n -= 36
    return n


def _data_codewords(ver: int) -> int:
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ver] * _NUM_BLOCKS[ver]


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, c in enumerate(divisor):
            result[i] ^= _gf_mul(c, factor)
    return result


def _codewords(payload: bytes, ver: int) -> list[int]:
    bits: list[int] = []

    def put(val: int, n: int) -> None:
        bits.extend((val >> i) & 1 for i in range(n - 1, -1, -1))

    put(0b0100, 4)
    put(len(payload), 8 if ver < 10 else 16)
    for b in payload:
        put(b, 8)
    cap = _data_codewords(ver) * 8
    put(0, min(4, cap - len(bits)))
    put(0, -len(bits) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < cap // 8:
        data.append(pad)
        pad ^= 0xEC ^ 0x11

    nb, ecc_len, raw = _NUM_BLOCKS[ver], _ECC_PER_BLOCK[ver], _raw_modules(ver) // 8
    short_blocks, short_len = nb - raw % nb, raw // nb
    divisor = _rs_divisor(ecc_len)
    blocks, k = [], 0
    for i in range(nb):
        dlen = short_len - ecc_len + (0 if i < short_blocks else 1)
        d = data[k:k + dlen]
        k += dlen
        ecc = _rs_remainder(d, divisor)
        if i < short_blocks:
            d = d + [0]  # case factice : aligne les blocs courts sur les longs pour l'entrelacement
        blocks.append(d + ecc)
    out = []
    for i in range(len(blocks[-1])):
        for j, blk in enumerate(blocks):
            if i != short_len - ecc_len or j >= short_blocks:
                out.append(blk[i])
    return out


class _Matrix:
    def __init__(self, ver: int):
        self.ver, self.size = ver, ver * 4 + 17
        self.m = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]
        self._draw_function_patterns()

    def _set(self, x: int, y: int, dark: bool) -> None:
        self.m[y][x] = dark
        self.fn[y][x] = True

    def _draw_function_patterns(self) -> None:
        s = self.size
        for i in range(s):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (s - 4, 3), (3, s - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < s and 0 <= y < s:
                        self._set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = self._align_positions()
        for i, px in enumerate(pos):
            for j, py in enumerate(pos):
                if (i, j) in ((0, 0), (0, len(pos) - 1), (len(pos) - 1, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(px + dx, py + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format(0)
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                a, b = s - 11 + i % 3, i // 3
                self._set(a, b, bool((bits >> i) & 1))
                self._set(b, a, bool((bits >> i) & 1))

    def _align_positions(self) -> list[int]:
        if self.ver == 1:
            return []
        n = self.ver // 7 + 2
        step = (self.ver * 4 + n * 2 + 1) // (n * 2 - 2) * 2
        return list(reversed([self.size - 7 - i * step for i in range(n - 1)] + [6]))

    def _draw_format(self, mask: int) -> None:
        s = self.size
        data = _FORMAT_BITS_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: bool((bits >> i) & 1)  # noqa: E731
        for i in range(6):
            self._set(8, i, bit(i))
        self._set(8, 7, bit(6))
        self._set(8, 8, bit(7))
        self._set(7, 8, bit(8))
        for i in range(9, 15):
            self._set(14 - i, 8, bit(i))
        for i in range(8):
            self._set(s - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set(8, s - 15 + i, bit(i))
        self._set(8, s - 8, True)  # module sombre fixe

    def place(self, codewords: list[int]) -> None:
        s, i = self.size, 0
        right = s - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(s):
                for j in range(2):
                    x = right - j
                    y = s - 1 - vert if ((right + 1) & 2) == 0 else vert
                    if not self.fn[y][x] and i < len(codewords) * 8:
                        self.m[y][x] = bool((codewords[i >> 3] >> (7 - (i & 7))) & 1)
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        for y in range(self.size):
            for x in range(self.size):
                inv = [(x + y) % 2 == 0, y % 2 == 0, x % 3 == 0, (x + y) % 3 == 0,
                       (x // 3 + y // 2) % 2 == 0, x * y % 2 + x * y % 3 == 0,
                       (x * y % 2 + x * y % 3) % 2 == 0, ((x + y) % 2 + x * y % 3) % 2 == 0][mask]
                if inv and not self.fn[y][x]:
                    self.m[y][x] = not self.m[y][x]

    def penalty(self) -> int:
        s, m, total = self.size, self.m, 0
        for lines in (m, [list(c) for c in zip(*m)]):  # lignes puis colonnes
            for row in lines:
                run, prev = 0, None
                for v in row + [None]:
                    if v == prev:
                        run += 1
                    else:
                        if run >= 5:
                            total += 3 + run - 5
                        run, prev = 1, v
                for i in range(s - 10):  # motif 1011101 entouré de 4 modules clairs
                    seg = row[i:i + 11]
                    if seg in ([True, False, True, True, True, False, True, False, False, False, False],
                               [False, False, False, False, True, False, True, True, True, False, True]):
                        total += 40
        for y in range(s - 1):
            for x in range(s - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    total += 3
        dark = sum(map(sum, m))
        k = (abs(dark * 20 - s * s * 10) + s * s - 1) // (s * s) - 1
        total += max(0, k) * 10
        return total


def make_matrix(text: str) -> list[list[bool]]:
    payload = text.encode("utf-8")
    for ver in range(1, MAX_VERSION + 1):
        if len(payload) <= _data_codewords(ver) - (2 if ver < 10 else 3):
            break
    else:
        raise ValueError("texte trop long pour le QR code")
    cw = _codewords(payload, ver)
    best = None
    for mask in range(8):
        mx = _Matrix(ver)
        mx.place(cw)
        mx.apply_mask(mask)
        mx._draw_format(mask)
        p = mx.penalty()
        if best is None or p < best[0]:
            best = (p, mx)
    return best[1].m


def to_svg(text: str, border: int = 4) -> str:
    m = make_matrix(text)
    n = len(m) + 2 * border
    cells = "".join(f"M{x + border},{y + border}h1v1h-1z"
                    for y, row in enumerate(m) for x, v in enumerate(row) if v)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{cells}" fill="#000"/></svg>')
