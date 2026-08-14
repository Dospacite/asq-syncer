"""Structural validation for generated application assets."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import PurePath
from typing import Any
from urllib.parse import urlparse


@dataclass
class ValidationStats:
    anime: int = 0
    songs: int = 0
    uploaded_songs: int = 0
    songs_with_audio: int = 0
    uploaded_songs_with_audio: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def validate_master_list(
    value: Any, *, max_errors: int = 100
) -> tuple[list[str], ValidationStats]:
    errors: list[str] = []
    stats = ValidationStats()

    def error(message: str) -> None:
        if len(errors) < max_errors:
            errors.append(message)

    if not isinstance(value, dict):
        return ["top-level value must be an object"], stats
    unexpected_keys = sorted(set(value) - {"animeMap", "masterListId"})
    if unexpected_keys:
        error(
            "top-level object contains unsupported field(s): "
            + ", ".join(unexpected_keys)
        )
    master_list_id = value.get("masterListId")
    if not isinstance(master_list_id, str) or not master_list_id.strip():
        error("masterListId must be a non-empty string")
    anime_map = value.get("animeMap")
    if not isinstance(anime_map, dict) or not anime_map:
        error("animeMap must be a non-empty object")
        return errors, stats

    for key, anime in anime_map.items():
        location = f"animeMap.{key}"
        if not isinstance(anime, dict):
            error(f"{location} must be an object")
            continue
        stats.anime += 1
        ann_id = anime.get("annId")
        if not isinstance(ann_id, int) or isinstance(ann_id, bool):
            error(f"{location}.annId must be an integer")
        elif str(ann_id) != str(key):
            error(f"{location}.annId does not match its map key")

        year = anime.get("year")
        if not isinstance(year, int) or isinstance(year, bool):
            error(f"{location}.year must be an integer")
        category = anime.get("category")
        if not isinstance(category, (str, dict)):
            error(f"{location}.category must be a string or object")
        names = anime.get("names")
        if not isinstance(names, list) or not names:
            error(f"{location}.names must be a non-empty list")
        elif not all(
            isinstance(name, dict)
            and isinstance(name.get("name"), str)
            and bool(name["name"].strip())
            for name in names
        ):
            error(f"{location}.names contains an invalid name")
        main_names = anime.get("mainNames")
        if not isinstance(main_names, dict) or not any(
            isinstance(name, str) and bool(name.strip())
            for name in main_names.values()
        ):
            error(f"{location}.mainNames must contain a display name")

        image_url = anime.get("annImage")
        if image_url is not None and (
            not isinstance(image_url, str)
            or urlparse(image_url).scheme not in {"http", "https"}
        ):
            error(f"{location}.annImage must be an HTTP(S) URL")

        extended = anime.get("animeExtendedInfo")
        if not isinstance(extended, dict):
            error(f"{location}.animeExtendedInfo must be an object")
        else:
            if not isinstance(extended.get("annId"), int) or isinstance(
                extended.get("annId"), bool
            ):
                error(f"{location}.animeExtendedInfo.annId must be an integer")
            for field in ("aniListId", "malId", "kitsuId"):
                identifier = extended.get(field)
                if identifier is not None and (
                    not isinstance(identifier, int) or isinstance(identifier, bool)
                ):
                    error(
                        f"{location}.animeExtendedInfo.{field} must be an "
                        "integer or null"
                    )
            for field in ("genres", "tags"):
                field_value = extended.get(field)
                if not isinstance(field_value, list) or not all(
                    isinstance(item, str) for item in field_value
                ):
                    error(
                        f"{location}.animeExtendedInfo.{field} must be a string list"
                    )

        song_links = anime.get("songLinks")
        if not isinstance(song_links, dict):
            error(f"{location}.songLinks must be an object")
            continue
        for song_type, links in song_links.items():
            if not isinstance(links, list):
                error(f"{location}.songLinks.{song_type} must be a list")
                continue
            for index, link in enumerate(links):
                song_location = f"{location}.songLinks.{song_type}[{index}]"
                if not isinstance(link, dict):
                    error(f"{song_location} must be an object")
                    continue
                stats.songs += 1
                uploaded = link.get("uploaded") in (1, True)
                stats.uploaded_songs += int(uploaded)
                song_extended = link.get("songExtendedInfo")
                if not isinstance(song_extended, dict):
                    error(f"{song_location}.songExtendedInfo must be an object")
                    continue
                if song_extended.get("songId") != link.get("songId"):
                    error(f"{song_location} songId mismatch")
                name = song_extended.get("name")
                if not isinstance(name, str) or not name.strip():
                    error(f"{song_location}.songExtendedInfo.name is required")
                file_name = song_extended.get("fileName")
                has_audio = isinstance(file_name, str) and bool(file_name)
                if has_audio and PurePath(file_name).name != file_name:
                    error(
                        f"{song_location}.songExtendedInfo.fileName must be a basename"
                    )
                stats.songs_with_audio += int(has_audio)
                stats.uploaded_songs_with_audio += int(uploaded and has_audio)

    return errors, stats
