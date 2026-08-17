"""Version-aware background updater process."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import multiprocessing
import os
from pathlib import Path
import queue
import signal
import tempfile
import threading
import time

from .cli import _safe_component, main as builder_main
from .http_client import HttpClient, write_json
from .sources import AMQ_MASTER_LIST_URL
from .store import VersionStore


LOGGER = logging.getLogger("master_list_updater")
STOP = False


def _stop(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def run_update(store: VersionStore, *, refresh_assets: bool = False) -> tuple[int, str]:
    raw = HttpClient(timeout=90).get_json(AMQ_MASTER_LIST_URL)
    if not isinstance(raw, dict) or not isinstance(raw.get("animeMap"), dict):
        raise ValueError("AMQ response does not contain animeMap")
    source_version = str(raw.get("masterListId") or "")
    if not source_version:
        raise ValueError("AMQ response does not contain masterListId")
    current = store.latest()
    if not refresh_assets and source_version == current["source_version"]:
        return int(current["version"]), "Already current; source version is unchanged."

    source_path = store.cache_dir / "raw" / f"{_safe_component(source_version)}.json"
    write_json(source_path, raw)
    with tempfile.TemporaryDirectory(dir=store.data_dir) as temporary_dir:
        output = Path(temporary_dir) / "libraryMasterList.json"
        arguments = [
            "update",
            "--source-file",
            str(source_path),
            "--output",
            str(output),
            "--cache-dir",
            str(store.cache_dir),
        ]
        if refresh_assets:
            # Cover art already in the cache was fetched by whichever builder
            # release wrote it, so an asset refresh has to ignore those batches.
            arguments.append("--refresh")
        exit_code = builder_main(arguments)
        if exit_code != 0:
            raise RuntimeError(f"master-list builder exited with status {exit_code}")
        previous_version = int(current["version"])
        version = store.commit_snapshot(output, allow_same_source=refresh_assets)
    if version != previous_version:
        store.get_or_create_diff(previous_version, version)
        return version, f"Published source version {source_version} as version {version}."
    return version, f"The rebuild produced no changes; version {version} still stands."


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/healthz":
            self.send_error(404)
            return
        body = b'{"status":"ok"}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _update_child(
    data_dir: str, seed_path: str, refresh_assets: bool, results: multiprocessing.Queue
) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    child_store = VersionStore(Path(data_dir))
    child_store.initialize(Path(seed_path))
    try:
        version, message = run_update(child_store, refresh_assets=refresh_assets)
        results.put((True, version, message))
    except Exception as error:
        results.put((False, None, str(error)))


def _interruptible_update(
    store: VersionStore, seed_path: Path, *, refresh_assets: bool = False
) -> tuple[int, str]:
    results: multiprocessing.Queue = multiprocessing.Queue(maxsize=1)
    process = multiprocessing.Process(
        target=_update_child,
        args=(str(store.data_dir), str(seed_path), refresh_assets, results),
        daemon=False,
    )
    process.start()
    while process.is_alive():
        process.join(timeout=0.5)
        if STOP:
            process.terminate()
            process.join(timeout=10)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            raise InterruptedError("update interrupted during worker shutdown")
    try:
        succeeded, version, message = results.get(timeout=1)
    except queue.Empty as error:
        raise RuntimeError(f"update process exited with status {process.exitcode}") from error
    if not succeeded:
        raise RuntimeError(message)
    return int(version), str(message)


def run_worker(store: VersionStore, seed_path: Path, interval_seconds: int) -> None:
    global STOP
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    next_scheduled = 0.0
    while not STOP:
        job = store.claim_job()
        scheduled = time.monotonic() >= next_scheduled
        if job is None and not scheduled:
            time.sleep(min(2.0, max(0.1, next_scheduled - time.monotonic())))
            continue
        if job is None:
            store.enqueue_update("scheduler")
            job = store.claim_job()
        job_id = int(job["id"]) if job else None
        refresh_assets = bool(job) and job["kind"] == "asset_refresh"
        try:
            version, message = _interruptible_update(
                store, seed_path, refresh_assets=refresh_assets
            )
            LOGGER.info(
                json.dumps(
                    {
                        "event": "update_complete",
                        "kind": job["kind"] if job else "update",
                        "version": version,
                        "message": message,
                    }
                )
            )
            if job_id is not None:
                store.finish_job(job_id, version=version, message=message)
        except InterruptedError as error:
            if job_id is not None:
                store.finish_job(job_id, version=None, message=str(error))
            return
        except Exception as error:
            LOGGER.exception(json.dumps({"event": "update_failed", "error": str(error)}))
            if job_id is not None:
                store.finish_job(job_id, version=None, message=str(error))
        next_scheduled = time.monotonic() + interval_seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the anime-list update worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--refresh-assets",
        action="store_true",
        help="with --once, re-download every upstream source instead of reusing caches",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(message)s")
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    seed = Path(os.getenv("SEED_LIST_PATH", "/app/seed/libraryMasterList.json"))
    store = VersionStore(data_dir)
    store.initialize(seed)
    if args.once:
        version, message = run_update(store, refresh_assets=args.refresh_assets)
        print(json.dumps({"version": version, "message": message}))
        return 0
    health_server = ThreadingHTTPServer(
        ("0.0.0.0", int(os.getenv("WORKER_PORT", "8081"))), _HealthHandler
    )
    health_thread = threading.Thread(target=health_server.serve_forever, daemon=True)
    health_thread.start()
    try:
        run_worker(
            store,
            seed,
            max(60, int(os.getenv("UPDATE_INTERVAL_SECONDS", "21600"))),
        )
    finally:
        health_server.shutdown()
        health_server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
