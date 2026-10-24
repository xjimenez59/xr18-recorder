"""Broches GPIO du Pi par l'interface noyau « GPIO character device v2 » (/dev/gpiochipN), sans bibliothèque.

Sert au bouton (entrée avec résistance de rappel interne, lue par interrogation : pas de configuration du
device tree) et aux LED (sorties). **Non vérifié sur Pi** : les structures et numéros d'ioctl suivent
<linux/gpio.h> ; les tailles sont contrôlées par les tests.
"""

from __future__ import annotations

import ctypes
import fcntl
import os

FLAG_INPUT = 1 << 2
FLAG_OUTPUT = 1 << 3
FLAG_BIAS_PULL_UP = 1 << 8


class _Attr(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("padding", ctypes.c_uint32), ("value", ctypes.c_uint64)]


class _ConfigAttr(ctypes.Structure):
    _fields_ = [("attr", _Attr), ("mask", ctypes.c_uint64)]


class _Config(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("num_attrs", ctypes.c_uint32), ("padding", ctypes.c_uint32 * 5),
                ("attrs", _ConfigAttr * 10)]


class _Request(ctypes.Structure):
    _fields_ = [("offsets", ctypes.c_uint32 * 64), ("consumer", ctypes.c_char * 32), ("config", _Config),
                ("num_lines", ctypes.c_uint32), ("event_buffer_size", ctypes.c_uint32),
                ("padding", ctypes.c_uint32 * 5), ("fd", ctypes.c_int32)]


class _Values(ctypes.Structure):
    _fields_ = [("bits", ctypes.c_uint64), ("mask", ctypes.c_uint64)]


def _iowr(nr: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (0xB4 << 8) | nr


GET_LINE = _iowr(0x07, ctypes.sizeof(_Request))
GET_VALUES = _iowr(0x0E, ctypes.sizeof(_Values))
SET_VALUES = _iowr(0x0F, ctypes.sizeof(_Values))


class GpioError(OSError):
    pass


class Lines:
    """Un groupe de lignes demandées ensemble (toutes en entrée ou toutes en sortie)."""

    def __init__(self, offsets: list[int], flags: int, chip: str = "/dev/gpiochip0", consumer: str = "xr18rec"):
        self.offsets = list(offsets)
        try:
            chip_fd = os.open(chip, os.O_RDWR)
        except OSError as exc:
            raise GpioError(f"{chip} inaccessible : {exc}") from exc
        try:
            req = _Request()
            for i, off in enumerate(self.offsets):
                req.offsets[i] = off
            req.num_lines = len(self.offsets)
            req.consumer = consumer.encode()[:31]
            req.config.flags = flags
            buf = bytearray(bytes(req))
            fcntl.ioctl(chip_fd, GET_LINE, buf, True)
            self.fd = _Request.from_buffer_copy(buf).fd
        except OSError as exc:
            raise GpioError(f"lignes GPIO {self.offsets} indisponibles : {exc}") from exc
        finally:
            os.close(chip_fd)

    def get(self) -> list[int]:
        buf = bytearray(bytes(_Values(0, (1 << len(self.offsets)) - 1)))
        fcntl.ioctl(self.fd, GET_VALUES, buf, True)
        bits = _Values.from_buffer_copy(buf).bits
        return [(bits >> i) & 1 for i in range(len(self.offsets))]

    def set(self, values: list[int]) -> None:
        bits = sum((1 << i) for i, v in enumerate(values) if v)
        fcntl.ioctl(self.fd, SET_VALUES, bytearray(bytes(_Values(bits, (1 << len(self.offsets)) - 1))), True)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass
