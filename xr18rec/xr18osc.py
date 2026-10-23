"""Interrogation de l'XR18 en OSC (UDP 10024) : noms et couleurs des voies.

Une requête OSC sans argument sur un paramètre (ex. ``/ch/01/config/name``) renvoie sa valeur
à l'expéditeur ; pas besoin de ``/xremote``. UDP pouvant perdre des paquets, les requêtes
manquantes sont renvoyées.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import struct
import subprocess
import time
from dataclasses import asdict, dataclass, field

NUM_INPUTS = 16
OSC_PORT = 10024

# Couleurs de l'XR18 (valeurs 0..15 ; 8..15 = variantes « inversées », même teinte).
COLOR_NAMES = ["OFF", "RD", "GN", "YE", "BL", "MG", "CY", "WH"]
COLOR_RGB: dict[str, tuple[int, int, int] | None] = {
    "OFF": None,  # pas de couleur : Reaper garde sa teinte par défaut
    "RD": (229, 57, 53),
    "GN": (67, 160, 71),
    "YE": (253, 216, 53),
    "BL": (30, 136, 229),
    "MG": (171, 71, 188),
    "CY": (0, 188, 212),
    "WH": (240, 240, 240),
}


class OscError(RuntimeError):
    pass


def color_name(value: int) -> str:
    return COLOR_NAMES[value % 8]


def color_rgb(value: int) -> tuple[int, int, int] | None:
    return COLOR_RGB[color_name(value)]


@dataclass
class Strip:
    name: str = ""
    color: int = 0


@dataclass
class Layout:
    channels: list[Strip] = field(default_factory=lambda: [Strip() for _ in range(NUM_INPUTS)])
    main: Strip = field(default_factory=Strip)
    missing: list[str] = field(default_factory=list)  # adresses sans réponse

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Layout":
        chans = [Strip(str(c.get("name", "")), int(c.get("color", 0))) for c in d.get("channels", [])]
        chans += [Strip() for _ in range(NUM_INPUTS - len(chans))]
        m = d.get("main", {})
        return cls(chans[:NUM_INPUTS], Strip(str(m.get("name", "")), int(m.get("color", 0))),
                   list(d.get("missing", [])))


# ---- OSC minimal -------------------------------------------------------------
def _pad(b: bytes) -> bytes:
    return b + b"\0" * (4 - len(b) % 4)


def encode(address: str, *args) -> bytes:
    tags = "," + "".join("i" if isinstance(a, int) else "f" if isinstance(a, float) else "s" for a in args)
    out = _pad(address.encode()) + _pad(tags.encode())
    for a in args:
        if isinstance(a, int):
            out += struct.pack(">i", a)
        elif isinstance(a, float):
            out += struct.pack(">f", a)
        else:
            out += _pad(str(a).encode())
    return out


def _read_str(data: bytes, pos: int) -> tuple[str, int]:
    end = data.find(b"\0", pos)
    if end < 0:
        raise OscError("chaîne OSC non terminée")
    return data[pos:end].decode("utf-8", "replace"), (end + 4) & ~3


def decode(data: bytes) -> tuple[str, list]:
    address, pos = _read_str(data, 0)
    if pos >= len(data):
        return address, []
    tags, pos = _read_str(data, pos)
    args: list = []
    try:
        for t in tags.lstrip(","):
            if t == "i":
                args.append(struct.unpack_from(">i", data, pos)[0])
                pos += 4
            elif t == "f":
                args.append(struct.unpack_from(">f", data, pos)[0])
                pos += 4
            elif t == "s":
                s, pos = _read_str(data, pos)
                args.append(s)
            else:
                break
    except struct.error as exc:
        raise OscError("message OSC tronqué") from exc
    return address, args


def decode_packet(data: bytes) -> list[tuple[str, list]]:
    """Décode un message ou un bundle OSC en liste de (adresse, arguments)."""
    if not data.startswith(b"#bundle"):
        return [decode(data)]
    out, pos = [], 16  # « #bundle\0 » + timetag
    while pos + 4 <= len(data):
        size = struct.unpack_from(">i", data, pos)[0]
        pos += 4
        if size <= 0 or pos + size > len(data):
            break
        out += decode_packet(data[pos:pos + size])
        pos += size
    return out


# ---- requête -----------------------------------------------------------------
def _addresses() -> list[str]:
    out = []
    for i in range(1, NUM_INPUTS + 1):
        out += [f"/ch/{i:02d}/config/name", f"/ch/{i:02d}/config/color"]
    return out + ["/lr/config/name", "/lr/config/color"]


def fetch_layout(host: str, port: int = OSC_PORT, timeout: float = 3.0) -> Layout:
    """Lit noms et couleurs des 16 entrées et du mix général. Lève OscError si l'XR18 ne répond pas."""
    addrs = _addresses()
    pending = set(addrs)
    values: dict[str, object] = {}
    deadline = time.monotonic() + timeout
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for attempt in range(3):
            for a in addrs:
                if a in pending:
                    sock.sendto(encode(a), (host, port))
            round_end = min(deadline, time.monotonic() + timeout / 3)
            while pending:
                remaining = round_end - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    data, _ = sock.recvfrom(2048)
                    addr, args = decode(data)
                except socket.timeout:
                    break
                except (OscError, OSError):
                    continue
                if addr in pending and args:
                    values[addr] = args[0]
                    pending.discard(addr)
            if not pending or time.monotonic() >= deadline:
                break
    except OSError as exc:
        raise OscError(f"XR18 injoignable ({host}:{port}) : {exc}") from exc
    finally:
        sock.close()
    if not values:
        raise OscError(f"pas de réponse de l'XR18 ({host}:{port})")

    def strip(prefix: str) -> Strip:
        name = values.get(prefix + "/name", "")
        color = values.get(prefix + "/color", 0)
        return Strip(str(name).strip(), int(color) if isinstance(color, int) else 0)

    layout = Layout([strip(f"/ch/{i:02d}/config") for i in range(1, NUM_INPUTS + 1)],
                    strip("/lr/config"), sorted(pending))
    return layout


# ---- détection de la console sur le réseau ----------------------------------------------------
def local_addresses() -> list[tuple[ipaddress.IPv4Address, ipaddress.IPv4Network]]:
    """Adresses IPv4 non locales de ce Pi avec leur réseau (réseaux plus larges que /22 ramenés à /24)."""
    out = []
    try:
        text = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        text = ""
    for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", text):
        ip = ipaddress.IPv4Address(m.group(1))
        if ip.is_loopback or ip.is_link_local:
            continue
        prefix = int(m.group(2))
        out.append((ip, ipaddress.ip_network(f"{ip}/{prefix if prefix >= 22 else 24}", strict=False)))
    if not out:  # repli sans iproute2 : adresse de la route par défaut, réseau supposé en /24
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))  # aucun paquet n'est émis : sert à connaître l'interface choisie
                ip = ipaddress.IPv4Address(s.getsockname()[0])
            if not ip.is_loopback:
                out.append((ip, ipaddress.ip_network(f"{ip}/24", strict=False)))
        except OSError:
            pass
    return out


def discover(networks=None, timeout: float = 2.5, port: int = OSC_PORT) -> list[dict]:
    """Cherche des consoles X Air : requête `/xinfo` envoyée à chaque adresse du réseau (pas de broadcast,
    souvent filtré). L'adresse retenue est celle de l'expéditeur de la réponse."""
    local = local_addresses()
    nets = list(networks) if networks is not None else [net for _ip, net in local]
    own = {str(ip) for ip, _net in local}
    request = encode("/xinfo")
    found: dict[str, dict] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setblocking(False)
        for net in nets:
            for host in net.hosts():
                if str(host) in own:
                    continue
                try:
                    sock.sendto(request, (str(host), port))
                except (BlockingIOError, OSError):
                    pass  # tampon plein ou hôte injoignable : on passe au suivant
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                data, (src, _p) = sock.recvfrom(2048)
                msgs = decode_packet(data)
            except socket.timeout:
                break
            except (OscError, OSError):
                continue
            for addr, args in msgs:
                if addr in ("/xinfo", "/info"):
                    strings = [str(a) for a in args]
                    found[src] = {"ip": src, "name": strings[1] if len(strings) > 1 else "",
                                  "model": strings[2] if len(strings) > 2 else "",
                                  "version": strings[3] if len(strings) > 3 else ""}
    finally:
        sock.close()
    return sorted(found.values(), key=lambda d: ipaddress.IPv4Address(d["ip"]))
