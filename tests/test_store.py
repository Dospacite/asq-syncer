from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from master_list_creation.http_client import read_json, write_json
from master_list_creation.store import VersionStore


def anime(identifier: int, title: str, year: int = 2024) -> dict:
    return {
        "annId": identifier,
        "category": {"name": "TV", "number": None},
        "year": year,
        "seasonId": 1,
        "names": [{"name": title, "language": "EN"}],
        "mainNames": {"EN": title, "JA": None},
        "songLinks": {"OP": [], "ED": [], "INS": []},
        "animeExtendedInfo": {
            "annId": identifier,
            "aniListId": -1,
            "malId": -1,
            "kitsuId": -1,
            "genres": [],
            "tags": [],
        },
    }


def snapshot(path: Path, source_version: str, records: dict[int, dict]) -> Path:
    write_json(
        path,
        {
            "masterListId": source_version,
            "animeMap": {str(identifier): value for identifier, value in records.items()},
        },
    )
    return path


class VersionStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.seed = snapshot(
            root / "seed.json", "source-1", {1: anime(1, "Alpha"), 2: anime(2, "Beta")}
        )
        self.store = VersionStore(root / "data")
        self.store.initialize(self.seed)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_versions_and_caches_diffs(self) -> None:
        root = Path(self.temporary.name)
        second = snapshot(
            root / "second.json",
            "source-2",
            {1: anime(1, "Alpha revised"), 3: anime(3, "Gamma")},
        )

        self.assertEqual(self.store.commit_snapshot(second), 2)
        path = self.store.get_or_create_diff(1, 2)
        payload = read_json(path)
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["fromVersion"], 1)
        self.assertEqual(payload["toVersion"], 2)
        self.assertEqual([item["id"] for item in payload["upserts"]], [1, 3])
        self.assertEqual(payload["deletes"], [2])
        self.assertTrue(payload["checksum"].startswith("sha256:"))
        modified = path.stat().st_mtime_ns
        self.assertEqual(self.store.get_or_create_diff(1, 2), path)
        self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_current_version_diff_is_empty(self) -> None:
        payload = read_json(self.store.get_or_create_diff(1, 1))
        self.assertEqual(payload["upserts"], [])
        self.assertEqual(payload["deletes"], [])

    def test_asset_refresh_republishes_the_same_source_version(self) -> None:
        root = Path(self.temporary.name)
        identical = snapshot(
            root / "identical.json",
            "source-1",
            {1: anime(1, "Alpha"), 2: anime(2, "Beta")},
        )
        self.assertEqual(self.store.commit_snapshot(identical, allow_same_source=True), 1)

        improved = snapshot(
            root / "improved.json",
            "source-1",
            {1: anime(1, "Alpha"), 2: anime(2, "Beta")},
        )
        value = read_json(improved)
        value["animeMap"]["1"]["annImage"] = "https://example.com/large.jpg"
        write_json(improved, value)

        self.assertEqual(self.store.commit_snapshot(improved, allow_same_source=True), 2)
        self.assertEqual(self.store.latest()["source_version"], "source-1")
        # Without the flag a repeat of the same source stays on its own version.
        self.assertEqual(self.store.commit_snapshot(improved), 1)

    def test_asset_refresh_supersedes_a_queued_update(self) -> None:
        first, created = self.store.enqueue_update("admin")
        self.assertTrue(created)
        repeat, created = self.store.enqueue_update("admin")
        self.assertEqual((repeat, created), (first, False))

        promoted, created = self.store.enqueue_update("admin", kind="asset_refresh")
        self.assertEqual((promoted, created), (first, False))
        self.assertEqual(self.store.recent_jobs()[0]["kind"], "asset_refresh")

        # A plain update is satisfied by the refresh that already covers it.
        self.assertEqual(self.store.enqueue_update("admin"), (first, False))
        self.assertEqual(len(self.store.recent_jobs()), 1)

    def test_legacy_database_migrates(self) -> None:
        root = Path(self.temporary.name) / "legacy"
        root.mkdir()
        database = sqlite3.connect(root / "state.sqlite3")
        database.executescript(
            """
            CREATE TABLE versions (
                version INTEGER PRIMARY KEY AUTOINCREMENT,
                source_version TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                checksum TEXT NOT NULL,
                anime_count INTEGER NOT NULL,
                file_name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE update_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL DEFAULT 'pending',
                requested_by TEXT NOT NULL,
                requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT,
                result_version INTEGER,
                message TEXT
            );
            INSERT INTO versions (version, source_version, checksum, anime_count, file_name)
                VALUES (7, 'source-legacy', 'sha256:legacy', 2, '7.json');
            INSERT INTO update_jobs (requested_by) VALUES ('scheduler');
            """
        )
        database.commit()
        database.close()

        store = VersionStore(root)
        store.initialize(self.seed)
        self.assertEqual(store.latest()["version"], 7)
        self.assertEqual(store.recent_jobs()[0]["kind"], "update")
        # The rebuilt table no longer rejects a second row for one source version.
        second = snapshot(
            Path(self.temporary.name) / "legacy-second.json",
            "source-legacy",
            {1: anime(1, "Alpha")},
        )
        self.assertEqual(store.commit_snapshot(second, allow_same_source=True), 8)

    def test_pruning_keeps_the_newest_versions_and_their_diffs(self) -> None:
        root = Path(self.temporary.name)
        for number in range(2, 5):
            source = snapshot(
                root / f"source-{number}.json",
                f"source-{number}",
                {1: anime(1, f"Alpha {number}")},
            )
            self.assertEqual(self.store.commit_snapshot(source), number)
        stale_diff = self.store.get_or_create_diff(1, 4)
        kept_diff = self.store.get_or_create_diff(3, 4)

        self.assertEqual(self.store.prune_versions(2), [1, 2])

        self.assertEqual([row["version"] for row in self.store.versions()], [4, 3])
        self.assertEqual(sorted(path.name for path in self.store.versions_dir.iterdir()), ["3.json", "4.json"])
        self.assertFalse(stale_diff.exists())
        self.assertTrue(kept_diff.exists())
        with self.assertRaises(LookupError):
            self.store.get_or_create_diff(1, 4)
        self.assertEqual(self.store.prune_versions(2), [])
        # Numbering continues from the newest version, not from the row count.
        fifth = snapshot(root / "source-5.json", "source-5", {1: anime(1, "Alpha 5")})
        self.assertEqual(self.store.commit_snapshot(fifth), 5)

    def test_browser_index_searches_title_and_id(self) -> None:
        records, total = self.store.anime_page("beta")
        self.assertEqual(total, 1)
        self.assertEqual(records[0]["id"], 2)
        records, total = self.store.anime_page("1")
        self.assertEqual(total, 1)
        self.assertEqual(records[0]["title"], "Alpha")


if __name__ == "__main__":
    unittest.main()
