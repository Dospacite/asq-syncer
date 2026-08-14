# Master list creation

This tool rebuilds `seed/libraryMasterList.json` from public, ID-addressable
sources. It joins records by ANN ID, AMQ song ID, and AniList ID; it never uses
fuzzy title matching.

It also includes an Asgard-ready web application for publishing the list as
versioned snapshots. The application provides:

- an administrator login and anime browser;
- manual and scheduled background refreshes;
- a token-authenticated API for full-list downloads and incremental updates;
- persistent integer versions, SHA-256 checksums, and cached version-pair diffs.

## Web application

The administrator email defaults to `ege@rousoftware.com`. The password is a
deployment secret and is never stored in Compose or in the image. A random
local development password and random tokens can be stored in the ignored
`.env` file. Generate a fresh deployment set with:

```bash
openssl rand -base64 24  # ADMIN_PASSWORD
openssl rand -hex 32     # SESSION_SECRET
openssl rand -hex 32     # APP_TOKEN
```

Copy `.env.example` to `.env`, replace all placeholder secrets, and keep
`COOKIE_SECURE=true` anywhere HTTPS terminates at the Asgard proxy.

Start the web process and worker locally:

```bash
docker compose up --build
```

The web service listens on container port `8080`; its readiness endpoint is
`GET /healthz`. The private worker listens on `8081` only for `GET /healthz`.
Both processes write structured operational output to stdout/stderr and handle
SIGTERM within the container grace period. Durable snapshots, SQLite state,
upstream response caches, and computed differences live in the named
`anime-list-data` volume.

For local browser access, add a temporary `ports: ["8080:8080"]` entry to the
web service or use a Compose override. Asgard routes the public primary service
without a host port.

### Public client API

Send the embedded app token in a header. Tokens in query strings are not
accepted because URLs are commonly logged:

```bash
curl -H "Authorization: Bearer $APP_TOKEN" \
  https://<project>.asgard.rousoftware.com/api/v1/anime-list/version

curl -H "Authorization: Bearer $APP_TOKEN" \
  https://<project>.asgard.rousoftware.com/api/v1/anime-list/updates?fromVersion=1

curl -H "Authorization: Bearer $APP_TOKEN" \
  https://<project>.asgard.rousoftware.com/api/v1/anime-list \
  --output libraryMasterList.json
```

`X-App-Token` is also supported. The small version endpoint returns the latest
application version, upstream source version, checksum, record count, and
publish time. The full-list endpoint returns the existing application JSON and
adds `X-Anime-List-Version` and `X-Anime-List-Checksum` response headers.

Incremental responses use this stable contract:

```json
{
  "schemaVersion": 1,
  "fromVersion": 1842,
  "toVersion": 1845,
  "upserts": [
    {
      "id": 123,
      "annId": 123,
      "names": [{"name": "Steins;Gate", "language": "EN"}],
      "songLinks": {}
    }
  ],
  "deletes": [789, 901],
  "checksum": "sha256:abc123..."
}
```

Each upsert contains the complete application anime record plus its numeric
map-key `id`. Apply all upserts by ID, remove every delete ID, then SHA-256 hash
the resulting `animeMap` encoded as UTF-8 canonical JSON (object keys sorted,
no insignificant whitespace, Unicode unescaped). The checksum describes that
target map. If verification fails, fall back to the full-list endpoint.

Differences are immutable and cached by `(fromVersion, toVersion)`. The worker
eagerly computes each adjacent difference after publication; less common pairs
are computed on the first request and served from disk thereafter. Asking for
the current version returns an empty update with the current checksum.

### Background updates

The worker runs immediately at startup and then every
`UPDATE_INTERVAL_SECONDS` (six hours by default). It first downloads the AMQ
source header/list and compares `masterListId` with the published source
version. If unchanged, it stops there. A changed version is saved once, passed
through the existing enrichment/validation pipeline, atomically committed as
the next integer application version, and diffed from its predecessor. The
admin “Queue list update” button inserts a deduplicated job in the same queue.

### Deploying on Asgard

Import the repository using `compose.yaml`. The Compose
file follows the current Asgard safe subset and declares `web` as its public
primary service and `worker` as private. Before deployment, configure these
required values in the project environment:

| Variable | Required | Purpose |
| --- | --- | --- |
| `ADMIN_PASSWORD` | yes | Random administrator password |
| `SESSION_SECRET` | yes | At least 32 random bytes for signed sessions |
| `APP_TOKEN` | yes | Token embedded in trusted app builds |
| `ADMIN_EMAIL` | no | Defaults to `ege@rousoftware.com` |
| `UPDATE_INTERVAL_SECONDS` | no | Refresh interval; default `21600` |
| `COOKIE_SECURE` | no | Keep `true` behind Asgard HTTPS |

Rotate `APP_TOKEN` by deploying the new value before releasing a client that
uses it. A token embedded in a public binary is an access gate, not a private
user credential; use Asgard rate controls at the public edge if abuse becomes
material.

## Sources and field ownership

| Source | Fields used |
| --- | --- |
| [Anime Music Quiz master list](https://animemusicquiz.com/libraryMasterList) | Anime names, year/type, song links, core song/artist/group records, and `masterListId` |
| [AniSongDB API](https://anisongdb.com/docs) | ANN-to-MAL/AniList/Kitsu links, song difficulty, duration, and audio/video file names |
| [AniList GraphQL API](https://docs.anilist.co/guide/graphql/) | Genres, tags whose relevance rank meets `--min-tag-rank`, and fallback cover images |
| [Anime News Network Encyclopedia API](https://www.animenewsnetwork.com/encyclopedia/api.php) | Primary and alternate encyclopedia images with dimensions |

The generated JSON contains the app-consumed `animeMap` and the source
`masterListId` needed to reproduce it. The large AMQ `songMap`, `artistMap`, and
`groupMap` are folded into each song link.

ANN permits batches of at most 50 titles and documents a limit of one request
per second. AniList currently advertises 30 requests per minute. The builder
enforces both limits. If ANN has no picture, the already-fetched AniList cover
is used instead. If a catalog link is unavailable, its ID is written as `-1`
so the Dart models can safely consume the record.

## Usage

No third-party Python packages are required. From the repository root:

```bash
python3 create_master_list.py update
```

`update` downloads the current AMQ master list, enriches it, validates it, and
atomically replaces `seed/libraryMasterList.json`. The first full update can
take several minutes because of upstream rate limits.
Every response batch is cached under
`.cache/<masterListId>/`; rerunning the same AMQ version is
fast and resumes an interrupted build. A new AMQ `masterListId` automatically
gets a fresh cache namespace.

Useful variants:

```bash
# Ten-anime smoke build without the rate-limited ANN source
python3 create_master_list.py build \
  --limit 10 --skip-ann-images --output /tmp/libraryMasterList.sample.json

# Re-fetch every upstream batch for the current AMQ version
python3 create_master_list.py update --refresh

# Reproduce a build from a saved raw AMQ response
python3 create_master_list.py build \
  --source-file /path/to/raw-libraryMasterList.json

# Use a raw snapshot and an already-populated cache without network access
python3 create_master_list.py build \
  --source-file /path/to/raw-libraryMasterList.json --offline

# Validate the checked-in application asset
python3 create_master_list.py validate
```

`--strict` additionally requires every anime to have linked IDs, AniList
metadata, and an image, and every uploaded song to have an audio file. This is
appropriate for CI only when all upstream databases are known to be in sync;
normal builds tolerate newly-added AMQ entries that have not propagated yet.

`build` has the same options as `update`, but is useful when writing a sample or
alternate output through `--output`. Run `update --help` for all options. Output
is minified by default to keep the Flutter asset small; pass `--pretty` for
inspection.

## Validation and tests

The builder validates map keys, catalog ID types, genre/tag lists, nested song
records, and song ID consistency before atomically replacing the output file.
To run the unit tests:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

When ANN-derived data is displayed in a user-facing surface, follow the
attribution and Encyclopedia-link requirements in the
[ANN API terms](https://www.animenewsnetwork.com/encyclopedia/api.php).
