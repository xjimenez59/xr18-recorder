"""Écran OLED 128×64 (SSD1306 / SH1106 / SSD1309) en I²C, sans bibliothèque : tampon d'image, texte 5×7 agrandissable, pilote.

Le tampon est organisé comme la mémoire de l'écran (8 pages de 128 octets, bit 0 = ligne du haut de la page) ;
seules les pages modifiées sont renvoyées. **Non vérifié sur le matériel** (brochage, adresse 0x3C/0x3D, décalage
de 2 colonnes du SH1106).
"""

from __future__ import annotations

import fcntl
import os
import unicodedata

WIDTH, HEIGHT = 128, 64
PAGES = HEIGHT // 8
I2C_SLAVE = 0x0703

# Police 5×7 classique, ASCII 32..126 : 5 colonnes par caractère, bit 0 = ligne du haut.
_FONT_HEX = """
0000000000 00005F0000 0007000700 147F147F14 242A7F2A12 2313086462 3649562050 0005030000 001C224100 0041221C00
2A1C7F1C2A 08083E0808 0050300000 0808080808 0060600000 2010080402 3E5149453E 00427F4000 4261514946 2141454B31
1814127F10 2745454539 3C4A494930 0171090503 3649494936 064949291E 0036360000 0056360000 0814224100 1414141414
0041221408 0201510906 324979413E 7E1111117E 7F49494936 3E41414122 7F4141221C 7F49494941 7F09090901 3E4149497A
7F0808087F 00417F4100 2040413F01 7F08142241 7F40404040 7F020C027F 7F0408107F 3E4141413E 7F09090906 3E4151215E
7F09192946 4649494931 01017F0101 3F4040403F 1F2040201F 3F4038403F 6314081463 0708700807 6151494543 007F414100
0204081020 0041417F00 0402010204 4040404040 0001020400 2054545478 7F48444438 3844444420 384444487F 3854545418
087E090102 0C5252523E 7F08040478 00447D4000 2040443D00 7F10284400 00417F4000 7C04180478 7C08040478 3844444438
7C14141408 081414187C 7C08040408 4854545420 043F444020 3C4040207C 1C2040201C 3C4030403C 4428102844 0C5050503C
4464544C44 0008364100 00007F0000 0041360800 0804081008
"""


def _build_font() -> dict[str, bytes]:
    glyphs = _FONT_HEX.split()
    assert len(glyphs) == 95 and all(len(g) == 10 for g in glyphs)
    return {chr(32 + i): bytes.fromhex(g) for i, g in enumerate(glyphs)}


FONT = _build_font()


def ascii_text(text: str) -> str:
    """Retire les accents et remplace ce que la police ne sait pas dessiner par « ? »."""
    out = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "".join(c if c in FONT else "?" for c in out)


class Canvas:
    def __init__(self):
        self.buf = bytearray(WIDTH * PAGES)

    def clear(self) -> None:
        self.buf[:] = bytes(len(self.buf))

    def pixel(self, x: int, y: int, on: bool = True) -> None:
        if 0 <= x < WIDTH and 0 <= y < HEIGHT:
            i = (y >> 3) * WIDTH + x
            if on:
                self.buf[i] |= 1 << (y & 7)
            else:
                self.buf[i] &= ~(1 << (y & 7))

    def rect(self, x: int, y: int, w: int, h: int, on: bool = True) -> None:
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.pixel(xx, yy, on)

    def outline(self, x: int, y: int, w: int, h: int) -> None:
        self.rect(x, y, w, 1); self.rect(x, y + h - 1, w, 1)
        self.rect(x, y, 1, h); self.rect(x + w - 1, y, 1, h)

    def disc(self, cx: int, cy: int, r: int, on: bool = True) -> None:
        for yy in range(cy - r, cy + r + 1):
            for xx in range(cx - r, cx + r + 1):
                if (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r + r // 2:
                    self.pixel(xx, yy, on)

    @staticmethod
    def text_width(text: str, scale: int = 1) -> int:
        n = len(ascii_text(text))
        return max(0, n * 6 * scale - scale)

    def text(self, x: int, y: int, text: str, scale: int = 1, invert: bool = False) -> int:
        """Écrit `text` ; renvoie l'abscisse qui suit. `invert` = pixels éteints sur fond allumé (le fond est à la charge de l'appelant)."""
        for ch in ascii_text(text):
            cols = FONT[ch]
            for cx, bits in enumerate(cols):
                for cy in range(7):
                    if bits >> cy & 1:
                        self.rect(x + cx * scale, y + cy * scale, scale, scale, not invert)
            x += 6 * scale
        return x

    def centered(self, y: int, text: str, scale: int = 1, invert: bool = False) -> None:
        self.text((WIDTH - self.text_width(text, scale)) // 2, y, text, scale, invert)

    def ascii_art(self) -> str:
        """Image en texte (un pixel = un caractère), pour les tests et le simulateur."""
        rows = []
        for y in range(HEIGHT):
            rows.append("".join("#" if self.buf[(y >> 3) * WIDTH + x] >> (y & 7) & 1 else "." for x in range(WIDTH)))
        return "\n".join(rows)


class I2cBus:
    def __init__(self, bus: int, address: int):
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        try:
            fcntl.ioctl(self.fd, I2C_SLAVE, address)
        except OSError:
            os.close(self.fd)
            raise

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)

    def close(self) -> None:
        os.close(self.fd)


class Oled:
    """Pilote SSD1306 / SH1106 : `link` n'a besoin que d'une méthode `write(bytes)`."""

    def __init__(self, link, driver: str = "ssd1306", rotate: bool = False):
        self.link, self.column_offset = link, (2 if driver == "sh1106" else 0)
        self.rotate = rotate
        self._shown: list[bytes | None] = [None] * PAGES
        self._on = True
        self._contrast = 0xCF

    def _cmd(self, *values: int) -> None:
        self.link.write(bytes([0x00, *values]))

    def init(self) -> None:
        self._cmd(0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40, 0x8D, 0x14, 0x20, 0x02)
        self._cmd(0xC0 if self.rotate else 0xC8, 0xA0 if self.rotate else 0xA1, 0xDA, 0x12, 0x81, self._contrast,
                  0xD9, 0xF1, 0xDB, 0x40, 0xA4, 0xA6, 0xAF)
        self._shown = [None] * PAGES
        self._on = True

    def power(self, on: bool) -> None:
        if on != self._on:
            self._cmd(0xAF if on else 0xAE)
            self._on = on

    def contrast(self, value: int) -> None:
        self._contrast = max(0, min(255, value))
        self._cmd(0x81, self._contrast)

    def show(self, canvas: Canvas) -> int:
        """Envoie les pages qui ont changé ; renvoie leur nombre."""
        sent = 0
        for page in range(PAGES):
            data = bytes(canvas.buf[page * WIDTH:(page + 1) * WIDTH])
            if data == self._shown[page]:
                continue
            col = self.column_offset
            self._cmd(0xB0 + page, col & 0x0F, 0x10 | (col >> 4))
            for i in range(0, WIDTH, 32):
                self.link.write(bytes([0x40]) + data[i:i + 32])
            self._shown[page] = data
            sent += 1
        return sent


def detect(bus: int = 1, addresses=(0x3C, 0x3D), opener=I2cBus):
    """Première adresse qui répond sur le bus I²C, sous forme de lien prêt à écrire ; None si aucun écran."""
    for addr in addresses:
        try:
            link = opener(bus, addr)
        except OSError:
            continue
        try:
            link.write(bytes([0x00, 0xAE]))  # « écran éteint » : une erreur d'écriture = personne à cette adresse
            return link
        except OSError:
            link.close()
    return None
