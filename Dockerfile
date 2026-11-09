# Doorman Deluxe - official image.
# Published by .github/workflows/release.yml from git tags (vX.Y.Z).
# The version is passed as the DOORMAN_VERSION build arg (falls back to 0.0.0
# for local builds).
FROM python:3.13-slim

ARG DOORMAN_VERSION=0.0.0

LABEL org.opencontainers.image.title="doorman-deluxe" \
      org.opencontainers.image.description="AI-speaking agentic doorbell: Frigate triggers + Gemini Live or local STT/LLM/TTS voice + go2rtc talkback" \
      org.opencontainers.image.url="https://github.com/ryang3d/doorman-deluxe" \
      org.opencontainers.image.source="https://github.com/ryang3d/doorman-deluxe" \
      org.opencontainers.image.version="${DOORMAN_VERSION}"

# ffmpeg is required by the mic capture path (raw PCM via subprocess).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Run as an unprivileged user. UI port 8090 is >1024, host networking is fine.
RUN useradd --create-home --uid 1000 doorman

WORKDIR /app

# Dependencies first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source.
COPY src/ src/

# Writable dirs: model's local snapshots + UI data (transcripts/session
# snapshots). These are expected to be mounted as volumes (see
# docker-compose.yml / README quick start).
RUN mkdir -p /snapshots /data/transcripts /data/snapshots \
    && chown -R doorman:doorman /app /snapshots /data

# Neutral default TZ; override per host (docker-compose.yml sets it too).
ENV TZ=Etc/UTC \
    DOORMAN_SNAPSHOT_DIR=/snapshots \
    DOORMAN_DATA_DIR=/data

USER doorman

# Liveness: the in-container web UI (enabled by default) exposes /healthz.
# If you run with DOORMAN_UI_ENABLED=false, drop or override this healthcheck.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('DOORMAN_UI_PORT','8090'), timeout=4)" || exit 1

# No args = subscribe to Frigate MQTT + serve door events forever.
ENTRYPOINT ["python", "src/doorman.py"]
