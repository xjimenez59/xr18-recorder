"""État de santé du Pi : charge, température, mémoire, alimentation ; et les alertes qui en découlent.

Tout vient du noyau (/proc, /sys) ; l'alimentation (sous-tension, limitation thermique) vient de `vcgencmd
get_throttled` s'il existe, sinon de l'alarme matérielle `rpi_volt` (état *actuel* seulement). Une sous-tension est
une cause classique de coupures USB avec un disque branché sur un Pi 2.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

PROC = "/proc"  # remplaçables dans les tests
SYS = "/sys"
RUN = subprocess.run
TEMP_WARN, TEMP_ERROR = 75.0, 80.0  # °C
LOAD_WARN = 0.9  # charge sur 1 minute par cœur
MEM_WARN = 90.0  # % de mémoire utilisée

# bits de `vcgencmd get_throttled`
_NOW = {"undervoltage_now": 0, "freq_capped_now": 1, "throttled_now": 2, "temp_limit_now": 3}
_SEEN = {"undervoltage_seen": 16, "freq_capped_seen": 17, "throttled_seen": 18, "temp_limit_seen": 19}


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _cpu_times() -> tuple[int, int] | None:
    text = _read(f"{PROC}/stat")
    if not text:
        return None
    fields = [int(x) for x in text.splitlines()[0].split()[1:9]]
    return fields[3] + fields[4], sum(fields)  # (inactif, total) : idle + iowait


def temperature_c() -> float | None:
    raw = _read(f"{SYS}/class/thermal/thermal_zone0/temp")
    try:
        return round(int(raw) / 1000, 1) if raw else None
    except ValueError:
        return None


def memory() -> tuple[float, int] | None:
    """(% utilisé, total en Mo) d'après MemAvailable."""
    text = _read(f"{PROC}/meminfo")
    if not text:
        return None
    info = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            info[key] = int(rest.split()[0])
    if "MemTotal" not in info or "MemAvailable" not in info:
        return None
    return round(100 * (1 - info["MemAvailable"] / info["MemTotal"]), 1), info["MemTotal"] // 1024


def power() -> dict | None:
    """État de l'alimentation et de la limitation thermique, ou None si le matériel ne le dit pas."""
    try:
        r = RUN(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and "=" in r.stdout:
            raw = int(r.stdout.split("=", 1)[1].strip(), 0)
            out = {"source": "vcgencmd", "raw": raw}
            out.update({k: bool(raw >> b & 1) for k, b in {**_NOW, **_SEEN}.items()})
            return out
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:  # repli : alarme matérielle de sous-tension (état actuel seulement)
        for name in sorted(os.listdir(f"{SYS}/class/hwmon")):
            base = f"{SYS}/class/hwmon/{name}"
            if (_read(f"{base}/name") or "").strip() == "rpi_volt":
                alarm = (_read(f"{base}/in0_lcrit_alarm") or "0").strip() == "1"
                return {"source": "hwmon", "undervoltage_now": alarm, "undervoltage_seen": alarm,
                        "freq_capped_now": False, "freq_capped_seen": False, "throttled_now": False,
                        "throttled_seen": False, "temp_limit_now": False, "temp_limit_seen": False}
    except OSError:
        pass
    return None


class Health:
    """Mesures instantanées, mises en cache quelques secondes (l'interface interroge chaque seconde)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._prev: tuple[int, int] | None = None
        self._cache: dict | None = None
        self._cache_at = 0.0

    def snapshot(self, max_age: float = 2.0) -> dict:
        with self._lock:
            now = time.monotonic()
            if self._cache is not None and now - self._cache_at < max_age:
                return self._cache
            cur = _cpu_times()
            cpu = None
            if cur and self._prev and cur[1] > self._prev[1]:
                cpu = round(100 * (1 - (cur[0] - self._prev[0]) / (cur[1] - self._prev[1])), 1)
            self._prev = cur
            load = [float(x) for x in (_read(f"{PROC}/loadavg") or "0 0 0").split()[:3]]
            mem = memory()
            self._cache = {"cpu_pct": cpu, "load1": load[0], "load5": load[1], "load15": load[2],
                           "cpus": os.cpu_count() or 1, "temp_c": temperature_c(),
                           "mem_used_pct": mem[0] if mem else None, "mem_total_mb": mem[1] if mem else None,
                           "power": power()}
            self._cache_at = now
            return self._cache


def assess(snap: dict) -> list[dict]:
    """Alertes sur l'état *actuel* du Pi : [{level, code, message}] ; level = « warn » ou « error »."""
    out: list[dict] = []
    pw = snap.get("power") or {}
    if pw.get("undervoltage_now"):
        out.append(_a("error", "undervoltage", "Sous-tension en ce moment : alimentation du Pi insuffisante "
                      "(vérifiez le bloc secteur, le câble et le disque USB)."))
    elif pw.get("undervoltage_seen"):
        out.append(_a("warn", "undervoltage_seen", "Sous-tension détectée depuis le démarrage : alimentation du Pi à vérifier."))
    if pw.get("throttled_now") or pw.get("freq_capped_now"):
        out.append(_a("warn", "throttled", "Le processeur du Pi est ralenti (alimentation ou chaleur)."))
    t = snap.get("temp_c")
    if t is not None and t >= TEMP_ERROR:
        out.append(_a("error", "temp", f"Pi trop chaud : {t:.0f} °C."))
    elif t is not None and t >= TEMP_WARN:
        out.append(_a("warn", "temp", f"Pi chaud : {t:.0f} °C."))
    cpus = max(1, snap.get("cpus") or 1)
    if snap.get("load1", 0) / cpus >= LOAD_WARN:
        out.append(_a("warn", "load", f"Pi très chargé (charge {snap['load1']:.1f} sur {cpus} cœurs)."))
    if (snap.get("mem_used_pct") or 0) >= MEM_WARN:
        out.append(_a("warn", "memory", f"Mémoire presque pleine ({snap['mem_used_pct']:.0f} %)."))
    return out


def _a(level: str, code: str, message: str) -> dict:
    return {"level": level, "code": code, "message": message}


def fmt_time(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
