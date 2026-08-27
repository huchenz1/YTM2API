# Architecture

## Overview

One Python service. It speaks two protocols on its front: the Subsonic API that
music clients understand, and a small private HTTP API that predates it. Behind
both sits the same machinery — InnerTube for search, yt-dlp for stream
resolution, SQLite for the library.

```text
Subsonic client
     │  GET /rest/{action}.view?u=…&t=…&s=…&v=1.13.0&c=Amperfy
     ▼
subsonic.py  ── Subsonic layer, XML, auth, library endpoints
     │
     ├─ library.py   → SQLite: playlists, stars, song metadata
     └─ main.py      → search, stream resolution, byte proxying
             │
             ├─ search: POST music.youtube.com/youtubei/v1/search (WEB_REMIX, Songs filter)
             ├─ meta:   POST music.youtube.com/youtubei/v1/player (anonymous)
             └─ audio:  yt-dlp → googlevideo.com, bytes proxied through
```

`subsonic.py` knows nothing about yt-dlp or InnerTube; it reuses `main`.
`library.py` knows nothing about YouTube; it only knows SQL. `main.py` holds
every YouTube-shaped assumption in the project, which is why a break upstream
is a one-file fix.

## Why a server at all, and why it proxies bytes

Both answers were measured, not assumed (see [DESIGN-NOTES.md](DESIGN-NOTES.md)
D-006 and D-007):

- An anonymous `POST /youtubei/v1/player` with the `WEB_REMIX` client returns
  `playabilityStatus.status = UNPLAYABLE` for every track, with or without
  `visitorData`. Resolving a stream on the client device would require a
  PoToken from Google's anti-abuse system.
- The signed media URL includes an `ip` parameter, and that parameter is inside
  the signed `sparams`. The original URL returns `206`; the same URL with a
  changed `ip` returns `403`.

The first means resolution has to happen server-side. The second means the
result cannot simply be handed to the client as a redirect — it is bound to the
IP that obtained it. So the server **proxies the bytes**.

## The audio path

The server serves the `itag 140` AAC track (~129 kbps), but not in the
container it arrives in. googlevideo delivers it as *fragmented* mp4, in which
`AudioFileStreamSeek` fails and clients crash on seek — the full investigation
is [PITFALLS.md](PITFALLS.md) #14.

So the whole track is downloaded (0.15 s for ~5 MB) and repacked with
`ffmpeg -c:a copy -f adts`. That is a bitstream copy, not a re-encode: the same
AAC frames plus a 7-byte header each, +0.7% in size. Ranges are then cut from
the bytes in memory, which makes `Content-Length` and `Content-Range` exact.
The last three tracks stay cached.

Two details that are load-bearing rather than incidental:

- Upstream always gets `Range: bytes=0-`, even when the whole file is wanted.
  Without the header googlevideo throttles to ~32 KB/s; with it the same 5.2 MB
  file arrives in 0.15 s. A client that asked for no range gets its 206
  rewritten back to 200.
- Past the end of the file the answer is `416`, never `502`. Clients treat any
  status ≥ 300 as an error and restart the track; on a plain 416 they record
  end-of-file and move on.

## Subsonic layer

The full contract, endpoint by endpoint, is in [SUBSONIC.md](SUBSONIC.md).
The shape of it:

- **Library** is what you put in it — playlists and starred tracks, in SQLite.
  A track enters the library the moment it is added to a playlist or starred,
  and its metadata is fixed at that point.
- **Search** is a live query against YouTube Music that stores nothing.
- **Artists and albums** are derived from the library's `songs` table by
  grouping on `artist` and on (`artist`, `album`). They are not stored.

Identifiers: a song is its `videoId`; a playlist is its SQLite rowid; an artist
is `ar-{sha1(name)[:16]}`; an album is `al-{sha1(artist \0 album)[:16]}`.

Lists (`getArtists`, `getAlbumList2`) read **only** the library. Point lookups
(`getArtist`, `getAlbum`, `getSong`) also consult the search cache. Mixing the
cache into the lists would make entries appear from recent queries and vanish
after a restart, and clients read a disappearance as a deletion.

## Private HTTP API

Predates the Subsonic layer and is kept working. Useful if you want to build
your own client rather than use an existing one.

### `GET /search`

| | |
|---|---|
| Parameters | `q` — non-empty string; `limit` — 1..50, default 20; `continuation` — token from a previous response |
| 200 | `{"tracks": [Track, …], "continuation": "…"\|null}` |
| 400 | `{"error": "empty_query"}` — neither `q` nor `continuation` |
| 502 | `{"error": "upstream"}` — YouTube did not answer, or the answer did not parse |

```json
{
  "tracks": [
    {
      "id": "wU26xVT_vBU",
      "title": "One More Time",
      "artist": "Daft Punk",
      "artworkURL": "https://lh3.googleusercontent.com/…",
      "durationSeconds": 320
    }
  ]
}
```

`artworkURL` and `durationSeconds` may be `null`; `id`, `title` and `artist`
are always non-empty strings.

The request carries the "Songs" filter. Without it, a query about an artist
returns an artist card and a mixture of clips and albums: measured on one
query, **5** tracks against **20 per page** with the filter.

`continuation` is the next page's token, `null` means the end. Following a
token needs no `q` — the token already carries the query. The token is withheld
when the page was truncated by `limit`, since a client following it would skip
the cut-off tail.

### `GET /prefetch/{videoId}`

| | |
|---|---|
| 204 | URL resolved and cached, no body |
| 404 | track is not available anonymously |
| 502 | resolution failed |

A yt-dlp resolve takes ~2 s and is almost all the delay before sound. A client
calls this for the **next** track once the current one is playing.

Warm depth is **one track**, and that is a requirement, not an implementation
detail — warming a whole list produces a queue of resolves from one address,
which reads as a bot and earns a captcha ([PITFALLS.md](PITFALLS.md) #1).

### `GET /stream/{videoId}`

| | |
|---|---|
| Request header | `Range` — sliced locally; upstream always receives `bytes=0-` |
| 200 / 206 / 416 | audio bytes, `Content-Type: audio/aac`, `Accept-Ranges: bytes`, exact `Content-Length` and `Content-Range` |
| 404 | not available anonymously (region, age, privacy) |
| 502 | resolution failed |

## Caches

| Cache | Contents | Bound |
|---|---|---|
| InnerTube config | API key, client name and version, scraped from the homepage | one entry, refreshed on demand |
| Stream URLs | `videoId` → signed googlevideo URL, valid ~6 h | unbounded |
| ADTS audio | last repacked tracks | 3 tracks |
| Search metadata | `videoId` → title, artist, artwork, duration | 2000 entries, LRU |

The search metadata cache bridges `search3` and `updatePlaylist`: a client
sends only a `videoId` when adding a track, so without it the title and artist
would be lost. A miss is not fatal — `main.get_song_details` recovers them from
an anonymous `/youtubei/v1/player` call, which never involves yt-dlp.

The stream URL cache has no eviction. For one listener that is fine; it is
marked as such in the code.

## Storage

One SQLite file, `/data/rilyafy.db`, in the only writable mount. The container
itself stays `read_only: true`. Schema: `songs`, `playlists`, `playlist_items`,
`starred`, `spotify_map` — see [SUBSONIC.md](SUBSONIC.md) §4.

The journal is WAL, because there are two writers: the server and the separate
`spotify_import.py` process. In the default mode the importer would lock the
whole database and a client would hit `database is locked` mid-song.

Audio is never written to disk.

## Anti-captcha discipline

A queue of yt-dlp resolves from one address is the single most reliable way to
break this server, and the Subsonic layer makes it easier to trigger than the
private API did.

- Stream resolution is serialised behind one process-wide semaphore. Already
  cached URLs bypass it.
- Metadata never goes through yt-dlp. Duration, title, artist and artwork all
  come from search results or from `/youtubei/v1/player`.
- Bulk operations (the Spotify import) use the same path and pace themselves.
- Client-side offline caching must be off; the server cannot enforce this.

## Security and privacy

- No accounts, cookies or authorisation toward YouTube. Ever.
- Local Subsonic credentials are checked on every `/rest` request. The protocol
  sends them with each call, so redaction of the uvicorn access log is not
  optional — see [PITFALLS.md](PITFALLS.md) #10.
- Full media URLs are never logged: they contain a signature and an IP.
- The server writes no request history.
- It binds to loopback and expects a VPN, a tunnel or a reverse proxy in front.

## Tests

148 tests, none of which touch the network. Live tests are marked `live` and
deselected by default.

- Search parsing: fixture parsing, ATV filtering, config caching, pagination,
  de-duplication within and across pages.
- Streaming: range slicing, the always-`bytes=0-` upstream rule, 416 handling,
  retry on a cut download, repack-once caching, error code mapping.
- Subsonic: auth in both token and plaintext forms, every endpoint's XML shape,
  playlist reordering semantics, the derived artist/album tabs, credential
  redaction in logs.
- Library: insert-fills-gaps behaviour, playlist ordering, cascade deletes.
- Import: scoring thresholds against real mismatches, CSV parsing, re-import
  idempotency, manual mapping.
