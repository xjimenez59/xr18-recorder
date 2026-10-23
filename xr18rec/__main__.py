"""Point d'entrée : ``python3 -m xr18rec <commande>``.

Commandes : serve (serveur web), split (WAV multicanal -> mono), info (en-tête WAV), config (affiche).
"""

from __future__ import annotations

import argparse
import glob
import json
import signal
import sys
import threading

from . import config as config_mod
from . import reaper, wavtools, xr18osc
from .app import App
from .mixer import MixerClient, SOURCES
from .server import make_server
from .session import Recorder


def cmd_serve(args) -> int:
    cfg = config_mod.load(args.config)
    applied = config_mod.apply_overlay(cfg)  # réglages faits dans l'interface, par-dessus le fichier
    if applied:
        print(f"réglages de l'interface appliqués : {', '.join(sorted(applied))}", flush=True)
    rec = Recorder(cfg)
    mixer = (MixerClient(cfg.xr18_host, cfg.xr18_osc_port, cfg.mix_max_db, cfg.mix_buses)
             if cfg.xr18_host else None)
    restart = {"asked": False}
    app = App(cfg, rec, mixer, base_path=args.config)
    srv = make_server(rec, mixer=mixer, app=app)

    def request_restart():
        restart["asked"] = True  # arrêt propre après la réponse HTTP ; systemd relance (code de sortie 3)
        threading.Timer(0.5, srv.shutdown).start()
    app.restart = request_restart
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    print(f"xr18rec : http://{cfg.host}:{srv.server_address[1]}  sortie {cfg.output_dir}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app.mixer:
            app.mixer.close()
        rec.shutdown()  # finalise proprement les WAV en cours
        srv.server_close()
    return 3 if restart["asked"] else 0


def cmd_split(args) -> int:
    files = []
    for pattern in args.files:
        files.extend(sorted(glob.glob(pattern)) or [pattern])
    out = wavtools.split_channels(files, args.out, prefix=args.prefix)
    print(f"{len(out)} fichiers mono écrits dans {args.out}")
    return 0


def cmd_info(args) -> int:
    for p in args.files:
        i = wavtools.read_info(p)
        print(f"{p}: {i.channels} voies, {i.rate} Hz, {i.bits} bits, {i.seconds:.1f} s")
    return 0


def cmd_config(args) -> int:
    print(config_mod.dump(config_mod.load(args.config)))
    return 0


def cmd_bootconfig(args) -> int:
    from . import bootconfig
    result = bootconfig.sync(args.boot or bootconfig.default_boot_dir(), args.dest)
    print(f"réglages de la partition de démarrage : {result}")
    return 0  # même invalide : le service doit démarrer avec la configuration précédente


def cmd_automount(args) -> int:
    from . import automount
    return automount.mount(args.device) if args.action == "mount" else automount.umount(args.device)


def cmd_names(args) -> int:
    host = args.host or config_mod.load(args.config).xr18_host
    if not host:
        raise ValueError("adresse de l'XR18 inconnue : --host ou xr18_host dans la configuration")
    layout = xr18osc.fetch_layout(host)
    if args.json:
        print(json.dumps(layout.to_dict(), ensure_ascii=False, indent=2))
        return 0
    rows = [(f"Ch {i + 1:02d}", s) for i, s in enumerate(layout.channels)] + [("Main L/R", layout.main)]
    for label, s in rows:
        print(f"{label:9} {xr18osc.color_name(s.color):4} {s.name}")
    if layout.missing:
        print(f"attention : {len(layout.missing)} paramètre(s) sans réponse", file=sys.stderr)
    return 0


def cmd_mixprobe(args) -> int:
    host = args.host or config_mod.load(args.config).xr18_host
    if not host:
        raise ValueError("adresse de l'XR18 inconnue : --host ou xr18_host dans la configuration")
    mixer = MixerClient(host)
    try:
        mixer.load_bus(args.bus)
        found = {s["id"] for s in mixer.get_bus(args.bus)["sources"]}
    finally:
        mixer.close()
    for sid, prefix, _ in SOURCES:
        print(f"{'ok ' if sid in found else 'ABSENT'} {prefix}/mix/{args.bus:02d}/level")
    return 0


def cmd_reaper(args) -> int:
    layout = None
    host = args.host or (config_mod.load(args.config).xr18_host if args.config else "")
    if host:
        layout = xr18osc.fetch_layout(host)
    else:
        layout = reaper.load_layout(args.session)  # channels.json écrit au démarrage de la session
    path = reaper.write_project(args.session, layout)
    print(f"projet Reaper écrit : {path}" + ("" if layout else " (sans noms : channels.json absent)"))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="xr18rec", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="lance le serveur web")
    s.add_argument("--config", help="fichier JSON de configuration")
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("config", help="affiche la configuration effective")
    s.add_argument("--config")
    s.set_defaults(fn=cmd_config)
    s = sub.add_parser("split", help="sépare des WAV multicanaux (segments dans l'ordre) en WAV mono")
    s.add_argument("files", nargs="+")
    s.add_argument("--out", required=True)
    s.add_argument("--prefix", default="ch")
    s.set_defaults(fn=cmd_split)
    s = sub.add_parser("bootconfig", help="copie /boot/firmware/xr18rec.json vers /etc après validation")
    s.add_argument("--boot", help="partition de démarrage (défaut : /boot/firmware ou /boot)")
    s.add_argument("--dest", default="/etc/xr18rec.json")
    s.set_defaults(fn=cmd_bootconfig)
    s = sub.add_parser("automount", help="(root) monte ou démonte un disque USB ; appelé par systemd")
    s.add_argument("action", choices=["mount", "umount"])
    s.add_argument("device")
    s.set_defaults(fn=cmd_automount)
    s = sub.add_parser("names", help="lit noms et couleurs des voies sur l'XR18 (OSC)")
    s.add_argument("--host", help="IP de l'XR18 (défaut : xr18_host de la configuration)")
    s.add_argument("--config")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_names)
    s = sub.add_parser("mixprobe", help="vérifie quelles sources de retour l'XR18 expose (OSC)")
    s.add_argument("--host")
    s.add_argument("--config")
    s.add_argument("--bus", type=int, default=1)
    s.set_defaults(fn=cmd_mixprobe)
    s = sub.add_parser("reaper", help="génère le projet Reaper d'un dossier de session")
    s.add_argument("session", help="dossier de session contenant take*.wav")
    s.add_argument("--host", help="IP de l'XR18 : relit les noms maintenant au lieu de channels.json")
    s.add_argument("--config")
    s.set_defaults(fn=cmd_reaper)
    s = sub.add_parser("info", help="affiche l'en-tête de WAV")
    s.add_argument("files", nargs="+")
    s.set_defaults(fn=cmd_info)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (ValueError, OSError, xr18osc.OscError) as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
