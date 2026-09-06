# Changelog

All notable changes to Mirasonic are documented here.

## [Unreleased] — client compatibility

### Added

- Bare `/rest/{action}` paths (no `.view` suffix) are routed identically —
  the suffix has been optional since Subsonic API 1.8, and clients like
  ListenNow send only the bare form and failed the handshake with 404.
- `f=json` (and `jsonp`, as plain JSON) now returns the official Subsonic
  JSON convention: attributes as fields, collection children as arrays,
  singletons as objects. XML remains the default and is unchanged.

## [Unreleased] — YTM login fork

### Added

- Optional YouTube Music login via `YTM_COOKIES_FILE` (`ytm_auth.py`,
  docs/LOGIN.md): stream resolution runs with the account's privileges —
  up to ~256 kbps AAC on a Premium account instead of the anonymous ~129,
  plus age-restricted tracks. The cookie file is handed to yt-dlp as a
  fresh writable copy per re-export; the original is never opened for
  writing. Unset stays fully anonymous, byte for byte the upstream behavior.
- `ytm_sync.py`: pulls the account's liked songs (as starred tracks) and
  playlists (as local playlists) into the library through ytmusicapi — one
  account API call per playlist, no search, no yt-dlp. Strictly additive and
  one-way; the `ytm_playlists` table keeps re-syncs on the same local
  playlist across renames.
- ADTS remux now falls back to a one-pass AAC re-encode when a resolved
  stream is not AAC at all (Opus fallback of `bestaudio`), instead of 502ing
  the track.

## [0.2.0] - 2026-08-31

### Added

- Optional weekly discovery agent (`--profile agent`): syncs listening
  history to ListenBrainz, requests recommendations and fresh releases, and
  builds a `Discoveries` playlist from tracks it can match on YouTube Music.
  Off by default; the playback server never imports it or needs its
  credentials.
- Local, network-free library ranking (`music_agent.py rankings`) by play
  frequency, recency, and starred status.
- Real `scrobble.view` handling: playing-now pings from clients that never
  send `submission=true` (Amperfy included) now credit a listen.

### Fixed

- Scrobbles synced to ListenBrainz hourly instead of only once a completed
  discovery week rolled over, which had delayed a Monday-afternoon listen by
  up to two weeks before it could affect recommendations.
- The agent now logs at INFO level, so a successful run leaves a trace
  instead of looking identical to a container that never started.

### Known limitations

- Other Subsonic clients have not been verified yet.
- Age-restricted tracks cannot be resolved anonymously.
- There is no multi-user support, transcoding, lyrics, podcasts, or radio.
- YouTube internal interfaces can change without notice and temporarily break playback.

[0.2.0]: https://github.com/rilya888/Mirasonic/releases/tag/v0.2.0

## [0.1.0] - 2026-08-27

### Added

- Anonymous YouTube Music search through the Subsonic API.
- AAC streaming through a self-hosted FastAPI service.
- Persistent playlists, starred tracks, artists, and albums in SQLite.
- Spotify playlist import from Exportify CSV files.
- Docker Compose deployment with a read-only container filesystem.
- Compatibility with Amperfy on iOS.

### Known limitations

- Other Subsonic clients have not been verified yet.
- Age-restricted tracks cannot be resolved anonymously.
- There is no multi-user support, transcoding, lyrics, podcasts, or radio.
- YouTube internal interfaces can change without notice and temporarily break playback.

[0.1.0]: https://github.com/rilya888/Mirasonic/releases/tag/v0.1.0
