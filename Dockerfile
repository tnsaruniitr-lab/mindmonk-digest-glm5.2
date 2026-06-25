# Podcast Digest — worker image.
# This is a long-running scheduler (APScheduler), NOT a web service: no port
# is exposed. Railway should run this as a service with the default healthcheck
# disabled (it's a worker, not an HTTP server).

FROM python:3.12-slim

# yt-dlp needs ffmpeg to read some video metadata (no downloads happen, but
# the extractor probes media info). ca-certificates for TLS to YouTube/TG/LLM.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app.
COPY . .

# Defaults: real values come from Railway variables at runtime.
# These are overridden by Railway's injected env; kept only so local docker
# runs don't hard-fail on missing files.
ENV PYTHONUNBUFFERED=1 \
    CONFIG_PATH=/app/config.yaml \
    PROFILE_PATH=/app/profile.yaml

# The worker: runs the APScheduler loop (main.py without --once).
CMD ["python", "main.py"]
