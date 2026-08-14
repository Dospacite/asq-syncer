from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from master_list_creation.store import VersionStore
from master_list_creation.updater import run_update
from test_store import anime, snapshot


class UpdaterTest(unittest.TestCase):
    def test_unchanged_source_skips_builder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed = snapshot(root / "seed.json", "source-1", {1: anime(1, "Alpha")})
            store = VersionStore(root / "data")
            store.initialize(seed)
            raw = {
                "masterListId": "source-1",
                "animeMap": {},
                "songMap": {},
                "artistMap": {},
                "groupMap": {},
            }
            with patch(
                "master_list_creation.updater.HttpClient.get_json", return_value=raw
            ), patch("master_list_creation.updater.builder_main") as builder:
                version, message = run_update(store)

            self.assertEqual(version, 1)
            self.assertIn("unchanged", message)
            builder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
