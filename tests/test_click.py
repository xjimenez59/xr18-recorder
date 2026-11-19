import json
import os
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

from test_recorder import make_cfg  # noqa: E402
from xr18rec import click  # noqa: E402
from xr18rec.click import ClickError, Metronome  # noqa: E402
from xr18rec.server import make_server  # noqa: E402
from xr18rec.session import Recorder  # noqa: E402

FAKE_APLAY = os.path.join(HERE, "fake_aplay.py")


def cfg_with_aplay(tmp, **kw):
    wrapper = os.path.join(tmp, "aplay")
    with open(wrapper, "w") as f:
        f.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_APLAY}" "$@"\n')
    os.chmod(wrapper, 0o755)
    cfg = make_cfg(tmp, **kw)
    cfg.aplay_bin = wrapper
    cfg.format = "S24_3LE"
    cfg.validate()
    return cfg


def onsets(path, channel, width, channels, rate=48000):
    """Trames où un clic commence (voie 1-based), après au moins 50 ms de silence."""
    with open(path, "rb") as f:
        data = f.read()
    frame = channels * width
    off = (channel - 1) * width
    found, last = [], -10**9
    for i in range(len(data) // frame):
        if any(data[i * frame + off:i * frame + off + width]):
            if i - last > rate // 20:
                found.append(i)
            last = i
    return found, data


class SynthTests(unittest.TestCase):
    def test_every_sound_is_short_smooth_and_bounded(self):
        for name in click.SOUNDS:
            for accent in (False, True):
                s = click.synth(name, accent, 48000)
                self.assertLessEqual(len(s), int(click.MAX_CLIP_S * 48000), name)
                self.assertLessEqual(max(abs(v) for v in s), 1.0 + 1e-9, name)
                self.assertGreater(max(abs(v) for v in s), 0.5, name)
                self.assertEqual(s[0], 0.0, name)  # départ à zéro : pas de claquement
                self.assertLess(abs(s[-1]), 0.02, name)  # fin quasi nulle

    def test_accent_is_higher_pitched(self):
        def rate_of_crossings(v):
            return sum(1 for a, b in zip(v, v[1:]) if (a < 0) != (b < 0)) / len(v)
        for name in ("bip", "bois"):
            self.assertGreater(rate_of_crossings(click.synth(name, True, 48000)),
                               rate_of_crossings(click.synth(name, False, 48000)))

    def test_encode_formats(self):
        self.assertEqual(click.encode(1, "S24_3LE"), b"\x01\x00\x00")
        self.assertEqual(click.encode(-1, "S24_3LE"), b"\xff\xff\xff")
        self.assertEqual(click.encode(1, "S32_LE"), b"\x00\x01\x00\x00")
        self.assertEqual(click.encode(-1, "S32_LE"), b"\x00\xff\xff\xff")
        self.assertEqual(click.encode(0x123456, "S16_LE"), (0x1234).to_bytes(2, "little"))

    def test_render_only_on_chosen_channel_and_level(self):
        data = click.render("bip", False, 1.0, -6.0, 3, 5, "S24_3LE", 48000, 3)
        frame = 5 * 3
        self.assertEqual(len(data) % frame, 0)
        peak = 0
        for i in range(0, len(data), frame):
            f = data[i:i + frame]
            self.assertFalse(any(f[:6]) or any(f[9:]))  # voies 1, 2, 4, 5 muettes
            peak = max(peak, abs(int.from_bytes(f[6:9], "little", signed=True)))
        self.assertAlmostEqual(peak / (2**23 - 1), 10 ** (-6 / 20), delta=0.01)


class ConfigTests(unittest.TestCase):
    def test_click_settings_validated(self):
        from xr18rec import config as C
        C.Config().validate()
        for bad in ({"click_bpm": 20}, {"click_bpm": 300}, {"click_beats": 0}, {"click_sound": "klaxon"},
                    {"click_channel": 65}, {"click_level_db": 3.0}, {"click_accent": "oui"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                C.Config(**bad).validate()


class MetronomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "out.raw")
        os.environ["FAKE_APLAY_OUT"] = self.out
        os.environ["FAKE_APLAY_SPEED"] = "20"
        self.cfg = cfg_with_aplay(self.tmp.name, click_bpm=240, click_beats=4)
        self.cfg.click_sound = "tic"
        self.m = Metronome(self.cfg)

    def tearDown(self):
        self.m.stop()
        for k in ("FAKE_APLAY_OUT", "FAKE_APLAY_SPEED", "FAKE_APLAY_FAIL", "FAKE_APLAY_UNDERRUN"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def test_beats_are_sample_exact_and_on_one_channel(self):
        self.cfg.click_bpm = 117  # période non entière (24615,38 trames) : un seul arrondi par battement
        self.m.start()
        time.sleep(0.8)  # environ 16 s d'audio à 20x
        self.m.stop()
        found, data = onsets(self.out, 16, 3, 18)
        self.assertGreater(len(found), 10)
        for k, pos in enumerate(found):
            self.assertEqual(pos - 1, round(k * 60 * 48000 / 117), k)  # le 1er échantillon d'un battement vaut 0
        frame = 54
        other = bytearray()
        for i in range(0, min(len(data), frame * 60000), frame):
            other += data[i:i + 45] + data[i + 48:i + 54]
        self.assertFalse(any(other))  # aucune autre voie ne reçoit de signal

    def test_accent_and_flat_levels(self):
        def peaks(accent):
            self.cfg.click_accent = accent
            if os.path.exists(self.out):
                os.remove(self.out)
            self.m.start()
            time.sleep(0.6)
            self.m.stop()
            found, data = onsets(self.out, 16, 3, 18)
            res = []
            for pos in found[:8]:
                res.append(max(abs(int.from_bytes(data[(pos + j) * 54 + 45:(pos + j) * 54 + 48], "little", signed=True))
                               for j in range(40)))
            return res
        acc = peaks(True)
        self.assertGreater(acc[0], acc[1] * 1.2)  # temps 1 plus fort
        self.assertGreater(acc[4], acc[5] * 1.2)  # le 5e battement ouvre la mesure suivante
        flat = peaks(False)
        self.assertLess(max(flat) - min(flat), max(flat) * 0.05)  # tous les temps identiques

    def test_start_stop_and_status(self):
        self.assertFalse(self.m.status()["running"])
        self.assertTrue(self.m.start()["running"])
        self.assertTrue(self.m.start()["running"])  # idempotent
        self.assertFalse(self.m.stop()["running"])
        self.assertFalse(self.m.stop()["running"])

    def test_aplay_failure_is_reported(self):
        os.environ["FAKE_APLAY_FAIL"] = "1"
        self.m.start()
        time.sleep(0.5)
        st = self.m.status()
        self.assertFalse(st["running"])
        self.assertIn("busy", st["error"])

    def test_underruns_counted(self):
        os.environ["FAKE_APLAY_UNDERRUN"] = "1"
        self.m.start()
        time.sleep(0.5)
        self.assertEqual(self.m.status()["underruns"], 1)

    def test_channel_beyond_card_refused(self):
        self.cfg.channels = 8
        with self.assertRaises(ClickError):
            self.m.start()


class WithRecTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FAKE_APLAY_OUT"] = os.path.join(self.tmp.name, "out.raw")
        os.environ["FAKE_APLAY_SPEED"] = "1"
        self.cfg = cfg_with_aplay(self.tmp.name)
        self.rec = Recorder(self.cfg)
        self.m = Metronome(self.cfg, self.rec)
        self.rec.on_started = self.m.on_rec_started

    def tearDown(self):
        self.m.stop()
        self.rec.shutdown()
        os.environ.pop("FAKE_APLAY_OUT", None)
        os.environ.pop("FAKE_APLAY_SPEED", None)
        self.tmp.cleanup()

    def wait(self, cond, t=5):
        end = time.time() + t
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_option_off_does_not_start_click(self):
        self.rec.start("x")
        time.sleep(0.3)
        self.assertFalse(self.m.running())
        self.rec.stop()

    def test_click_follows_rec(self):
        self.cfg.click_with_rec = True
        self.rec.start("x")
        self.assertTrue(self.m.running())
        self.assertTrue(self.m.status()["started_by_rec"])
        self.rec.stop()
        self.assertTrue(self.wait(lambda: not self.m.running()), "le clic doit s'arrêter avec la prise")

    def test_manual_click_survives_rec_stop(self):
        self.cfg.click_with_rec = True
        self.m.start()  # démarré à la main avant REC
        self.rec.start("x")
        self.assertFalse(self.m.status()["started_by_rec"])
        self.rec.stop()
        time.sleep(0.8)
        self.assertTrue(self.m.running())

    def test_click_failure_does_not_block_rec(self):
        self.cfg.click_with_rec = True
        os.environ["FAKE_APLAY_FAIL"] = "1"
        try:
            self.rec.start("x")
            self.assertTrue(self.rec.is_recording())
        finally:
            os.environ.pop("FAKE_APLAY_FAIL", None)
        self.rec.stop()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FAKE_APLAY_OUT"] = os.path.join(self.tmp.name, "out.raw")
        self.cfg = cfg_with_aplay(self.tmp.name)
        self.rec = Recorder(self.cfg)
        self.srv = make_server(self.rec)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.app.click.stop()
        self.srv.shutdown()
        self.srv.server_close()
        self.rec.shutdown()
        os.environ.pop("FAKE_APLAY_OUT", None)
        self.tmp.cleanup()

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_status_start_stop(self):
        code, st = self.call("/api/click")
        self.assertEqual((code, st["running"], st["bpm"], st["sound"]), (200, False, 100, "bois"))
        self.assertEqual([s["id"] for s in st["sounds"]], ["bois", "cloche", "bip", "tic"])
        code, st = self.call("/api/click/start", {})
        self.assertEqual(code, 200)
        self.assertTrue(st["running"])
        code, st = self.call("/api/click/stop", {})
        self.assertFalse(st["running"])

    def test_settings_change_applies_live(self):
        code, _ = self.call("/api/settings", {"click_bpm": 140, "click_accent": False, "click_sound": "cloche",
                                              "click_with_rec": True})
        self.assertEqual(code, 200)
        code, st = self.call("/api/click")
        self.assertEqual((st["bpm"], st["accent"], st["sound"], st["with_rec"]), (140, False, "cloche", True))
        self.assertEqual(self.call("/api/settings", {"click_bpm": 10})[0], 400)
        self.assertEqual(self.call("/api/settings", {"click_accent": "oui"})[0], 400)

    def test_settings_changeable_during_recording(self):
        self.rec.start("x")
        try:
            self.assertEqual(self.call("/api/settings", {"click_bpm": 90})[0], 200)
        finally:
            self.rec.stop()


if __name__ == "__main__":
    unittest.main()
