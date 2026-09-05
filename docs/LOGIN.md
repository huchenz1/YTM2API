# Logging in to YouTube Music (optional)

The server runs fully anonymous by default — no account, no cookies, no
browser, and that remains the recommended setup for a single listener who is
happy with the public catalogue. Login support exists for two things
anonymity cannot give you:

| | Anonymous (default) | Logged in (Premium cookie) |
|---|---|---|
| Audio ceiling | ~129 kbps AAC (itag 140) | up to ~256 kbps AAC (itag 141) |
| Age-restricted tracks | 404, never resolve | play |
| Liked songs | not visible | synced as starred tracks |
| Account playlists | not visible | synced as local playlists |
| Uploaded music | not visible | not exposed yet |

## 1. Export the cookies

From a browser profile that is logged in to `music.youtube.com`:

```sh
yt-dlp --cookies-from-browser firefox --cookies cookies.txt "https://music.youtube.com/"
```

(`--cookies-from-browser` understands `chrome`, `edge`, `brave`, `safari` and
`vivaldi` too. A "Get cookies.txt" browser extension works as well.) The file
is Netscape-format text and is a **live credential**: treat it like a
password, never commit it, never share it.

Two practical notes:

- Export from a profile you **keep** logged in. Rotating the Google session
  (sign-out everywhere, password change, security sweep) invalidates the
  export.
- Premium quality requires a Premium account. A free account's cookies work
  for likes/playlists sync and age-gated tracks, but the audio ceiling does
  not move: free streams are ~128 kbps for everyone.

## 2. Wire it up

Put the file next to the database and point `.env` at it:

```sh
mv cookies.txt data/cookies.txt          # the LIBRARY_PATH directory
echo 'YTM_COOKIES_FILE=/data/cookies.txt' >> .env
docker compose up -d --force-recreate worker
```

The worker reads the file on the next stream resolve and copies it to the
container's tmp directory — the original is never opened for writing, and a
re-exported file (same path, new content) is picked up on the following
track without a restart. Unset the variable to go back to fully anonymous.

The startup log says which mode is live:

```
cookies: 41 active, 0 expired (from /data/cookies.txt)
```

A file with no `SAPISID`-family cookie logs a loud warning: requests are then
sent with cookies but behave anonymously, which is almost never what was
meant.

## 3. Sync the account's library

With the same variable set, pull likes and playlists into the local library:

```sh
docker compose exec worker python ytm_sync.py --dry-run all
docker compose exec worker python ytm_sync.py all
# or one at a time:
docker compose exec worker python ytm_sync.py likes
docker compose exec worker python ytm_sync.py playlists
```

No matching happens here — a YTM playlist track carries its `videoId`, which
is this server's primary key — so the sync is one account API call per
playlist: no search burst, no captcha risk (docs/PITFALLS.md #1).

The sync is deliberately **additive and one-way**:

- un-liking on YouTube does not unstar locally; removing a track from a YTM
  playlist does not remove it locally. Stars and playlist edits are the
  client's territory — the sync only appends what is new, in YTM order;
- starring in a Subsonic client never writes back to YouTube;
- the mapping between a YTM playlist and the local playlist it created lives
  in the `ytm_playlists` table, so re-syncs land in the right playlist even
  after you rename it.

## Risks, stated plainly

- **The traffic is now tied to a real account.** Anonymous, the worst case
  from volume is a captcha for the server's IP. Logged in, the same volume
  lands on your Google account instead — the prefetch-depth-of-one rule
  (docs/PITFALLS.md #1) and the offline-caching warning exist for exactly
  this and matter more now, not less.
- **Cookies expire** — typically after months, sooner if the account's
  security state changes. Symptom: tracks resolve anonymously again (the
  log line shows it). Fix: re-export, replace the file, done.
- **The quality ceiling is Google's to move.** The high-bitrate path is the
  same yt-dlp extraction YouTube regularly breaks (that is why yt-dlp is
  unpinned in requirements.txt); a logged-in Premium stream is subject to
  the same churn, plus whatever SABR changes Google ships next. When
  extraction breaks, the fix is the usual rebuild against a fresh yt-dlp.
- **Third-party use of a YouTube Music account is not an officially
  supported Google scenario.** This is the same cookie-based mechanism
  ytmusicapi and Music Assistant use, but "widely used" is not "sanctioned".

## What login does not add

- No uploads: uploaded music is not exposed, even though cookies could see it.
- No write-back, no two-way sync: the server stays read-only toward the
  account.
- No per-track bitrate reporting: the Subsonic layer keeps advertising the
  anonymous ~129 kbps figure; a client that displays it may understate a
  track that actually arrived at 256. Estimating real per-track bitrate
  would need a resolve per track, which is exactly the request pattern this
  server refuses to make.
