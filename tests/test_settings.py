import ipaddress
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from test_recorder import FakeXR18Mixer, make_cfg  # noqa: E402
from xr18rec import audio, system, xr18osc  # noqa: E402
from xr18rec import config as config_mod  # noqa: E402
from xr18rec.app import App  # noqa: E402
from xr18rec.server import make_server  # noqa: E402
from xr18rec.session import Recorder, RecorderError  # noqa: E402

HW_PARAMS = """HW Params of device "hw:CARD=XR18,DEV=0":
--------------------
ACCESS:  MMAP_INTERLEAVED RW_INTERLEAVED
FORMAT:  S24_3LE S32_LE
SUBFORMAT:  STD
SAMPLE_BITS: [24 32]
CHANNELS: 18
RATE: [44100 48000]
PERIOD_TIME: (125 1000000)
--------------------
"""
ARECORD_L = """**** List of CAPTURE Hardware Devices ****
card 0: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones [bcm2835 Headphones]
card 1: XR18 [XR18], device 0: USB Audio [USB Audio]
"""


class OverlayTests(unittest.TestCase):
    def test_save_apply_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            self.assertEqual(config_mod.apply_overlay(cfg), {})  # pas de fichier
            config_mod.save_overlay(cfg, {"xr18_host": "10.0.0.5"})
            config_mod.save_overlay(cfg, {"mix_max_db": -6.0})
            fresh = make_cfg(tmp)
            self.assertEqual(config_mod.apply_overlay(fresh), {"xr18_host": "10.0.0.5", "mix_max_db": -6.0})
            self.assertEqual((fresh.xr18_host, fresh.mix_max_db), ("10.0.0.5", -6.0))
            config_mod.clear_overlay(cfg)
            self.assertEqual(config_mod.read_overlay(cfg), {})

    def test_invalid_or_foreign_overlay_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            with open(cfg.settings_file, "w") as f:
                json.dump({"rate": 96000, "port": 1}, f)  # valeur invalide + clé non modifiable
            self.assertEqual(config_mod.apply_overlay(cfg), {})
            self.assertEqual(cfg.rate, 48000)
            with open(cfg.settings_file, "w") as f:
                f.write("pas du json")
            self.assertEqual(config_mod.apply_overlay(cfg), {})


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(self.tmp.name)
        self.rec = Recorder(self.cfg)
        self.app = App(self.cfg, self.rec)
        self.xr = FakeXR18Mixer()

    def tearDown(self):
        self.rec.shutdown()
        if self.app.mixer:
            self.app.mixer.close()
        self.xr.close()
        self.tmp.cleanup()

    def test_apply_live_and_persist(self):
        out = self.app.apply({"rate": 44100, "segment_seconds": 600})
        self.assertEqual((self.cfg.rate, self.cfg.segment_seconds), (44100, 600))
        self.assertEqual(sorted(out["modified"]), ["rate", "segment_seconds"])
        self.assertEqual(config_mod.read_overlay(self.cfg)["rate"], 44100)

    def test_rejects_invalid_and_unknown(self):
        for bad in ({"rate": 96000}, {"rate": "48000"}, {"rate": True}, {"port": 81}, {"mix_buses": [7]},
                    {"mix_max_db": 50}, {"segment_seconds": 5000}, {"xr18_host": 5}, {"format": "FLOAT"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.app.apply(bad)
        self.assertEqual(self.cfg.rate, 48000)
        self.assertEqual(config_mod.read_overlay(self.cfg), {})  # rien n'est mémorisé quand c'est refusé

    def test_unchanged_values_are_not_saved(self):
        self.app.apply({"rate": 48000})
        self.assertEqual(config_mod.read_overlay(self.cfg), {})

    def test_audio_locked_while_recording(self):
        self.rec.start("x")
        time.sleep(0.3)
        with self.assertRaises(RecorderError):
            self.app.apply({"rate": 44100})
        self.app.apply({"mix_max_db": -3.0})  # le plafond des retours reste réglable
        self.assertEqual(self.cfg.mix_max_db, -3.0)
        with self.assertRaises(RecorderError):
            self.app.restart_app()

    def test_xr18_host_creates_and_removes_mixer(self):
        self.cfg.xr18_osc_port = self.xr.port
        self.assertIsNone(self.app.mixer)
        self.app.apply({"xr18_host": "127.0.0.1"})
        self.assertIsNotNone(self.app.mixer)
        self.assertEqual(len(self.app.mixer.get_bus(1)["sources"]), 17)  # joint la console
        self.app.apply({"mix_max_db": -9.0, "mix_buses": [1, 2]})
        self.assertEqual((self.app.mixer.max_db, self.app.mixer.buses), (-9.0, [1, 2]))
        self.app.apply({"xr18_host": ""})
        self.assertIsNone(self.app.mixer)

    def test_reset_restores_base(self):
        base = os.path.join(self.tmp.name, "base.json")
        with open(base, "w") as f:
            json.dump({"rate": 48000, "mix_max_db": -12.0}, f)
        app = App(config_mod.load(base), self.rec, base_path=base)
        app.cfg.settings_file = self.cfg.settings_file
        app.apply({"mix_max_db": 0.0, "rate": 44100})
        out = app.reset()
        self.assertEqual((app.cfg.rate, app.cfg.mix_max_db, out["modified"]), (48000, -12.0, []))

    def test_restart_callback(self):
        asked = []
        self.app.restart = lambda: asked.append(1)
        self.app.restart_app()
        self.assertEqual(asked, [1])


class AudioTests(unittest.TestCase):
    def test_parse(self):
        p = audio.parse_hw_params(HW_PARAMS)
        self.assertEqual((p["formats"], p["channels"], p["rates"]), (["S24_3LE", "S32_LE"], [18], [44100, 48000]))
        single = audio.parse_hw_params("FORMAT:  S32_LE\nCHANNELS: [1 18]\nRATE: 44100\n")
        self.assertEqual((single["rates"], single["channels"][0], single["channels"][-1]), ([44100], 1, 18))
        wide = audio.parse_hw_params("RATE: [8000 192000]\n")
        self.assertEqual(wide["rates"], [44100, 48000])
        cards = audio.parse_cards(ARECORD_L)
        self.assertEqual([c["hw"] for c in cards], ["hw:CARD=Headphones,DEV=0", "hw:CARD=XR18,DEV=0"])

    def test_probe_checks(self):
        class Cfg:
            arecord_bin, device, rate, format, channels = "arecord", "hw:CARD=XR18,DEV=0", 48000, "S16_LE", 18

        def fake_run(cmd, **kw):
            out = ARECORD_L if "-l" in cmd else ""
            err = "" if "-l" in cmd else HW_PARAMS
            return subprocess.CompletedProcess(cmd, 0, out, err)

        r = audio.probe(Cfg, False, run=fake_run)
        self.assertEqual(r["checks"], {"rate": True, "format": False, "channels": True})  # S16_LE non proposé
        self.assertIn("occupée par l'enregistrement", audio.probe(Cfg, True, run=fake_run)["error"])

    def test_probe_error_message(self):
        class Cfg:
            arecord_bin, device, rate, format, channels = "arecord", "hw:CARD=XR18,DEV=0", 48000, "S32_LE", 18

        def fake_run(cmd, **kw):
            if "-l" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 1, "", "arecord: main:831: audio open error: No such file or directory")

        r = audio.probe(Cfg, False, run=fake_run)
        self.assertIn("No such file", r["error"])
        self.assertIsNone(r["params"])


class DiscoverTests(unittest.TestCase):
    def test_finds_console_by_scanning(self):
        xr = FakeXR18Mixer()
        try:
            found = xr18osc.discover([ipaddress.ip_network("127.0.0.0/30")], timeout=1.0, port=xr.port)
        finally:
            xr.close()
        self.assertEqual([(d["ip"], d["name"], d["model"], d["version"]) for d in found],
                         [("127.0.0.1", "XR18-test", "XR18", "1.18")])

    def test_nothing_found(self):
        self.assertEqual(xr18osc.discover([ipaddress.ip_network("127.0.0.0/30")], timeout=0.3, port=9), [])


class SystemTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.synced = "no"

        def fake_run(cmd, **kw):
            self.calls.append(cmd)
            if cmd[:2] == ["timedatectl", "show"]:
                return subprocess.CompletedProcess(cmd, 0, self.synced + "\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        self.orig = system.RUN
        system.RUN = fake_run

    def tearDown(self):
        system.RUN = self.orig

    def test_set_time(self):
        self.assertEqual(system.set_time(1_800_000_000), "horloge réglée")
        names = [c[1] for c in self.calls if c[0] == "timedatectl"]
        self.assertEqual(names, ["show", "set-ntp", "set-time"])
        self.assertEqual(self.calls[-1][0:2], ["timedatectl", "set-time"])

    def test_set_time_skipped_when_ntp_works(self):
        self.synced = "yes"
        self.assertIn("inutile", system.set_time(1_800_000_000))
        self.assertFalse([c for c in self.calls if c[1] == "set-time"])

    def test_invalid_time_and_power(self):
        for bad in (0, -5, "1800000000", 10**12, None):
            with self.assertRaises(system.SystemActionError):
                system.set_time(bad)
        system.power("poweroff")
        self.assertEqual(self.calls[-1], ["systemctl", "poweroff"])
        with self.assertRaises(system.SystemActionError):
            system.power("rm -rf")

    def test_failure_is_reported(self):
        system.RUN = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "Access denied")
        with self.assertRaises(system.SystemActionError) as cm:
            system.power("reboot")
        self.assertIn("Access denied", str(cm.exception))


class HttpTests(unittest.TestCase):
    def test_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(make_cfg(tmp))
            srv = make_server(rec)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"

            def call(path, data=None):
                req = urllib.request.Request(base + path, method="POST" if data is not None else "GET",
                                             data=json.dumps(data).encode() if data is not None else None)
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        return r.status, json.loads(r.read())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read())

            try:
                code, s = call("/api/settings")
                self.assertEqual((code, s["values"]["rate"]), (200, 48000))
                self.assertEqual(call("/api/settings", {"rate": 44100})[1]["values"]["rate"], 44100)
                code, err = call("/api/settings", {"rate": 96000})
                self.assertEqual(code, 400)
                self.assertIn("rate", err["error"])
                self.assertEqual(call("/api/settings", {"port": 1})[0], 400)
                self.assertEqual(call("/api/settings/reset", {})[1]["modified"], [])
                code, info = call("/api/system")
                self.assertEqual(code, 200)
                self.assertIn("hostname", info)
                self.assertEqual(call("/api/system/time", {"epoch": 5})[0], 502)  # invalide
                self.assertEqual(call("/api/system/restart-app", {})[0], 409)  # pas de mode de redémarrage ici
                code, probe = call("/api/audio")
                self.assertEqual((code, probe["checks"]["rate"], probe["cards"][0]["name"]), (200, True, "XR18"))
                self.assertEqual(urllib.request.urlopen(base + "/settings", timeout=5).status, 200)
                rec.start("x")
                time.sleep(0.3)
                self.assertEqual(call("/api/system/power", {"action": "poweroff"})[0], 409)
                self.assertEqual(call("/api/settings", {"rate": 48000, "segment_seconds": 300})[0], 409)
                rec.stop()
            finally:
                rec.shutdown()
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
