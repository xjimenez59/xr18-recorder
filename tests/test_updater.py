import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import make_update_package as mkpkg  # noqa: E402
from xr18rec import updater  # noqa: E402

HAVE_OPENSSL = shutil.which("openssl") is not None


def gen_key(directory, name):
    key, pub = os.path.join(directory, name + ".pem"), os.path.join(directory, name + ".pub")
    subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", key], check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-out", pub], check=True, capture_output=True)
    return key, pub


def raw_payload(members):
    """Payload malveillant sur mesure : members = [(TarInfo, données ou None)]."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def file_info(name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info


@unittest.skipUnless(HAVE_OPENSSL, "openssl absent")
class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.key, self.pub = gen_key(t, "good")
        self.other_key, _ = gen_key(t, "other")
        self.state, self.releases = os.path.join(t, "state"), os.path.join(t, "opt", "releases")
        self.current = os.path.join(t, "opt", "current")
        self.sysdirs = {k: os.path.join(t, "etc", k) for k in ("systemd", "udev", "polkit")}
        self.config = os.path.join(t, "xr18rec.json")
        with open(self.config, "w") as f:
            json.dump({"port": 8999}, f)
        os.makedirs(self.state)
        old = os.path.join(self.releases, "0.1.0")
        os.makedirs(os.path.join(old, "xr18rec"))
        open(os.path.join(old, "xr18rec", "__main__.py"), "w").close()
        open(os.path.join(old, "VERSION"), "w").write("0.1.0\n")
        os.symlink(old, self.current)
        self.calls = []
        self.healthy = True
        self.recording = False
        self.saved = {k: getattr(updater, k) for k in (
            "STATE_DIR", "RESULT_DIR", "RELEASES_DIR", "CURRENT_LINK", "PUBKEY", "CONFIG_FILE", "SYSTEM_DESTS", "RUN",
            "healthy", "recording_in_progress", "HEALTH_TIMEOUT")}
        updater.STATE_DIR, updater.RELEASES_DIR, updater.CURRENT_LINK = self.state, self.releases, self.current
        updater.PUBKEY, updater.CONFIG_FILE, updater.SYSTEM_DESTS = self.pub, self.config, self.sysdirs
        updater.RESULT_DIR = os.path.join(t, "result")  # dossier de root : distinct de celui de l'application
        updater.HEALTH_TIMEOUT = 2.0
        updater.healthy = lambda expected: self.healthy
        updater.recording_in_progress = lambda: self.recording
        real_run = subprocess.run

        def fake_run(cmd, **kw):
            if cmd[0] in ("systemctl", "udevadm"):
                self.calls.append(cmd[:3])
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return real_run(cmd, **kw)

        updater.RUN = fake_run

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(updater, k, v)
        self.tmp.cleanup()

    # -- aides
    def package(self, version="0.3.0", key=None):
        return mkpkg.build(ROOT, version, key or self.key, os.path.join(self.tmp.name, "dist"), "essai")

    def bundle_from_payload(self, payload, key=None):
        path = os.path.join(self.tmp.name, "dist", "custom.tar")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(mkpkg.bundle(payload, mkpkg.sign(payload, key or self.key)))
        return path

    def version_on_disk(self):
        return open(os.path.join(self.current, "VERSION")).read().strip()

    # -- cas nominaux
    def test_install_switches_and_keeps_old(self):
        result = updater.apply(self.package("0.3.0"))
        self.assertEqual((result["state"], result["version"]), ("ok", "0.3.0"))
        self.assertEqual(self.version_on_disk(), "0.3.0")
        self.assertTrue(os.path.isdir(os.path.join(self.releases, "0.1.0")))  # l'ancienne reste pour le retour arrière
        self.assertIn(["systemctl", "restart", "xr18rec.service"], self.calls)
        self.assertTrue(os.path.isfile(os.path.join(self.sysdirs["systemd"], "xr18rec.service")))
        self.assertTrue(os.path.isfile(os.path.join(self.sysdirs["polkit"], "50-xr18rec.rules")))
        self.assertEqual(updater.read_state()["state"], "ok")
        self.assertEqual(os.listdir(self.releases).count(".incoming-"), 0)

    def test_inspect_does_not_install(self):
        man = updater.inspect(self.package("0.3.0"), self.pub)
        self.assertEqual(man["version"], "0.3.0")
        self.assertEqual(self.version_on_disk(), "0.1.0")

    def test_prunes_old_releases(self):
        for v in ("0.2.0", "0.3.0", "0.4.0", "0.5.0"):
            self.assertEqual(updater.apply(self.package(v))["state"], "ok")
        self.assertEqual(sorted(os.listdir(self.releases)), ["0.3.0", "0.4.0", "0.5.0"])

    # -- refus
    def test_wrong_signature_is_refused(self):
        result = updater.apply(self.package("0.3.0", key=self.other_key))
        self.assertEqual(result["state"], "failed")
        self.assertIn("signature", result["message"])
        self.assertEqual(self.version_on_disk(), "0.1.0")
        self.assertFalse(os.path.exists(os.path.join(self.releases, "0.3.0")))

    def test_tampered_payload_is_refused(self):
        path = self.package("0.3.0")
        with tarfile.open(path) as t:
            payload = bytearray(t.extractfile("payload.tar.gz").read())
            sig = t.extractfile("payload.sig").read()
        payload[len(payload) // 2] ^= 0xFF
        with open(path, "wb") as f:
            f.write(mkpkg.bundle(bytes(payload), sig))
        self.assertEqual(updater.apply(path)["state"], "failed")
        self.assertEqual(self.version_on_disk(), "0.1.0")

    def test_path_traversal_is_refused_even_when_signed(self):
        evil = os.path.join(self.tmp.name, "evil")
        payload = raw_payload([(file_info("../../evil", b"x"), b"x")])
        result = updater.apply(self.bundle_from_payload(payload))
        self.assertEqual(result["state"], "failed")
        self.assertIn("chemin refusé", result["message"])
        self.assertFalse(os.path.exists(evil))

    def test_links_are_refused_even_when_signed(self):
        link = tarfile.TarInfo("release/xr18rec/__main__.py")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        result = updater.apply(self.bundle_from_payload(raw_payload([(link, None)])))
        self.assertEqual(result["state"], "failed")
        self.assertIn("refusée", result["message"])

    def test_unexpected_system_destination_is_refused(self):
        man = {"version": "0.3.0", "updater_level": 1, "system": [{"kind": "etc", "name": "passwd"}]}
        main = file_info("release/xr18rec/__main__.py", b"")
        payload = raw_payload([(main, b""), (file_info("manifest.json", json.dumps(man).encode()),
                                              json.dumps(man).encode())])
        result = updater.apply(self.bundle_from_payload(payload))
        self.assertEqual(result["state"], "failed")
        self.assertIn("système refusé", result["message"])

    def test_downgrade_refused_unless_allowed(self):
        updater.apply(self.package("0.3.0"))
        older = self.package("0.2.0")
        self.assertEqual(updater.apply(older)["state"], "failed")
        self.assertEqual(self.version_on_disk(), "0.3.0")
        self.assertEqual(updater.apply(older, allow_downgrade=True)["state"], "ok")
        self.assertEqual(self.version_on_disk(), "0.2.0")

    def test_same_version_refused(self):
        updater.apply(self.package("0.3.0"))
        self.assertIn("plus récente", updater.apply(self.package("0.3.0"))["message"])

    def test_requires_newer_updater(self):
        man = {"version": "0.3.0", "updater_level": 99}
        payload = raw_payload([(file_info("manifest.json", json.dumps(man).encode()), json.dumps(man).encode())])
        result = updater.apply(self.bundle_from_payload(payload))
        self.assertIn("reflasher", result["message"])

    def test_refused_while_recording(self):
        self.recording = True
        result = updater.apply(self.package("0.3.0"))
        self.assertEqual(result["state"], "failed")
        self.assertIn("enregistrement", result["message"])
        self.assertEqual(self.calls, [])  # l'application n'a pas été touchée

    def test_oversized_bundle_refused(self):
        big = os.path.join(self.tmp.name, "big.tar")
        with open(big, "wb") as f:
            f.truncate(updater.MAX_BUNDLE + 1)
        self.assertEqual(updater.apply(big)["state"], "failed")

    def test_garbage_file_refused(self):
        junk = os.path.join(self.tmp.name, "junk.tar")
        open(junk, "wb").write(b"pas une archive")
        self.assertEqual(updater.apply(junk)["state"], "failed")

    # -- retour arrière
    def test_rollback_when_new_version_does_not_answer(self):
        self.healthy = False
        result = updater.apply(self.package("0.3.0"))
        self.assertEqual(result["state"], "rolled_back")
        self.assertIn("ancienne version rétablie", result["message"])
        self.assertEqual(self.version_on_disk(), "0.1.0")
        self.assertEqual(self.calls.count(["systemctl", "restart", "xr18rec.service"]), 2)  # puis de nouveau l'ancienne
        self.assertFalse(os.path.exists(os.path.join(self.sysdirs["systemd"], "xr18rec.service")))  # fichiers système retirés

    def test_rollback_restores_previous_system_file(self):
        os.makedirs(self.sysdirs["systemd"])
        dest = os.path.join(self.sysdirs["systemd"], "xr18rec.service")
        open(dest, "w").write("ANCIEN")
        self.healthy = False
        updater.apply(self.package("0.3.0"))
        self.assertEqual(open(dest).read(), "ANCIEN")

    def test_broken_new_version_is_not_switched(self):
        main = file_info("release/xr18rec/__main__.py", b"raise SystemExit('casse')\n")
        man = {"version": "0.3.0", "updater_level": 1}
        payload = raw_payload([(main, b"raise SystemExit('casse')\n"),
                               (file_info("manifest.json", json.dumps(man).encode()), json.dumps(man).encode())])
        result = updater.apply(self.bundle_from_payload(payload))
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.version_on_disk(), "0.1.0")
        self.assertNotIn(["systemctl", "restart", "xr18rec.service"], self.calls)

    # -- déclenchement par l'application
    def test_run_pending_uses_only_the_basename(self):
        shutil.copy(self.package("0.3.0"), os.path.join(self.state, "incoming.tar"))
        with open(os.path.join(self.state, "ready"), "w") as f:
            json.dump({"bundle": "../../etc/shadow/../incoming.tar"}, f)
        self.assertEqual(updater.run_pending(), 0)
        self.assertEqual(self.version_on_disk(), "0.3.0")
        self.assertFalse(os.path.exists(os.path.join(self.state, "ready")))

    def test_run_pending_without_request_does_nothing(self):
        self.assertEqual(updater.run_pending(), 0)
        self.assertEqual(updater.read_state()["state"], "none")


class VersionTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updater.parse_version("v0.2.0"), (0, 2, 0))
        self.assertEqual(updater.parse_version("1.10.3-5-gabc"), (1, 10, 3))
        self.assertIsNone(updater.parse_version("f5cbfe2"))
        self.assertLess(updater.parse_version("0.9.0"), updater.parse_version("0.10.0"))


if __name__ == "__main__":
    unittest.main()
