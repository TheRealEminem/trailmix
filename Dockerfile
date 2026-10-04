# Trailmix for a server (Linux, no MLX). Transcription goes to a remote endpoint:
# your Mac running `./trailmix worker`, or a cloud API. See docs/self-hosting.md.
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app/backend
COPY backend/requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt
COPY backend/*.py ./
COPY backend/assets ./assets
COPY --from=web /web/dist /app/frontend/dist

ENV TRAILMIX_DATA_DIR=/data \
    TRAILMIX_UI_DIR=/app/frontend/dist \
    TRAILMIX_TRANSCRIBE_ENGINE=remote \
    TRAILMIX_EXPORT_DIR=/data/exports \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765
HEALTHCHECK CMD curl -fs http://localhost:8765/api/auth || exit 1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers", "--forwarded-allow-ips", "*"]
