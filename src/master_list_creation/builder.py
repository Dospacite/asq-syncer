"""Join upstream records into the compact application schema."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence


# AniList serves every cover from a fixed-width box, and its GraphQL field names
# sit one step above the CDN path they resolve to: extraLarge comes from
# /cover/large/ at 460px, large from /cover/medium/ at 230px, medium from
# /cover/small/ at 100px. Heights vary per title, so only widths are recorded.
ANILIST_COVER_WIDTHS = {"extraLarge": 460, "large": 230, "medium": 100}


@dataclass
class BuildStats:
    anime: int = 0
    anime_with_linked_ids: int = 0
    anime_with_anilist_metadata: int = 0
    anime_with_images: int = 0
    songs: int = 0
    songs_with_media: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def _int_id(value: Any) -> int:
    if isinstance(value, bool):
        return -1
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return -1
    return -1


def _lookup(mapping: Mapping[str, Any], identifier: Any) -> dict[str, Any]:
    key = str(identifier)
    value = mapping.get(key)
    return value if isinstance(value, dict) else {}


def _first_name(mapping: Mapping[str, Any], identifier: Any) -> str | None:
    entry = _lookup(mapping, identifier)
    value = entry.get("name")
    return value if isinstance(value, str) and value else None


def _song_record(
    records: Sequence[dict[str, Any]], ann_id: int, ann_song_id: int
) -> dict[str, Any] | None:
    for record in records:
        if _int_id(record.get("annId")) == ann_id and _int_id(
            record.get("annSongId")
        ) == ann_song_id:
            return record
    return None


def _difficulty(value: Any) -> int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return max(0, min(100, round(float(value))))


def _media_map(record: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, source_key in (("0", "audio"), ("480", "MQ"), ("720", "HQ")):
        value = record.get(source_key)
        if isinstance(value, str) and value:
            result[key] = value
    return result


def _attach_credit_names(
    link: dict[str, Any],
    extended: Mapping[str, Any],
    record: Mapping[str, Any],
    artist_map: Mapping[str, Any],
    group_map: Mapping[str, Any],
) -> None:
    credits = (
        ("songArtistId", "songArtistName", artist_map, "songArtist"),
        ("songGroupId", "songGroupName", group_map, "songArtist"),
        ("composerArtistId", "composerArtistName", artist_map, "songComposer"),
        ("composerGroupId", "composerGroupName", group_map, "songComposer"),
        ("arrangerArtistId", "arrangerArtistName", artist_map, "songArranger"),
        ("arrangerGroupId", "arrangerGroupName", group_map, "songArranger"),
    )
    for id_field, name_field, source_map, fallback_field in credits:
        identifier = extended.get(id_field)
        if identifier is None:
            continue
        name = _first_name(source_map, identifier)
        fallback = record.get(fallback_field)
        if name is None and isinstance(fallback, str) and fallback:
            name = fallback
        if name:
            link[name_field] = name


def _enrich_song_link(
    raw_link: Mapping[str, Any],
    *,
    ann_id: int,
    song_map: Mapping[str, Any],
    artist_map: Mapping[str, Any],
    group_map: Mapping[str, Any],
    records_by_song_id: Mapping[int, Sequence[dict[str, Any]]],
) -> tuple[dict[str, Any], bool]:
    link = deepcopy(dict(raw_link))
    song_id = _int_id(link.get("songId"))
    ann_song_id = _int_id(link.get("annSongId"))
    extended = deepcopy(_lookup(song_map, song_id))
    extended.setdefault("songId", song_id)
    extended["annId"] = ann_id
    extended["annSongId"] = ann_song_id

    record = _song_record(records_by_song_id.get(song_id, []), ann_id, ann_song_id)
    has_media = False
    if record is not None:
        if not extended.get("name") and isinstance(record.get("songName"), str):
            extended["name"] = record["songName"]
        percent = _difficulty(record.get("songDifficulty"))
        if percent is not None:
            extended["globalPercent"] = percent
        media = _media_map(record)
        if media:
            extended["fileNameMap"] = media
        if "0" in media:
            extended["fileName"] = media["0"]
            has_media = True
        length = record.get("songLength")
        if isinstance(length, (int, float)) and not isinstance(length, bool):
            extended["songLength"] = length

    extended.setdefault("globalPercent", 0)
    extended.setdefault("recentPercent", 0)
    extended.setdefault("totalCorrectCount", 0)
    extended.setdefault("totalWrongCount", 0)
    extended.setdefault("totalMultipleChoiceCorrectCount", 0)
    extended.setdefault("totalMultipleChoiceWrongCount", 0)
    link["songExtendedInfo"] = extended
    _attach_credit_names(
        link,
        extended,
        record or {},
        artist_map,
        group_map,
    )
    return link, has_media


def _linked_ids(
    linked_entry: Mapping[str, Any],
    song_records: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    linked = linked_entry.get("linked_ids")
    if not isinstance(linked, dict) and song_records:
        candidate = song_records[0].get("linked_ids")
        linked = candidate if isinstance(candidate, dict) else {}
    return linked if isinstance(linked, dict) else {}


def _tags(media: Mapping[str, Any], minimum_rank: int) -> list[str]:
    result: list[str] = []
    for tag in media.get("tags", []):
        if not isinstance(tag, dict):
            continue
        name = tag.get("name")
        rank = tag.get("rank")
        if isinstance(name, str) and isinstance(rank, int) and rank >= minimum_rank:
            result.append(name)
    return result


def _image_pixels(image: Mapping[str, Any]) -> int:
    width = image.get("width")
    height = image.get("height")
    if not isinstance(width, int) or isinstance(width, bool):
        return -1
    if isinstance(height, int) and not isinstance(height, bool):
        return width * height
    # Cover art is never wider than it is tall, so a square is a safe lower
    # bound for an image whose height the upstream source does not publish.
    return width * width


def _largest_first(images: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    # Sorting is stable, so images we cannot measure keep their source order
    # behind every image we can.
    return sorted(deepcopy(list(images)), key=_image_pixels, reverse=True)


def _anilist_candidates(cover: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for variant, width in ANILIST_COVER_WIDTHS.items():
        src = cover.get(variant)
        if isinstance(src, str) and src and src not in seen:
            seen.add(src)
            candidates.append(
                {
                    "src": src,
                    "width": width,
                    "height": None,
                    "variant": variant,
                    "source": "anilist",
                }
            )
    return candidates


def _apply_images(
    anime: dict[str, Any],
    ann_id: int,
    ann_images: Mapping[int, Sequence[dict[str, Any]]],
    media: Mapping[str, Any],
) -> bool:
    candidates = [
        {**deepcopy(image), "source": "ann"} for image in ann_images.get(ann_id, [])
    ]
    cover = media.get("coverImage")
    if isinstance(cover, dict):
        candidates.extend(_anilist_candidates(cover))
    if not candidates:
        return False

    # ANN publishes only a 200x200 thumbnail for most titles, so the two sources
    # compete on measured size rather than ANN winning whenever it has anything.
    images = _largest_first(candidates)
    anime["annImage"] = images[0]["src"]
    anime["annImages"] = images
    if images[0]["source"] == "anilist":
        anime["imageSource"] = "anilist"
        anime["aniListCoverImage"] = deepcopy(cover)
    return True


def build_master_list(
    raw: Mapping[str, Any],
    *,
    linked_entries: Mapping[str, Any],
    anisongdb_songs: Sequence[dict[str, Any]],
    anilist_metadata: Mapping[int, dict[str, Any]],
    ann_images: Mapping[int, Sequence[dict[str, Any]]],
    minimum_tag_rank: int = 60,
    limit: int | None = None,
) -> tuple[dict[str, Any], BuildStats]:
    raw_anime_map = raw["animeMap"]
    song_map = raw["songMap"]
    artist_map = raw["artistMap"]
    group_map = raw["groupMap"]

    records_by_song_id: dict[int, list[dict[str, Any]]] = {}
    records_by_ann_id: dict[int, list[dict[str, Any]]] = {}
    for record in anisongdb_songs:
        song_id = _int_id(record.get("amqSongId"))
        ann_id = _int_id(record.get("annId"))
        records_by_song_id.setdefault(song_id, []).append(record)
        records_by_ann_id.setdefault(ann_id, []).append(record)

    ordered_items = sorted(
        raw_anime_map.items(), key=lambda item: _int_id(item[0])
    )
    if limit is not None:
        ordered_items = ordered_items[:limit]

    result_map: dict[str, Any] = {}
    stats = BuildStats()
    for key, raw_anime in ordered_items:
        if not isinstance(raw_anime, dict):
            continue
        anime = deepcopy(raw_anime)
        ann_id = _int_id(anime.get("annId"))
        anime_records = records_by_ann_id.get(ann_id, [])
        linked_entry = linked_entries.get(str(ann_id), {})
        if not isinstance(linked_entry, dict):
            linked_entry = {}
        ids = _linked_ids(linked_entry, anime_records)
        anilist_id = _int_id(ids.get("anilist"))
        mal_id = _int_id(ids.get("myanimelist"))
        kitsu_id = _int_id(ids.get("kitsu"))
        media = anilist_metadata.get(anilist_id, {})

        genres = media.get("genres", [])
        if not isinstance(genres, list):
            genres = []
        anime["animeExtendedInfo"] = {
            "annId": ann_id,
            "aniListId": anilist_id,
            "malId": mal_id,
            "kitsuId": kitsu_id,
            "genres": [value for value in genres if isinstance(value, str)],
            "tags": _tags(media, minimum_tag_rank),
        }

        song_links = anime.get("songLinks", {})
        if isinstance(song_links, dict):
            enriched_links: dict[str, list[dict[str, Any]]] = {}
            for song_type, links in song_links.items():
                if not isinstance(links, list):
                    continue
                enriched_links[song_type] = []
                for raw_link in links:
                    if not isinstance(raw_link, dict):
                        continue
                    link, has_media = _enrich_song_link(
                        raw_link,
                        ann_id=ann_id,
                        song_map=song_map,
                        artist_map=artist_map,
                        group_map=group_map,
                        records_by_song_id=records_by_song_id,
                    )
                    enriched_links[song_type].append(link)
                    stats.songs += 1
                    stats.songs_with_media += int(has_media)
            anime["songLinks"] = enriched_links

        stats.anime += 1
        stats.anime_with_linked_ids += int(
            any(_int_id(ids.get(field)) >= 0 for field in ("anilist", "myanimelist", "kitsu"))
        )
        stats.anime_with_anilist_metadata += int(bool(media))
        stats.anime_with_images += int(_apply_images(anime, ann_id, ann_images, media))
        result_map[str(key)] = anime

    # Keep the source version for reproducibility while omitting AMQ transport
    # maps that the application does not consume.
    return {
        "masterListId": raw.get("masterListId"),
        "animeMap": result_map,
    }, stats
