from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from master_list_creation.http_client import write_json
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

    def test_previously_published_source_keeps_latest_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed = snapshot(root / "seed.json", "source-1", {1: anime(1, "Alpha")})
            store = VersionStore(root / "data")
            store.initialize(seed)
            newer = snapshot(root / "source-2.json", "source-2", {1: anime(1, "Alpha 2")})
            store.commit_snapshot(newer)
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

            self.assertEqual(version, 2)
            self.assertIn("already published as version 1", message)
            builder.assert_not_called()
            self.assertEqual(store.latest()["source_version"], "source-2")

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

    def test_update_drops_superseded_caches_and_old_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed = snapshot(root / "seed.json", "source-1", {1: anime(1, "Alpha")})
            store = VersionStore(root / "data")
            store.initialize(seed)
            for number in range(2, 4):
                older = snapshot(
                    root / f"source-{number}.json",
                    f"source-{number}",
                    {1: anime(1, f"Alpha {number}")},
                )
                store.commit_snapshot(older)
            for name in ("source-1", "source-3", "source-4"):
                (store.cache_dir / name / "anilist").mkdir(parents=True)
                write_json(store.cache_dir / "raw" / f"{name}.json", {})
            raw = {
                "masterListId": "source-4",
                "animeMap": {},
                "songMap": {},
                "artistMap": {},
                "groupMap": {},
            }

            def build(arguments: list[str]) -> int:
                output = Path(arguments[arguments.index("--output") + 1])
                snapshot(output, "source-4", {1: anime(1, "Alpha 4")})
                return 0

            with patch.dict("os.environ", {"VERSION_RETENTION": "2"}), patch(
                "master_list_creation.updater.HttpClient.get_json", return_value=raw
            ), patch("master_list_creation.updater.builder_main", side_effect=build):
                version, _message = run_update(store)

            self.assertEqual(version, 4)
            self.assertEqual(sorted(path.name for path in store.cache_dir.iterdir()), ["raw", "source-4"])
            self.assertEqual(
                [path.name for path in (store.cache_dir / "raw").iterdir()], ["source-4.json"]
            )
            self.assertEqual([row["version"] for row in store.versions()], [4, 3])
            # The adjacent diff written for this publication survives the pruning.
            self.assertTrue((store.diffs_dir / "3-4.json").exists())


if __name__ == "__main__":
    unittest.main()
