FROM python:3.13-slim

# ffmpeg is required by audio_bridge mic capture (raw PCM via subprocess).
# No SSH client needed: notifications attach a doorbell frame captured by HA's own
# camera.snapshot service (HA-native), served at HA /local.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# copy requirements + install deps first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy source
COPY src/ src/
COPY deploy/health_check.py deploy/

# snapshot dir (for the model's local still tooling) as a volume
RUN mkdir -p /snapshots
ENV DOORMAN_SNAPSHOT_DIR=/snapshots

# UI + transcripts data dir (DOORMAN_DATA_DIR); session snapshots + sessions.jsonl
RUN mkdir -p /data/transcripts /data/snapshots

# Household timezone (Los Angeles) for date/logging/strftime; mirrors TZ in
# docker-compose.yml. The web UI renders timestamps in this zone explicitly too.
ENV TZ=America/Los_Angeles

# run the service (no args = subscribe to Frigate MQTT + serve door events forever)
ENTRYPOINT ["python", "src/doorman.py"]
