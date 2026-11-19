"""Mix de retour (aux / in-ear) pilotable depuis un navigateur : pont HTTP -> OSC vers l'XR18.

Un seul client OSC (ce programme) parle à l'XR18, quel que soit le nombre de musiciens connectés :
l'XR18 n'accepte qu'un nombre limité de clients (8 abonnés /xremote). Les valeurs reçues sont
mises en cache ; l'abonnement /xremote (valable 10 s) est renouvelé en tâche de fond, ce qui
reflète aussi les changements faits depuis X Air Edit.

Les départs voie -> bus (/ch/NN/mix/BB/level) sont documentés. Les retours d'effets FX1/FX2 sont
à /rtn/1 et /rtn/2 (relevé du 19/11/2026 ; /fxrtn est l'adresse de la X32) : un curseur « réverb » par
musicien, qui règle la part du retour d'effet envoyée dans son bus. Le retour /rtn/aux n'est proposé
que si l'XR18 répond.

Égaliseur simple d'un bus (graves / médiums / aigus) : l'application pilote les six bandes du bus
(type, fréquence et gain, ce qui écrase un réglage fin fait ailleurs, par exemple dans Mixing Station).
Les trois curseurs fixent le gain à 100 Hz, 1 kHz et 5 kHz ; les six bandes sont réparties de 100 Hz à
5 kHz, et celles qui ne tombent pas sur un curseur prennent une valeur interpolée (en fréquence
logarithmique) entre leurs voisines : la courbe de la console suit les curseurs. L'état « actif » de
l'égaliseur n'est changé que par une demande explicite.
"""

from __future__ import annotations

import math
import socket
import threading
import time

from . import xr18osc
from .xr18osc import NUM_INPUTS, OscError

NUM_BUSES = 6  # sorties Aux 1..6 de l'XR18 (bus 7..10 = départs FX, hors sujet)
QUERY_TIMEOUT = 2.0
MIN_DB = -90.0


def fader_to_db(f: float) -> float:
    """Valeur flottante 0..1 des faders X Air -> dB (0,75 = 0 dB, 1,0 = +10 dB)."""
    if f >= 0.5:
        return f * 40 - 30
    if f >= 0.25:
        return f * 80 - 50
    if f >= 0.0625:
        return f * 160 - 70
    return f * 480 - 90


def db_to_fader(db: float) -> float:
    if db <= MIN_DB:
        return 0.0
    if db < -60:
        return (db + 90) / 480
    if db < -30:
        return (db + 70) / 160
    if db < -10:
        return (db + 50) / 80
    return min((db + 30) / 40, 1.0)


# (identifiant, préfixe OSC, nom par défaut)
# Retours d'effets proposés aux musiciens : FX1 = instruments, FX2 = voix (nom et couleur fixés ici, pas lus
# sur la console, où ils sont vides ou peu parlants).
FX_SOURCES = {"fx1": ("/rtn/1", "Réverb instr.", "#7c4dff"), "fx2": ("/rtn/2", "Réverb voix", "#00897b")}
SOURCES = (  # les voies d'abord, les retours d'effets (réglés rarement) à la fin
    [(f"ch{n:02d}", f"/ch/{n:02d}", f"Ch {n:02d}") for n in range(1, NUM_INPUTS + 1)]
    + [("aux", "/rtn/aux", "Aux")]
    + [(sid, prefix, name) for sid, (prefix, name, _c) in FX_SOURCES.items()]
)
_BY_ID = {s[0]: s for s in SOURCES}

# Égaliseur simple : (identifiant, libellé, bande de la console, type de bande, fréquence en Hz).
# Types XR18 : 0 LCut, 1 LShv, 2 PEQ, 3 VEQ, 4 HShv, 5 HCut. Gain : flottant 0..1 = -15..+15 dB.
# Les trois curseurs : (identifiant, libellé, fréquence en Hz).
EQ_CONTROLS = (("low", "Graves", 100.0), ("mid", "Médiums", 1000.0), ("high", "Aigus", 5000.0))
# Les six bandes de la console : (type, fréquence en Hz), de 100 Hz à 5 kHz en échelle logarithmique.
# Les bandes 1, 4 et 6 sont celles des curseurs ; les bandes 2, 3 et 5 en sont interpolées.
EQ_LAYOUT = ((1, 100.0), (2, 220.0), (2, 480.0), (2, 1000.0), (2, 2300.0), (4, 5000.0))
EQ_CONTROL_BAND = {"low": 1, "mid": 4, "high": 6}
EQ_MIN_DB = -15.0
EQ_RANGE_DB = 15.0  # le gain de la console va de -15 à +15 dB


def freq_to_f(hz: float) -> float:
    """Fréquence en Hz -> flottant 0..1 de l'XR18 (échelle logarithmique 20 Hz..20 kHz)."""
    return math.log10(hz / 20.0) / 3.0


def eq_db_to_g(db: float) -> float:
    return (db + EQ_RANGE_DB) / (2 * EQ_RANGE_DB)


def eq_g_to_db(g: float) -> float:
    return g * 2 * EQ_RANGE_DB - EQ_RANGE_DB


def eq_band_gains(low: float, mid: float, high: float) -> list[float]:
    """Gain (dB) des six bandes pour les trois curseurs : interpolation linéaire en log(fréquence)."""
    anchors = [(f, g) for (_i, _l, f), g in zip(EQ_CONTROLS, (low, mid, high))]
    out = []
    for _type, hz in EQ_LAYOUT:
        if hz <= anchors[0][0]:
            out.append(anchors[0][1])
        elif hz >= anchors[-1][0]:
            out.append(anchors[-1][1])
        else:
            for (f0, g0), (f1, g1) in zip(anchors, anchors[1:]):
                if f0 <= hz <= f1:
                    w = math.log(hz / f0) / math.log(f1 / f0)
                    out.append(g0 + (g1 - g0) * w)
                    break
    return [round(g, 1) for g in out]


def _eq_addr(bus: int, band: int, what: str) -> str:
    return f"/bus/{bus}/eq/{band}/{what}"


def _level_addr(prefix: str, bus: int) -> str:
    return f"{prefix}/mix/{bus:02d}/level"


MASTER = "master"  # identifiant du volume général du bus


def _master_addr(bus: int) -> str:
    return f"/bus/{bus}/mix/fader"  # adresse non confirmée : proposée seulement si l'XR18 répond


def _source_addr(source: str, bus: int) -> str | None:
    if source == MASTER:
        return _master_addr(bus)
    return _level_addr(_BY_ID[source][1], bus) if source in _BY_ID else None


class MixerClient:
    def __init__(self, host: str, port: int = xr18osc.OSC_PORT, max_db: float = 0.0,
                 buses: list[int] | None = None, eq_max_db: float = 6.0):
        self.addr = (host, port)
        self.max_db = max_db
        self.eq_max_db = eq_max_db
        self.buses = [b for b in (buses or range(1, NUM_BUSES + 1)) if 1 <= b <= NUM_BUSES]
        self.version = 0
        self._values: dict[str, object] = {}
        self._fresh: set[str] = set()  # adresses reçues depuis la dernière interrogation
        self._loaded: dict[int, float] = {}
        self._load_lock = threading.Lock()
        self._names_loaded = False
        self._absent: set[str] = set()
        self._master_absent = False
        self._master_seen = False
        self._eq_absent = False
        self._eq_seen = False
        self._loaded_ids: set[str] = set()
        self._mute_loaded = -1e9
        self._pre_mute: dict[str, dict[str, int]] = {}  # états « on » d'avant le mute, par cible
        self._cond = threading.Condition()
        self._closed = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("", 0))
        self._sock.settimeout(0.5)
        self._send(xr18osc.encode("/xremote"))
        for target, name in ((self._recv_loop, "osc-recv"), (self._keepalive_loop, "osc-keepalive")):
            threading.Thread(target=target, daemon=True, name=name).start()

    # ---- réseau -----------------------------------------------------------
    def _send(self, data: bytes) -> None:
        try:
            self._sock.sendto(data, self.addr)
        except OSError:
            pass  # réseau momentanément indisponible : on réessaiera au prochain cycle

    def _recv_loop(self) -> None:
        while not self._closed.is_set():
            try:
                data, _ = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                msgs = xr18osc.decode_packet(data)
            except OscError:
                continue
            with self._cond:
                for addr, args in msgs:
                    if args:
                        self._fresh.add(addr)
                        if self._values.get(addr) != args[0]:
                            self.version += 1
                        self._values[addr] = args[0]
                self._cond.notify_all()

    def _keepalive_loop(self) -> None:
        while not self._closed.wait(8):  # l'abonnement expire après 10 s
            self._send(xr18osc.encode("/xremote"))

    def close(self) -> None:
        self._closed.set()
        self._sock.close()

    # ---- lecture ----------------------------------------------------------
    def _query(self, addrs: list[str], timeout: float = QUERY_TIMEOUT) -> None:
        """Interroge l'XR18 et attend les réponses (avec relances) ; les absentes restent absentes."""
        with self._cond:
            self._fresh.difference_update(addrs)
        deadline = time.monotonic() + timeout
        for _ in range(3):
            with self._cond:
                missing = [a for a in addrs if a not in self._fresh]
            if not missing:
                return
            for a in missing:
                self._send(xr18osc.encode(a))
            round_end = min(deadline, time.monotonic() + timeout / 3)
            with self._cond:
                while any(a not in self._fresh for a in addrs):
                    left = round_end - time.monotonic()
                    if left <= 0:
                        break
                    self._cond.wait(left)
            if time.monotonic() >= deadline:
                return

    def _check_bus(self, bus: int) -> None:
        if bus not in self.buses:
            raise MixError(f"bus {bus} non autorisé")

    def load_bus(self, bus: int) -> None:
        self._check_bus(bus)
        with self._load_lock:  # une seule interrogation à la fois, même avec plusieurs musiciens
            if time.monotonic() - self._loaded.get(bus, -1e9) < 5:
                return
            self._load_bus(bus)

    def _load_bus(self, bus: int) -> None:
        addrs = [f"/bus/{bus}/config/name", f"/bus/{bus}/config/color"]
        for sid, prefix, _name in SOURCES:
            if sid not in self._absent:  # source sans réponse au 1er essai : ne pas attendre à chaque fois
                addrs.append(_level_addr(prefix, bus))
                if sid not in FX_SOURCES:
                    addrs += [prefix + "/config/name", prefix + "/config/color"]
        if not self._master_absent:
            addrs.append(_master_addr(bus))
        eq_addrs = self._eq_addrs(bus)
        if not self._eq_absent:
            addrs += eq_addrs
        self._query(addrs)
        if not any(_level_addr(p, bus) in self._fresh for _i, p, _n in SOURCES):
            raise OscError(f"pas de réponse de l'XR18 ({self.addr[0]}:{self.addr[1]})")
        if _master_addr(bus) in self._fresh:
            self._master_seen = True
        elif not self._master_seen:
            self._master_absent = True
        if all(a in self._fresh for a in eq_addrs):
            self._eq_seen = True
        elif not self._eq_seen:
            self._eq_absent = True
        for sid, prefix, _name in SOURCES:
            if _level_addr(prefix, bus) not in self._fresh and sid not in self._loaded_ids:
                self._absent.add(sid)
        self._loaded_ids = {sid for sid, p, _n in SOURCES if _level_addr(p, bus) in self._fresh}
        self._loaded[bus] = time.monotonic()
        with self._cond:
            self.version += 1

    @staticmethod
    def _eq_addrs(bus: int) -> list[str]:
        return [f"/bus/{bus}/eq/on"] + [_eq_addr(bus, band, w) for band in range(1, 7)
                                       for w in ("type", "f", "g")]

    def bus_list(self) -> list[dict]:
        if not self._names_loaded:
            with self._load_lock:
                names = [f"/bus/{b}/config/name" for b in self.buses]
                self._query(names, timeout=1.5)
                self._names_loaded = any(a in self._fresh for a in names)  # sinon on réessaiera
        out = []
        for b in self.buses:
            name = self._values.get(f"/bus/{b}/config/name")
            out.append({"bus": b, "name": str(name).strip() if name else f"Aux {b}"})
        return out

    def get_bus(self, bus: int) -> dict:
        self._check_bus(bus)
        if time.monotonic() - self._loaded.get(bus, -1e9) > 60:
            self.load_bus(bus)
        with self._cond:
            v = dict(self._values)
            version = self.version
        sources = []
        master = v.get(_master_addr(bus))
        if isinstance(master, (int, float)):
            sources.append({"id": MASTER, "name": "Volume général", "master": True, "color": None,
                            "fader": round(float(master), 4), "db": round(fader_to_db(float(master)), 1)})
        for sid, prefix, default in SOURCES:
            level = v.get(_level_addr(prefix, bus))
            if not isinstance(level, (int, float)):
                continue  # source absente (ou non supportée par ce firmware)
            if sid in FX_SOURCES:
                name, hexcolor = default, FX_SOURCES[sid][2]
            else:
                name = str(v.get(prefix + "/config/name") or "").strip() or default
                color = v.get(prefix + "/config/color")
                rgb = xr18osc.color_rgb(color) if isinstance(color, int) else None
                hexcolor = "#%02x%02x%02x" % rgb if rgb else None
            sources.append({
                "id": sid, "name": name, "color": hexcolor, "kind": "fx" if sid in FX_SOURCES else "ch",
                "fader": round(float(level), 4), "db": round(fader_to_db(float(level)), 1),
            })
        bname = v.get(f"/bus/{bus}/config/name")
        return {"bus": bus, "name": str(bname).strip() if bname else f"Aux {bus}",
                "max_db": self.max_db, "max_fader": round(db_to_fader(self.max_db), 4),
                "version": version, "sources": sources, "eq": self._eq_view(bus, v)}

    def _eq_view(self, bus: int, v: dict) -> dict:
        on, bands = v.get(f"/bus/{bus}/eq/on"), []
        for bid, label, hz in EQ_CONTROLS:
            g = v.get(_eq_addr(bus, EQ_CONTROL_BAND[bid], "g"))
            if not isinstance(g, (int, float)) or g != g:
                return {"available": False}
            bands.append({"id": bid, "name": label, "freq": hz, "db": round(eq_g_to_db(float(g)), 1)})
        if not isinstance(on, (int, float)):
            return {"available": False}
        return {"available": True, "on": bool(on), "bands": bands, "min_db": EQ_MIN_DB, "max_db": self.eq_max_db}

    # ---- mute général -----------------------------------------------------
    # Adresses /lr/mix/on et /bus/N/mix/on (1 = actif, 0 = coupé) : non confirmées dans la documentation
    # consultée ; la cible n'est « disponible » que si l'XR18 répond.
    def _mute_addrs(self, target: str) -> list[str]:
        if target == "main":
            return ["/lr/mix/on"]
        if target == "buses":
            return [f"/bus/{b}/mix/on" for b in self.buses]
        raise MixError(f"cible de mute inconnue : {target}")

    def mute_state(self) -> dict:
        with self._load_lock:
            if time.monotonic() - self._mute_loaded > 60:  # resynchronisation (l'abonnement tient le reste à jour)
                addrs = self._mute_addrs("main") + self._mute_addrs("buses")
                self._query(addrs, timeout=1.0)
                if any(a in self._fresh for a in addrs):
                    self._mute_loaded = time.monotonic()
        with self._cond:
            v = dict(self._values)
        out = {}
        for target in ("main", "buses"):
            states = [v.get(a) for a in self._mute_addrs(target)]
            known = [int(x) for x in states if isinstance(x, (int, float))]
            available = len(known) == len(states)
            muted = available and all(x == 0 for x in known)
            out[target] = {"available": available, "muted": muted,
                           "partial": available and not muted and any(x == 0 for x in known)}
        return out

    def set_mute(self, target: str, muted: bool) -> dict:
        addrs = self._mute_addrs(target)
        with self._cond:
            current = {a: self._values.get(a) for a in addrs}
        if not all(isinstance(x, (int, float)) for x in current.values()):
            raise MixError("mute indisponible : l'XR18 n'a pas répondu pour cette cible")
        if muted:
            # on retient ce qui était actif, pour ne pas réactiver ensuite une sortie coupée volontairement
            if any(int(x) == 1 for x in current.values()):
                self._pre_mute[target] = {a: int(x) for a, x in current.items()}
            new = {a: 0 for a in addrs}
        else:
            before = self._pre_mute.pop(target, {})
            new = {a: before.get(a, 1) for a in addrs}
        for a, val in new.items():
            self._send(xr18osc.encode(a, int(val)))
        with self._cond:
            self._values.update(new)
            self.version += 1
        return self.mute_state()

    # ---- écriture ---------------------------------------------------------
    def set_send(self, bus: int, source: str, db: float) -> float:
        """Règle le départ `source` vers `bus` (dB, borné à [-90 ; max_db]). Renvoie la valeur appliquée."""
        self._check_bus(bus)
        addr = _source_addr(source, bus)
        if addr is None:
            raise MixError(f"source inconnue : {source}")
        if not isinstance(db, (int, float)) or db != db:
            raise MixError("niveau invalide")
        db = max(MIN_DB, min(float(db), self.max_db))
        if addr not in self._values:
            raise MixError(f"source indisponible : {source}")
        fader = round(db_to_fader(db), 4)
        self._send(xr18osc.encode(addr, float(fader)))
        with self._cond:
            self._values[addr] = fader
            self.version += 1
        return round(fader_to_db(fader), 1)

    def set_eq(self, bus: int, band_id: str, db) -> dict:
        """Règle un curseur de l'égaliseur simple (borné à [-15 ; eq_max_db]) et recalcule les six bandes."""
        self._check_bus(bus)
        if band_id not in EQ_CONTROL_BAND:
            raise MixError(f"bande inconnue : {band_id}")
        if isinstance(db, bool) or not isinstance(db, (int, float)) or db != db:
            raise MixError("gain invalide")
        with self._cond:
            v = dict(self._values)
        needed = [_eq_addr(bus, b, w) for b in range(1, 7) for w in ("type", "f", "g")]
        if not all(isinstance(v.get(a), (int, float)) for a in needed):
            raise MixError("égaliseur indisponible : l'XR18 n'a pas répondu")
        db = round(max(EQ_MIN_DB, min(float(db), self.eq_max_db)), 1)
        cur = {bid: eq_g_to_db(float(v[_eq_addr(bus, EQ_CONTROL_BAND[bid], "g")])) for bid in EQ_CONTROL_BAND}
        cur = {k: max(EQ_MIN_DB, min(x, self.eq_max_db)) for k, x in cur.items()}  # plafond aussi sur l'existant
        cur[band_id] = db
        gains = eq_band_gains(cur["low"], cur["mid"], cur["high"])
        sent = {}
        for band, ((btype, hz), gdb) in enumerate(zip(EQ_LAYOUT, gains), start=1):
            ta, fa, ga = (_eq_addr(bus, band, w) for w in ("type", "f", "g"))
            if int(v[ta]) != btype:
                sent[ta] = int(btype)
            if abs(v[fa] - freq_to_f(hz)) > 0.003:
                sent[fa] = float(round(freq_to_f(hz), 4))
            g = float(round(eq_db_to_g(gdb), 4))
            if abs(v[ga] - g) > 0.0005:
                sent[ga] = g
        for a, val in sorted(sent.items(), key=lambda kv: kv[0].endswith("/g")):  # types et fréquences avant les gains
            self._send(xr18osc.encode(a, val))
        with self._cond:
            self._values.update(sent)
            self.version += 1
        return {"id": band_id, "db": db}

    def set_eq_on(self, bus: int, on: bool) -> dict:
        self._check_bus(bus)
        addr = f"/bus/{bus}/eq/on"
        if addr not in self._values:
            raise MixError("égaliseur indisponible : l'XR18 n'a pas répondu")
        self._send(xr18osc.encode(addr, 1 if on else 0))
        with self._cond:
            self._values[addr] = 1 if on else 0
            self.version += 1
        return {"on": bool(on)}


class MixError(ValueError):
    pass
