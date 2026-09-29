"""Keep the two archive profiles on the same sealed generic provider."""

from __future__ import annotations

import hashlib
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ArchiveProviderSyncTests(unittest.TestCase):
    def test_archive_profiles_share_the_same_provider_release(self):
        # Both archive profiles use the same generic collector. Their source
        # configuration and checkpoints remain separate; intelligence adapters
        # are different providers and deliberately outside this contract.
        primary = ROOT / "sealed/provider-overlay.py.age"
        secondary = ROOT / "sealed/archive-sources/source-b/provider-overlay.py.age"
        for path in (primary, secondary):
            self.assertTrue(path.is_file(), f"Missing sealed archive provider: {path.relative_to(ROOT)}")
        primary_bytes = primary.read_bytes()
        secondary_bytes = secondary.read_bytes()
        for payload in (primary_bytes, secondary_bytes):
            self.assertTrue(payload.startswith(b"age-encryption.org/v1\n"), "Archive provider must remain sealed")
        self.assertEqual(
            hashlib.sha256(primary_bytes).hexdigest(),
            hashlib.sha256(secondary_bytes).hexdigest(),
            "Archive provider upgrade must update both sealed profiles together",
        )


if __name__ == "__main__":
    unittest.main()
