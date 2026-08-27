"""The server's persistent library — playlists and starred songs
(docs/SUBSONIC.md §4, phase 2). The schema is copied from the contract as-is,
not reinvented here.

Neither InnerTube nor yt-dlp is mentioned in this file: the caller
(subsonic.py) resolves a track's metadata itself (from the search cache or
main.get_song_details) and passes finished values in. library.py knows only
about SQLite.
"""
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

DEFAULT_DB_PATH = "/data/mirasonic.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
  id             TEXT PRIMARY KEY,   -- videoId
  title          TEXT NOT NULL,
  artist         TEXT NOT NULL,
  album          TEXT,               -- NULL when InnerTube gave none
  duration       INTEGER,            -- seconds; NULL until resolved
  artwork_url    TEXT,
  added_at       TEXT NOT NULL       -- ISO 8601 with milliseconds, UTC
);

CREATE TABLE IF NOT EXISTS playlists (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_items (
  playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  position    INTEGER NOT NULL,      -- 0-based, no gaps
  song_id     TEXT    NOT NULL REFERENCES songs(id),
  PRIMARY KEY (playlist_id, position)
);

CREATE TABLE IF NOT EXISTS starred (
  song_id    TEXT PRIMARY KEY REFERENCES songs(id),
  starred_at TEXT NOT NULL
);

-- What has already been matched to YouTube: Spotify playlists get refreshed
-- monthly, and without this table every re-import would search for all
-- hundred-odd tracks again. It also survives hand editing: a correction stays
-- corrected.
CREATE TABLE IF NOT EXISTS spotify_map (
  spotify_uri TEXT PRIMARY KEY,
  song_id     TEXT NOT NULL REFERENCES songs(id),
  mapped_at   TEXT NOT NULL
);
"""


def _now_iso() -> str:
    """Same format as subsonic._iso_created — Amperfy parses `created` only
    with milliseconds (ISO8601DateFormatter.withFractionalSeconds)."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class Library:
    """One connection per process; writes go through a shared lock.

    A second writer exists (`spotify_import.py`, a separate process), so the
    journal is set to WAL: in the default mode a writer locks the whole
    database, and importing a hundred tracks would break playback with
    `database is locked` mid-song. Under WAL, readers never wait for a writer.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.environ.get("MIRASONIC_DB", DEFAULT_DB_PATH)
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")  # or ON DELETE CASCADE stays silent
        self._conn.execute("PRAGMA journal_mode = WAL")  # reader and writer stop blocking each other
        self._conn.execute("PRAGMA busy_timeout = 5000")  # wait for another transaction instead of failing
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    # -- songs ---------------------------------------------------------

    def _insert_song_unlocked(self, song_id, title, artist, album, duration, artwork_url):
        """Only while self._lock is already held — Lock is not reentrant.

        Re-adding the same track does not overwrite what is already known, but
        fills in what is missing: duration and artwork may have been unknown
        when the track first entered the database (a search-cache miss), and
        this is the second chance to record them. INSERT OR IGNORE simply threw
        that chance away.
        """
        self._conn.execute(
            "INSERT INTO songs (id, title, artist, album, duration, artwork_url, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  title       = COALESCE(NULLIF(songs.title, ''), excluded.title),"
            "  artist      = COALESCE(NULLIF(songs.artist, ''), excluded.artist),"
            "  album       = COALESCE(songs.album, excluded.album),"
            "  duration    = COALESCE(songs.duration, excluded.duration),"
            "  artwork_url = COALESCE(songs.artwork_url, excluded.artwork_url)",
            (song_id, title or "", artist or "", album, duration, artwork_url, _now_iso()),
        )

    def upsert_song(self, song_id: str, title: str, artist: str, album=None,
                    duration=None, artwork_url=None) -> None:
        """Record a track without attaching it to a playlist or a star."""
        with self._lock:
            self._insert_song_unlocked(song_id, title, artist, album, duration, artwork_url)
            self._conn.commit()

    def get_song(self, song_id: str) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        return dict(row) if row else None

    def get_songs(self) -> list[dict]:
        """The whole library in one query — subsonic.py builds artists and
        albums out of it (phase 3). Three hundred rows; grouping that in SQL
        for savings nobody can measure is not worth it."""
        return [dict(row) for row in self._conn.execute("SELECT * FROM songs")]

    def get_random_songs(self, size: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM songs ORDER BY RANDOM() LIMIT ?", (size,)
        ).fetchall()
        return [dict(row) for row in rows]

    # -- playlists -------------------------------------------------------

    def get_playlists(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT p.id, p.name, p.created_at, p.changed_at, "
            "COUNT(pi.song_id) AS song_count, COALESCE(SUM(s.duration), 0) AS duration "
            "FROM playlists p "
            "LEFT JOIN playlist_items pi ON pi.playlist_id = p.id "
            "LEFT JOIN songs s ON s.id = pi.song_id "
            "GROUP BY p.id ORDER BY p.id"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_playlist(self, playlist_id: int) -> Optional[dict]:
        prow = self._conn.execute(
            "SELECT id, name, created_at, changed_at FROM playlists WHERE id = ?",
            (playlist_id,),
        ).fetchone()
        if prow is None:
            return None
        items = self._conn.execute(
            "SELECT s.* FROM playlist_items pi JOIN songs s ON s.id = pi.song_id "
            "WHERE pi.playlist_id = ? ORDER BY pi.position",
            (playlist_id,),
        ).fetchall()
        songs = [dict(row) for row in items]
        duration = sum(song.get("duration") or 0 for song in songs)
        result = dict(prow)
        result["songs"] = songs
        result["song_count"] = len(songs)
        result["duration"] = duration
        return result

    def create_playlist(self, name: str) -> int:
        with self._lock:
            now = _now_iso()
            cur = self._conn.execute(
                "INSERT INTO playlists (name, created_at, changed_at) VALUES (?, ?, ?)",
                (name, now, now),
            )
            self._conn.commit()
            return cur.lastrowid

    def delete_playlist(self, playlist_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def update_playlist(self, playlist_id: int, name: str,
                        remove_indices: list[int],
                        add_songs: list[tuple]) -> bool:
        """`remove_indices` are positions in the list as it was BEFORE this call.
        `add_songs` is [(song_id, title, artist, album, duration, artwork_url), …],
        appended at the end in the given order.

        The order is mandatory (docs/SUBSONIC.md §5): every index is resolved
        at once against the pre-operation state, and only then do the additions
        happen. Amperfy reorders a playlist by removing `0..n-1` and re-adding
        the whole list — recomputing indices while deleting one at a time
        breaks that.
        """
        with self._lock:
            prow = self._conn.execute(
                "SELECT id FROM playlists WHERE id = ?", (playlist_id,)
            ).fetchone()
            if prow is None:
                return False

            current = [r["song_id"] for r in self._conn.execute(
                "SELECT song_id FROM playlist_items WHERE playlist_id = ? ORDER BY position",
                (playlist_id,),
            ).fetchall()]

            remove_set = set(remove_indices)
            kept = [song_id for i, song_id in enumerate(current) if i not in remove_set]

            for song_id, title, artist, album, duration, artwork_url in add_songs:
                self._insert_song_unlocked(song_id, title, artist, album, duration, artwork_url)
                kept.append(song_id)

            self._conn.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,))
            self._conn.executemany(
                "INSERT INTO playlist_items (playlist_id, position, song_id) VALUES (?, ?, ?)",
                [(playlist_id, position, song_id) for position, song_id in enumerate(kept)],
            )
            self._conn.execute(
                "UPDATE playlists SET name = ?, changed_at = ? WHERE id = ?",
                (name, _now_iso(), playlist_id),
            )
            self._conn.commit()
            return True

    # -- Spotify mappings -------------------------------------------------

    def get_spotify_map(self) -> dict[str, str]:
        return {r["spotify_uri"]: r["song_id"] for r in
                self._conn.execute("SELECT spotify_uri, song_id FROM spotify_map")}

    def put_spotify_map(self, pairs: list[tuple[str, str]]) -> None:
        """`pairs` is [(spotify_uri, song_id), …]. The track must already be in songs."""
        with self._lock:
            self._conn.executemany(
                "INSERT INTO spotify_map (spotify_uri, song_id, mapped_at) VALUES (?, ?, ?) "
                "ON CONFLICT(spotify_uri) DO UPDATE SET song_id = excluded.song_id, "
                "mapped_at = excluded.mapped_at",
                [(uri, song_id, _now_iso()) for uri, song_id in pairs],
            )
            self._conn.commit()

    # -- starred ---------------------------------------------------------

    def star(self, song_id: str, title: str, artist: str, album=None,
             duration=None, artwork_url=None) -> None:
        with self._lock:
            self._insert_song_unlocked(song_id, title, artist, album, duration, artwork_url)
            self._conn.execute(
                "INSERT OR REPLACE INTO starred (song_id, starred_at) VALUES (?, ?)",
                (song_id, _now_iso()),
            )
            self._conn.commit()

    def unstar(self, song_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM starred WHERE song_id = ?", (song_id,))
            self._conn.commit()

    def get_starred(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT s.*, st.starred_at FROM starred st JOIN songs s ON s.id = st.song_id "
            "ORDER BY st.starred_at"
        ).fetchall()
        return [dict(row) for row in rows]
