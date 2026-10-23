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

from test_recorder import FakeXR18Mixer, make_cfg  # noqa: E402
from xr18rec.mixer import MixError, MixerClient  # noqa: E402
from xr18rec.server import make_server  # noqa: E402
from xr18rec.session import Recorder  # noqa: E402


class MuteTests(unittest.TestCase):
    def setUp(self):
        self.xr = FakeXR18Mixer()
        self.mx = MixerClient("127.0.0.1", self.xr.port, buses=[1, 2, 3])

    def tearDown(self):
        self.mx.close()
        self.xr.close()

    def wait(self, cond):
        for _ in range(30):
            if cond():
                return True
            time.sleep(0.1)
        return False

    def test_initial_state(self):
        st = self.mx.mute_state()
        self.assertEqual(st["main"], {"available": True, "muted": False, "partial": False})
        self.assertEqual(st["buses"], {"available": True, "muted": False, "partial": False})

    def test_mute_buses_only_configured_ones(self):
        self.mx.mute_state()
        st = self.mx.set_mute("buses", True)
        self.assertTrue(st["buses"]["muted"])
        self.assertFalse(st["main"]["muted"])  # indépendants
        self.assertTrue(self.wait(lambda: all(self.xr.values[f"/bus/{b}/mix/on"] == 0 for b in (1, 2, 3))))
        self.assertEqual([self.xr.values[f"/bus/{b}/mix/on"] for b in (4, 5, 6)], [1, 1, 1])  # hors config : intacts
        self.assertEqual(self.xr.values["/lr/mix/on"], 1)

    def test_unmute_restores_previous_state(self):
        self.xr.values["/bus/2/mix/on"] = 0  # bus 2 déjà coupé volontairement avant le mute
        self.mx.mute_state()
        self.assertTrue(self.mx.mute_state()["buses"]["partial"])
        self.mx.set_mute("buses", True)
        self.assertTrue(self.mx.mute_state()["buses"]["muted"])
        self.mx.set_mute("buses", False)
        self.assertTrue(self.wait(lambda: self.xr.values["/bus/1/mix/on"] == 1))
        self.assertEqual([self.xr.values[f"/bus/{b}/mix/on"] for b in (1, 2, 3)], [1, 0, 1])

    def test_main_mute_and_external_change(self):
        self.mx.mute_state()
        self.mx.set_mute("main", True)
        self.assertTrue(self.wait(lambda: self.xr.values["/lr/mix/on"] == 0))
        self.xr.push("/lr/mix/on", 1)  # rétabli depuis X Air Edit
        self.assertTrue(self.wait(lambda: not self.mx.mute_state()["main"]["muted"]))

    def test_unavailable_when_console_does_not_answer(self):
        del self.xr.values["/lr/mix/on"]
        st = self.mx.mute_state()
        self.assertFalse(st["main"]["available"])
        self.assertTrue(st["buses"]["available"])
        with self.assertRaises(MixError):
            self.mx.set_mute("main", True)
        with self.assertRaises(MixError):
            self.mx.set_mute("nope", True)

    def test_http_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = make_server(Recorder(make_cfg(tmp)), mixer=self.mx)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"

            def call(path, data=None):
                req = urllib.request.Request(base + path, method="POST" if data is not None else "GET",
                                             data=json.dumps(data).encode() if data is not None else None)
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status, json.loads(r.read())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read())
            try:
                self.assertFalse(call("/api/mute")[1]["buses"]["muted"])
                code, st = call("/api/mute", {"target": "buses", "muted": True})
                self.assertEqual((code, st["buses"]["muted"]), (200, True))
                self.assertEqual(call("/api/mute", {"target": "zzz", "muted": True})[0], 400)
                code, st = call("/api/mute", {"target": "buses", "muted": False})
                self.assertFalse(st["buses"]["muted"])
            finally:
                srv.shutdown()
                srv.server_close()

    def test_api_without_mixer(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = make_server(Recorder(make_cfg(tmp)))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/mute", timeout=5)
                self.assertEqual(cm.exception.code, 503)
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
