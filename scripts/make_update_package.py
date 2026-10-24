#!/usr/bin/env python3
"""Fabrique un fichier de mise à jour signé : xr18rec-update-X.Y.Z.tar (voir xr18rec/updater.py pour le format).

Usage : python3 scripts/make_update_package.py --version 0.3.0 --key cle-privee.pem --out dist/
La clé privée est une clé Ed25519 PEM (openssl genpkey -algorithm ED25519). Elle ne doit JAMAIS entrer dans le dépôt.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile

# fichiers système de l'image transmis avec chaque mise à jour : (dossier du dépôt, nom, type)
SYSTEM_FILES = [
    ("xr18rec.service", "systemd"), ("xr18rec-mount@.service", "systemd"), ("xr18rec-growfs.service", "systemd"),
    ("xr18rec-update.service", "systemd"), ("xr18rec-update.path", "systemd"),
    ("99-xr18rec-usb.rules", "udev"), ("98-xr18rec-panel.rules", "udev"), ("50-xr18rec.rules", "polkit"),
]
UPDATER_LEVEL = 1


def _add(tar: tarfile.TarFile, arcname: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(arcname)
    info.size, info.mode, info.mtime, info.uid, info.gid = len(data), mode, 0, 0, 0  # reproductible
    tar.addfile(info, io.BytesIO(data))


def build_payload(root: str, version: str, notes: str = "") -> bytes:
    manifest = {"name": "xr18rec", "version": version, "updater_level": UPDATER_LEVEL, "notes": notes, "system": []}
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        pkg = os.path.join(root, "xr18rec")
        for folder, dirs, files in os.walk(pkg):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if name.endswith((".pyc", ".tmp")):
                    continue
                path = os.path.join(folder, name)
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                with open(path, "rb") as f:
                    _add(tar, "release/" + rel, f.read())
        for name, kind in SYSTEM_FILES:
            path = os.path.join(root, "image", "files", name)
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    _add(tar, f"system/{kind}/{name}", f.read())
                manifest["system"].append({"kind": kind, "name": name})
        _add(tar, "manifest.json", json.dumps(manifest, indent=1, ensure_ascii=False).encode("utf-8"))
    return buf.getvalue()


def sign(payload: bytes, key: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        data, sig = os.path.join(tmp, "payload"), os.path.join(tmp, "sig")
        with open(data, "wb") as f:
            f.write(payload)
        subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", key, "-rawin", "-in", data, "-out", sig], check=True)
        with open(sig, "rb") as f:
            return f.read()


def bundle(payload: bytes, signature: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        _add(tar, "payload.tar.gz", payload)
        _add(tar, "payload.sig", signature)
    return buf.getvalue()


def build(root: str, version: str, key: str, out_dir: str, notes: str = "") -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"version invalide : {version} (attendu X.Y.Z)")
    payload = build_payload(root, version, notes)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"xr18rec-update-{version}.tar")
    with open(path, "wb") as f:
        f.write(bundle(payload, sign(payload, key)))
    return path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", required=True, help="X.Y.Z (sans « v »)")
    p.add_argument("--key", required=True, help="clé privée Ed25519 au format PEM")
    p.add_argument("--out", default="dist")
    p.add_argument("--notes", default="")
    p.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    a = p.parse_args()
    print(build(os.path.abspath(a.root), a.version, a.key, a.out, a.notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
