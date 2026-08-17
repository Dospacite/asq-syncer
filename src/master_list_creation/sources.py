"""Upstream adapters used by the master-list builder."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

from .http_client import (
    HttpClient,
    RateLimiter,
    RequestError,
    read_json,
    write_json,
)


AMQ_MASTER_LIST_URL = "https://animemusicquiz.com/libraryMasterList"
ANISONGDB_LINKS_URL = "https://anisongdb.com/api/annid_linked_ids"
ANISONGDB_SONGS_URL = "https://anisongdb.com/api/ann_ids_request"
ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"
ANN_API_URL = "https://cdn.animenewsnetwork.com/encyclopedia/api.xml"

Progress = Callable[[str], None]

ANILIST_QUERY = """
query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      idMal
      genres
      tags { name rank isMediaSpoiler }
      coverImage { extraLarge large medium }
    }
  }
}
""".strip()


def _chunks(values: Sequence[int], size: int) -> Iterable[list[int]]:
    for index in range(0, len(values), size):
        yield list(values[index : index + size])


def _cache_name(ids: Sequence[int], suffix: str) -> str:
    digest = hashlib.sha1(",".join(map(str, ids)).encode("ascii")).hexdigest()[:12]
    return f"{ids[0]}-{ids[-1]}-{digest}.{suffix}"


def _cached_json(
    path: Path,
    fetch: Callable[[], Any],
    *,
    refresh: bool,
    offline: bool,
) -> Any:
    if path.exists() and not refresh:
        return read_json(path)
    if offline:
        raise FileNotFoundError(f"offline cache entry is missing: {path}")
    value = fetch()
    write_json(path, value)
    return value


def fetch_amq_master_list(
    client: HttpClient,
    *,
    source_file: Path | None,
    progress: Progress,
) -> dict[str, Any]:
    if source_file is not None:
        progress(f"Reading AMQ source snapshot: {source_file}")
        value = read_json(source_file)
    else:
        progress(f"Downloading AMQ master list: {AMQ_MASTER_LIST_URL}")
        value = client.get_json(AMQ_MASTER_LIST_URL)

    if not isinstance(value, dict) or not isinstance(value.get("animeMap"), dict):
        raise ValueError("AMQ source does not contain an animeMap object")
    for required_map in ("songMap", "artistMap", "groupMap"):
        if not isinstance(value.get(required_map), dict):
            raise ValueError(
                f"AMQ source is missing {required_map}; use the raw AMQ endpoint, "
                "not an already-enriched application asset"
            )
    return value


def fetch_anisongdb_links(
    client: HttpClient,
    cache_dir: Path,
    *,
    refresh: bool,
    offline: bool,
    progress: Progress,
) -> dict[str, Any]:
    path = cache_dir / "anisongdb-linked-ids.json"
    progress("Loading AniSongDB ANN-to-catalog ID links")
    value = _cached_json(
        path,
        lambda: client.get_json(ANISONGDB_LINKS_URL),
        refresh=refresh,
        offline=offline,
    )
    if not isinstance(value, dict):
        raise ValueError("AniSongDB linked-ID response is not an object")
    return value


def fetch_anisongdb_songs(
    client: HttpClient,
    cache_dir: Path,
    ann_ids: Sequence[int],
    *,
    refresh: bool,
    offline: bool,
    progress: Progress,
) -> list[dict[str, Any]]:
    unique_ids = sorted(set(ann_ids))
    # The endpoint enforces this limit even though it is not declared in the
    # generated OpenAPI request schema.
    batches = list(_chunks(unique_ids, 500))
    limiter = RateLimiter(0.25)
    records: list[dict[str, Any]] = []

    for number, batch in enumerate(batches, start=1):
        path = cache_dir / "anisongdb-songs" / _cache_name(batch, "json")
        progress(f"AniSongDB songs: batch {number}/{len(batches)}")
        value = _cached_json(
            path,
            lambda batch=batch: client.post_json(
                ANISONGDB_SONGS_URL,
                {"ann_ids": batch, "ignore_duplicate": False},
                rate_limiter=limiter,
            ),
            refresh=refresh,
            offline=offline,
        )
        if not isinstance(value, list):
            raise ValueError("AniSongDB song response is not a list")
        records.extend(record for record in value if isinstance(record, dict))
    return records


def fetch_anilist_metadata(
    client: HttpClient,
    cache_dir: Path,
    anilist_ids: Sequence[int],
    *,
    refresh: bool,
    offline: bool,
    progress: Progress,
) -> dict[int, dict[str, Any]]:
    unique_ids = sorted({value for value in anilist_ids if value > 0})
    batches = list(_chunks(unique_ids, 50))
    # AniList currently advertises 30 requests/minute in its response headers.
    limiter = RateLimiter(2.1)
    metadata: dict[int, dict[str, Any]] = {}

    for number, batch in enumerate(batches, start=1):
        path = cache_dir / "anilist" / _cache_name(batch, "json")
        progress(f"AniList metadata: batch {number}/{len(batches)}")
        response = _cached_json(
            path,
            lambda batch=batch: client.post_json(
                ANILIST_GRAPHQL_URL,
                {"query": ANILIST_QUERY, "variables": {"ids": batch}},
                rate_limiter=limiter,
            ),
            refresh=refresh,
            offline=offline,
        )
        if not isinstance(response, dict) or response.get("errors"):
            raise RequestError(f"AniList GraphQL error: {response}")
        media = response.get("data", {}).get("Page", {}).get("media", [])
        if not isinstance(media, list):
            raise ValueError("AniList response does not contain Page.media")
        for item in media:
            if isinstance(item, dict) and isinstance(item.get("id"), int):
                metadata[item["id"]] = item
    return metadata


def parse_ann_images(xml: bytes) -> dict[int, list[dict[str, Any]]]:
    root = ET.fromstring(xml)
    result: dict[int, list[dict[str, Any]]] = {}
    for anime in root.findall("anime"):
        try:
            ann_id = int(anime.attrib["id"])
        except (KeyError, ValueError):
            continue
        images: list[dict[str, Any]] = []
        seen: set[str] = set()
        for info in anime.findall("info"):
            if info.attrib.get("type") != "Picture":
                continue
            src = info.attrib.get("src")
            if not src or src in seen:
                continue
            seen.add(src)
            image: dict[str, Any] = {"src": src}
            for dimension in ("width", "height"):
                raw_value = info.attrib.get(dimension)
                try:
                    image[dimension] = int(raw_value) if raw_value else None
                except ValueError:
                    image[dimension] = None
            images.append(image)
        result[ann_id] = images
    return result


def fetch_ann_images(
    client: HttpClient,
    cache_dir: Path,
    ann_ids: Sequence[int],
    *,
    refresh: bool,
    offline: bool,
    progress: Progress,
) -> dict[int, list[dict[str, Any]]]:
    unique_ids = sorted(set(ann_ids))
    batches = list(_chunks(unique_ids, 50))
    # The ANN API documents a one-request-per-second limit.
    limiter = RateLimiter(1.05)
    result: dict[int, list[dict[str, Any]]] = {}

    for number, batch in enumerate(batches, start=1):
        path = cache_dir / "ann" / _cache_name(batch, "json")
        progress(f"Anime News Network images: batch {number}/{len(batches)}")
        if path.exists() and not refresh:
            cached = read_json(path)
            if not isinstance(cached, dict):
                raise ValueError(f"ANN image cache is not an object: {path}")
            parsed = {
                int(key): images
                for key, images in cached.items()
                if str(key).isdigit() and isinstance(images, list)
            }
        else:
            if offline:
                raise FileNotFoundError(f"offline cache entry is missing: {path}")
            query = urlencode({"anime": "/".join(map(str, batch))})
            xml = client.get_bytes(f"{ANN_API_URL}?{query}", rate_limiter=limiter)
            # Parse before caching so a transient HTML error never poisons cache.
            parsed = parse_ann_images(xml)
            # Cache only the small extracted result; ANN's detail XML contains
            # considerably more data than this application needs.
            write_json(path, parsed)
        result.update(parsed)
    return result
