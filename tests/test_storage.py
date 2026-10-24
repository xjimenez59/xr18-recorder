import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from test_recorder import make_cfg  # noqa: E402
from xr18rec.session import Recorder, RecorderError  # noqa: E402


def fake_system(tmp):
    """Faux /proc/mounts : racine sur la SD + un disque USB (nom avec espace) + /boot."""
    usb = os.path.join(tmp, "usb disk")
    os.makedirs(usb)
    mounts = os.path.join(tmp, "mounts")
    escaped = usb.replace(" ", "\\040")
    with open(mounts, "w") as f:
        f.write(f"/dev/mmcblk0p2 / ext4 rw 0 0\n/dev/sda1 {escaped} ext4 rw 0 0\n/dev/mmcblk0p1 /boot vfat rw 0 0\n")
    labels = os.path.join(tmp, "labels")
    os.makedirs(labels)
    os.symlink("/dev/sda1", os.path.join(labels, "SSD\\x20ROCK"))  # étiquette « SSD ROCK » encodée par udev
    return usb, mounts, labels


def patched(mounts, labels):
    return mock.patch.multiple("xr18rec.storage", MOUNTS_FILE=mounts, LABEL_DIR=labels)


class StorageTests(unittest.TestCase):
    def test_volumes_and_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            usb, mounts, labels = fake_system(tmp)
            with patched(mounts, labels):
                rec = Recorder(make_cfg(tmp))
                info = rec.storage_info()
                self.assertEqual([(o["kind"], o["active"]) for o in info["options"]],
                                 [("sd", True), ("usb", False)])
                usb_opt = info["options"][1]
                self.assertEqual((usb_opt["label"], usb_opt["path"]),
                                 ("SSD ROCK", os.path.join(usb, "xr18-recordings")))
                with self.assertRaises(RecorderError):
                    rec.set_storage("/etc")  # emplacement non proposé
                rec.set_storage(usb_opt["path"])
                name = rec.start("usb")
                self.assertTrue(os.path.isdir(os.path.join(usb, "xr18-recordings", name)))
                with self.assertRaises(RecorderError):
                    rec.set_storage(info["options"][0]["path"])  # pendant l'enregistrement
                for _ in range(50):  # attend l'apparition d'au moins un segment
                    if rec.status()["files"]:
                        break
                    time.sleep(0.1)
                rec.stop()
                sessions = rec.sessions()
                self.assertEqual((sessions[0]["name"], sessions[0]["location"]), (name, "SSD ROCK"))
                self.assertTrue(rec.resolve_file(name, sessions[0]["files"][0]["name"]))
                self.assertIsNone(rec.resolve_file(name, "absent.wav"))
                # le choix est mémorisé
                self.assertEqual(Recorder(make_cfg(tmp)).storage_info()["active"], usb_opt["path"])

    def test_remaining_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(make_cfg(tmp))  # min_free_gb = 0 dans les tests : tout l'espace libre est utilisable
            bps = rec.cfg.bytes_per_second
            self.assertEqual(rec._remaining_s(1.0), int(1024**3 / bps))  # 1 Gio à 3,5 Mo/s ≈ 5 min
            self.assertEqual(rec._remaining_s(0.0), 0)
            rec.cfg.min_free_gb = 2.0  # la prise s'arrête sous 1 Gio : 3 Gio libres = 2 Gio utilisables
            self.assertEqual(rec._remaining_s(3.0), int(2 * 1024**3 / bps))
            self.assertEqual(rec._remaining_s(0.5), 0)
            status = rec.status()
            self.assertIn("remaining_s", status)
            self.assertGreater(status["remaining_s"], 0)
            self.assertTrue(all("remaining_s" in o for o in rec.storage_info()["options"]))

    def test_usb_unplugged(self):
        with tempfile.TemporaryDirectory() as tmp:
            usb, mounts, labels = fake_system(tmp)
            with patched(mounts, labels):
                rec = Recorder(make_cfg(tmp))
                rec.set_storage(rec.storage_info()["options"][1]["path"])
                with open(mounts, "w") as f:
                    f.write("/dev/mmcblk0p2 / ext4 rw 0 0\n")  # disque débranché
                with self.assertRaises(RecorderError):
                    rec.start()
                self.assertIn("absent", rec.storage_info()["notice"])
                fresh = Recorder(make_cfg(tmp))  # au redémarrage : retour à la SD avec avertissement
                self.assertEqual(fresh.storage_info()["active"], fresh.cfg.output_dir)
                self.assertIn("absent", fresh.storage_info()["notice"])

    def test_no_proc_mounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("xr18rec.storage.MOUNTS_FILE", os.path.join(tmp, "none")):
                self.assertEqual(len(Recorder(make_cfg(tmp)).storage_info()["options"]), 1)


if __name__ == "__main__":
    unittest.main()
