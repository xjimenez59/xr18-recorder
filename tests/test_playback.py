import json
import os
import random
import struct
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
from xr18rec import config as config_mod  # noqa: E402
from xr18rec import playback, xr18osc  # noqa: E402
from xr18rec.server import make_server  # noqa: E402
from xr18rec.session import Recorder  # noqa: E402

RATE = 8000


def left_val(n):
    return (n * 37) % 60000 - 30000


def right_val(n):
    return (n * 91 + 5) % 60000 - 30000


def sample(val16, width):
    """Échantillon dont les 16 bits de poids fort valent val16 ; le reste est du « bruit » (doit être ignoré)."""
    if width == 4:
        n = (val16 << 16) | 0x1234
    elif width == 3:
        n = (val16 << 8) | 0x5A
    else:
        n = val16
    return n.to_bytes(width, "little", signed=True)


def write_segment(path, first, count, channels=18, width=4, finalize=True, mix=(16, 17)):
    data = bytearray()
    for n in range(first, first + count):
        for c in range(channels):
            if c == mix[0]:
                data += sample(left_val(n), width)
            elif c == mix[1]:
                data += sample(right_val(n), width)
            else:
                data += sample((n * 13 + c * 977) % 65536 - 32768, width)  # autres voies : ne doivent jamais passer
    block = channels * width
    size = len(data) if finalize else 0
    head = (b"RIFF" + struct.pack("<I", 36 + size) + b"WAVE" + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, channels, RATE, RATE * block, block, width * 8)
            + b"data" + struct.pack("<I", size))
    with open(path, "wb") as f:
        f.write(head + data)


def expected_audio(total, left=left_val, right=right_val):
    return b"".join(struct.pack("<hh", left(n), right(n)) for n in range(total))


def make_session(directory, segments=(100, 150), **kw):
    os.makedirs(directory, exist_ok=True)
    names = ["take.wav"] + [f"take-{i:02d}.wav" for i in range(1, len(segments))]
    first = 0
    for i, (name, count) in enumerate(zip(names, segments)):
        write_segment(os.path.join(directory, name), first, count,
                      finalize=kw.get("finalize_last", True) or i < len(segments) - 1,
                      **{k: v for k, v in kw.items() if k != "finalize_last"})
        first += count
    return sum(segments)


class MixSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "s")

    def tearDown(self):
        self.tmp.cleanup()

    def full(self, src):
        return b"".join(src.iter_range(0, src.size - 1))

    def test_header_and_size(self):
        total = make_session(self.dir)
        src = playback.MixSource(self.dir)
        self.assertEqual((src.total_frames, src.size, src.rate), (total, 44 + total * 4, RATE))
        h = src.header()
        self.assertEqual((h[:4], h[8:12], h[12:16], h[36:40]), (b"RIFF", b"WAVE", b"fmt ", b"data"))
        self.assertEqual(struct.unpack("<IHHIIHH", h[16:36]), (16, 1, 2, RATE, RATE * 4, 4, 16))
        self.assertEqual(struct.unpack("<I", h[40:44])[0], total * 4)
        self.assertAlmostEqual(src.duration_s, total / RATE)

    def test_content_across_segments(self):
        total = make_session(self.dir)
        src = playback.MixSource(self.dir)
        self.assertEqual(self.full(src), src.header() + expected_audio(total))

    def test_arbitrary_byte_ranges(self):
        total = make_session(self.dir, segments=(100, 150, 90))
        src = playback.MixSource(self.dir)
        whole = src.header() + expected_audio(total)
        rnd = random.Random(7)
        cases = [(0, 0), (0, 43), (43, 44), (40, 60), (src.size - 1, src.size - 1), (src.size - 5, src.size - 1)]
        cases += [tuple(sorted((rnd.randrange(src.size), rnd.randrange(src.size)))) for _ in range(150)]
        cases += [(44 + 4 * 99 - 1, 44 + 4 * 101 + 2)]  # à cheval sur la jonction des deux premiers segments
        for a, b in cases:
            self.assertEqual(b"".join(src.iter_range(a, b)), whole[a:b + 1], (a, b))

    def test_other_widths(self):
        for width in (2, 3):
            d = os.path.join(self.tmp.name, f"w{width}")
            total = make_session(d, width=width)
            src = playback.MixSource(d)
            self.assertEqual(self.full(src), src.header() + expected_audio(total), width)

    def test_24_in_32_container_uses_recorded_format(self):
        d = os.path.join(self.tmp.name, "s24le")
        os.makedirs(d)
        # S24_LE : 24 bits dans un conteneur de 32 ; les 16 bits de poids fort du signal sont aux octets 1 et 2
        data = b"".join(struct.pack("<i", (left_val(n) << 8) | 0x5A) + struct.pack("<i", (right_val(n) << 8) | 0x5A) for n in range(50))
        head = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, RATE, RATE * 8, 8, 32)
                + b"data" + struct.pack("<I", len(data)))
        open(os.path.join(d, "take.wav"), "wb").write(head + data)
        json.dump({"format": "S24_LE"}, open(os.path.join(d, "recording.json"), "w"))
        src = playback.MixSource(d, (1, 2))
        self.assertEqual(self.full(src)[44:], expected_audio(50))

    def test_unfinalized_last_segment(self):
        total = make_session(self.dir, finalize_last=False)  # arrêt brutal : en-tête non finalisé
        src = playback.MixSource(self.dir)
        self.assertEqual(self.full(src), src.header() + expected_audio(total))

    def test_fewer_channels_falls_back(self):
        d = os.path.join(self.tmp.name, "stereo")
        total = make_session(d, segments=(60,), channels=2, mix=(0, 1))
        src = playback.MixSource(d)  # 17/18 n'existent pas : on joue les deux premières voies
        self.assertTrue(src.fallback)
        self.assertEqual(self.full(src)[44:], expected_audio(total))

    def test_custom_mix_pair(self):
        d = os.path.join(self.tmp.name, "pair")
        total = make_session(d, segments=(40,), mix=(2, 5))
        self.assertEqual(self.full(playback.MixSource(d, (3, 6)))[44:], expected_audio(total))

    def test_errors(self):
        os.makedirs(self.dir)
        with self.assertRaises(playback.PlaybackError):
            playback.MixSource(self.dir)  # aucun fichier
        make_session(self.dir, segments=(10,))
        write_segment(os.path.join(self.dir, "take-01.wav"), 10, 10, channels=4)  # format différent
        with self.assertRaises(playback.PlaybackError):
            playback.MixSource(self.dir)

    def test_config_validation(self):
        for bad in ([17], [0, 2], [17, 99], "17,18", [1.5, 2]):
            with self.assertRaises(ValueError):
                config_mod.Config(play_mix=bad).validate()
        config_mod.Config(play_mix=[3, 4]).validate()


class HttpPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rec = Recorder(make_cfg(self.tmp.name))
        self.name = "20260101-000000-essai"
        self.dir = os.path.join(self.rec.cfg.output_dir, self.name)
        self.total = make_session(self.dir)
        layout = xr18osc.Layout()
        layout.main = xr18osc.Strip("Salle", 7)
        json.dump(layout.to_dict(), open(os.path.join(self.dir, "channels.json"), "w"))
        self.srv = make_server(self.rec)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.whole = playback.MixSource(self.dir).header() + expected_audio(self.total)

    def tearDown(self):
        self.rec.shutdown()
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def url(self):
        return f"/play/{self.name}/mix.wav"

    def test_full_download(self):
        code, h, body = self.get(self.url())
        self.assertEqual((code, h["Content-Type"], h["Accept-Ranges"], int(h["Content-Length"])), (200, "audio/wav", "bytes", len(self.whole)))
        self.assertEqual(body, self.whole)

    def test_ranges(self):
        code, h, body = self.get(self.url(), {"Range": "bytes=0-1"})  # sonde que certains navigateurs envoient d'abord
        self.assertEqual((code, h["Content-Range"], body), (206, f"bytes 0-1/{len(self.whole)}", self.whole[:2]))
        code, h, body = self.get(self.url(), {"Range": "bytes=1000-"})
        self.assertEqual((code, body, h["Content-Range"]), (206, self.whole[1000:], f"bytes 1000-{len(self.whole) - 1}/{len(self.whole)}"))
        code, _h, body = self.get(self.url(), {"Range": "bytes=-100"})  # les 100 derniers octets
        self.assertEqual((code, body), (206, self.whole[-100:]))
        code, _h, body = self.get(self.url(), {"Range": f"bytes=500-{len(self.whole) * 2}"})  # fin trop grande : tronquée
        self.assertEqual((code, body), (206, self.whole[500:]))

    def test_unsatisfiable_range(self):
        code, h, _b = self.get(self.url(), {"Range": f"bytes={len(self.whole)}-"})
        self.assertEqual((code, h["Content-Range"]), (416, f"bytes */{len(self.whole)}"))
        self.assertEqual(self.get(self.url(), {"Range": "bytes=50-10"})[0], 416)

    def test_metadata(self):
        code, _h, body = self.get(f"/api/play/{self.name}")
        meta = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual((meta["mix"], meta["mix_names"], meta["rate"], meta["segments"], meta["blocked"]),
                         ([17, 18], ["Salle L", "Salle R"], RATE, 2, False))
        self.assertAlmostEqual(meta["duration_s"], round(self.total / RATE, 1), places=1)
        self.assertEqual(meta["url"], self.url())

    def test_unknown_and_unsafe_sessions(self):
        for path in ("/play/inconnue/mix.wav", "/api/play/inconnue", "/play/../mix.wav", "/api/play/.."):
            self.assertIn(self.get(path)[0], (400, 404), path)

    def test_page_is_served(self):
        code, h, body = self.get("/play?s=" + self.name)
        self.assertEqual(code, 200)
        self.assertIn(b"<audio", body)
        code, h, body = self.get("/multi?s=" + self.name)
        self.assertEqual(code, 200)
        self.assertIn(b"AudioContext", body)

    def test_blocked_while_recording(self):
        self.rec.start("prise")
        time.sleep(0.4)
        try:
            code, _h, body = self.get(self.url())
            self.assertEqual(code, 409)
            self.assertIn("enregistrement", json.loads(body)["error"])
            self.assertTrue(json.loads(self.get(f"/api/play/{self.name}")[2])["blocked"])
        finally:
            self.rec.stop()
        self.assertEqual(self.get(self.url(), {"Range": "bytes=0-3"})[0], 206)  # de nouveau disponible

    def pcm(self, ch, start=0, dur=1):
        return self.get(f"/api/play/{self.name}/pcm?ch={ch}&start={start}&dur={dur}")

    def test_pcm_channels(self):
        code, h, body = self.pcm("17,18")
        self.assertEqual((code, int(h["X-Frames"]), int(h["X-Rate"])), (200, self.total, RATE))
        n = self.total * 2
        left = b"".join(struct.pack("<h", left_val(i)) for i in range(self.total))
        right = b"".join(struct.pack("<h", right_val(i)) for i in range(self.total))
        self.assertEqual((body[:n], body[n:]), (left, right))
        code, h, body = self.pcm("18", start=0.0125, dur=0.0125)  # 100 trames, à cheval sur la jonction des segments
        self.assertEqual((code, int(h["X-Frames"])), (200, 100))
        self.assertEqual(body, b"".join(struct.pack("<h", right_val(i)) for i in range(100, 200)))

    def test_pcm_rejects_bad_requests(self):
        for q in ("", "0", "19", "1,1", "a", "1&dur=0", "1&dur=99", "1&start=-1"):
            self.assertEqual(self.get(f"/api/play/{self.name}/pcm?ch={q}")[0], 400, q)
        self.assertEqual(self.get("/api/play/inconnue/pcm?ch=1")[0], 404)

    def test_pcm_blocked_while_recording(self):
        self.rec.start("prise")
        time.sleep(0.4)
        try:
            self.assertEqual(self.pcm("1")[0], 409)
        finally:
            self.rec.stop()

    def test_tracks_silent_flag(self):
        tracks = json.loads(self.get(f"/api/play/{self.name}")[2])["tracks"]
        self.assertEqual(len(tracks), 18)
        self.assertEqual((tracks[0]["index"], tracks[17]["name"]), (1, "Salle R"))
        self.assertFalse(any(t["silent"] for t in tracks))  # pas de bilan : on ne cache rien
        json.dump({"skipped_s": 0, "channels": [{"channel": 1, "peak_dbfs": None, "clip_samples": 0, "events": []},
                                                {"channel": 2, "peak_dbfs": -12.0, "clip_samples": 0, "events": []},
                                                {"channel": 3, "peak_dbfs": -42.1, "clip_samples": 0, "events": []}]},  # plancher de mesure
                  open(os.path.join(self.dir, "levels.json"), "w"))
        tracks = json.loads(self.get(f"/api/play/{self.name}")[2])["tracks"]
        self.assertEqual([t["silent"] for t in tracks[:4]], [True, False, True, False])  # voie 3 : bruit (-42,1 dB) ; voie 4 : absente du bilan
        json.dump({"skipped_s": 4, "channels": [{"channel": 1, "peak_dbfs": None}]},
                  open(os.path.join(self.dir, "levels.json"), "w"))
        tracks = json.loads(self.get(f"/api/play/{self.name}")[2])["tracks"]
        self.assertFalse(tracks[0]["silent"])  # analyse incomplète : peu fiable

    def test_client_abort_does_not_break_the_server(self):
        import socket
        host, port = "127.0.0.1", self.srv.server_address[1]
        s = socket.create_connection((host, port))
        s.sendall(f"GET {self.url()} HTTP/1.1\r\nHost: x\r\nRange: bytes=0-\r\n\r\n".encode())
        s.recv(100)
        s.close()  # le navigateur change de position : il coupe la connexion en cours de route
        time.sleep(0.3)
        self.assertEqual(self.get(self.url(), {"Range": "bytes=0-9"})[0], 206)


if __name__ == "__main__":
    unittest.main()
