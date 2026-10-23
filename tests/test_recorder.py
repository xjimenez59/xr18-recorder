import json
import os
import struct
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from xr18rec import config as config_mod  # noqa: E402
from xr18rec import wavtools  # noqa: E402
from xr18rec.server import make_server  # noqa: E402
from xr18rec.session import Recorder, RecorderError, sanitize_name  # noqa: E402

FAKE = os.path.join(HERE, "fake_arecord.py")


def make_cfg(tmp, **kw):
    # arecord_bin pointe sur un petit script qui relance le faux arecord avec le même Python
    wrapper = os.path.join(tmp, "arecord")
    with open(wrapper, "w") as f:
        f.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    os.chmod(wrapper, 0o755)
    cfg = config_mod.Config(output_dir=os.path.join(tmp, "rec"), arecord_bin=wrapper,
                            segment_seconds=1, min_free_gb=0.0, port=0, host="127.0.0.1",
                            state_file=os.path.join(tmp, "state.json"),
                            settings_file=os.path.join(tmp, "settings.json"))
    cfg.validate()
    return cfg


class ConfigTests(unittest.TestCase):
    def test_defaults_valid(self):
        cfg = config_mod.Config()
        cfg.validate()
        self.assertEqual(cfg.bytes_per_second, 18 * 48000 * 4)

    def test_segment_too_long(self):
        with self.assertRaises(ValueError):
            config_mod.Config(segment_seconds=1300).validate()  # 4,2 Gio
        config_mod.Config(segment_seconds=1100).validate()

    def test_bad_values(self):
        for kw in ({"format": "FLOAT"}, {"rate": 96000}, {"channels": 0}):
            with self.assertRaises(ValueError):
                config_mod.Config(**kw).validate()

    def test_load_rejects_unknown_keys(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            json.dump({"chanels": 18}, open(p, "w"))
            with self.assertRaises(ValueError):
                config_mod.load(p)
            json.dump({"channels": 8, "port": 9000}, open(p, "w"))
            self.assertEqual(config_mod.load(p).channels, 8)


class WavTests(unittest.TestCase):
    def _write_wav(self, path, ch, width, frames, finalize=True):
        data = bytearray()
        for n in range(frames):
            for c in range(ch):
                val = (c + 1) * 1000 + n  # valeur identifiable par voie et par trame
                data += val.to_bytes(width, "little", signed=True)
        with open(path, "wb") as f:
            size = len(data) if finalize else 0
            block = ch * width
            f.write(b"RIFF" + struct.pack("<I", 36 + size) + b"WAVE" + b"fmt "
                    + struct.pack("<IHHIIHH", 16, 1, ch, 48000, 48000 * block, block, width * 8)
                    + b"data" + struct.pack("<I", size))
            f.write(data)

    def test_info_and_unfinalized(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.wav")
            self._write_wav(p, 4, 4, 100, finalize=False)
            i = wavtools.read_info(p)
            self.assertEqual((i.channels, i.rate, i.bits, i.frames), (4, 48000, 32, 100))

    def test_split_32_and_24(self):
        for width in (4, 3):
            with tempfile.TemporaryDirectory() as d:
                a, b = os.path.join(d, "a.wav"), os.path.join(d, "b.wav")
                self._write_wav(a, 3, width, 10)
                self._write_wav(b, 3, width, 5)
                out = wavtools.split_channels([a, b], os.path.join(d, "out"))
                self.assertEqual(len(out), 3)
                for c, p in enumerate(out):
                    i = wavtools.read_info(p)
                    self.assertEqual((i.channels, i.frames), (1, 15))
                    raw = open(p, "rb").read()[i.data_offset:]
                    vals = [int.from_bytes(raw[k * width:(k + 1) * width], "little", signed=True)
                            for k in range(15)]
                    expected = [(c + 1) * 1000 + n for n in range(10)] + [(c + 1) * 1000 + n for n in range(5)]
                    self.assertEqual(vals, expected)

    def test_mismatch_and_garbage(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a.wav"), os.path.join(d, "b.wav")
            self._write_wav(a, 2, 4, 3)
            self._write_wav(b, 3, 4, 3)
            with self.assertRaises(wavtools.WavError):
                wavtools.split_channels([a, b], os.path.join(d, "o"))
            open(os.path.join(d, "x.wav"), "wb").write(b"not a wav at all")
            with self.assertRaises(wavtools.WavError):
                wavtools.read_info(os.path.join(d, "x.wav"))


def start_fake_xr18(names, colors, drop_first=False):
    """Faux XR18 : répond aux requêtes OSC /ch/NN/config/{name,color} et /lr/config/*."""
    import socket
    import threading
    from xr18rec import xr18osc
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.2)
    stop = threading.Event()
    dropped = set()

    def run():
        while not stop.is_set():
            try:
                data, peer = sock.recvfrom(1024)
            except socket.timeout:
                continue
            addr, _ = xr18osc.decode(data)
            if drop_first and addr not in dropped:
                dropped.add(addr)  # simule la perte du premier paquet de chaque requête
                continue
            parts = addr.split("/")  # ['', 'ch', '01', 'config', 'name']
            key = "lr" if parts[1] == "lr" else int(parts[2])
            if parts[-1] == "name":
                sock.sendto(xr18osc.encode(addr, names.get(key, "")), peer)
            else:
                sock.sendto(xr18osc.encode(addr, colors.get(key, 0)), peer)
        sock.close()

    threading.Thread(target=run, daemon=True).start()
    return sock.getsockname()[1], stop


class OscReaperTests(unittest.TestCase):
    NAMES = {1: "Kick", 2: "Snare", 16: "Voix", "lr": "Mix"}
    COLORS = {1: 1, 2: 4, 16: 10, "lr": 7}  # RD, BL, GN inversé, WH

    def test_osc_roundtrip(self):
        from xr18rec import xr18osc
        self.assertEqual(xr18osc.decode(xr18osc.encode("/ch/01/config/name", "Kick")),
                         ("/ch/01/config/name", ["Kick"]))
        self.assertEqual(xr18osc.decode(xr18osc.encode("/lr/config/color", 5)), ("/lr/config/color", [5]))

    def test_fetch_layout_with_packet_loss(self):
        from xr18rec import xr18osc
        port, stop = start_fake_xr18(self.NAMES, self.COLORS, drop_first=True)
        try:
            lay = xr18osc.fetch_layout("127.0.0.1", port, timeout=3)
        finally:
            stop.set()
        self.assertEqual([lay.channels[0].name, lay.channels[1].name, lay.channels[15].name],
                         ["Kick", "Snare", "Voix"])
        self.assertEqual(xr18osc.color_name(lay.channels[15].color), "GN")
        self.assertEqual((lay.main.name, lay.main.color, lay.missing), ("Mix", 7, []))

    def test_fetch_no_answer(self):
        from xr18rec import xr18osc
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        try:
            with self.assertRaises(xr18osc.OscError):
                xr18osc.fetch_layout("127.0.0.1", s.getsockname()[1], timeout=0.6)
        finally:
            s.close()

    def test_build_project(self):
        from xr18rec import reaper, xr18osc
        lay = xr18osc.Layout()
        lay.channels[0] = xr18osc.Strip('Ki"ck', 1)
        lay.main = xr18osc.Strip("Mix", 7)
        with tempfile.TemporaryDirectory() as d:
            for n in ("take.wav", "take-01.wav", "take-02.wav"):
                WavTests()._write_wav(os.path.join(d, n), 18, 4, 48000 // 10)
            self.assertEqual(reaper.find_segments(d), ["take.wav", "take-01.wav", "take-02.wav"])
            path = reaper.write_project(d, lay, name="proj")
            text = open(path, encoding="utf-8").read()
        self.assertEqual(text.count("<TRACK"), 17)  # 16 entrées + mix
        self.assertEqual(text.count("<ITEM"), 17 * 3)
        self.assertIn('NAME "Ki\'ck"', text)
        self.assertIn('NAME "Ch 02"', text)  # nom vide -> repli
        self.assertIn("PEAKCOL %d" % (0x1000000 | 229 | (57 << 8) | (53 << 16)), text)
        self.assertIn('NAME "Mix"', text)
        self.assertIn("CHANMODE 83", text)  # stéréo voies 17/18
        self.assertIn("SAMPLERATE 48000", text)
        self.assertIn("POSITION 0.200000", text)  # 3e segment après 2 × 0,1 s
        self.assertEqual(text.count("<"), text.count(">"))

    def test_session_writes_project(self):
        port, stop = start_fake_xr18(self.NAMES, self.COLORS)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = make_cfg(tmp)
                cfg.xr18_host, cfg.xr18_osc_port = "127.0.0.1", port
                rec = Recorder(cfg)
                name = rec.start("rpp")
                time.sleep(1.5)
                st = rec.stop()
                d = os.path.join(cfg.output_dir, name)
                self.assertTrue(os.path.exists(os.path.join(d, "channels.json")))
                self.assertIn(name + ".rpp", [f["name"] for f in st["files"]])
                text = open(os.path.join(d, name + ".rpp"), encoding="utf-8").read()
                self.assertIn('NAME "Kick"', text)
        finally:
            stop.set()


class RecorderTests(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(sanitize_name("Concert / Été 1!"), "Concert_t_1")
        self.assertEqual(sanitize_name("../.."), "")
        self.assertEqual(sanitize_name(None), "")

    def test_start_stop_segments_and_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(make_cfg(tmp))
            name = rec.start("test")
            self.assertTrue(name.endswith("-test"))
            with self.assertRaises(RecorderError):
                rec.start()
            time.sleep(2.6)
            st = rec.status()
            self.assertEqual(st["state"], "recording")
            self.assertGreaterEqual(len(st["files"]), 2)
            st = rec.stop()
            self.assertEqual(st["state"], "idle")
            self.assertIsNone(st["error"])
            names = [f["name"] for f in st["files"] if f["name"].endswith(".wav")]
            self.assertIn("take-01.wav", names)
            d = os.path.join(rec.cfg.output_dir, name)
            for n in names:  # en-têtes finalisés : taille déclarée = taille réelle
                p = os.path.join(d, n)
                info = wavtools.read_info(p)
                self.assertEqual(info.channels, 18)
                self.assertEqual(info.data_size, os.path.getsize(p) - info.data_offset)
            with self.assertRaises(RecorderError):
                rec.stop()

    def test_overruns_counted(self):
        os.environ["FAKE_ARECORD_OVERRUN"] = "1"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                rec = Recorder(make_cfg(tmp))
                rec.start()
                time.sleep(1.2)
                st = rec.stop()
                self.assertGreaterEqual(st["overruns"], 2)
        finally:
            del os.environ["FAKE_ARECORD_OVERRUN"]

    def test_refuses_when_disk_low(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            cfg.min_free_gb = 10**9
            with self.assertRaises(RecorderError):
                Recorder(cfg).start()

    def test_missing_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            cfg.arecord_bin = os.path.join(tmp, "nope")
            with self.assertRaises(RecorderError):
                Recorder(cfg).start()

    def test_crash_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            bad = os.path.join(tmp, "arecord_bad")
            open(bad, "w").write("#!/bin/sh\necho 'audio open error: No such device' >&2\nexit 1\n")
            os.chmod(bad, 0o755)
            cfg.arecord_bin = bad
            rec = Recorder(cfg)
            rec.start()
            time.sleep(0.5)
            st = rec.status()
            self.assertEqual(st["state"], "error")
            self.assertIn("No such device", st["error"])


class ServerTests(unittest.TestCase):
    def test_api(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(make_cfg(tmp))
            srv = make_server(rec)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"

            def call(path, data=None):
                req = urllib.request.Request(base + path, method="POST" if data is not None else "GET",
                                             data=json.dumps(data).encode() if data is not None else None)
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status, r.read()
                except urllib.error.HTTPError as e:
                    return e.code, e.read()

            try:
                code, body = call("/")
                self.assertEqual(code, 200)
                self.assertIn(b"Enregistreur XR18", body)
                self.assertIn(b'id="recbtn"', call("/")[1])  # commandes sur l'accueil
                code, body = call("/rec")
                self.assertEqual(code, 200)
                self.assertIn(b'id="sessions"', body)
                self.assertNotIn(b'id="recbtn"', body)
                self.assertEqual(call("/favicon.ico")[0], 204)
                self.assertEqual(call("/static/app.css")[0], 200)
                self.assertEqual(call("/static/app.js")[0], 200)
                self.assertEqual(call("/static/../server.py")[0], 404)
                self.assertEqual(call("/static/nope.css")[0], 404)
                self.assertEqual(json.loads(call("/api/status")[1])["state"], "idle")
                code, body = call("/api/start", {"name": "srv"})
                self.assertEqual(code, 200)
                session = json.loads(body)["session"]
                self.assertEqual(call("/api/start", {})[0], 409)
                time.sleep(1.3)
                code, body = call("/api/stop", {})
                self.assertEqual(code, 200)
                self.assertEqual(call("/api/stop", {})[0], 409)
                sessions = json.loads(call("/api/sessions")[1])
                self.assertEqual(sessions[0]["name"], session)
                fname = next(f["name"] for f in sessions[0]["files"] if f["name"].endswith(".wav"))
                code, body = call(f"/files/{session}/{fname}")
                self.assertEqual(code, 200)
                self.assertTrue(body.startswith(b"RIFF"))
                self.assertEqual(call(f"/files/{session}/..%2F..%2Fetc")[0], 400)
                self.assertEqual(call("/files/../../etc/passwd")[0], 400)
                self.assertEqual(call("/nope")[0], 404)
            finally:
                rec.shutdown()
                srv.shutdown()
                srv.server_close()


class FakeXR18Mixer:
    """Faux XR18 pour le mix : mémorise les valeurs, répond aux requêtes, pousse aux abonnés /xremote."""

    def __init__(self):
        import socket
        import threading
        self.values = {}
        for n in range(1, 17):
            self.values[f"/ch/{n:02d}/config/name"] = f"Src{n}" if n != 3 else ""
            self.values[f"/ch/{n:02d}/config/color"] = 1
            for b in range(1, 7):
                self.values[f"/ch/{n:02d}/mix/{b:02d}/level"] = 0.5
        self.values["/bus/2/config/name"] = "Chant"
        self.values["/lr/mix/on"] = 1
        for b in range(1, 7):
            self.values[f"/bus/{b}/mix/on"] = 1
        for b in range(1, 7):
            self.values[f"/bus/{b}/mix/fader"] = 0.6
        self.sets = []
        self.subscribers = set()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        from xr18rec import xr18osc
        while not self._stop.is_set():
            try:
                data, peer = self.sock.recvfrom(1024)
            except OSError:
                continue
            for addr, args in xr18osc.decode_packet(data):
                if addr == "/xremote":
                    self.subscribers.add(peer)
                elif addr == "/xinfo":
                    self.sock.sendto(xr18osc.encode("/xinfo", "192.168.1.50", "XR18-test", "XR18", "1.18"), peer)
                elif args:
                    self.values[addr] = args[0]
                    self.sets.append((addr, args[0]))
                elif addr in self.values:
                    self.sock.sendto(xr18osc.encode(addr, self.values[addr]), peer)

    def push(self, addr, value):
        from xr18rec import xr18osc
        self.values[addr] = value
        for peer in self.subscribers:
            self.sock.sendto(xr18osc.encode(addr, value), peer)

    def close(self):
        self._stop.set()


class QrTests(unittest.TestCase):
    def test_structure(self):
        from xr18rec import qr
        m = qr.make_matrix("http://xr18rec.local:8080/mix")
        self.assertEqual(len(m), 29)  # version 3
        self.assertTrue(all(m[0][i] for i in range(7)))  # bandeau haut du repère en haut à gauche
        self.assertTrue(m[len(m) - 8][8])  # module sombre fixe
        with self.assertRaises(ValueError):
            qr.make_matrix("x" * 300)
        self.assertTrue(qr.to_svg("http://a/mix").startswith("<svg"))


class MixerTests(unittest.TestCase):
    def setUp(self):
        from xr18rec.mixer import MixerClient
        self.xr = FakeXR18Mixer()
        self.mx = MixerClient("127.0.0.1", self.xr.port, max_db=-6.0, buses=[1, 2, 3])

    def tearDown(self):
        self.mx.close()
        self.xr.close()

    def test_fader_curve(self):
        from xr18rec.mixer import db_to_fader, fader_to_db
        self.assertAlmostEqual(fader_to_db(0.75), 0.0)
        self.assertAlmostEqual(db_to_fader(0.0), 0.75)
        for db in (-80, -45, -20, -3, 6):
            self.assertAlmostEqual(fader_to_db(db_to_fader(db)), db, places=6)
        self.assertEqual(db_to_fader(-90), 0.0)

    def test_get_bus(self):
        d = self.mx.get_bus(2)
        self.assertEqual((d["name"], len(d["sources"])), ("Chant", 17))  # master + 16 ; aux/FX absents : masqués
        self.assertEqual((d["sources"][0]["id"], d["sources"][0]["fader"]), ("master", 0.6))
        self.assertEqual(d["sources"][1]["name"], "Src1")
        self.assertEqual(d["sources"][3]["name"], "Ch 03")  # nom vide -> repli
        self.assertEqual(d["sources"][1]["color"], "#e53935")
        self.assertEqual(self.mx.bus_list()[1], {"bus": 2, "name": "Chant"})

    def test_set_clamped_and_restricted(self):
        from xr18rec.mixer import MixError, fader_to_db
        self.mx.get_bus(1)
        applied = self.mx.set_send(1, "ch05", 6.0)  # au-dessus du plafond de -6 dB
        self.assertAlmostEqual(applied, -6.0, places=1)
        time.sleep(0.3)
        addr, val = self.xr.sets[-1]
        self.assertEqual(addr, "/ch/05/mix/01/level")
        self.assertAlmostEqual(fader_to_db(val), -6.0, places=1)
        for bad in (lambda: self.mx.set_send(4, "ch01", -10),  # bus non autorisé
                    lambda: self.mx.set_send(1, "nope", -10),
                    lambda: self.mx.set_send(1, "aux", -10),  # source absente
                    lambda: self.mx.set_send(1, "ch01", float("nan"))):
            with self.assertRaises(MixError):
                bad()

    def test_master_clamped(self):
        from xr18rec.mixer import fader_to_db
        self.mx.get_bus(1)
        self.assertAlmostEqual(self.mx.set_send(1, "master", 5.0), -6.0, places=1)
        time.sleep(0.3)
        addr, val = self.xr.sets[-1]
        self.assertEqual(addr, "/bus/1/mix/fader")
        self.assertAlmostEqual(fader_to_db(val), -6.0, places=1)

    def test_master_hidden_when_unsupported(self):
        for b in range(1, 7):
            del self.xr.values[f"/bus/{b}/mix/fader"]
        d = self.mx.get_bus(3)
        self.assertEqual((len(d["sources"]), d["sources"][0]["id"]), (16, "ch01"))

    def test_external_change_is_seen(self):
        self.mx.get_bus(1)
        v0 = self.mx.version
        self.xr.push("/ch/02/mix/01/level", 0.25)
        for _ in range(20):
            if self.mx.version != v0:
                break
            time.sleep(0.1)
        self.assertEqual(self.mx.get_bus(1)["sources"][2]["fader"], 0.25)

    def test_http_api(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(make_cfg(tmp))
            srv = make_server(rec, mixer=self.mx)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"

            def call(path, data=None):
                req = urllib.request.Request(base + path, method="POST" if data is not None else "GET",
                                             data=json.dumps(data).encode() if data is not None else None)
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status, json.loads(r.read() or b"{}")
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read() or b"{}")
            try:
                self.assertEqual(len(call("/api/mix/buses")[1]), 3)
                code, d = call("/api/mix/1")
                self.assertEqual((code, len(d["sources"])), (200, 17))
                self.assertEqual(call("/api/mix/1/send", {"source": "ch01", "db": -20})[0], 200)
                self.assertEqual(call("/api/mix/1/send", {"source": "zzz", "db": -20})[0], 400)
                self.assertEqual(call("/api/mix/6")[0], 400)
            finally:
                srv.shutdown()
                srv.server_close()

    def test_qr_endpoint(self):
        import socket
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            srv = make_server(Recorder(make_cfg(tmp)))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            port = srv.server_address[1]
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/qr/mix.svg",
                                             headers={"Host": "xr18rec.local:8080"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    self.assertEqual(r.headers["Content-Type"], "image/svg+xml")
                    self.assertTrue(r.read().startswith(b"<svg"))
                s = socket.create_connection(("127.0.0.1", port))
                CRLF = bytes([13, 10])
                s.sendall(b"GET /qr/mix.svg HTTP/1.1" + CRLF + b"Host: bad host" + CRLF + b"Connection: close" + CRLF + CRLF)
                self.assertIn(b" 400 ", s.recv(200))
                s.close()
            finally:
                srv.shutdown()
                srv.server_close()

    def test_no_mixer_configured(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            srv = make_server(Recorder(make_cfg(tmp)))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/mix/1", timeout=5)
                self.assertEqual(cm.exception.code, 503)
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
