"""Ce que l'interface audio expose réellement (ALSA) : cartes, formats, fréquences, voies.

L'XR18 a sa propre horloge (44,1 ou 48 kHz, réglée dans X Air Edit, Setup > Audio/MIDI) ; le Pi doit
s'y accorder. Ces fonctions interrogent ALSA pour vérifier que le réglage du Pi est accepté par la carte.
"""

from __future__ import annotations

import re
import subprocess

STANDARD_RATES = (44100, 48000)
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
