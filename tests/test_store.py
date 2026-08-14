from __future__ import annotations

import json
from pathlib import Path
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

    def test_browser_index_searches_title_and_id(self) -> None:
        records, total = self.store.anime_page("beta")
        self.assertEqual(total, 1)
        self.assertEqual(records[0]["id"], 2)
        records, total = self.store.anime_page("1")
        self.assertEqual(total, 1)
        self.assertEqual(records[0]["title"], "Alpha")


if __name__ == "__main__":
    unittest.main()
