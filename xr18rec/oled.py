"""Écran OLED 128×64 (SSD1306 / SH1106 / SSD1309) en I²C, sans bibliothèque : tampon d'image, texte 5×7 agrandissable, pilote.

Le tampon est organisé comme la mémoire de l'écran (8 pages de 128 octets, bit 0 = ligne du haut de la page) ;
seules les pages modifiées sont renvoyées. **Non vérifié sur le matériel** (brochage, adresse 0x3C/0x3D, décalage
de 2 colonnes du SH1106).
"""

from __future__ import annotations

import fcntl
import os
import unicodedata

from . import fontdata

WIDTH, HEIGHT = 128, 64
PAGES = HEIGHT // 8
I2C_SLAVE = 0x0703

SMALL = "6x12"  # police courante ; "12x24" et "16x32" pour les titres (majuscules et chiffres seulement)
FONTS = fontdata.FONTS


def ascii_text(text: str, font: str = SMALL) -> str:
    """Ne garde que ce que la police sait dessiner : majuscule si la police n'a que des capitales, sinon la lettre sans
    accent, sinon « ? » (ou rien si la police n'a pas de point d'interrogation)."""
    glyphs = FONTS[font][2]
    out = []
    for c in unicodedata.normalize("NFC", text):
        if c in glyphs:
            out.append(c)
        elif c.upper() in glyphs:
            out.append(c.upper())
        else:
            base = unicodedata.normalize("NFKD", c).encode("ascii", "ignore").decode()
            if base and base in glyphs:
                out.append(base)
            elif base and base.upper() in glyphs:
                out.append(base.upper())
            elif not base.strip() and c.isprintable() and ord(c) < 128:
                out.append(" ")
            # caractère inconnu non décomposable (● par exemple) : ignoré
    return "".join(out)


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

    def _blit_column(self, x: int, y: int, bits: int, height: int, on: bool) -> None:
        """Une colonne de pixels (bit 0 = ligne du haut) posée en (x, y), page par page."""
        if not 0 <= x < WIDTH or y >= HEIGHT or y + height <= 0:
            return
        if y < 0:
            bits, height, y = bits >> -y, height + y, 0
        height = min(height, HEIGHT - y)
        bits &= (1 << height) - 1
        buf = self.buf
        for page in range(y >> 3, ((y + height - 1) >> 3) + 1):
            mask = (bits << y >> (page * 8)) & 0xFF
            if mask:
                if on:
                    buf[page * WIDTH + x] |= mask
                else:
                    buf[page * WIDTH + x] &= ~mask & 0xFF

    def rect(self, x: int, y: int, w: int, h: int, on: bool = True) -> None:
        if w <= 0 or h <= 0:
            return
        bits = (1 << h) - 1
        for xx in range(max(0, x), min(WIDTH, x + w)):
            self._blit_column(xx, y, bits, h, on)

    def outline(self, x: int, y: int, w: int, h: int) -> None:
        self.rect(x, y, w, 1); self.rect(x, y + h - 1, w, 1)
        self.rect(x, y, 1, h); self.rect(x + w - 1, y, 1, h)

    def disc(self, cx: int, cy: int, r: int, on: bool = True) -> None:
        for yy in range(cy - r, cy + r + 1):
            for xx in range(cx - r, cx + r + 1):
                if (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r + r // 2:
                    self.pixel(xx, yy, on)

    @staticmethod
    def text_width(text: str, font: str = SMALL) -> int:
        return len(ascii_text(text, font)) * FONTS[font][0]

    @staticmethod
    def text_height(font: str = SMALL) -> int:
        return FONTS[font][1]

    def text(self, x: int, y: int, text: str, font: str = SMALL, invert: bool = False) -> int:
        """Écrit `text` ; renvoie l'abscisse qui suit. `invert` = pixels éteints sur fond allumé (le fond est à la charge de l'appelant)."""
        cell_w, cell_h, glyphs = FONTS[font]
        for ch in ascii_text(text, font):
            for i, bits in enumerate(glyphs[ch]):
                if bits:
                    self._blit_column(x + i, y, bits, cell_h, not invert)
            x += cell_w
        return x

    def centered(self, y: int, text: str, font: str = SMALL, invert: bool = False) -> None:
        self.text((WIDTH - self.text_width(text, font)) // 2, y, text, font, invert)

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
