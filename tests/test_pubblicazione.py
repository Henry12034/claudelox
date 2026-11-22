"""Il repository e' pubblico: nessun file con i dati di una casa deve entrarci, nemmeno con una Pull Request."""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = re.compile(
    r"(^|/)(\.env|token\.json|client\.json|accesso\.json|CLAUDE\.local\.md|permissions\.bin|LoxAPP3\.json|def\.log"
    r"|id_(rsa|ed25519)(\.pub)?)$"
    r"|^(\.state|data)/"
    r"|\.(loxone|loxcc|loxplan|pem|key|p12|pfx|npz)$"
    r"|(^|/)sps_[^/]*\.zip$", re.I)


def is_private(path: str) -> bool:
    return bool(FORBIDDEN.search(path))


class PublicFilesTest(unittest.TestCase):
    def test_the_filter_recognizes_private_files(self):
        for path in (".env", "data/programmi/x.zip", ".state/token.json", "CLAUDE.local.md", "a/casa.Loxone",
                     "sps_0283_20261122094830.zip", "b/LoxAPP3.json", "c/permissions.bin", "id_ed25519"):
            self.assertTrue(is_private(path), path)
        for path in (".env.example", "CLAUDE.local.example.md", "claudelox/config.py", "tests/test_offline.py",
                     "integrazioni/govee-bluetooth/loxone-govee.env.example"):
            self.assertFalse(is_private(path), path)

    @unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "serve una copia git del progetto")
    def test_no_private_file_is_tracked(self):
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True).stdout
        self.assertEqual([p for p in out.splitlines() if is_private(p)], [])


if __name__ == "__main__":
    unittest.main()
