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

from test_levels import pcm  # noqa: E402
from test_recorder import make_cfg  # noqa: E402
from xr18rec import audio, meters, session  # noqa: E402
from xr18rec.server import make_server  # noqa: E402
from xr18rec.session import Recorder, RecorderError  # noqa: E402


def wait_for(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class FeedTests(unittest.TestCase):
    def test_stream_in_odd_pieces(self):
        mon = meters.LevelMonitor(None, 1000, 2, 4, 3)
        data = pcm([[0.1, 0.1]] * 50 + [[0.1, 1.0]] * 3 + [[0.1, 0.1]] * 47, 4)
        total = 0
        for i in range(0, len(data), 37):  # morceaux qui coupent les trames en deux
            total += mon.feed(data[i:i + 37])
        self.assertEqual(total, 100)
        ch = mon.snapshot()["channels"][1]
        self.assertTrue(ch["clipped"])
        self.assertEqual((ch["clip_samples"], ch["events"]), (3, [0.05]))

    def test_no_file_and_no_save(self):
        mon = meters.LevelMonitor(None, 1000, 1, 4, 3)
        mon.feed(pcm([[0.5]] * 10, 4))
        mon.save()  # ne fait rien et ne plante pas (pas de dossier de prise)

    def test_window_forgets_old_audio(self):
        mon = meters.LevelMonitor(None, 1000, 1, 4, 3)
        mon.feed(pcm([[1.0]] * 100 + [[0.01]] * 1000, 4))  # un pic, puis 1 s de quasi-silence
        ch = mon.snapshot()["channels"][0]
        self.assertLess(ch["window_db"] if ch["window_db"] is not None else -90, -30)  # le pic est sorti de la fenêtre
        self.assertEqual(ch["take_peak_db"], meters.dbfs(127))  # mais la crête reste en mémoire


class CaptureBusyTests(unittest.TestCase):
    def test_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "card1", "pcm0c", "sub0"))
            open(os.path.join(tmp, "cards"), "w").write(" 0 [Headphones     ]: bcm2835 - bcm2835\n 1 [XR18           ]: USB-Audio - XR18\n")
            status = os.path.join(tmp, "card1", "pcm0c", "sub0", "status")
            old = audio.ASOUND
            audio.ASOUND = tmp
            try:
                open(status, "w").write("closed\n")
                self.assertIs(audio.capture_busy("hw:CARD=XR18,DEV=0"), False)
                open(status, "w").write("state: RUNNING\nowner_pid   : 123\n")
                self.assertIs(audio.capture_busy("hw:CARD=XR18,DEV=0"), True)
                self.assertIs(audio.capture_busy("hw:1,0"), True)
                self.assertIsNone(audio.capture_busy("hw:CARD=Inconnue,DEV=0"))
                self.assertIsNone(audio.capture_busy("default"))
            finally:
                audio.ASOUND = old


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = os.path.join(self.tmp.name, "carte.lock")
        os.environ.update({"FAKE_ARECORD_SIGNAL": "1", "FAKE_ARECORD_BPS": "200000", "FAKE_ARECORD_LOCK": self.lock})
        self.rec = Recorder(make_cfg(self.tmp.name))

    def tearDown(self):
        self.rec.shutdown()
        for k in ("FAKE_ARECORD_SIGNAL", "FAKE_ARECORD_BPS", "FAKE_ARECORD_LOCK"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def test_levels_before_recording(self):
        out = self.rec.monitor_start()
        self.assertEqual(out["mode"], "monitor")
        self.assertTrue(wait_for(lambda: self.rec.levels()["channels"] and self.rec.levels()["channels"][2]["clipped"]))
        lv = self.rec.levels()
        self.assertEqual((lv["mode"], lv["active"], len(lv["channels"])), ("monitor", True, 18))
        self.assertFalse(lv["channels"][0]["clipped"])
        self.assertEqual(lv["names"][0], "Ch 01")
        self.assertEqual(self.rec.status()["levels"]["clipped"], [])  # la prise n'a pas commencé : rien à signaler

    def test_rec_during_monitor_succeeds_with_exclusive_card(self):
        self.rec.monitor_start()
        self.assertTrue(os.path.exists(self.lock))  # le contrôle tient la carte
        name = self.rec.start("apres-controle")  # REC : doit libérer la carte puis enregistrer, sans échec
        self.assertTrue(name.endswith("apres-controle"))
        time.sleep(0.8)
        self.assertEqual(self.rec.status()["state"], "recording")
        self.assertEqual(self.rec.levels()["mode"], "recording")
        self.assertIsNone(self.rec.status()["error"])
        self.rec.stop()
        self.assertEqual(self.rec.levels()["mode"], "last")
        self.assertFalse(os.path.exists(self.lock))

    def test_stops_when_nobody_looks(self):
        old = session.MONITOR_IDLE_S
        session.MONITOR_IDLE_S = 0.6
        try:
            self.rec.monitor_start()
            time.sleep(0.2)
            self.assertTrue(os.path.exists(self.lock))
            self.assertTrue(wait_for(lambda: not os.path.exists(self.lock), 6))  # arrêt automatique, carte rendue
            self.assertTrue(wait_for(lambda: self.rec.levels()["mode"] == "none", 3))  # le fil de lecture finit de se refermer
        finally:
            session.MONITOR_IDLE_S = old

    def test_polling_keeps_it_alive(self):
        old = session.MONITOR_IDLE_S
        session.MONITOR_IDLE_S = 0.8
        try:
            self.rec.monitor_start()
            end = time.monotonic() + 2.0
            while time.monotonic() < end:
                self.rec.levels()  # la page interroge : le contrôle continue
                time.sleep(0.2)
            self.assertEqual(self.rec.levels()["mode"], "monitor")
        finally:
            session.MONITOR_IDLE_S = old

    def test_refused_while_recording_and_when_busy(self):
        self.rec.monitor_start()
        self.rec.monitor_stop()
        self.assertFalse(wait_for(lambda: os.path.exists(self.lock), 0.3))
        open(self.lock, "w").close()  # un autre programme tient la carte
        with self.assertRaises(RecorderError) as cm:
            self.rec.monitor_start()
        self.assertIn("busy", str(cm.exception))
        os.remove(self.lock)
        self.rec.start("x")
        time.sleep(0.5)
        with self.assertRaises(RecorderError):
            self.rec.monitor_start()
        self.rec.stop()

    def test_start_is_idempotent_and_stop_too(self):
        self.rec.monitor_start()
        self.rec.monitor_start()  # déjà en cours : pas de second flux (la carte est exclusive)
        self.rec.monitor_stop()
        self.rec.monitor_stop()
        self.assertEqual(self.rec.levels()["mode"], "none")

    def test_reset_applies_to_monitor(self):
        self.rec.monitor_start()
        self.assertTrue(wait_for(lambda: self.rec.levels()["channels"] and self.rec.levels()["channels"][2]["clipped"]))
        self.assertIsNotNone(self.rec.reset_levels()["channels"][2]["take_peak_db"])

    def test_api(self):
        srv = make_server(self.rec)
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
            code, lv = call("/api/levels/monitor", {"on": True})
            self.assertEqual((code, lv["mode"]), (200, "monitor"))
            self.assertEqual(call("/api/levels")[1]["mode"], "monitor")
            self.assertEqual(call("/api/levels/monitor", {"on": False})[1]["mode"], "none")
            open(self.lock, "w").close()
            code, err = call("/api/levels/monitor", {"on": True})
            self.assertEqual(code, 409)
            self.assertIn("busy", err["error"])
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
