# Changelog

All notable changes to Mirasonic are documented here.

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
