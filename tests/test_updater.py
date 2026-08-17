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

    def test_asset_refresh_rebuilds_unchanged_source(self) -> None:
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

            def build(arguments: list[str]) -> int:
                # Stand in for the builder by writing a list whose records differ
                # from the seed only in their cover art.
                output = Path(arguments[arguments.index("--output") + 1])
                record = anime(1, "Alpha")
                record["annImage"] = "https://example.com/large.jpg"
                snapshot(output, "source-1", {1: record})
                return 0

            with patch(
                "master_list_creation.updater.HttpClient.get_json", return_value=raw
            ), patch(
                "master_list_creation.updater.builder_main", side_effect=build
            ) as builder:
                version, message = run_update(store, refresh_assets=True)

            self.assertEqual(version, 2)
            self.assertIn("--refresh", builder.call_args.args[0])
            self.assertEqual(store.latest()["source_version"], "source-1")
            self.assertIn("version 2", message)

    def test_asset_refresh_without_changes_keeps_version(self) -> None:
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

            def build(arguments: list[str]) -> int:
                output = Path(arguments[arguments.index("--output") + 1])
                snapshot(output, "source-1", {1: anime(1, "Alpha")})
                return 0

            with patch(
                "master_list_creation.updater.HttpClient.get_json", return_value=raw
            ), patch("master_list_creation.updater.builder_main", side_effect=build):
                version, message = run_update(store, refresh_assets=True)

            self.assertEqual(version, 1)
            self.assertIn("no changes", message)
            self.assertEqual(len(store.versions()), 1)


if __name__ == "__main__":
    unittest.main()
