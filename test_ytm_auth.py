"""ytm_auth.py — Netscape parsing, the anonymous default, the writable copy.

No network, no yt-dlp: the whole module is file parsing and env reading, and
these tests keep it that way.
"""
import os

import pytest

import ytm_auth


def write_cookies(tmp_path, body: str) -> str:
    path = tmp_path / "cookies.txt"
    path.write_text(body, encoding="utf-8")
    return str(path)


VALID = """\
# Netscape HTTP Cookie File
# This is a generated file! Do not edit.

.youtube.com\tTRUE\t/\tTRUE\t0\tVISITOR_INFO1_LIVE\tabc123
#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t2000000000\tSAPISID\tsapisid-value
.youtube.com\tTRUE\t/\tTRUE\t1000\tEXPIREDLONGAGO\tgone
"""


@pytest.fixture(autouse=True)
def _reset_copy_cache():
    ytm_auth._copy.clear()
    yield
    ytm_auth._copy.clear()


def test_parse_netscape_reads_cookies_comments_and_httponly(tmp_path):
    cookies = ytm_auth.parse_netscape(write_cookies(tmp_path, VALID))
    names = [c["name"] for c in cookies]
    assert names == ["VISITOR_INFO1_LIVE", "SAPISID", "EXPIREDLONGAGO"]
    assert cookies[1]["value"] == "sapisid-value"
    # session cookie: expiry 0 never counts as expired
    assert cookies[0]["expiry"] == 0
    assert cookies[1]["expiry"] == 2000000000


def test_parse_netscape_skips_malformed_lines(tmp_path):
    body = "no-tabs-here\n\t\t\t\n.youtube.com\tTRUE\t/\tTRUE\t0\tGOOD\tok\n"
    cookies = ytm_auth.parse_netscape(write_cookies(tmp_path, body))
    assert [c["name"] for c in cookies] == ["GOOD"]


def test_parse_netscape_missing_file_is_empty(tmp_path):
    assert ytm_auth.parse_netscape(str(tmp_path / "nope.txt")) == []


def test_summarize_counts_expired_and_sapisid():
    cookies = [
        {"name": "SID", "value": "x", "expiry": 0},
        {"name": "SAPISID", "value": "x", "expiry": 2000000000},
        {"name": "OLD", "value": "x", "expiry": 1000},
    ]
    summary = ytm_auth.summarize(cookies, now=1_500_000_000)
    assert summary == {
        "total": 3, "expired": 1, "active": 2, "has_sapisid": True,
    }


def test_summarize_without_sapisid_family():
    cookies = [{"name": "PREF", "value": "x", "expiry": 0}]
    assert ytm_auth.summarize(cookies)["has_sapisid"] is False


def test_cookies_path_unset_or_empty_is_none(monkeypatch):
    monkeypatch.delenv(ytm_auth.ENV_VAR, raising=False)
    assert ytm_auth.cookies_path() is None
    monkeypatch.setenv(ytm_auth.ENV_VAR, "   ")
    assert ytm_auth.cookies_path() is None


def test_active_cookie_copy_unset_is_silent_none(monkeypatch):
    monkeypatch.delenv(ytm_auth.ENV_VAR, raising=False)
    assert ytm_auth.active_cookie_copy() is None


def test_active_cookie_copy_missing_file_warns_and_goes_anonymous(
        monkeypatch, tmp_path, caplog):
    monkeypatch.setenv(ytm_auth.ENV_VAR, str(tmp_path / "absent.txt"))
    with caplog.at_level("WARNING", logger="worker"):
        assert ytm_auth.active_cookie_copy() is None
    assert "does not exist" in caplog.text


def test_active_cookie_copy_no_cookies_warns_and_goes_anonymous(
        monkeypatch, tmp_path, caplog):
    path = write_cookies(tmp_path, "# Netscape HTTP Cookie File\n\n")
    monkeypatch.setenv(ytm_auth.ENV_VAR, path)
    with caplog.at_level("WARNING", logger="worker"):
        assert ytm_auth.active_cookie_copy() is None
    assert "no parseable cookies" in caplog.text


def test_active_cookie_copy_without_sapisid_still_hands_out_the_file(
        monkeypatch, tmp_path, caplog):
    body = ".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tonly-pref\n"
    path = write_cookies(tmp_path, body)
    monkeypatch.setenv(ytm_auth.ENV_VAR, path)
    with caplog.at_level("INFO", logger="worker"):
        copy_path = ytm_auth.active_cookie_copy()
    assert copy_path is not None
    assert "SAPISID" in caplog.text


def test_active_cookie_copy_content_matches_source_and_survives_rewrite(
        monkeypatch, tmp_path):
    path = write_cookies(tmp_path, VALID)
    monkeypatch.setenv(ytm_auth.ENV_VAR, path)
    first = ytm_auth.active_cookie_copy()
    assert first is not None and first != path
    with open(first, encoding="utf-8") as handle:
        assert handle.read() == VALID
    # The same source hands out the same copy — yt-dlp keeps jar state per file.
    assert ytm_auth.active_cookie_copy() == first
    # A changed source (a re-export) is picked up without a restart.
    path2 = write_cookies(tmp_path, VALID + ".youtube.com\tTRUE\t/\tTRUE\t0\tNEW\t1\n")
    monkeypatch.setenv(ytm_auth.ENV_VAR, path2)
    second = ytm_auth.active_cookie_copy()
    assert second != first
    with open(second, encoding="utf-8") as handle:
        assert "NEW\t1" in handle.read()


def test_main_ydl_opts_carries_cookiefile_only_when_configured(monkeypatch, tmp_path):
    import main

    monkeypatch.delenv(ytm_auth.ENV_VAR, raising=False)
    opts = main._ydl_opts()
    assert opts["format"] == main.YDL_OPTS["format"]
    assert "cookiefile" not in opts

    monkeypatch.setenv(ytm_auth.ENV_VAR, write_cookies(tmp_path, VALID))
    opts = main._ydl_opts()
    assert opts.get("cookiefile")
    assert os.path.exists(opts["cookiefile"])
