"""ytm_sync.py — likes → stars, playlists → playlists, all offline.

The ytm client is injected (`sync_likes(lib, ytm)`), so every test drives a
stub with the same three-method surface ytmusicapi exposes and none of them
touches the network or imports ytmusicapi at all.
"""
import asyncio

import pytest

import library
import ytm_sync


def track(video_id, title="T", artist="A", album="AL", seconds=200):
    return {
        "videoId": video_id,
        "title": title,
        "artists": [{"name": artist}],
        "album": {"name": album} if album else None,
        "duration_seconds": seconds,
        "thumbnails": [{"url": f"img/{video_id}-small", "width": 100, "height": 100},
                       {"url": f"img/{video_id}-big", "width": 500, "height": 500}],
    }


def parseable_cookies(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(
        "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t2000000000\tSAPISID\tsv\n",
        encoding="utf-8")
    return str(path)


class FakeYTM:
    """The three calls ytm_sync makes, canned."""

    def __init__(self, liked=None, playlists=None, playlist_details=None):
        self.liked = {"tracks": liked or []}
        self.playlists = playlists or []
        self.playlist_details = playlist_details or {}

    def get_liked_songs(self):
        return self.liked

    def get_library_playlists(self):
        return self.playlists

    def get_playlist(self, playlist_id):
        return self.playlist_details[playlist_id]


# -- track_row -------------------------------------------------------------

def test_browser_headers_satisfy_ytmusicapi_browser_detection(tmp_path, monkeypatch):
    """ytmusicapi classifies auth as browser by an Authorization header
    containing SAPISIDHASH, then regenerates the real value per request from
    the __Secure-3PAPISID cookie and the origin. A Cookie-only dict reads as
    an OAuth token and is refused (observed live with ytmusicapi 1.12)."""
    import ytm_auth

    cookies = ytm_auth.parse_netscape(parseable_cookies(tmp_path))
    headers = ytm_sync._browser_headers(cookies)
    assert "SAPISIDHASH" in headers["Authorization"]
    assert "__Secure-3PAPISID=" in headers["Cookie"]
    assert headers["X-Origin"] == "https://music.youtube.com"


def test_track_row_maps_the_full_shape():
    row = ytm_sync.track_row(track("v1", title="Song", artist="Artist", album="Album"))
    assert row == ("v1", "Song", "Artist", "Album", 200, "img/v1-big")


def test_track_row_tolerates_missing_fields():
    row = ytm_sync.track_row({"videoId": "v2", "title": None, "artists": [],
                              "duration": "3:05", "thumbnails": []})
    assert row == ("v2", "v2", "", None, 185, None)


def test_track_row_without_videoid_is_none():
    # upload/episode-shaped entries have no playable videoId
    assert ytm_sync.track_row({"title": "upload", "artists": []}) is None


# -- likes -----------------------------------------------------------------

def test_sync_likes_stars_and_upserts(tmp_path):
    lib = library.Library(str(tmp_path / "db.sqlite"))
    ytm = FakeYTM(liked=[track("v1"), track("v2")])

    report = asyncio.run(ytm_sync.sync_likes(lib, ytm))
    assert report["added"] == 2
    assert {song["id"] for song in lib.get_starred()} == {"v1", "v2"}
    # the star carried the metadata in, not just the id
    song = lib.get_song("v1")
    assert song["title"] == "T" and song["artwork_url"] == "img/v1-big"


def test_sync_likes_is_idempotent(tmp_path):
    lib = library.Library(str(tmp_path / "db.sqlite"))
    ytm = FakeYTM(liked=[track("v1"), track("v2")])
    asyncio.run(ytm_sync.sync_likes(lib, ytm))

    report = asyncio.run(ytm_sync.sync_likes(lib, ytm))
    assert report["added"] == 0
    assert report["already_starred"] == 2
    assert len(lib.get_starred()) == 2


def test_sync_likes_survives_entries_without_videoid(tmp_path):
    lib = library.Library(str(tmp_path / "db.sqlite"))
    ytm = FakeYTM(liked=[track("v1"), {"title": "upload"}, track("v2")])

    report = asyncio.run(ytm_sync.sync_likes(lib, ytm))
    assert report["resolved"] == 2
    assert report["total"] == 3


def test_sync_likes_dry_run_writes_nothing(tmp_path):
    lib = library.Library(str(tmp_path / "db.sqlite"))
    ytm = FakeYTM(liked=[track("v1")])

    report = asyncio.run(ytm_sync.sync_likes(lib, ytm, dry_run=True))
    assert report["added"] == 1
    assert lib.get_starred() == []
    assert lib.get_song("v1") is None


# -- playlists -------------------------------------------------------------

def two_playlist_fixture():
    ytm = FakeYTM(
        playlists=[
            {"playlistId": "PL1", "title": "Road"},
            {"playlistId": "LM", "title": "Your Likes"},  # auto shelf — skipped
        ],
        playlist_details={
            "PL1": {"tracks": [track("v1"), track("v2")]},
        },
    )
    return ytm


def test_sync_playlists_creates_mapped_playlist_in_order(tmp_path):
    lib = library.Library(str(tmp_path / "db.sqlite"))
    ytm = two_playlist_fixture()

    report = asyncio.run(ytm_sync.sync_playlists(lib, ytm))
    assert len(report["playlists"]) == 1  # LM never becomes a playlist

    playlist = lib.get_playlist_by_name("Road")
    assert [song["id"] for song in playlist["songs"]] == ["v1", "v2"]
    assert lib.get_ytm_playlist_map() == {"PL1": playlist["id"]}


def test_sync_playlists_second_run_only_appends_new(tmp_path):
    lib = library.Library(str(tmp_path / "db.sqlite"))
    ytm = two_playlist_fixture()
    asyncio.run(ytm_sync.sync_playlists(lib, ytm))

    ytm.playlist_details["PL1"]["tracks"] = [track("v2"), track("v3")]
    report = asyncio.run(ytm_sync.sync_playlists(lib, ytm))

    # append at the end, never reorder, never delete: v2 stays where it was
    playlist = lib.get_playlist_by_name("Road")
    assert [song["id"] for song in playlist["songs"]] == ["v1", "v2", "v3"]
    assert report["playlists"][0]["added"] == 1
    assert report["playlists"][0]["created"] is False


def test_sync_playlists_recreates_after_local_delete(tmp_path):
    lib = library.Library(str(tmp_path / "db.sqlite"))
    ytm = two_playlist_fixture()
    asyncio.run(ytm_sync.sync_playlists(lib, ytm))
    old_id = lib.get_playlist_by_name("Road")["id"]
    lib.delete_playlist(old_id)

    asyncio.run(ytm_sync.sync_playlists(lib, ytm))

    playlist = lib.get_playlist_by_name("Road")
    assert playlist["id"] != old_id
    assert [song["id"] for song in playlist["songs"]] == ["v1", "v2"]
    assert lib.get_ytm_playlist_map() == {"PL1": playlist["id"]}


def test_sync_playlists_dry_run_writes_nothing(tmp_path):
    lib = library.Library(str(tmp_path / "db.sqlite"))
    ytm = two_playlist_fixture()

    report = asyncio.run(ytm_sync.sync_playlists(lib, ytm, dry_run=True))
    assert report["playlists"][0]["added"] == 2
    assert lib.get_playlists() == []
    assert lib.get_ytm_playlist_map() == {}


def test_sync_playlists_does_not_adopt_same_named_playlist(tmp_path):
    lib = library.Library(str(tmp_path / "db.sqlite"))
    lib.create_playlist("Road")
    ytm = two_playlist_fixture()

    asyncio.run(ytm_sync.sync_playlists(lib, ytm))
    # two playlists: the pre-existing hand-made one and the sync's own
    roads = [p for p in lib.get_playlists() if p["name"] == "Road"]
    assert len(roads) == 2
