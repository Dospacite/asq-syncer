"""Persistent versions, cached differences, and update jobs."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any

from .http_client import read_json, write_json
from .validation import validate_master_list


SCHEMA_VERSION = 1
# "update" republishes only when AMQ ships a new master list; "asset_refresh"
# re-downloads every upstream source and republishes whenever the records change.
JOB_KINDS = ("update", "asset_refresh")


class VersionStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.versions_dir = data_dir / "versions"
        self.diffs_dir = data_dir / "diffs"
        self.cache_dir = data_dir / "cache"
        self.db_path = data_dir / "state.sqlite3"
        self.lock_path = data_dir / "diff.lock"
        self.init_lock_path = data_dir / "init.lock"
        self._anime_index_version: int | None = None
        self._anime_index: list[dict[str, Any]] = []

    def initialize(self, seed_path: Path | None = None) -> None:
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.diffs_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with self.init_lock_path.open("a+") as init_lock:
            fcntl.flock(init_lock, fcntl.LOCK_EX)
            with self._database() as database:
                database.executescript(
                """
                CREATE TABLE IF NOT EXISTS versions (
                    version INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_version TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    checksum TEXT NOT NULL,
                    anime_count INTEGER NOT NULL,
                    file_name TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS diff_cache (
                    from_version INTEGER NOT NULL,
                    to_version INTEGER NOT NULL,
                    file_name TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (from_version, to_version)
                );
                CREATE TABLE IF NOT EXISTS update_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    kind TEXT NOT NULL DEFAULT 'update',
                    requested_by TEXT NOT NULL,
                    requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at TEXT,
                    finished_at TEXT,
                    result_version INTEGER,
                    message TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_status_idx
                    ON update_jobs(status, id);
                """
                )
                self._migrate(database)
                count = database.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
            if count == 0:
                if seed_path is None or not seed_path.exists():
                    raise FileNotFoundError("no stored versions and seed master list is missing")
                self.commit_snapshot(seed_path)

    @staticmethod
    def _migrate(database: sqlite3.Connection) -> None:
        """Bring a database created by an earlier release up to date."""
        columns = {
            row["name"]
            for row in database.execute("PRAGMA table_info(update_jobs)").fetchall()
        }
        if "kind" not in columns:
            database.execute(
                "ALTER TABLE update_jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'update'"
            )

        # Releases before asset refreshes existed allowed one version per AMQ
        # source version. A refresh republishes the same source with better cover
        # art, so that uniqueness constraint has to go; SQLite can only drop it by
        # rebuilding the table.
        definition = database.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'versions'"
        ).fetchone()
        if definition is None or "source_version TEXT NOT NULL UNIQUE" not in definition["sql"]:
            return
        database.executescript(
            """
            CREATE TABLE versions_migrated (
                version INTEGER PRIMARY KEY AUTOINCREMENT,
                source_version TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                checksum TEXT NOT NULL,
                anime_count INTEGER NOT NULL,
                file_name TEXT NOT NULL UNIQUE
            );
            INSERT INTO versions_migrated
                (version, source_version, created_at, checksum, anime_count, file_name)
                SELECT version, source_version, created_at, checksum, anime_count, file_name
                FROM versions;
            DROP TABLE versions;
            ALTER TABLE versions_migrated RENAME TO versions;
            """
        )

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.db_path, timeout=30)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA foreign_keys=ON")
        return database

    @contextmanager
    def _database(self):
        database = self._connect()
        try:
            with database:
                yield database
        finally:
            database.close()

    @staticmethod
    def _anime_checksum(value: dict[str, Any]) -> str:
        anime_map = value["animeMap"]
        digest = hashlib.sha256(
            json.dumps(
                anime_map,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return f"sha256:{digest}"

    def commit_snapshot(self, source_path: Path, *, allow_same_source: bool = False) -> int:
        value = read_json(source_path)
        errors, stats = validate_master_list(value)
        if errors:
            raise ValueError("invalid master list: " + "; ".join(errors[:5]))
        source_version = str(value["masterListId"])
        checksum = self._anime_checksum(value)
        with self._database() as database:
            if allow_same_source:
                # An asset refresh reuses the current source version, so identical
                # records are the only thing that makes it a no-op.
                existing = database.execute(
                    "SELECT version FROM versions WHERE checksum = ? "
                    "ORDER BY version DESC LIMIT 1",
                    (checksum,),
                ).fetchone()
            else:
                existing = database.execute(
                    "SELECT version FROM versions WHERE source_version = ?",
                    (source_version,),
                ).fetchone()
            if existing:
                return int(existing["version"])
            version = int(
                database.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM versions").fetchone()[0]
            )
            file_name = f"{version}.json"
            target = self.versions_dir / file_name
            temporary = target.with_suffix(".json.tmp")
            shutil.copyfile(source_path, temporary)
            os.replace(temporary, target)
            database.execute(
                """INSERT INTO versions
                   (version, source_version, checksum, anime_count, file_name)
                   VALUES (?, ?, ?, ?, ?)""",
                (version, source_version, checksum, stats.anime, file_name),
            )
        return version

    def latest(self) -> dict[str, Any]:
        with self._database() as database:
            row = database.execute(
                "SELECT * FROM versions ORDER BY version DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise LookupError("no anime-list version is available")
        return dict(row)

    def versions(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._database() as database:
            rows = database.execute(
                "SELECT * FROM versions ORDER BY version DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def version(self, version: int) -> dict[str, Any] | None:
        with self._database() as database:
            row = database.execute(
                "SELECT * FROM versions WHERE version = ?", (version,)
            ).fetchone()
        return dict(row) if row else None

    def snapshot_path(self, version: int | None = None) -> Path:
        record = self.latest() if version is None else self.version(version)
        if record is None:
            raise LookupError(f"anime-list version {version} does not exist")
        return self.versions_dir / record["file_name"]

    def anime_page(
        self, query: str = "", page: int = 1, per_page: int = 48
    ) -> tuple[list[dict[str, Any]], int]:
        latest_version = int(self.latest()["version"])
        if self._anime_index_version != latest_version:
            value = read_json(self.snapshot_path(latest_version))
            index: list[dict[str, Any]] = []
            for raw_id, anime in value["animeMap"].items():
                names = anime.get("names", [])
                searchable = " ".join(
                    item.get("name", "") for item in names if isinstance(item, dict)
                ).casefold()
                display = next(
                    (
                        name
                        for name in anime.get("mainNames", {}).values()
                        if isinstance(name, str) and name.strip()
                    ),
                    f"Anime {raw_id}",
                )
                song_count = sum(
                    len(items)
                    for items in anime.get("songLinks", {}).values()
                    if isinstance(items, list)
                )
                category = anime.get("category", "Unknown")
                if isinstance(category, dict):
                    category = category.get("name", "Unknown")
                index.append(
                    {
                        "id": int(raw_id),
                        "title": display,
                        "searchable": searchable,
                        "year": anime.get("year"),
                        "category": category,
                        "songs": song_count,
                        "image": anime.get("annImage"),
                    }
                )
            index.sort(key=lambda item: (item["title"].casefold(), item["id"]))
            self._anime_index = index
            self._anime_index_version = latest_version
        normalized = query.casefold().strip()
        matches = [
            item
            for item in self._anime_index
            if not normalized
            or normalized in item["searchable"]
            or normalized in str(item["id"])
        ]
        total = len(matches)
        start = (page - 1) * per_page
        return matches[start : start + per_page], total

    def get_or_create_diff(self, from_version: int, to_version: int) -> Path:
        if from_version > to_version:
            raise ValueError("fromVersion must not be greater than toVersion")
        if self.version(from_version) is None or self.version(to_version) is None:
            raise LookupError("one or both requested versions do not exist")
        with self._database() as database:
            cached = database.execute(
                "SELECT file_name FROM diff_cache WHERE from_version = ? AND to_version = ?",
                (from_version, to_version),
            ).fetchone()
        if cached and (self.diffs_dir / cached["file_name"]).exists():
            return self.diffs_dir / cached["file_name"]

        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self._database() as database:
                cached = database.execute(
                    "SELECT file_name FROM diff_cache WHERE from_version = ? AND to_version = ?",
                    (from_version, to_version),
                ).fetchone()
            if cached and (self.diffs_dir / cached["file_name"]).exists():
                return self.diffs_dir / cached["file_name"]

            # Pruning holds this lock, so a version that survived the first check
            # may have gone by now.
            target_record = self.version(to_version)
            if self.version(from_version) is None or target_record is None:
                raise LookupError("one or both requested versions do not exist")
            if from_version == to_version:
                upserts: list[dict[str, Any]] = []
                deletes: list[int] = []
            else:
                before = read_json(self.snapshot_path(from_version))["animeMap"]
                after = read_json(self.snapshot_path(to_version))["animeMap"]
                upserts = []
                for identifier in sorted(after, key=lambda value: int(value)):
                    if before.get(identifier) != after[identifier]:
                        upserts.append({**after[identifier], "id": int(identifier)})
                deletes = sorted(int(identifier) for identifier in set(before) - set(after))
            payload = {
                "schemaVersion": SCHEMA_VERSION,
                "fromVersion": from_version,
                "toVersion": to_version,
                "upserts": upserts,
                "deletes": deletes,
                "checksum": target_record["checksum"],
            }
            file_name = f"{from_version}-{to_version}.json"
            path = self.diffs_dir / file_name
            write_json(path, payload)
            with self._database() as database:
                database.execute(
                    """INSERT OR REPLACE INTO diff_cache
                       (from_version, to_version, file_name) VALUES (?, ?, ?)""",
                    (from_version, to_version, file_name),
                )
            return path

    def prune_versions(self, keep: int) -> list[int]:
        """Drop all but the newest ``keep`` versions and every diff touching them.

        Clients asking for updates from a dropped version get ``version_not_found``
        and fall back to the full list.
        """
        if keep < 1:
            raise ValueError("at least one version must be kept")
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self._database() as database:
                pruned = database.execute(
                    "SELECT version, file_name FROM versions ORDER BY version DESC LIMIT -1 OFFSET ?",
                    (keep,),
                ).fetchall()
                if not pruned:
                    return []
                oldest_kept = int(pruned[0]["version"]) + 1
                diffs = database.execute(
                    "SELECT file_name FROM diff_cache WHERE from_version < ? OR to_version < ?",
                    (oldest_kept, oldest_kept),
                ).fetchall()
                database.execute(
                    "DELETE FROM diff_cache WHERE from_version < ? OR to_version < ?",
                    (oldest_kept, oldest_kept),
                )
                database.execute("DELETE FROM versions WHERE version < ?", (oldest_kept,))
            for row in diffs:
                (self.diffs_dir / row["file_name"]).unlink(missing_ok=True)
            for row in pruned:
                (self.versions_dir / row["file_name"]).unlink(missing_ok=True)
        return sorted(int(row["version"]) for row in pruned)

    def enqueue_update(
        self, requested_by: str, kind: str = "update"
    ) -> tuple[int, bool]:
        """Queue a job of ``kind``, returning its id and whether it was created."""
        if kind not in JOB_KINDS:
            raise ValueError(f"unknown job kind: {kind}")
        with self._database() as database:
            database.execute("BEGIN IMMEDIATE")
            active = database.execute(
                "SELECT id, status, kind FROM update_jobs "
                "WHERE status IN ('pending', 'running') ORDER BY id"
            ).fetchall()
            # An asset refresh rebuilds everything a plain update would, so an
            # active refresh satisfies both kinds while an active update only
            # satisfies another update.
            for job in active:
                if kind == "update" or job["kind"] == "asset_refresh":
                    return int(job["id"]), False
            pending = next((job for job in active if job["status"] == "pending"), None)
            if pending is not None:
                database.execute(
                    "UPDATE update_jobs SET kind = ?, requested_by = ? WHERE id = ?",
                    (kind, requested_by, pending["id"]),
                )
                return int(pending["id"]), False
            cursor = database.execute(
                "INSERT INTO update_jobs (requested_by, kind) VALUES (?, ?)",
                (requested_by, kind),
            )
            return int(cursor.lastrowid), True

    def claim_job(self) -> dict[str, Any] | None:
        with self._database() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT * FROM update_jobs WHERE status = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            database.execute(
                "UPDATE update_jobs SET status = 'running', started_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row["id"],),
            )
        return dict(row)

    def finish_job(self, job_id: int, *, version: int | None, message: str) -> None:
        status = "succeeded" if version is not None else "failed"
        with self._database() as database:
            database.execute(
                """UPDATE update_jobs SET status = ?, finished_at = CURRENT_TIMESTAMP,
                   result_version = ?, message = ? WHERE id = ?""",
                (status, version, message[:1000], job_id),
            )

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._database() as database:
            rows = database.execute(
                "SELECT * FROM update_jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]
