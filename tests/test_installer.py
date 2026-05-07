"""End-to-end test for install.sh against a sandboxed HOME."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "install.sh"


def run_installer(home: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    return subprocess.run(
        ["bash", str(INSTALLER)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccsb-install-")
        self.home = Path(self.tmp)
        self.dest_dir = self.home / ".claude"
        self.dest_script = self.dest_dir / "ccstatusbar.py"
        self.settings = self.dest_dir / "settings.json"
        self.backup = self.dest_dir / "settings.json.bak"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_installer_is_executable(self):
        self.assertTrue(os.access(INSTALLER, os.X_OK))

    def test_fresh_install_creates_script_and_settings(self):
        result = run_installer(self.home)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertTrue(self.dest_script.exists())
        self.assertTrue(os.access(self.dest_script, os.X_OK))
        self.assertTrue(self.settings.exists())
        with open(self.settings) as f:
            data = json.load(f)
        self.assertEqual(data["statusLine"]["type"], "command")
        self.assertEqual(data["statusLine"]["command"], str(self.dest_script))
        # No backup made when there was no prior settings file.
        self.assertFalse(self.backup.exists())

    def test_install_preserves_existing_keys_and_writes_backup(self):
        self.dest_dir.mkdir(parents=True)
        original = {"theme": "dark", "statusLine": {"type": "static", "value": "hello"}}
        with open(self.settings, "w") as f:
            json.dump(original, f)

        result = run_installer(self.home)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

        with open(self.settings) as f:
            data = json.load(f)
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["statusLine"]["type"], "command")
        self.assertEqual(data["statusLine"]["command"], str(self.dest_script))

        self.assertTrue(self.backup.exists())
        with open(self.backup) as f:
            backed_up = json.load(f)
        self.assertEqual(backed_up, original)

    def test_install_handles_corrupt_settings_via_backup(self):
        self.dest_dir.mkdir(parents=True)
        corrupt = "{ this is not json"
        with open(self.settings, "w") as f:
            f.write(corrupt)

        result = run_installer(self.home)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

        with open(self.settings) as f:
            data = json.load(f)
        self.assertEqual(data["statusLine"]["command"], str(self.dest_script))
        # Backup retains the original corrupt content verbatim.
        with open(self.backup) as f:
            self.assertEqual(f.read(), corrupt)


if __name__ == "__main__":
    unittest.main()
