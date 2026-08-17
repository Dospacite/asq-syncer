from __future__ import annotations

import unittest

from master_list_creation.builder import build_master_list
from master_list_creation.validation import validate_master_list


class BuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = {
            "masterListId": "test-1",
            "animeMap": {
                "1": {
                    "annId": 1,
                    "category": {"name": "TV", "number": None},
                    "year": 1999,
                    "seasonId": 1,
                    "names": [{"name": "Angel Links", "language": "EN"}],
                    "mainNames": {"EN": "Angel Links", "JA": None},
                    "songLinks": {
                        "OP": [
                            {
                                "songId": 10,
                                "annSongId": 100,
                                "number": 1,
                                "type": 1,
                                "uploaded": 1,
                            }
                        ],
                        "ED": [],
                        "INS": [],
                    },
                },
                "2": {
                    "annId": 2,
                    "category": {"name": "Movie", "number": None},
                    "year": 2000,
                    "seasonId": 2,
                    "names": [{"name": "Fallback", "language": "EN"}],
                    "mainNames": {"EN": "Fallback", "JA": None},
                    "songLinks": {"OP": [], "ED": [], "INS": []},
                },
            },
            "songMap": {
                "10": {
                    "songId": 10,
                    "name": "All My Soul",
                    "songArtistId": 20,
                    "songGroupId": None,
                    "composerArtistId": None,
                    "composerGroupId": 30,
                    "arrangerArtistId": None,
                    "arrangerGroupId": None,
                    "category": 4,
                }
            },
            "artistMap": {"20": {"songArtistId": 20, "name": "NAW NAW"}},
            "groupMap": {"30": {"groupId": 30, "name": "Composer Group"}},
        }
        self.links = {
            "1": {
                "linked_ids": {
                    "annId": 1,
                    "myanimelist": 1226,
                    "anilist": 1226,
                    "kitsu": 1103,
                }
            },
            "2": {
                "linked_ids": {
                    "annId": 2,
                    "myanimelist": 2,
                    "anilist": 2,
                    "kitsu": 2,
                }
            },
        }
        self.songs = [
            {
                "annId": 1,
                "annSongId": 100,
                "amqSongId": 10,
                "songName": "All My Soul",
                "songArtist": "fallback artist",
                "songComposer": "fallback composer",
                "songArranger": "",
                "songDifficulty": 16.6,
                "songLength": 88.9,
                "audio": "audio.mp3",
                "MQ": "medium.webm",
                "HQ": None,
                "linked_ids": self.links["1"]["linked_ids"],
            }
        ]
        self.anilist = {
            1226: {
                "id": 1226,
                "genres": ["Adventure", "Sci-Fi"],
                "tags": [
                    {"name": "Space", "rank": 80},
                    {"name": "Low relevance", "rank": 20},
                ],
                "coverImage": {
                    "medium": "https://example.com/small.jpg",
                    "large": "https://example.com/medium.jpg",
                    "extraLarge": "https://example.com/large.jpg",
                },
            },
            2: {
                "id": 2,
                "genres": ["Drama"],
                "tags": [],
                "coverImage": {
                    "medium": "https://example.com/fallback-small.jpg",
                    "large": "https://example.com/fallback.jpg",
                    "extraLarge": "https://example.com/fallback-extra.jpg",
                },
            },
        }
        self.images = {
            1: [
                {
                    "src": "https://example.com/ann-small.jpg",
                    "width": 100,
                    "height": 100,
                },
                {
                    "src": "https://example.com/ann.jpg",
                    "width": 200,
                    "height": 200,
                },
                {
                    "src": "https://example.com/ann-unknown.jpg",
                    "width": None,
                    "height": None,
                },
            ],
            2: [],
        }

    def test_builds_application_schema(self) -> None:
        result, stats = build_master_list(
            self.raw,
            linked_entries=self.links,
            anisongdb_songs=self.songs,
            anilist_metadata=self.anilist,
            ann_images=self.images,
        )

        self.assertEqual(set(result), {"masterListId", "animeMap"})
        anime = result["animeMap"]["1"]
        self.assertEqual(anime["animeExtendedInfo"]["malId"], 1226)
        self.assertEqual(anime["animeExtendedInfo"]["genres"], ["Adventure", "Sci-Fi"])
        self.assertEqual(anime["animeExtendedInfo"]["tags"], ["Space"])
        # ANN offers nothing above 200x200 here, so the 460px AniList cover wins,
        # and both sources are ranked together in one largest-first list.
        self.assertEqual(anime["annImage"], "https://example.com/large.jpg")
        self.assertEqual(anime["imageSource"], "anilist")
        self.assertEqual(
            [image["src"] for image in anime["annImages"]],
            [
                "https://example.com/large.jpg",
                "https://example.com/medium.jpg",
                "https://example.com/ann.jpg",
                "https://example.com/ann-small.jpg",
                "https://example.com/small.jpg",
                "https://example.com/ann-unknown.jpg",
            ],
        )

        song = anime["songLinks"]["OP"][0]
        self.assertEqual(song["songArtistName"], "NAW NAW")
        self.assertEqual(song["composerGroupName"], "Composer Group")
        self.assertEqual(song["songExtendedInfo"]["globalPercent"], 17)
        self.assertEqual(song["songExtendedInfo"]["fileName"], "audio.mp3")
        self.assertEqual(
            song["songExtendedInfo"]["fileNameMap"],
            {"0": "audio.mp3", "480": "medium.webm"},
        )

        fallback = result["animeMap"]["2"]
        self.assertEqual(
            fallback["annImage"], "https://example.com/fallback-extra.jpg"
        )
        self.assertEqual(fallback["imageSource"], "anilist")
        self.assertEqual(stats.anime, 2)
        self.assertEqual(stats.songs, 1)
        self.assertEqual(stats.songs_with_media, 1)

        errors, validation_stats = validate_master_list(result)
        self.assertEqual(errors, [])
        self.assertEqual(validation_stats.songs_with_audio, 1)
        self.assertEqual(validation_stats.uploaded_songs_with_audio, 1)

    def test_largest_ann_image_beats_the_anilist_cover(self) -> None:
        self.images[1] = [
            {"src": "https://example.com/ann-thumb.jpg", "width": 200, "height": 200},
            {"src": "https://example.com/ann-full.jpg", "width": 900, "height": 1200},
        ]
        result, _ = build_master_list(
            self.raw,
            linked_entries=self.links,
            anisongdb_songs=self.songs,
            anilist_metadata=self.anilist,
            ann_images=self.images,
        )

        anime = result["animeMap"]["1"]
        self.assertEqual(anime["annImage"], "https://example.com/ann-full.jpg")
        self.assertEqual(anime["annImages"][0]["source"], "ann")
        self.assertNotIn("imageSource", anime)

    def test_missing_ids_use_negative_one(self) -> None:
        result, _ = build_master_list(
            self.raw,
            linked_entries={},
            anisongdb_songs=[],
            anilist_metadata={},
            ann_images={},
            limit=1,
        )
        extended = result["animeMap"]["1"]["animeExtendedInfo"]
        self.assertEqual(extended["aniListId"], -1)
        self.assertEqual(extended["malId"], -1)
        self.assertEqual(extended["kitsuId"], -1)


if __name__ == "__main__":
    unittest.main()
