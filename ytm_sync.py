"""Pulls the logged-in account's YouTube Music library into the server's
SQLite library: liked songs (as starred tracks) and playlists.

Counterpart of spotify_import.py — with the hard part missing. A YTM playlist
track carries its videoId, which is this server's primary key: there is no
search, no scoring, no matching. And no bot-profile risk either (pitfall #1):
one account API call per playlist, zero yt-dlp.

Requires YTM_COOKIES_FILE (docs/LOGIN.md). ytmusicapi reads the same file via
a Cookie header built here. Run it inside the container, where both the file
and the database live:

    docker compose exec worker python ytm_sync.py likes
    docker compose exec worker python ytm_sync.py playlists
    docker compose exec worker python ytm_sync.py all --dry-run

What this deliberately does NOT do (same philosophy as spotify_import.py):

- it never resolves a stream;
- it never deletes anything. A track un-liked on YouTube keeps its local
  star; a track removed from a YTM playlist keeps its place in the local one.
  Starring and playlist edits are the client's territory — this only ever
  adds, in YTM order, at the end;
- it never writes back to YouTube. star/unstar in the Subsonic client are
  local facts, not API calls against the account.
"""
import argparse
import asyncio
import os
import sys
from typing import Optional

import library
import main
import ytm_auth

# Read once at import — the sync is a one-shot process, not a service.
REGION = os.environ.get("REGION", "US")

# Playlist ids that are views, not playlists: "LM" is the auto-generated
# "Your Likes" shelf ytmusicapi's library listing can include. sync likes
# handles that content; a local playlist for it would be a snapshot
# pretending to be live.
AUTO_PLAYLIST_PREFIXES = ("LM",)


def _browser_headers(cookies: list[dict]) -> dict:
    """Browser-shaped auth headers ytmusicapi accepts.

    ytmusicapi classifies the auth by the presence of an Authorization header
    containing "SAPISIDHASH" — without it a Cookie-only dict is read as an
    OAuth token and refused. The value here is a marker only: for browser
    auth ytmusicapi regenerates the real SAPISIDHASH on every request from
    the __Secure-3PAPISID cookie and the origin header, both of which must be
    present.
    """
    return {
        "Cookie": "; ".join(f"{c['name']}={c['value']}" for c in cookies),
        "User-Agent": main.USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "X-Goog-AuthUser": "0",
        "X-Origin": "https://music.youtube.com",
        "Origin": "https://music.youtube.com",
        "Authorization": "SAPISIDHASH",
    }


def build_client(cookie_path: str):
    """A ytmusicapi client speaking with the account's cookies.

    Imported here, not at module level: the worker shares this image but
    never needs it.
    """
    from ytmusicapi import YTMusic

    return YTMusic(auth=_browser_headers(ytm_auth.parse_netscape(cookie_path)),
                   location=REGION)


def track_row(track: dict) -> Optional[tuple]:
    """A ytmusicapi track as an upsert row, or None without a videoId.

    `(song_id, title, artist, album, duration, artwork_url)` — the same
    shape library.update_playlist and library.star expect. Album is None for
    singles and for video-type tracks; duration_seconds is absent on some
    older payloads, where the 'M:SS' string is parsed instead.
    """
    video_id = track.get("videoId")
    if not video_id:
        return None
    duration = track.get("duration_seconds")
    if duration is None and isinstance(track.get("duration"), str):
        duration = main.parse_duration(track["duration"])
    return (
        video_id,
        track.get("title") or video_id,
        ", ".join(a["name"] for a in track.get("artists") or [] if a.get("name")),
        (track.get("album") or {}).get("name"),
        duration,
        main._best_thumbnail(track.get("thumbnails") or []),
    )


def _upsert_rows(lib: library.Library, rows: list[tuple]) -> None:
    for row in rows:
        lib.upsert_song(*row)


async def sync_likes(lib: library.Library, ytm, dry_run: bool = False) -> dict:
    """Liked songs → starred tracks, oldest like first (the YTM order)."""
    response = await asyncio.to_thread(ytm.get_liked_songs)
    tracks = response.get("tracks") or []
    starred = {song["id"] for song in lib.get_starred()}

    rows = [row for row in (track_row(t) for t in tracks) if row is not None]
    fresh = [row for row in rows if row[0] not in starred]
    if not dry_run:
        _upsert_rows(lib, fresh)
        for row in fresh:
            lib.star(*row)
    return {
        "kind": "likes",
        "total": len(tracks),
        "resolved": len(rows),
        "added": len(fresh),
        "already_starred": len(rows) - len(fresh),
    }


def _local_playlist_id(lib: library.Library, mapping: dict[str, int],
                       ytm_id: str, name: str, dry_run: bool) -> Optional[int]:
    """By mapping first; a stale mapping (playlist deleted in the client)
    recreates instead of failing. Only the mapping decides identity — a
    same-named hand-made playlist is never adopted."""
    playlist_id = mapping.get(ytm_id)
    if playlist_id is not None and lib.get_playlist(playlist_id) is not None:
        return playlist_id
    if dry_run:
        return None
    return lib.create_playlist(name)


async def sync_playlists(lib: library.Library, ytm, dry_run: bool = False) -> dict:
    mapping = {} if dry_run else lib.get_ytm_playlist_map()
    summaries = await asyncio.to_thread(ytm.get_library_playlists)

    results = []
    for summary in summaries or []:
        ytm_id = summary.get("playlistId") or ""
        if not ytm_id or ytm_id.startswith(AUTO_PLAYLIST_PREFIXES):
            continue
        name = summary.get("title") or ytm_id
        detail = await asyncio.to_thread(ytm.get_playlist, ytm_id)
        rows = [row for row in (track_row(t) for t in detail.get("tracks") or [])
                if row is not None]

        playlist_id = _local_playlist_id(lib, mapping, ytm_id, name, dry_run)
        if playlist_id is None:
            # Dry run against a playlist that does not exist yet: every row
            # would land in the playlist the real run creates.
            results.append({"ytm_id": ytm_id, "name": name, "total": len(rows),
                            "added": len(rows), "skipped_existing": 0,
                            "created": False, "dry_run": True})
            continue

        existing = {song["id"] for song in
                    (lib.get_playlist(playlist_id) or {}).get("songs", [])}
        to_add = [row for row in rows if row[0] not in existing]
        created = mapping.get(ytm_id) != playlist_id
        if not dry_run:
            _upsert_rows(lib, to_add)
            if to_add:
                lib.update_playlist(playlist_id, name, [], to_add)
            if created:
                lib.put_ytm_playlist_map([(ytm_id, playlist_id)])
        results.append({
            "ytm_id": ytm_id, "name": name, "total": len(rows),
            "added": len(to_add), "skipped_existing": len(rows) - len(to_add),
            "created": created, "dry_run": False,
        })
    return {"kind": "playlists", "playlists": results}


async def whoami(ytm) -> int:
    """Prints which account the cookies are signed in as — the operational
    check for 'is my export still alive'. A session Google has signed out
    (rotation, a new export from the wrong profile, cookie staleness) gets a
    menu without any active account, which ytmusicapi surfaces as a
    navigation error; that is a diagnosis, not a crash."""
    try:
        info = await asyncio.to_thread(ytm.get_account_info)
    except Exception as exc:
        print(f"no active session — the cookies are signed out or invalid "
              f"({type(exc).__name__}). Re-export and re-upload (docs/LOGIN.md).")
        return 1
    runs = info.get("accountName") or []
    name = "".join(r.get("text", "") for r in runs if isinstance(r, dict))
    print(f"logged in as: {name or info}")
    return 0


def print_likes_report(report: dict) -> None:
    print(f"\nlikes: {report['total']} tracks in the account")
    print(f"  with videoId:   {report['resolved']}")
    print(f"  starred now:    {report['added']}")
    print(f"  already starred: {report['already_starred']}")


def print_playlists_report(report: dict) -> None:
    for item in report["playlists"]:
        state = "created" if item["created"] else "updated"
        suffix = " (dry run)" if item.get("dry_run") else ""
        print(f"\n{item['name']} [{item['ytm_id']}]: {item['total']} tracks")
        print(f"  {state}{suffix}:           {item['added']}")
        print(f"  already in playlist: {item['skipped_existing']}")


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Sync the logged-in YouTube Music account (likes, playlists)")
    parser.add_argument("what", choices=("likes", "playlists", "all", "whoami"))
    parser.add_argument("--dry-run", action="store_true",
                        help="report only, write nothing")
    args = parser.parse_args(argv)

    source = ytm_auth.cookies_path()
    if source is None:
        parser.error(f"{ytm_auth.ENV_VAR} must be set in the environment "
                     "(docs/LOGIN.md)")
    cookies = ytm_auth.parse_netscape(source)
    if not cookies:
        parser.error(f"{source} holds no parseable cookies — re-export it "
                     "(docs/LOGIN.md)")

    lib = library.Library() if args.what != "whoami" else None
    ytm = build_client(source)

    if args.what == "whoami":
        return await whoami(ytm)
    if args.what in ("likes", "all"):
        report = await sync_likes(lib, ytm, args.dry_run)
        print_likes_report(report)
    if args.what in ("playlists", "all"):
        report = await sync_playlists(lib, ytm, args.dry_run)
        print_playlists_report(report)
    if args.dry_run:
        print("\n--dry-run: nothing was written to the database")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
