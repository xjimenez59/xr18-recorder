import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import make_update_package as mkpkg  # noqa: E402
from test_recorder import make_cfg  # noqa: E402
from test_updater import gen_key  # noqa: E402
from xr18rec import storage, updater, updates  # noqa: E402
from xr18rec.server import make_server  # noqa: E402
from xr18rec.session import Recorder, RecorderError  # noqa: E402
from xr18rec.updater import UpdateError  # noqa: E402

HAVE_OPENSSL = shutil.which("openssl") is not None


@unittest.skipUnless(HAVE_OPENSSL, "openssl absent")
class UpdatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.key, self.pub = gen_key(t, "good")
        self.other_key, _ = gen_key(t, "other")
        self.state = os.path.join(t, "state")
        self.releases = os.path.join(t, "opt", "releases")
        self.current = os.path.join(t, "opt", "current")
        old = os.path.join(self.releases, "0.1.0")
        os.makedirs(os.path.join(old, "xr18rec"))
        open(os.path.join(old, "xr18rec", "__main__.py"), "w").close()
        open(os.path.join(old, "VERSION"), "w").write("0.1.0\n")
        os.symlink(old, self.current)
        self.saved = {k: getattr(updater, k) for k in ("STATE_DIR", "RESULT_DIR", "RELEASES_DIR", "CURRENT_LINK", "PUBKEY", "RUN",
                                                       "healthy", "recording_in_progress", "CONFIG_FILE", "SYSTEM_DESTS")}
        self.saved_api = updates.GITHUB_API
        updates.GITHUB_API = "http://127.0.0.1:9"  # personne n'écoute : les tests ne touchent jamais au vrai GitHub
        updater.STATE_DIR, updater.RELEASES_DIR, updater.CURRENT_LINK = self.state, self.releases, self.current
        updater.PUBKEY = self.pub
        updater.RESULT_DIR = os.path.join(t, "result")
        updater.CONFIG_FILE = os.path.join(t, "cfg.json")
        open(updater.CONFIG_FILE, "w").write('{"port": 8999}')
        updater.SYSTEM_DESTS = {k: os.path.join(t, "etc", k) for k in ("systemd", "udev", "polkit")}
        updater.healthy = lambda expected: True
        updater.recording_in_progress = lambda: False
        real_run = subprocess.run
        updater.RUN = lambda cmd, **kw: (subprocess.CompletedProcess(cmd, 0, "", "")
                                         if cmd[0] in ("systemctl", "udevadm") else real_run(cmd, **kw))
        self.rec = Recorder(make_cfg(t))
        self.svc = updates.UpdateService(self.rec.cfg, self.rec)
        self.pkg = lambda v="0.3.0", key=None: mkpkg.build(ROOT, v, key or self.key, os.path.join(t, "dist"))

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(updater, k, v)
        updates.GITHUB_API = self.saved_api
        self.rec.shutdown()
        self.tmp.cleanup()

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    # ---- envoi depuis la page
    def test_upload_valid_and_invalid(self):
        data = self.read(self.pkg("0.3.0"))
        info = self.svc.stage_stream(io.BytesIO(data), len(data))
        self.assertEqual((info["version"], info["source"]), ("0.3.0", "envoi depuis la page"))
        self.assertEqual(self.svc.staged()["version"], "0.3.0")
        bad = self.read(self.pkg("0.4.0", key=self.other_key))
        with self.assertRaises(UpdateError):
            self.svc.stage_stream(io.BytesIO(bad), len(bad))
        self.assertFalse(os.path.exists(os.path.join(self.state, "incoming.tar.part")))  # pas de reste
        self.assertEqual(self.svc.staged()["version"], "0.3.0")  # le paquet valide précédent n'est pas écrasé

    def test_upload_limits(self):
        for length in (0, updater.MAX_BUNDLE + 1):
            with self.assertRaises(UpdateError):
                self.svc.stage_stream(io.BytesIO(b"x"), length)
        with self.assertRaises(UpdateError):  # reçu tronqué
            self.svc.stage_stream(io.BytesIO(b"abc"), 10)

    # ---- disque USB
    def test_usb_candidates_and_staging(self):
        mount = os.path.join(self.tmp.name, "usb")
        os.makedirs(os.path.join(mount, "xr18-recordings"))
        shutil.copy(self.pkg("0.3.0"), os.path.join(mount, "xr18rec-update-0.3.0.tar"))
        shutil.copy(self.pkg("0.4.0", key=self.other_key), os.path.join(mount, "xr18rec-update-0.4.0.tar"))
        shutil.copy(self.pkg("0.5.0"), os.path.join(mount, "xr18-recordings", "xr18rec-update-0.5.0.tar"))  # sous-dossier : ignoré
        open(os.path.join(mount, "autre.tar"), "w").close()
        old = storage.read_mounts
        storage.read_mounts = lambda: [("/dev/sda1", mount, "exfat"), ("/dev/mmcblk0p1", "/boot/firmware", "vfat")]
        try:
            found = {c["name"]: c for c in self.svc.usb_candidates()}
            self.assertEqual(sorted(found), ["xr18rec-update-0.3.0.tar", "xr18rec-update-0.4.0.tar"])
            self.assertIsNone(found["xr18rec-update-0.3.0.tar"]["error"])
            self.assertIn("signature", found["xr18rec-update-0.4.0.tar"]["error"])
            info = self.svc.stage_usb(found["xr18rec-update-0.3.0.tar"]["path"])
            self.assertEqual((info["version"], info["source"]), ("0.3.0", "disque USB"))
            with self.assertRaises(UpdateError):  # un chemin quelconque n'est jamais accepté
                self.svc.stage_usb("/etc/passwd")
            with self.assertRaises(UpdateError):  # ni un paquet qui échoue à la vérification
                self.svc.stage_usb(found["xr18rec-update-0.4.0.tar"]["path"])
        finally:
            storage.read_mounts = old

    # ---- Internet
    def serve_github(self, release, files):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/repos/xjimenez59/xr18-recorder/releases/latest":
                    if release is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    body = json.dumps(release(self.server.server_address[1])).encode()
                elif self.path.startswith("/dl/") and self.path[4:] in files:
                    body = outer.read(files[self.path[4:]])
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        updates.GITHUB_API = f"http://127.0.0.1:{srv.server_address[1]}"
        return srv

    def release(self, tag, with_asset=True):
        def make(port):
            assets = [{"name": f"xr18rec-update-{tag.lstrip('v')}.tar",
                       "browser_download_url": f"http://127.0.0.1:{port}/dl/xr18rec-update-{tag.lstrip('v')}.tar"}] if with_asset else []
            return {"tag_name": tag, "body": "Notes de version", "assets": assets + [{"name": "image.img.xz", "browser_download_url": "x"}]}
        return make

    def test_online_check_and_download(self):
        pkg = self.pkg("0.3.0")
        self.serve_github(self.release("v0.3.0"), {"xr18rec-update-0.3.0.tar": pkg})
        info = self.svc.check_online()
        self.assertEqual((info["latest"], info["current"], info["newer"], info["notes"]), ("0.3.0", "0.1.0", True, "Notes de version"))
        staged = self.svc.stage_online()
        self.assertEqual((staged["version"], staged["source"]), ("0.3.0", "Internet"))

    def test_online_no_update_needed_or_available(self):
        self.serve_github(self.release("v0.1.0"), {})
        self.assertFalse(self.svc.check_online()["newer"])
        with self.assertRaises(UpdateError) as cm:
            self.svc.stage_online()
        self.assertIn("déjà à jour", str(cm.exception))

    def test_online_check_is_cached_unless_forced(self):
        hits = []
        make = self.release("v0.3.0")
        srv = self.serve_github(lambda port: (hits.append(1), make(port))[1], {})
        first = self.svc.check_online(force=False)
        self.assertEqual((first["latest"], len(hits)), ("0.3.0", 1))
        second = self.svc.check_online(force=False)
        self.assertEqual((len(hits), second["checked_at"]), (1, first["checked_at"]))  # réponse mémorisée
        self.svc.check_online(force=True)
        self.assertEqual(len(hits), 2)
        srv.shutdown()
        updates.GITHUB_API = "http://127.0.0.1:9"
        self.assertEqual(self.svc.check_online(force=False)["latest"], "0.3.0")  # hors ligne : la mémoire sert encore 3 h
        with self.assertRaises(UpdateError):
            self.svc.check_online(force=True)

    def test_only_a_newer_staged_package_is_offered(self):
        self.stage("0.1.0")  # déjà la version installée
        self.assertEqual(self.svc.staged()["version"], "0.1.0")
        self.assertIsNone(self.svc.status()["staged"])
        self.stage("0.3.0")
        self.assertEqual(self.svc.status()["staged"]["version"], "0.3.0")

    def test_online_release_without_package(self):
        self.serve_github(self.release("v0.3.0", with_asset=False), {})
        info = self.svc.check_online()
        self.assertEqual((info["has_package"], info["newer"]), (False, False))
        with self.assertRaises(UpdateError):
            self.svc.stage_online()

    def test_online_errors_are_explained(self):
        self.serve_github(None, {})  # 404 : dépôt privé ou sans release
        with self.assertRaises(UpdateError) as cm:
            self.svc.check_online()
        self.assertIn("privé", str(cm.exception))
        updates.GITHUB_API = "http://127.0.0.1:9"  # personne n'écoute : pas d'Internet
        with self.assertRaises(UpdateError) as cm:
            self.svc.check_online()
        self.assertIn("Internet", str(cm.exception))

    def test_online_corrupted_download_is_refused(self):
        bad = os.path.join(self.tmp.name, "bad.tar")
        open(bad, "wb").write(b"contenu falsifie")
        self.serve_github(self.release("v0.3.0"), {"xr18rec-update-0.3.0.tar": bad})
        with self.assertRaises(UpdateError):
            self.svc.stage_online()
        self.assertIsNone(self.svc.staged())

    # ---- demande d'installation
    def stage(self, version="0.3.0"):
        data = self.read(self.pkg(version))
        return self.svc.stage_stream(io.BytesIO(data), len(data))

    def test_install_request_and_full_chain(self):
        self.stage("0.3.0")
        status = self.svc.install()
        self.assertEqual(status["last"]["state"], "requested")
        ready = json.load(open(os.path.join(self.state, "ready")))
        self.assertEqual(ready, {"bundle": "incoming.tar", "allow_downgrade": False})
        self.assertEqual(updater.run_pending(), 0)  # ce que fait le service root, déclenché par le fichier « ready »
        self.assertEqual(updater.current_version(), "0.3.0")
        self.assertEqual(self.svc.status()["last"]["state"], "ok")

    def test_install_refusals(self):
        with self.assertRaises(UpdateError):
            self.svc.install()  # rien de préparé
        self.stage("0.1.0")  # même version que l'installée
        with self.assertRaises(UpdateError):
            self.svc.install()
        self.assertFalse(os.path.exists(os.path.join(self.state, "ready")))
        self.svc.install(allow_downgrade=True)
        self.assertTrue(os.path.exists(os.path.join(self.state, "ready")))

    def test_install_refused_while_recording(self):
        self.stage("0.3.0")
        self.rec.start("x")
        try:
            with self.assertRaises(RecorderError):
                self.svc.install()
        finally:
            self.rec.stop()
        self.assertFalse(os.path.exists(os.path.join(self.state, "ready")))

    # ---- API HTTP
    def test_http_api(self):
        srv = make_server(self.rec)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def call(path, data=None, raw=None):
            body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
            req = urllib.request.Request(base + path, method="POST" if body is not None else "GET", data=body)
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        try:
            self.assertEqual(call("/api/update")[1]["current"], "0.1.0")
            self.assertTrue(call("/api/update")[1]["can_verify"])
            code, err = call("/api/update/upload", raw=b"pas un paquet")
            self.assertEqual(code, 400)
            code, info = call("/api/update/upload", raw=self.read(self.pkg("0.3.0")))
            self.assertEqual((code, info["version"]), (200, "0.3.0"))
            self.assertEqual(call("/api/update")[1]["staged"]["version"], "0.3.0")
            code, status = call("/api/update/install", {})
            self.assertEqual((code, status["last"]["state"]), (200, "requested"))
            self.rec.start("x")
            self.assertEqual(call("/api/update/install", {})[0], 409)  # pendant une prise
            self.rec.stop()
            self.assertEqual(call("/api/update/scan", {})[0], 200)
            self.assertEqual(call("/api/update/stage", {"path": "/etc/passwd"})[0], 400)
            self.assertEqual(call("/api/update/check", {})[0], 400)  # pas d'Internet ici
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
