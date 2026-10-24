import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from xr18rec import growfs  # noqa: E402

GIB = 1024**3 // 512  # secteurs par Gio


def make_sys(tmp, disk_gib=119, start=1064960, part_sectors=3_870_544):
    """Faux /sys : disque mmcblk0 avec p1 (démarrage) et p2 (racine)."""
    dev = os.path.join(tmp, "sys", "devices", "mmcblk0")
    for name, st, size, num in (("mmcblk0p1", 16384, 1048576, 1), ("mmcblk0p2", start, part_sectors, 2)):
        os.makedirs(os.path.join(dev, name))
        for fname, val in (("start", st), ("size", size), ("partition", num)):
            open(os.path.join(dev, name, fname), "w").write(str(val))
    open(os.path.join(dev, "size"), "w").write(str(disk_gib * GIB))
    os.makedirs(os.path.join(tmp, "sys", "class", "block"))
    os.symlink(os.path.join(dev, "mmcblk0p2"), os.path.join(tmp, "sys", "class", "block", "mmcblk0p2"))
    return os.path.join(tmp, "sys")


class NeedsTests(unittest.TestCase):
    def test_nothing_to_do_when_full(self):
        disk = 119 * GIB
        start = 1064960
        size = disk - start  # partition jusqu'au bout
        self.assertEqual(growfs.needs(disk, start, size, (size * 512) - 1024**2, False), (False, False))

    def test_partition_not_grown(self):
        self.assertEqual(growfs.needs(119 * GIB, 1064960, 3_870_544, 1_900_000_000, False), (True, True))

    def test_filesystem_half_grown(self):
        disk, start = 119 * GIB, 1064960
        size = disk - start
        self.assertEqual(growfs.needs(disk, start, size, int(6.7 * 1024**3), False), (False, True))  # votre cas

    def test_other_partition_behind_is_left_alone(self):
        self.assertEqual(growfs.needs(119 * GIB, 1064960, 3_870_544, 1_900_000_000, True), (False, True))

    def test_small_gap_is_ignored(self):
        start, size = 1064960, 3_870_544
        self.assertEqual(growfs.needs(start + size + 1000, start, size, size * 512, False), (False, False))


class RunTests(unittest.TestCase):
    def run_with(self, sysdir, fstype="ext4", fs_gib=6.7, grow_rc=0):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[0] == "findmnt":
                return subprocess.CompletedProcess(cmd, 0, f"/dev/mmcblk0p2 {fstype}\n", "")
            if cmd[0] == "dumpe2fs":
                blocks = int(fs_gib * 1024**3 / 4096)
                return subprocess.CompletedProcess(cmd, 0, f"Block count:              {blocks}\nBlock size:               4096\n", "")
            if cmd[0] == "growpart":
                return subprocess.CompletedProcess(cmd, grow_rc, "CHANGED", "")
            return subprocess.CompletedProcess(cmd, 0, "Resizing done", "")

        old_sys, old_run = growfs.SYS, growfs.RUN
        growfs.SYS, growfs.RUN = sysdir, fake_run
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = growfs.run()
        finally:
            growfs.SYS, growfs.RUN = old_sys, old_run
        return rc, [c[0] for c in calls]

    def test_completes_half_grown_filesystem(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysdir = make_sys(tmp, part_sectors=119 * GIB - 1064960)  # partition déjà complète
            rc, cmds = self.run_with(sysdir, fs_gib=6.7)
            self.assertEqual((rc, cmds), (0, ["findmnt", "dumpe2fs", "resize2fs"]))  # pas de growpart

    def test_grows_partition_then_filesystem(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysdir = make_sys(tmp)  # partition de 1,85 Gio sur une carte de 119
            rc, cmds = self.run_with(sysdir, fs_gib=1.85)
            self.assertEqual((rc, cmds), (0, ["findmnt", "dumpe2fs", "growpart", "resize2fs"]))

    def test_growpart_failure_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, cmds = self.run_with(make_sys(tmp), fs_gib=1.85, grow_rc=2)
            self.assertEqual((rc, cmds[-1]), (1, "growpart"))

    def test_nothing_when_already_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysdir = make_sys(tmp, part_sectors=119 * GIB - 1064960)
            rc, cmds = self.run_with(sysdir, fs_gib=119 - 1064960 * 512 / 1024**3 - 0.001)
            self.assertEqual((rc, cmds), (0, ["findmnt", "dumpe2fs"]))

    def test_non_ext4_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, cmds = self.run_with(make_sys(tmp), fstype="btrfs")
            self.assertEqual((rc, cmds), (0, ["findmnt"]))


if __name__ == "__main__":
    unittest.main()
