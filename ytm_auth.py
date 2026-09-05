"""YouTube Music login support — one optional cookie file, two consumers.

Anonymous access (the default, and the upstream design) resolves streams at
~129 kbps and sees only the public catalogue. With `YTM_COOKIES_FILE` pointing
at a Netscape-format cookies.txt exported from a logged-in
music.youtube.com session:

- yt-dlp resolves with the account's privileges, which raises the audio
  ceiling for a Premium account (up to ~256 kbps AAC) and unlocks tracks that
  are age-gated anonymously;
- `ytm_sync.py` reads the same file to pull the account's liked songs and
  playlists into the library (a separate process — the worker never imports
  it, and this module never imports ytmusicapi).

yt-dlp loads the cookie jar and writes it back to the file it was given when
the session closes. The user's export is never touched: `active_cookie_copy`
hands out a copy in the writable tmp directory instead, refreshed whenever
the source file changes on disk — which is also how a re-export picked up
without a container restart.

Deliberately stdlib-only and network-free: the worker imports it on the
playback path, and the default test suite must not touch the network. No
cookie value is ever logged — counts and names only (the same discipline
main.py applies to signed URLs and Subsonic credentials).
"""
import logging
import os
import shutil
import tempfile
import time
from typing import Optional

logger = logging.getLogger("worker")

ENV_VAR = "YTM_COOKIES_FILE"

# The cookie names a YouTube authorization is built from. yt-dlp signs
# requests with a SAPISIDHASH of one of these; a file without any of them
# behaves anonymously no matter how many cookies it carries.
SAPISID_NAMES = ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID")

# The writable copy handed to yt-dlp. One per process: yt-dlp keeps its own
# parsed-jar state between resolves, and swapping files under it would throw
# that away on every track.
_copy: dict = {}


def cookies_path() -> Optional[str]:
    """The configured path, or None. Unset and empty are the same thing:
    login support is off entirely."""
    raw = (os.environ.get(ENV_VAR) or "").strip()
    if not raw:
        return None
    return os.path.abspath(os.path.expanduser(raw))


def parse_netscape(path: str) -> list[dict]:
    """A Netscape cookies.txt as {name, value, expiry} dicts.

    - Tab-separated, 7 fields, `expiry` epoch seconds (0 = session cookie,
      which never expires as far as this check is concerned);
    - `#HttpOnly_…` lines are cookies — yt-dlp's own exports prefix them that
      way; every other `#` line is a comment or header;
    - malformed lines are skipped, not fatal: a stray edit to the export
      should degrade to "fewer cookies", never to "no playback".
    """
    cookies = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.rstrip("\n")
                if not line:
                    continue
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_"):]
                elif line.startswith("#"):
                    continue
                fields = line.split("\t")
                if len(fields) < 7:
                    continue
                expiry: Optional[int]
                try:
                    expiry = int(fields[4])
                except ValueError:
                    expiry = None
                cookies.append({
                    "domain": fields[0],
                    "name": fields[5],
                    "value": fields[6],
                    "expiry": expiry,
                })
    except OSError:
        return []
    return cookies


def summarize(cookies: list[dict], now: Optional[float] = None) -> dict:
    """Counts for the log line and for tests. Never the values themselves."""
    if now is None:
        now = time.time()
    expired = sum(
        # expiry 0 is a session cookie, which this check never counts as expired
        1 for c in cookies if c["expiry"] and c["expiry"] < now
    )
    names = {c["name"] for c in cookies}
    return {
        "total": len(cookies),
        "expired": expired,
        "active": len(cookies) - expired,
        "has_sapisid": any(name in names for name in SAPISID_NAMES),
    }


def active_cookie_copy() -> Optional[str]:
    """A writable copy of the configured cookie file, or None to go anonymous.

    - Unset → None, silently: the anonymous default costs one os.environ.get.
    - Configured but missing/unreadable/empty → None with a warning, every
      time it is consulted (once per stream resolve — minutes apart, not a
      hot loop).
    - No SAPISID-family cookie → a loud warning but still the file: the
      remaining cookies can still carry session state, and the user may be
      mid-fix. The log line says the file behaves anonymously.
    """
    source = cookies_path()
    if source is None:
        return None

    try:
        stat = os.stat(source)
    except OSError:
        logger.warning("%s is set but %s does not exist — going anonymous",
                       ENV_VAR, source)
        return None

    cookies = parse_netscape(source)
    summary = summarize(cookies)
    if summary["total"] == 0:
        logger.warning("%s=%s holds no parseable cookies — going anonymous",
                       ENV_VAR, source)
        return None
    if not summary["has_sapisid"]:
        logger.warning(
            "%s=%s carries no SAPISID-family cookie — requests will behave "
            "anonymously despite being sent with cookies", ENV_VAR, source)
    logger.info("cookies: %d active, %d expired (from %s)",
                summary["active"], summary["expired"], source)

    key = (source, stat.st_mtime_ns, stat.st_size)
    if _copy.get("key") == key:
        return _copy["path"]
    try:
        with open(source, "rb") as src:
            data = src.read()
    except OSError:
        logger.warning("%s=%s became unreadable — going anonymous", ENV_VAR, source)
        return None
    handle, copy_path = tempfile.mkstemp(prefix="ytm-cookies-", suffix=".txt")
    with os.fdopen(handle, "wb") as out:
        out.write(data)
    _copy.clear()
    _copy["key"] = key
    _copy["path"] = copy_path
    return copy_path
