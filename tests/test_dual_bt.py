"""End-to-end tests against a fake Windows hive and a fake /var/lib/bluetooth.

Run:  python -m unittest discover tests
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "..", "src", "dual_bt.py")
FIXTURE = os.path.join(HERE, "fixtures", "SYSTEM")
CTRL = "AA:BB:CC:00:00:01"
CLASSIC = {
    "11:22:33:44:55:01": "AABBCCDDEEFF00112233445566778899",
    "11:22:33:44:55:02": "CAFEBABECAFEBABECAFEBABECAFEBABE",
    "11:22:33:44:55:03": "11111111222222223333333344444444",
}
BLE = "11:22:33:44:55:04"
OLD_KEY = "0123456789ABCDEF0123456789ABCDEF"


class DualBtTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cfg = os.path.join(self.tmp, "win", "Windows", "System32", "config")
        os.makedirs(cfg)
        shutil.copy(FIXTURE, os.path.join(cfg, "SYSTEM"))
        self.bluez = os.path.join(self.tmp, "bluez")
        for mac in CLASSIC:
            d = os.path.join(self.bluez, CTRL, mac)
            os.makedirs(d)
            with open(os.path.join(d, "info"), "w") as f:
                f.write(f"[General]\nName=dev\nTrusted=true\n\n[LinkKey]\nKey={OLD_KEY}\nType=4\nPINLength=0\n")
            os.chmod(os.path.join(d, "info"), 0o600)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_tool(self, *extra):
        cmd = [sys.executable, TOOL, "--windows-root", os.path.join(self.tmp, "win"),
               "--bluez-root", self.bluez, "--work-dir", os.path.join(self.tmp, "work"), *extra]
        r = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def info(self, mac):
        with open(os.path.join(self.bluez, CTRL, mac, "info")) as f:
            return f.read()

    def test_dry_run_changes_nothing(self):
        out = self.run_tool()
        self.assertIn("Dry run only", out)
        for mac in CLASSIC:
            self.assertIn(f"Key={OLD_KEY}", self.info(mac))
        self.assertFalse(os.path.exists(os.path.join(self.bluez, CTRL, BLE)))

    def test_apply_classic(self):
        self.run_tool("--apply", "--yes", "--no-service")
        for mac, key in CLASSIC.items():
            txt = self.info(mac)
            self.assertIn(f"Key={key}", txt)
            self.assertIn("Type=4", txt)  # other lines untouched
            self.assertEqual(os.stat(os.path.join(self.bluez, CTRL, mac, "info")).st_mode & 0o777, 0o600)

    def test_apply_ble(self):
        self.run_tool("--apply", "--yes", "--no-service")
        txt = self.info(BLE)
        self.assertIn("Name=Test Gamepad", txt)
        self.assertIn("AddressType=static", txt)
        self.assertIn("[IdentityResolvingKey]\nKey=AFAEADACABAAA9A8A7A6A5A4A3A2A1A0", txt)  # reversed
        self.assertIn("[LongTermKey]\nKey=00112233445566778899AABBCCDDEEFF", txt)
        self.assertIn("EDiv=4660", txt)
        self.assertIn("Rand=1234605616436508552", txt)
        self.assertIn("EncSize=16", txt)

    def test_backup_and_idempotent(self):
        self.run_tool("--apply", "--yes", "--no-service")
        backups = [f for f in os.listdir(os.path.join(self.tmp, "work")) if f.startswith("bluez-backup-")]
        self.assertEqual(len(backups), 1)
        self.assertIn("Nothing to change.", self.run_tool())

    def test_windows_files_untouched(self):
        cfg = os.path.join(self.tmp, "win", "Windows", "System32", "config")

        def snapshot():
            out = {}
            for n in os.listdir(cfg):
                with open(os.path.join(cfg, n), "rb") as f:
                    out[n] = f.read()
            return out

        before = snapshot()
        self.run_tool("--apply", "--yes", "--no-service")
        self.assertEqual(before, snapshot())

    def test_bad_logs_fall_back(self):
        cfg = os.path.join(self.tmp, "win", "Windows", "System32", "config")
        with open(os.path.join(cfg, "SYSTEM.LOG1"), "wb") as f:
            f.write(os.urandom(4096))
        out = self.run_tool()
        self.assertIn("failed", out)
        self.assertIn("4 device(s)", out)


if __name__ == "__main__":
    unittest.main()
