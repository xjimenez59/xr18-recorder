"""Faux XR18 (OSC/UDP) pour le développement : noms, couleurs et départs aux factices.

Usage : python3 tests/fake_xr18.py [port]   (défaut 10024)
"""

import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from xr18rec import xr18osc  # noqa: E402

NAMES = ["Kick", "Snare", "Tom", "Basse", "Gtr 1", "Gtr 2", "Claviers G", "Claviers D",
         "Voix lead", "Choeur 1", "Choeur 2", "Choeur 3", "Cuivres", "Violon", "Click", ""]
BUS_NAMES = ["Chant", "Guitare", "Batterie", "Basse", "Clavier", "Cuivres"]


def initial_values():
    v = {}
    for n in range(1, 17):
        v[f"/ch/{n:02d}/config/name"] = NAMES[n - 1]
        v[f"/ch/{n:02d}/config/color"] = (n % 7) + 1
        for b in range(1, 7):
            v[f"/ch/{n:02d}/mix/{b:02d}/level"] = 0.5
    v["/lr/config/name"], v["/lr/config/color"] = "Mix", 7
    for b in range(1, 7):
        v[f"/bus/{b}/config/name"] = BUS_NAMES[b - 1]
        v[f"/bus/{b}/config/color"] = b
        v[f"/bus/{b}/mix/fader"] = 0.6
    return v


def main(port: int) -> None:
    values = initial_values()
    subscribers: set = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", port))
    print(f"faux XR18 sur udp://127.0.0.1:{port}", flush=True)
    while True:
        data, peer = sock.recvfrom(2048)
        try:
            msgs = xr18osc.decode_packet(data)
        except xr18osc.OscError:
            continue
        for addr, args in msgs:
            if addr == "/xremote":
                subscribers.add(peer)
            elif args:
                values[addr] = args[0]
                print(f"{addr} = {args[0]}", flush=True)
                for p in subscribers - {peer}:
                    sock.sendto(xr18osc.encode(addr, args[0]), p)
            elif addr in values:
                sock.sendto(xr18osc.encode(addr, values[addr]), peer)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 10024)
