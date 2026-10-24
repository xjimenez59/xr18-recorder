"""Installation d'une mise à jour signée de l'application (sans reflasher la carte SD).

Ce module est exécuté **en root** par le service `xr18rec-update` ; l'application web (utilisateur
`xr18rec`, sans droits) ne fait que déposer le paquet et demander l'installation. Rien de ce qui est déposé
n'est cru sur parole : le paquet est recopié dans un dossier appartenant à root, sa signature est vérifiée
avec la clé publique de l'image (`/etc/xr18rec/update-key.pub`), puis il est extrait sans suivre de lien ni de
chemin sortant.

Paquet : un fichier `xr18rec-update-X.Y.Z.tar` contenant `payload.tar.gz` et `payload.sig` (signature Ed25519
détachée de payload.tar.gz, vérifiée avec OpenSSL). Le payload contient `manifest.json`, `release/` (le
programme) et `system/<type>/<fichier>` (services systemd, règles udev et polkit : seuls ces trois types, vers
des dossiers fixes).

Installation : `releases/<version>/` puis bascule atomique du lien `current`, redémarrage de l'application,
vérification qu'elle répond avec la nouvelle version ; sinon retour automatique à la version précédente.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

STATE_DIR = os.environ.get("XR18REC_UPDATE_DIR", "/var/lib/xr18rec/update")  # surcharges : développement seulement
# Dossier d'échange (appartient à l'application, donc NON fiable) : paquet déposé et fichier « ready ».
# Le compte rendu de root est dans RESULT_DIR, réservé à root : l'application n'y a aucun droit d'écriture.
RESULT_DIR = os.environ.get("XR18REC_UPDATE_STATE", "/var/lib/xr18rec/update-state")
RELEASES_DIR = "/opt/xr18rec/releases"
CURRENT_LINK = "/opt/xr18rec/current"
PUBKEY = os.environ.get("XR18REC_UPDATE_PUBKEY", "/etc/xr18rec/update-key.pub")
CONFIG_FILE = "/etc/xr18rec.json"
SERVICE = "xr18rec.service"
UPDATER_LEVEL = 1  # un paquet qui exige un niveau supérieur demande une image plus récente
KEEP_RELEASES = 3  # version courante, précédente, et une de plus
MAX_BUNDLE = 30 * 1024**2
MAX_UNPACKED = 80 * 1024**2
MAX_FILES = 3000
HEALTH_TIMEOUT = 45.0
SYSTEM_DESTS = {"systemd": "/etc/systemd/system", "udev": "/etc/udev/rules.d", "polkit": "/etc/polkit-1/rules.d"}
RUN = subprocess.run  # remplaçable dans les tests
_SEMVER = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
_SYSTEM_NAME = re.compile(r"[A-Za-z0-9@][A-Za-z0-9@._-]*")


class UpdateError(Exception):
    """Mise à jour refusée ou échouée ; le message est destiné à l'utilisateur."""


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = _SEMVER.match(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def current_version() -> str:
    try:
        with open(os.path.join(CURRENT_LINK, "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


# ---- lecture et vérification d'un paquet ------------------------------------------------------
def _write_limited(src, dest: str, limit: int) -> int:
    size = 0
    with open(dest, "wb") as out:
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise UpdateError("paquet trop volumineux")
            out.write(chunk)
    return size


def read_bundle(path: str, workdir: str) -> tuple[str, str]:
    """Sépare payload.tar.gz et payload.sig du fichier de mise à jour (rien d'autre n'est accepté)."""
    if os.path.getsize(path) > MAX_BUNDLE:
        raise UpdateError("fichier de mise à jour trop volumineux")
    try:
        with tarfile.open(path, "r:") as tar:
            members = {m.name: m for m in tar.getmembers()}
            if set(members) != {"payload.tar.gz", "payload.sig"} or not all(m.isreg() for m in members.values()):
                raise UpdateError("fichier de mise à jour invalide (contenu inattendu)")
            payload, sig = os.path.join(workdir, "payload.tar.gz"), os.path.join(workdir, "payload.sig")
            _write_limited(tar.extractfile(members["payload.tar.gz"]), payload, MAX_BUNDLE)
            _write_limited(tar.extractfile(members["payload.sig"]), sig, 4096)
    except tarfile.TarError as exc:
        raise UpdateError("ce fichier n'est pas un paquet de mise à jour valide (attendu : xr18rec-update-X.Y.Z.tar)") from exc
    return payload, sig


def verify_signature(payload: str, sig: str, pubkey: str | None = None) -> None:
    pubkey = pubkey or PUBKEY
    if not os.path.isfile(pubkey):
        raise UpdateError("clé publique de mise à jour absente : cette image ne sait pas vérifier de mise à jour")
    try:
        r = RUN(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", pubkey, "-rawin", "-in", payload,
                 "-sigfile", sig], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"vérification de signature impossible : {exc}") from exc
    if r.returncode != 0:
        raise UpdateError("signature invalide : ce paquet n'a pas été signé par le projet (ou il est corrompu)")


def extract_payload(payload: str, dest: str) -> None:
    """Extrait en n'acceptant que fichiers et dossiers, chemins relatifs sans « .. », avec limites de taille."""
    total = count = 0
    try:
        with tarfile.open(payload, "r:gz") as tar:
            for m in tar:
                norm = posixpath.normpath(m.name)
                if norm.startswith("/") or norm == ".." or norm.startswith("../") or "/../" in norm:
                    raise UpdateError(f"chemin refusé dans le paquet : {m.name}")
                if not (m.isreg() or m.isdir()):
                    raise UpdateError(f"entrée refusée dans le paquet (lien ou fichier spécial) : {m.name}")
                count += 1
                total += m.size
                if count > MAX_FILES or total > MAX_UNPACKED:
                    raise UpdateError("paquet trop volumineux une fois extrait")
                target = os.path.join(dest, *norm.split("/"))
                if m.isdir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with tar.extractfile(m) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(target, 0o755 if m.mode & 0o111 else 0o644)
    except tarfile.TarError as exc:
        raise UpdateError(f"paquet illisible : {exc}") from exc


def load_manifest(root: str) -> dict:
    try:
        with open(os.path.join(root, "manifest.json"), encoding="utf-8") as f:
            man = json.load(f)
    except (OSError, ValueError) as exc:
        raise UpdateError("manifeste du paquet absent ou invalide") from exc
    version = man.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise UpdateError("version du paquet invalide")
    if int(man.get("updater_level", 1)) > UPDATER_LEVEL:
        raise UpdateError("cette mise à jour exige une image plus récente : il faut reflasher la carte SD")
    if not os.path.isfile(os.path.join(root, "release", "xr18rec", "__main__.py")):
        raise UpdateError("le paquet ne contient pas le programme")
    for item in man.get("system", []):
        kind, name = item.get("kind"), item.get("name", "")
        if kind not in SYSTEM_DESTS or not _SYSTEM_NAME.fullmatch(name):
            raise UpdateError(f"fichier système refusé : {kind}/{name}")
        if not os.path.isfile(os.path.join(root, "system", kind, name)):
            raise UpdateError(f"fichier système manquant dans le paquet : {kind}/{name}")
    return man


def inspect(bundle: str, pubkey: str | None = None) -> dict:
    """Vérifie un fichier de mise à jour sans rien installer ; renvoie son manifeste (ou lève UpdateError)."""
    with tempfile.TemporaryDirectory(prefix="xr18rec-inspect-") as work:
        payload, sig = read_bundle(bundle, work)
        verify_signature(payload, sig, pubkey)
        root = os.path.join(work, "root")
        os.makedirs(root)
        extract_payload(payload, root)
        return load_manifest(root)


# ---- état visible par l'interface ---------------------------------------------------------------
def write_state(state: str, message: str = "", version: str = "") -> None:
    os.makedirs(RESULT_DIR, exist_ok=True)
    tmp = os.path.join(RESULT_DIR, "last.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"state": state, "message": message, "version": version, "time": int(time.time())}, f,
                  ensure_ascii=False)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(RESULT_DIR, "last.json"))


def read_state() -> dict:
    try:
        with open(os.path.join(RESULT_DIR, "last.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"state": "none", "message": "", "version": "", "time": 0}


# ---- installation ---------------------------------------------------------------------------------
def _service_port() -> int:
    try:
        with open(CONFIG_FILE, encoding="utf-8-sig") as f:
            return int(json.load(f).get("port", 80))
    except (OSError, ValueError, TypeError):
        return 80


def _http_json(path: str, timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{_service_port()}{path}", timeout=timeout) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def recording_in_progress() -> bool:
    status = _http_json("/api/status")
    return bool(status) and status.get("state") == "recording"


def healthy(expected: str) -> bool:
    info = _http_json("/api/system")
    return bool(info) and str(info.get("version", "")).lstrip("v") == expected.lstrip("v")


def _systemctl(*args: str) -> None:
    RUN(["systemctl", *args], capture_output=True, text=True, timeout=120)


def _self_test(release_dir: str) -> None:
    """La nouvelle version doit au moins se charger avec la configuration actuelle."""
    env = dict(os.environ, PYTHONPATH=release_dir, PYTHONDONTWRITEBYTECODE="1")
    r = RUN([sys.executable, "-m", "xr18rec", "config", "--config", CONFIG_FILE], cwd=release_dir, env=env,
            capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise UpdateError("la nouvelle version ne démarre pas avec la configuration actuelle : "
                          + (r.stderr.strip().splitlines() or ["erreur inconnue"])[-1])


def _switch(target: str) -> None:
    tmp = CURRENT_LINK + ".new"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, CURRENT_LINK)  # remplacement atomique du lien


def _install_system_files(root: str, man: dict) -> list[tuple[str, str | None]]:
    """Copie les fichiers système du paquet ; renvoie (destination, sauvegarde) pour pouvoir revenir en arrière."""
    done: list[tuple[str, str | None]] = []
    for item in man.get("system", []):
        dest = os.path.join(SYSTEM_DESTS[item["kind"]], item["name"])
        backup = None
        if os.path.exists(dest):
            backup = dest + ".xr18rec-old"
            shutil.copy2(dest, backup)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(os.path.join(root, "system", item["kind"], item["name"]), dest)
        os.chmod(dest, 0o644)
        done.append((dest, backup))
    return done


def _restore_system_files(done: list[tuple[str, str | None]]) -> None:
    for dest, backup in done:
        try:
            if backup:
                os.replace(backup, dest)
            else:
                os.remove(dest)
        except OSError:
            pass


def _prune(keep: int, protect: set[str]) -> None:
    try:
        names = [n for n in os.listdir(RELEASES_DIR) if not n.startswith(".")]
    except OSError:
        return
    ordered = sorted(names, key=lambda n: parse_version(n) or (0, 0, 0), reverse=True)
    for name in ordered[keep:]:
        if name not in protect:
            shutil.rmtree(os.path.join(RELEASES_DIR, name), ignore_errors=True)


def apply(bundle: str, allow_downgrade: bool = False) -> dict:
    """Installe `bundle` ; renvoie l'état final. Ne lève pas : le résultat est aussi écrit dans last.json."""
    old_target = os.path.realpath(CURRENT_LINK) if os.path.lexists(CURRENT_LINK) else None
    switched = False
    system_done: list[tuple[str, str | None]] = []
    version = ""
    os.makedirs(RELEASES_DIR, exist_ok=True)
    try:
        write_state("running", "vérification du paquet")
        if recording_in_progress():
            raise UpdateError("un enregistrement est en cours : mise à jour reportée (elle redémarrerait l'application)")
        with tempfile.TemporaryDirectory(prefix=".incoming-", dir=RELEASES_DIR) as work:
            # copie dans un dossier de root avant toute vérification : l'original reste modifiable par l'application
            local = os.path.join(work, "bundle.tar")
            with open(bundle, "rb") as src:
                _write_limited(src, local, MAX_BUNDLE)
            payload, sig = read_bundle(local, work)
            verify_signature(payload, sig)
            root = os.path.join(work, "root")
            os.makedirs(root)
            extract_payload(payload, root)
            man = load_manifest(root)
            version = man["version"]
            now = current_version()
            if not allow_downgrade and parse_version(version) <= (parse_version(now) or (0, 0, 0)):
                raise UpdateError(f"la version {version} n'est pas plus récente que la version installée ({now})")
            write_state("running", f"installation de la version {version}", version)
            new_dir = os.path.join(RELEASES_DIR, version)
            if os.path.exists(new_dir):
                if os.path.realpath(new_dir) == old_target:
                    raise UpdateError(f"la version {version} est déjà la version active")
                shutil.rmtree(new_dir)
            release = os.path.join(root, "release")
            with open(os.path.join(release, "VERSION"), "w", encoding="utf-8") as f:
                f.write(version + "\n")
            os.rename(release, new_dir)
            _self_test(new_dir)
            system_done = _install_system_files(root, man)
            if system_done:
                _systemctl("daemon-reload")
                _reload_udev()
            _switch(new_dir)
            switched = True
            _systemctl("restart", SERVICE)
            deadline = time.monotonic() + HEALTH_TIMEOUT
            while time.monotonic() < deadline:
                if healthy(version):
                    break
                time.sleep(1.0)
            else:
                raise UpdateError("la nouvelle version ne répond pas après le redémarrage : retour à la précédente")
        _prune(KEEP_RELEASES, {version, os.path.basename(old_target or "")})
        write_state("ok", f"version {version} installée", version)
        return read_state()
    except UpdateError as exc:
        message = str(exc)
    except Exception as exc:  # jamais d'état « en cours » laissé en plan
        message = f"erreur inattendue : {type(exc).__name__}: {exc}"
    # échec : on remet l'ancienne version en service
    if switched and old_target:
        try:
            _switch(old_target)
            _restore_system_files(system_done)
            _systemctl("daemon-reload")
            _systemctl("restart", SERVICE)
            message += " (ancienne version rétablie)"
        except OSError as exc:
            message += f" ; retour en arrière impossible : {exc}"
    elif system_done:
        _restore_system_files(system_done)
    write_state("failed" if not switched else "rolled_back", message, version)
    return read_state()


def _reload_udev() -> None:
    try:
        RUN(["udevadm", "control", "--reload"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def run_pending() -> int:
    """Point d'entrée du service root : traite la demande déposée par l'application (fichier `ready`)."""
    ready = os.path.join(STATE_DIR, "ready")
    try:
        with open(ready, encoding="utf-8") as f:
            request = json.load(f)
        os.remove(ready)
    except (OSError, ValueError):
        return 0  # rien à faire (déclenchement parasite)
    name = os.path.basename(str(request.get("bundle", "")))  # jamais de chemin fourni par l'application
    bundle = os.path.join(STATE_DIR, name)
    if not name or not os.path.isfile(bundle):
        write_state("failed", "paquet introuvable")
        return 1
    result = apply(bundle, allow_downgrade=bool(request.get("allow_downgrade")))
    return 0 if result["state"] == "ok" else 1
