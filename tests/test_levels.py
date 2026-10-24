import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from test_recorder import make_cfg  # noqa: E402
from xr18rec import meters, reaper, xr18osc  # noqa: E402
from xr18rec.server import make_server  # noqa: E402
from xr18rec.session import Recorder  # noqa: E402

RATE = 1000  # fréquence factice : 1 trame = 1 ms, les instants se calculent à la main


def pcm(values_per_frame, width):
    """values_per_frame : liste de trames, chaque trame = liste de réels -1..1 par voie."""
    top = (1 << (8 * width - 1)) - 1
    out = bytearray()
    for frame in values_per_frame:
        for v in frame:
            n = max(-top - 1, min(top, int(round(v * top))))
            out += n.to_bytes(width, "little", signed=True)
    return bytes(out)


def write_wav(path, channels, width, data, finalize=True):
    block = channels * width
    size = len(data) if finalize else 0
    head = (b"RIFF" + struct.pack("<I", 36 + size) + b"WAVE" + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, channels, RATE, RATE * block, block, width * 8)
            + b"data" + struct.pack("<I", size))
    with open(path, "wb") as f:
        f.write(head + data)


class WindowStatsTests(unittest.TestCase):
    def frames(self, ch1_values, ch0=0.5):
        return [[ch0, v] for v in ch1_values]

    def test_peak_and_clip_s32(self):
        buf = pcm(self.frames([0.1, 0.2, 1.0, 1.0, -1.0, 0.0]), 4)
        (p0, c0, f0), (p1, c1, f1) = meters.window_stats(buf, 2, 4)
        self.assertEqual((p0, c0, f0), (64, 0, -1))  # 0,5 de la pleine échelle, jamais saturée
        self.assertEqual((p1, c1, f1), (128, 3, 2))  # 3 échantillons saturés, le premier à la trame 2

    def test_widths(self):
        for width, msb in ((2, 1), (3, 2), (4, 3)):
            buf = pcm(self.frames([0.25, 0.25, 0.9999]), width)
            stats = meters.window_stats(buf, 2, width, msb)
            self.assertEqual(stats[0][0], 64, width)
            self.assertEqual(stats[1][0], 127 if width != 2 else 127, width)

    def test_s24_le_container_uses_byte_2(self):
        # S24_LE : 24 bits dans un conteneur de 32 ; l'octet 3 ne contient que le signe
        top = (1 << 23) - 1
        buf = b"".join(struct.pack("<i", v) for v in (top, -top - 1, 100))
        right = meters.window_stats(buf, 1, 4, meters.msb_offset("S24_LE", 4))[0]
        wrong = meters.window_stats(buf, 1, 4)[0]  # sans correction : seul le signe est lu
        self.assertEqual((right[0], right[1]), (128, 2))
        self.assertLess(wrong[0], 4)

    def test_dbfs(self):
        self.assertEqual(meters.dbfs(128), 0.0)
        self.assertEqual(meters.dbfs(64), -6.0)
        self.assertIsNone(meters.dbfs(0))
        self.assertEqual(meters.msb_offset("S32_LE", 4), 3)

    def test_empty_buffer(self):
        self.assertEqual(meters.window_stats(b"", 2, 4), [(0, 0, -1), (0, 0, -1)])


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.mon = meters.LevelMonitor(self.d, RATE, 2, 4, 3)

    def tearDown(self):
        self.tmp.cleanup()

    def seg(self, name, frames, finalize=True):
        write_wav(os.path.join(self.d, name), 2, 4, pcm(frames, 4), finalize)

    def test_nothing_yet(self):
        self.assertEqual(self.mon.step(), 0)
        self.assertFalse(self.mon.snapshot()["has_data"])

    def test_clip_time_across_segments(self):
        quiet = [[0.1, 0.1]] * 500
        self.seg("take-01.wav", quiet + [[0.1, 1.0]] * 3)  # voie 2 sature à la trame 500 (0,5 s)
        self.seg("take-02.wav", quiet, finalize=False)  # suite, en-tête non finalisé (arrêt brutal)
        self.assertEqual(self.mon.step(), 503 + 500)
        snap = self.mon.snapshot()
        ch = snap["channels"]
        self.assertTrue(ch[1]["clipped"])
        self.assertFalse(ch[0]["clipped"])
        self.assertEqual((ch[1]["events"], ch[1]["clip_samples"], ch[1]["take_peak_db"]), ([0.5], 3, meters.dbfs(127)))  # + pleine échelle : octet 127 -> -0,1 dB
        self.assertAlmostEqual(snap["elapsed_s"], 1.0, delta=0.01)

    def test_growing_file_is_read_incrementally(self):
        path = os.path.join(self.d, "take.wav")
        data = pcm([[0.1, 0.1]] * 100, 4)
        write_wav(path, 2, 4, data, finalize=False)
        self.assertEqual(self.mon.step(), 100)
        with open(path, "ab") as f:  # arecord ajoute des trames
            f.write(pcm([[0.1, 0.9999]] * 50, 4))
        self.assertEqual(self.mon.step(), 50)
        self.assertEqual(self.mon.step(), 0)  # rien de nouveau
        self.assertTrue(self.mon.snapshot()["channels"][1]["clipped"])
        self.assertEqual(self.mon.snapshot()["channels"][1]["events"], [0.1])

    def test_partial_frame_waits_for_the_rest(self):
        path = os.path.join(self.d, "take.wav")
        write_wav(path, 2, 4, pcm([[0.1, 0.1]] * 10, 4), finalize=False)
        with open(path, "ab") as f:
            f.write(b"\x00\x00\x00")  # trame coupée en plein milieu
        self.assertEqual(self.mon.step(), 10)

    def test_events_are_merged_within_a_second(self):
        loud, quiet = [[0.1, 1.0]], [[0.1, 0.1]]
        frames = quiet * 100 + loud * 2 + quiet * 400 + loud * 2 + quiet * 1700 + loud * 2  # 0,1 s ; 0,5 s ; 2,2 s
        self.seg("take.wav", frames)
        self.mon.step(max_frames=10000)
        self.assertEqual(self.mon.snapshot()["channels"][1]["events"], [0.1, 2.2])

    def test_falls_back_to_present_when_too_far_behind(self):
        self.seg("take.wav", [[0.1, 1.0]] * 100 + [[0.1, 0.1]] * 9900)  # 10 s d'audio, saturation à 0,1 s
        self.mon.step(max_frames=100000)
        snap = self.mon.snapshot()
        self.assertEqual(snap["skipped_s"], 7.0)  # 10 s - 3 s toléré : les 7 premières secondes sont sautées
        self.assertEqual(snap["elapsed_s"], 10.0)
        self.assertFalse(snap["channels"][1]["clipped"])  # la saturation était dans l'audio sauté
        self.assertEqual(self.mon.report()["skipped_s"], 7.0)

    def test_no_skip_when_keeping_up(self):
        self.seg("take.wav", [[0.1, 0.1]] * 2500)
        self.mon.step()
        self.assertEqual(self.mon.snapshot()["skipped_s"], 0.0)

    def test_reset_display_keeps_take_record(self):
        self.seg("take.wav", [[0.1, 1.0]] * 5 + [[0.1, 0.1]] * 400)
        self.mon.step()
        self.mon.reset_display()
        ch = self.mon.snapshot()["channels"][1]
        self.assertLess(ch["peak_db"], -15)  # crête affichée repartie du niveau actuel (0,1 = -18 dB)
        self.assertEqual(ch["take_peak_db"], meters.dbfs(127))
        self.assertTrue(ch["clipped"])

    def test_save_report_and_reaper_loader(self):
        self.seg("take.wav", [[0.1, 1.0]] * 3)
        self.mon.step()
        self.mon.save()
        rep = reaper.load_levels(self.d)
        self.assertEqual((rep["rate"], rep["channels"][1]["channel"], rep["channels"][1]["clip_samples"]), (RATE, 2, 3))
        self.assertEqual(rep["channels"][0]["events"], [])
        self.assertGreaterEqual(rep["load_pct"], 0)  # charge de calcul mesurée, relevée dans le bilan de la prise
        self.assertIsInstance(self.mon.snapshot()["load_pct"], float)
        self.assertFalse(os.path.exists(os.path.join(self.d, "levels.json.tmp")))

    def test_run_stops_and_drains(self):
        self.seg("take.wav", [[0.1, 1.0]] * 3 + [[0.1, 0.1]] * 10)
        alive = {"v": True}
        t = threading.Thread(target=self.mon.run, args=(lambda: alive["v"], 0.02))
        t.start()
        time.sleep(0.2)
        alive["v"] = False
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertTrue(os.path.exists(os.path.join(self.d, "levels.json")))


class ReaperMarkersTests(unittest.TestCase):
    def build(self, with_levels):
        with tempfile.TemporaryDirectory() as d:
            write_wav(os.path.join(d, "take.wav"), 18, 4, pcm([[0.1] * 18] * 10, 4))
            if with_levels:
                levels = {"channels": [{"channel": 1, "events": [12.5, 80.0]}, {"channel": 17, "events": [30.0]},
                                       {"channel": 99, "events": [1.0]}]}
                json.dump(levels, open(os.path.join(d, "levels.json"), "w"))
            layout = xr18osc.Layout()
            layout.channels[0] = xr18osc.Strip("Kick", 1)
            return reaper.build_project(d, layout)

    def test_markers(self):
        text = self.build(True)
        self.assertEqual(text.count("  MARKER "), 3)  # la voie 99 n'existe pas : ignorée
        self.assertIn('MARKER 1 12.500000 "SAT Kick" 0 0 1 R {', text)
        self.assertIn('"SAT Mix L"', text)
        self.assertEqual(text.count("<"), text.count(">"))

    def test_no_markers_without_levels(self):
        self.assertNotIn("MARKER", self.build(False))


class RecorderLevelsTests(unittest.TestCase):
    def setUp(self):
        os.environ["FAKE_ARECORD_SIGNAL"] = "1"

    def tearDown(self):
        os.environ.pop("FAKE_ARECORD_SIGNAL", None)

    def test_levels_during_and_after_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(make_cfg(tmp))
            self.assertFalse(rec.levels()["has_data"])
            name = rec.start("niveaux")
            time.sleep(2.6)  # le faux arecord sature la voie 3 entre 1,0 et 1,5 s
            live = rec.levels()
            self.assertTrue(live["active"] and live["has_data"])
            self.assertEqual(len(live["channels"]), 18)
            self.assertTrue(live["channels"][2]["clipped"])
            self.assertFalse(live["channels"][0]["clipped"])
            self.assertEqual(live["names"][2], "Ch 03")
            self.assertEqual([c["channel"] for c in rec.status()["levels"]["clipped"]], [3])
            rec.stop()
            d = os.path.join(rec.cfg.output_dir, name)
            report = json.load(open(os.path.join(d, "levels.json")))
            self.assertGreater(report["channels"][2]["clip_samples"], 0)
            self.assertEqual(report["channels"][0]["clip_samples"], 0)
            project = open(os.path.join(d, name + ".rpp"), encoding="utf-8").read()
            self.assertIn('"SAT Ch 03"', project)
            self.assertFalse(rec.levels()["active"])  # les résultats restent consultables après l'arrêt

    def test_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(make_cfg(tmp))
            srv = make_server(rec)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"

            def call(path, data=None):
                req = urllib.request.Request(base + path, method="POST" if data is not None else "GET",
                                             data=json.dumps(data).encode() if data is not None else None)
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.loads(r.read())
            try:
                self.assertFalse(call("/api/levels")["has_data"])
                rec.start("api")
                time.sleep(2.2)
                data = call("/api/levels")
                self.assertTrue(data["channels"][2]["clipped"])
                after = call("/api/levels/reset", {})
                self.assertTrue(after["channels"][2]["clipped"])  # le bilan de la prise ne se remet pas à zéro
                self.assertEqual(len(call("/api/status")["levels"]["clipped"]), 1)
                rec.stop()
            finally:
                rec.shutdown()
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
