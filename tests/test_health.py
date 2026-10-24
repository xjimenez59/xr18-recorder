import json
import os
import subprocess
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
from xr18rec import health  # noqa: E402
from xr18rec.server import make_server  # noqa: E402
from xr18rec.session import Recorder  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def fake_pi(root, temp="52300", stat=(100, 800), load="0.50 0.40 0.30 1/100 123", mem=(1000000, 600000)):
    write(f"{root}/proc/stat", f"cpu  {stat[0]} 0 0 {stat[1]} 0 0 0 0\ncpu0 1 1 1 1 0 0 0 0\n")
    write(f"{root}/proc/loadavg", load + "\n")
    write(f"{root}/proc/meminfo", f"MemTotal:  {mem[0]} kB\nMemFree: 1 kB\nMemAvailable:  {mem[1]} kB\n")
    write(f"{root}/sys/class/thermal/thermal_zone0/temp", temp + "\n")


class PatchedHealth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.saved = (health.PROC, health.SYS, health.RUN)
        health.PROC, health.SYS = f"{self.root}/proc", f"{self.root}/sys"
        self.throttled = "throttled=0x0"
        self.calls = 0

        def fake_run(cmd, **kw):
            self.calls += 1
            if cmd[0] == "vcgencmd":
                return subprocess.CompletedProcess(cmd, 0, self.throttled_now() + "\n", "")
            raise FileNotFoundError(cmd[0])

        health.RUN = fake_run
        fake_pi(self.root)

    def throttled_now(self):
        return self.throttled

    def tearDown(self):
        health.PROC, health.SYS, health.RUN = self.saved
        self.tmp.cleanup()


class HealthReadingTests(PatchedHealth):
    def test_snapshot_values(self):
        h = health.Health()
        h.snapshot(max_age=0)  # première lecture : pas de différence de temps processeur possible
        fake_pi(self.root, stat=(200, 1600))  # 800 actifs sur 1700 -> mesure sur l'intervalle
        snap = h.snapshot(max_age=0)
        self.assertEqual((snap["load1"], snap["temp_c"], snap["mem_used_pct"], snap["mem_total_mb"]), (0.5, 52.3, 40.0, 976))
        self.assertAlmostEqual(snap["cpu_pct"], 11.1, delta=0.2)  # 100 unités actives sur 900 entre les deux relevés

    def test_cache_avoids_rereading(self):
        h = health.Health()
        first = h.snapshot()
        calls = self.calls
        self.assertIs(h.snapshot(), first)
        self.assertEqual(self.calls, calls)

    def test_power_bits(self):
        self.throttled = "throttled=0x50005"  # bits 0, 2, 16, 18
        pw = health.power()
        self.assertEqual(pw["source"], "vcgencmd")
        self.assertTrue(all(pw[k] for k in ("undervoltage_now", "throttled_now", "undervoltage_seen", "throttled_seen")))
        self.assertFalse(pw["freq_capped_now"] or pw["temp_limit_now"])

    def test_power_fallback_hwmon_and_absent(self):
        health.RUN = lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError("vcgencmd"))
        self.assertIsNone(health.power())
        write(f"{self.root}/sys/class/hwmon/hwmon2/name", "rpi_volt\n")
        write(f"{self.root}/sys/class/hwmon/hwmon2/in0_lcrit_alarm", "1\n")
        pw = health.power()
        self.assertEqual((pw["source"], pw["undervoltage_now"]), ("hwmon", True))
        write(f"{self.root}/sys/class/hwmon/hwmon2/in0_lcrit_alarm", "0\n")
        self.assertFalse(health.power()["undervoltage_now"])

    def test_missing_files_do_not_crash(self):
        health.PROC = health.SYS = f"{self.root}/inexistant"
        snap = health.Health().snapshot(max_age=0)
        self.assertIsNone(snap["temp_c"])
        self.assertIsNone(snap["mem_used_pct"])


class AssessTests(unittest.TestCase):
    def base(self, **kw):
        snap = {"cpu_pct": 10, "load1": 0.3, "cpus": 4, "temp_c": 50, "mem_used_pct": 40, "power": None}
        snap.update(kw)
        return snap

    def codes(self, snap):
        return {(a["code"], a["level"]) for a in health.assess(snap)}

    def test_healthy(self):
        self.assertEqual(health.assess(self.base()), [])

    def test_power(self):
        self.assertEqual(self.codes(self.base(power={"undervoltage_now": True})), {("undervoltage", "error")})
        self.assertEqual(self.codes(self.base(power={"undervoltage_seen": True})), {("undervoltage_seen", "warn")})
        self.assertEqual(self.codes(self.base(power={"throttled_now": True})), {("throttled", "warn")})

    def test_temperature_load_memory(self):
        self.assertEqual(self.codes(self.base(temp_c=77)), {("temp", "warn")})
        self.assertEqual(self.codes(self.base(temp_c=83)), {("temp", "error")})
        self.assertEqual(self.codes(self.base(load1=3.8)), {("load", "warn")})
        self.assertEqual(self.codes(self.base(mem_used_pct=95)), {("memory", "warn")})
        self.assertEqual(self.codes(self.base(temp_c=None, mem_used_pct=None)), set())

    def test_fmt_time(self):
        self.assertEqual((health.fmt_time(75), health.fmt_time(3725)), ("1:15", "1:02:05"))


class RecorderHealthTests(PatchedHealth):
    def setUp(self):
        super().setUp()
        self.rec = Recorder(make_cfg(self.root))

    def tearDown(self):
        self.rec.shutdown()
        os.environ.pop("FAKE_ARECORD_OVERRUN", None)
        super().tearDown()

    def codes(self):
        return {a["code"]: a for a in self.rec.status()["alerts"]}

    def test_overruns_are_alerted_and_saved(self):
        os.environ["FAKE_ARECORD_OVERRUN"] = "1"
        name = self.rec.start("depassements")
        time.sleep(1.6)
        alerts = self.codes()
        self.assertEqual(alerts["overruns"]["level"], "error")
        self.assertIn("dépassement", alerts["overruns"]["message"])
        self.assertIn("audio a été perdu", alerts["overruns"]["message"])
        self.rec.stop()
        self.assertIn("overruns", self.codes())  # l'alerte reste affichée après l'arrêt (preuve de la prise)
        info = json.load(open(os.path.join(self.rec.cfg.output_dir, name, "recording.json"), encoding="utf-8"))
        self.assertGreaterEqual(info["overruns"]["count"], 2)
        self.assertEqual(len(info["overruns"]["at_s"]), min(info["overruns"]["count"], 50))
        self.assertGreater(info["duration_s"], 1.0)
        self.assertEqual((info["state"], info["rate"], info["channels"]), ("ok", 48000, 18))
        self.assertGreaterEqual(info["health"]["samples"], 1)
        self.assertEqual(info["health"]["max_temp_c"], 52.3)
        self.assertTrue(info["stderr_tail"])
        # la prise suivante repart propre
        os.environ.pop("FAKE_ARECORD_OVERRUN")
        self.rec.start("propre")
        time.sleep(0.5)
        self.assertNotIn("overruns", self.codes())
        self.rec.stop()

    def test_clean_take_has_no_alert_and_a_report(self):
        name = self.rec.start("propre")
        time.sleep(1.0)
        self.assertEqual(self.rec.status()["alerts"], [])
        self.rec.stop()
        info = json.load(open(os.path.join(self.rec.cfg.output_dir, name, "recording.json"), encoding="utf-8"))
        self.assertEqual(info["overruns"], {"count": 0, "at_s": []})

    def test_undervoltage_during_take(self):
        seen = {"v": False}
        self.throttled_now = lambda: "throttled=0x10000" if seen["v"] else "throttled=0x0"  # bit 16 : « déjà arrivée »
        name = self.rec.start("tension")
        time.sleep(0.5)
        seen["v"] = True  # la sous-tension se produit pendant la prise, puis disparaît
        health_wait = time.monotonic() + 6
        while "take_undervoltage" not in self.codes() and time.monotonic() < health_wait:
            time.sleep(0.2)
        alerts = self.codes()
        self.assertEqual(alerts["take_undervoltage"]["level"], "error")
        self.assertNotIn("undervoltage_seen", alerts)  # pas de doublon avec l'état actuel du Pi
        self.rec.stop()
        info = json.load(open(os.path.join(self.rec.cfg.output_dir, name, "recording.json"), encoding="utf-8"))
        self.assertTrue(info["health"]["undervoltage"])

    def test_low_space_warns_during_recording(self):
        self.rec.free_gb = lambda: 0.3  # 0,3 Gio : environ 1,5 min à 3,5 Mo/s (min_free_gb vaut 0 dans les tests)
        self.rec.start("espace")
        time.sleep(0.4)
        self.assertEqual(self.codes()["space"]["level"], "error")
        self.rec.stop()
        self.assertNotIn("space", self.codes())  # l'alerte d'espace ne concerne que la prise en cours

    def test_live_pi_conditions_show_when_idle(self):
        self.throttled = "throttled=0x1"  # sous-tension en ce moment
        fake_pi(self.root, temp="84000")
        self.rec._health = health.Health()
        codes = self.codes()
        self.assertEqual((codes["undervoltage"]["level"], codes["temp"]["level"]), ("error", "error"))

    def test_status_carries_the_running_version(self):
        self.assertTrue(self.rec.status()["version"])  # affichée sur l'accueil et dans les paramètres

    def test_api_health(self):
        srv = make_server(self.rec)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/health", timeout=10) as r:
                data = json.loads(r.read())
            self.assertEqual((data["live"]["temp_c"], data["recording"], data["alerts"]), (52.3, False, []))
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
