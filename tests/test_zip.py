import io
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from test_recorder import make_cfg  # noqa: E402
from xr18rec.server import make_server  # noqa: E402
from xr18rec.session import Recorder  # noqa: E402
from xr18rec.ziputil import ZipStream  # noqa: E402


def make_files(d):
    data = {"a.wav": os.urandom(3_000_000 + 17), "vide.bin": b"", "notes é.txt": "précis".encode()}
    for name, content in data.items():
        with open(os.path.join(d, name), "wb") as f:
            f.write(content)
    return data


class ZipTests(unittest.TestCase):
    def test_roundtrip_and_exact_size(self):
        with tempfile.TemporaryDirectory() as d:
            data = make_files(d)
            archive = ZipStream([(os.path.join(d, n), f"sess/{n}") for n in sorted(data)])
            buf = io.BytesIO()
            written = archive.write_to(buf)
            self.assertEqual((written, len(buf.getvalue())), (archive.size, archive.size))  # taille annoncée exacte
            with zipfile.ZipFile(buf) as z:
                self.assertIsNone(z.testzip())  # CRC vérifiés
                self.assertEqual(sorted(z.namelist()), sorted(f"sess/{n}" for n in data))
                for n, content in data.items():
                    self.assertEqual(z.read(f"sess/{n}"), content)
                    self.assertEqual(z.getinfo(f"sess/{n}").CRC, zlib.crc32(content))

    def test_empty_archive(self):
        buf = io.BytesIO()
        ZipStream([]).write_to(buf)
        self.assertEqual(len(zipfile.ZipFile(buf).namelist()), 0)

    def test_http_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(make_cfg(tmp))
            session = os.path.join(rec.cfg.output_dir, "20260101-000000-test")
            os.makedirs(session)
            data = make_files(session)
            srv = make_server(rec)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            try:
                with urllib.request.urlopen(base + "/zip/20260101-000000-test", timeout=10) as r:
                    body = r.read()
                    self.assertEqual(r.headers["Content-Type"], "application/zip")
                    self.assertEqual(int(r.headers["Content-Length"]), len(body))
                    self.assertIn("20260101-000000-test.zip", r.headers["Content-Disposition"])
                with zipfile.ZipFile(io.BytesIO(body)) as z:
                    self.assertIsNone(z.testzip())
                    self.assertEqual(z.read("20260101-000000-test/a.wav"), data["a.wav"])
                for bad, code in (("/zip/inconnue", 404), ("/zip/..", 400), ("/zip/", 400)):
                    with self.assertRaises(urllib.error.HTTPError) as cm:
                        urllib.request.urlopen(base + bad, timeout=5)
                    self.assertEqual(cm.exception.code, code, bad)
                # refusé pendant un enregistrement
                rec.start("x")
                time.sleep(0.3)
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(base + "/zip/20260101-000000-test", timeout=5)
                self.assertEqual(cm.exception.code, 409)
                rec.stop()
            finally:
                rec.shutdown()
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
