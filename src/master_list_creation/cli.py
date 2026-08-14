"""Command-line interface for master-list creation and validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

from .builder import build_master_list
from .http_client import HttpClient, RequestError, read_json, write_json
from .sources import (
    fetch_amq_master_list,
    fetch_anilist_metadata,
    fetch_anisongdb_links,
    fetch_anisongdb_songs,
    fetch_ann_images,
)
from .validation import validate_master_list


TOOL_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = TOOL_ROOT / "seed" / "libraryMasterList.json"
DEFAULT_CACHE = TOOL_ROOT / ".cache"


def _safe_component(value: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "unknown"))
    return cleaned[:120] or "unknown"


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _selected_ann_ids(raw: dict[str, Any], limit: int | None) -> list[int]:
    ids = sorted(
        anime.get("annId")
        for anime in raw["animeMap"].values()
        if isinstance(anime, dict) and isinstance(anime.get("annId"), int)
    )
    return ids[:limit] if limit is not None else ids


def _build(args: argparse.Namespace) -> int:
    if args.offline and args.source_file is None:
        print("--offline requires --source-file for the raw AMQ snapshot", file=sys.stderr)
        return 2

    client = HttpClient(timeout=args.timeout)
    raw = fetch_amq_master_list(
        client,
        source_file=args.source_file,
        progress=_progress,
    )
    master_list_id = _safe_component(raw.get("masterListId"))
    cache_dir = args.cache_dir / master_list_id
    ann_ids = _selected_ann_ids(raw, args.limit)
    if not ann_ids:
        print("AMQ source contains no selectable anime", file=sys.stderr)
        return 1

    links = fetch_anisongdb_links(
        client,
        cache_dir,
        refresh=args.refresh,
        offline=args.offline,
        progress=_progress,
    )
    songs = fetch_anisongdb_songs(
        client,
        cache_dir,
        ann_ids,
        refresh=args.refresh,
        offline=args.offline,
        progress=_progress,
    )

    anilist_ids: list[int] = []
    linked_ann_ids: set[int] = set()
    for ann_id in ann_ids:
        entry = links.get(str(ann_id), {})
        linked = entry.get("linked_ids", {}) if isinstance(entry, dict) else {}
        identifier = linked.get("anilist") if isinstance(linked, dict) else None
        if isinstance(identifier, int):
            anilist_ids.append(identifier)
            linked_ann_ids.add(ann_id)
    # AniSongDB song responses repeat the links and can cover a just-added entry
    # that has not appeared in the bulk linked-ID snapshot yet.
    for song in songs:
        if not isinstance(song, dict):
            continue
        song_ann_id = song.get("annId")
        if not isinstance(song_ann_id, int) or song_ann_id in linked_ann_ids:
            continue
        linked = song.get("linked_ids")
        identifier = linked.get("anilist") if isinstance(linked, dict) else None
        if isinstance(identifier, int):
            anilist_ids.append(identifier)
            linked_ann_ids.add(song_ann_id)
    anilist = (
        {}
        if args.skip_anilist
        else fetch_anilist_metadata(
            client,
            cache_dir,
            anilist_ids,
            refresh=args.refresh,
            offline=args.offline,
            progress=_progress,
        )
    )
    images = (
        {}
        if args.skip_ann_images
        else fetch_ann_images(
            client,
            cache_dir,
            ann_ids,
            refresh=args.refresh,
            offline=args.offline,
            progress=_progress,
        )
    )

    _progress("Joining and validating source data")
    result, build_stats = build_master_list(
        raw,
        linked_entries=links,
        anisongdb_songs=songs,
        anilist_metadata=anilist,
        ann_images=images,
        minimum_tag_rank=args.min_tag_rank,
        limit=args.limit,
    )
    errors, validation_stats = validate_master_list(result)
    if errors:
        print("Generated list is invalid:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    if args.strict:
        missing = []
        if build_stats.anime_with_linked_ids != build_stats.anime:
            missing.append("catalog ID links")
        if build_stats.anime_with_anilist_metadata != build_stats.anime:
            missing.append("AniList metadata")
        if build_stats.anime_with_images != build_stats.anime:
            missing.append("images")
        if (
            validation_stats.uploaded_songs_with_audio
            != validation_stats.uploaded_songs
        ):
            missing.append("uploaded-song audio files")
        if missing:
            print(
                "Strict coverage check failed: " + ", ".join(missing), file=sys.stderr
            )
            return 1

    write_json(args.output, result, pretty=args.pretty)
    summary = {
        "output": str(args.output),
        "masterListId": raw.get("masterListId"),
        "build": build_stats.to_dict(),
        "validation": validation_stats.to_dict(),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _update(args: argparse.Namespace) -> int:
    """Refresh the application asset using the current AMQ master list."""
    return _build(args)


def _validate(args: argparse.Namespace) -> int:
    value = read_json(args.input)
    errors, stats = validate_master_list(value)
    print(
        json.dumps(
            {"input": str(args.input), "valid": not errors, "stats": stats.to_dict()},
            indent=2,
        )
    )
    if errors:
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    return 0


def _add_build_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--source-file",
        type=Path,
        help="use a raw AMQ JSON snapshot instead of downloading it",
    )
    parser.add_argument("--refresh", action="store_true", help="ignore cached batches")
    parser.add_argument(
        "--offline", action="store_true", help="fail instead of making network requests"
    )
    parser.add_argument(
        "--skip-anilist",
        action="store_true",
        help="leave genres and tags empty (useful for a quick smoke build)",
    )
    parser.add_argument(
        "--skip-ann-images",
        action="store_true",
        help="skip ANN image lookup and use AniList fallback images",
    )
    parser.add_argument(
        "--min-tag-rank",
        type=int,
        default=60,
        choices=range(0, 101),
        metavar="0..100",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="build only the first N anime for development/smoke testing",
    )
    parser.add_argument("--pretty", action="store_true", help="pretty-print output JSON")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="require complete metadata, image, and uploaded-song media coverage",
    )
    parser.add_argument("--timeout", type=float, default=90.0)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the enriched Anime Song Quiz library master list."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="download, enrich, and write a list")
    _add_build_arguments(build)
    build.set_defaults(handler=_build)

    update = subparsers.add_parser(
        "update",
        help="refresh seed/libraryMasterList.json from the current AMQ list",
    )
    _add_build_arguments(update)
    update.set_defaults(handler=_update)

    validate = subparsers.add_parser("validate", help="validate an existing output file")
    validate.add_argument("input", type=Path, nargs="?", default=DEFAULT_OUTPUT)
    validate.set_defaults(handler=_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if getattr(args, "limit", None) is not None and args.limit <= 0:
        print("--limit must be greater than zero", file=sys.stderr)
        return 2
    try:
        return args.handler(args)
    except (FileNotFoundError, OSError, RequestError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
