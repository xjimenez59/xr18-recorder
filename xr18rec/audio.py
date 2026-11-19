"""Ce que l'interface audio expose réellement (ALSA) : cartes, formats, fréquences, voies.

L'XR18 a sa propre horloge (44,1 ou 48 kHz, réglée dans X Air Edit, Setup > Audio/MIDI) ; le Pi doit
s'y accorder. Ces fonctions interrogent ALSA pour vérifier que le réglage du Pi est accepté par la carte.
"""

from __future__ import annotations

import os
import re
import subprocess

STANDARD_RATES = (44100, 48000)
ASOUND = "/proc/asound"  # remplaçable dans les tests
_CARD = re.compile(r"card (\d+): (\S+) \[(.*?)\], device (\d+): (.*?) \[")


def parse_cards(text: str) -> list[dict]:
    """Sortie de `arecord -l` -> [{card, name, desc, device, hw}]."""
    out = []
    for m in _CARD.finditer(text):
        card, name, desc, dev = m.group(1), m.group(2), m.group(3), m.group(4)
        out.append({"card": int(card), "name": name, "desc": desc, "device": int(dev),
                    "hw": f"hw:CARD={name},DEV={dev}"})
    return out


def _numbers(value: str) -> tuple[list[int], bool]:
    """« [44100 48000] » -> ([44100, 48000], True) ; « 48000 » -> ([48000], False)."""
    nums = [int(n) for n in re.findall(r"\d+", value)]
    return nums, value.strip().startswith("[")


def parse_hw_params(text: str) -> dict:
    """Sortie de `arecord --dump-hw-params` -> {formats, channels, rates} (listes triées)."""
    params: dict = {"formats": [], "channels": [], "rates": []}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "FORMAT":
            params["formats"] = value.split()
        elif key == "CHANNELS":
            nums, is_range = _numbers(value)
            params["channels"] = list(range(nums[0], nums[1] + 1)) if is_range and len(nums) == 2 else nums
        elif key == "RATE":
            nums, is_range = _numbers(value)
            if is_range and len(nums) == 2:  # plage : on ne retient que les fréquences usuelles qu'elle contient
                params["rates"] = [r for r in STANDARD_RATES if nums[0] <= r <= nums[1]]
            else:
                params["rates"] = nums
    return params


def probe(cfg, recording: bool, run=subprocess.run) -> dict:
    """Cartes présentes et paramètres acceptés par `cfg.device`, comparés au réglage actuel."""
    result: dict = {"cards": [], "device": cfg.device, "params": None, "error": None, "checks": {}}
    try:
        listing = run([cfg.arecord_bin, "-l"], capture_output=True, text=True, timeout=10)
        result["cards"] = parse_cards(listing.stdout)
    except (OSError, subprocess.SubprocessError) as exc:
        result["error"] = f"arecord indisponible : {exc}"
        return result
    if recording:
        result["error"] = "interface occupée par l'enregistrement en cours : relevé impossible"
        return result
    try:
        proc = run([cfg.arecord_bin, "-D", cfg.device, "--dump-hw-params", "-d", "1", "/dev/null"],
                   capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        result["error"] = f"relevé impossible : {exc}"
        return result
    text = proc.stdout + "\n" + proc.stderr
    if "HW Params" not in text:
        result["error"] = (text.strip().splitlines() or ["aucune réponse de la carte"])[-1]
        return result
    p = parse_hw_params(text)
    result["params"] = p
    result["checks"] = {
        "rate": cfg.rate in p["rates"] if p["rates"] else None,
        "format": cfg.format in p["formats"] if p["formats"] else None,
        "channels": cfg.channels in p["channels"] if p["channels"] else None,
    }
    return result


_DEVICE = re.compile(r"(?:plug)?hw:(?:CARD=)?([^,]+)(?:,(?:DEV=)?(\d+))?")


def _card(device: str):
    """(index de carte ALSA, nom, n° de périphérique, lisible) ; None si `device` n'est pas de la forme hw:CARD=nom,DEV=n.

    L'index est None si la carte n'est pas (ou n'est plus) listée ; `lisible` est False si /proc/asound/cards ne se lit pas."""
    m = _DEVICE.fullmatch(device)
    if not m:
        return None
    name, dev = m.group(1), int(m.group(2) or 0)
    if name.isdigit():
        return (int(name) if os.path.isdir(f"{ASOUND}/card{name}") else None), name, dev, True
    try:
        with open(f"{ASOUND}/cards", encoding="utf-8") as f:
            for card in re.finditer(r"^\s*(\d+) \[(\S+)\s*\]", f.read(), re.M):
                if card.group(2) == name:
                    return int(card.group(1)), name, dev, True
    except OSError:
        return None, name, dev, False
    return None, name, dev, True


def device_present(device: str) -> bool | None:
    """True si la carte son `device` (l'XR18 branché en USB) est vue par ALSA, False si elle est absente, None si on ne sait pas.

    Lecture de /proc/asound/cards : aucun programme lancé, aucun accès à la carte (elle reste libre pour arecord)."""
    found = _card(device)
    if found is None or not found[3]:
        return None
    return found[0] is not None


def capture_busy(device: str) -> bool | None:
    """True si la capture de `device` est ouverte par un programme, False si elle est libre, None si on ne sait pas.

    Lit l'état ALSA (`/proc/asound/cardN/pcmDc/sub0/status` vaut « closed » quand la carte est libre)."""
    found = _card(device)
    if found is None or found[0] is None:
        return None
    index, _, dev, _ = found
    try:
        with open(f"{ASOUND}/card{index}/pcm{dev}c/sub0/status", encoding="utf-8") as f:
            return not f.read().strip().startswith("closed")
    except OSError:
        return None
