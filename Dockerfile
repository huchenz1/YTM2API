FROM python:3.12-slim

# ffmpeg repackages googlevideo's fragmented mp4 into ADTS — without it
# clients crash on seek (see the long comment in main.py).
# curl fetches the deno binary below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# deno: yt-dlp solves YouTube's n-challenge through its EJS system, which
# needs a JavaScript runtime. Without one, every logged-in (premium) session
# fails at "n challenge solving failed" and resolves nothing at all — only
# the anonymous session's 128 kbps itag 140 survives (observed 2026-09-07).
# Pinned loosely to "latest": the solver must track whatever YouTube ships.
RUN ARCH=$(uname -m) \
 && case "$ARCH" in \
      x86_64)  DENO_TARGET="x86_64-unknown-linux-gnu" ;; \
      aarch64) DENO_TARGET="aarch64-unknown-linux-gnu" ;; \
      *) echo "unsupported arch: $ARCH" && exit 1 ;; \
    esac \
 && curl -fsSL -o /tmp/deno.zip \
      "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_TARGET}.zip" \
 && python3 -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extractall('/usr/local/bin')" \
 && chmod +x /usr/local/bin/deno && rm -f /tmp/deno.zip \
 && deno --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py subsonic.py library.py spotify_import.py \
     ranking.py listenbrainz_client.py music_agent.py \
     ytm_auth.py ytm_sync.py ./

# 0.0.0.0, not 127.0.0.1: inside a container loopback is unreachable from
# outside even with -p 8080:8080. Isolation comes from how the port is
# published on the host, not from the bind address in here.
ENV HOST=0.0.0.0 PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host $HOST --port $PORT"]
