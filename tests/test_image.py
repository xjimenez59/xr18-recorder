import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from xr18rec import automount, bootconfig  # noqa: E402
from xr18rec import config as config_mod  # noqa: E402


class BootConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.boot = os.path.join(self.tmp.name, "boot")
        os.makedirs(self.boot)
        self.dest = os.path.join(self.tmp.name, "etc-xr18rec.json")
        with open(self.dest, "w") as f:
            f.write('{"port": 8080}')

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text, encoding="utf-8"):
        with open(os.path.join(self.boot, "xr18rec.json"), "w", encoding=encoding) as f:
            f.write(text)

    def test_absent_leaves_config_alone(self):
        self.assertEqual(bootconfig.sync(self.boot, self.dest), "absent")
        self.assertEqual(json.load(open(self.dest))["port"], 8080)

    def test_valid_file_is_copied(self):
        self.write('{"port": 80, "xr18_host": "192.168.1.50"}')
        self.assertEqual(bootconfig.sync(self.boot, self.dest), "copié")
        self.assertEqual(config_mod.load(self.dest).xr18_host, "192.168.1.50")
        self.assertFalse(os.path.exists(os.path.join(self.boot, "xr18rec-erreur.txt")))
        self.assertFalse(os.path.exists(self.dest + ".tmp"))

    def test_invalid_keeps_previous_and_reports(self):
        for bad in ('{"port": 80,}', '{"prot": 80}', '{"rate": 96000}', "pas du json"):
            self.write(bad)
            self.assertEqual(bootconfig.sync(self.boot, self.dest), "invalide", bad)
            self.assertEqual(json.load(open(self.dest))["port"], 8080)  # inchangé
            report = open(os.path.join(self.boot, "xr18rec-erreur.txt"), encoding="utf-8").read()
            self.assertIn("pas été pris en compte", report)

    def test_error_report_removed_once_fixed(self):
        self.write("cassé")
        bootconfig.sync(self.boot, self.dest)
        self.assertTrue(os.path.exists(os.path.join(self.boot, "xr18rec-erreur.txt")))
        self.write('{"port": 81}')
        self.assertEqual(bootconfig.sync(self.boot, self.dest), "copié")
        self.assertFalse(os.path.exists(os.path.join(self.boot, "xr18rec-erreur.txt")))

    def test_windows_bom_and_crlf(self):
        self.write('{\r\n  "port": 80\r\n}\r\n', encoding="utf-8-sig")  # Bloc-notes : BOM + fins de ligne Windows
        self.assertEqual(bootconfig.sync(self.boot, self.dest), "copié")
        self.assertEqual(config_mod.load(self.dest).port, 80)


class AutomountTests(unittest.TestCase):
    def plan(self, **props):
        return automount.plan("/dev/sda1", props, 995, 995, root="/media/xr18")

    def test_parse_blkid(self):
        out = "DEVNAME=/dev/sda1\nLABEL=SSD ROCK\nUUID=ab-12\nTYPE=ext4\n"
        self.assertEqual(automount.parse_blkid(out)["LABEL"], "SSD ROCK")

    def test_linux_fs(self):
        for fs in ("ext4", "xfs", "btrfs"):
            p = self.plan(TYPE=fs, LABEL="recordings")
            self.assertEqual((p.mountpoint, p.fstype, p.own_dir), ("/media/xr18/recordings", fs, True))

    def test_windows_fs_get_owner_options(self):
        v = self.plan(TYPE="vfat", LABEL="CLE")
        self.assertIn("uid=995,gid=995", v.options)
        self.assertFalse(v.own_dir)
        self.assertEqual(self.plan(TYPE="exfat", LABEL="X").fstype, "exfat")
        self.assertEqual(self.plan(TYPE="ntfs", LABEL="X").fstype, "ntfs-3g")

    def test_name_is_sanitized(self):
        self.assertEqual(self.plan(TYPE="ext4", LABEL="Mon disque/été").mountpoint, "/media/xr18/Mon_disque_t")
        self.assertEqual(self.plan(TYPE="ext4", LABEL="../..").mountpoint, "/media/xr18/sda1")  # pas d'échappement
        self.assertEqual(self.plan(TYPE="ext4", UUID="1234-ABCD").mountpoint, "/media/xr18/1234-ABCD")
        self.assertEqual(self.plan(TYPE="ext4").mountpoint, "/media/xr18/sda1")

    def test_not_mountable(self):
        for props in ({}, {"TYPE": "swap"}, {"TYPE": "crypto_LUKS"}, {"TYPE": "zfs_member"}):
            self.assertIsNone(self.plan(**props), props)


if __name__ == "__main__":
    unittest.main()
