# Trailmix for a server (Linux, amd64 or arm64). It transcribes by itself with whisper.cpp on the CPU, downloading
# the speech models into the data volume the first time; or set TRAILMIX_TRANSCRIBE_ENGINE=remote to send audio to
# a transcription worker (./trailmix worker on a Mac, PC or Linux machine) or a cloud API. See docs/self-hosting.md.
#
# Build without whisper.cpp (remote transcription only, a smaller image): docker build --build-arg WHISPERCPP=0 .
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# The Python packages, in a virtual environment that's copied into the final image on its own. Only prebuilt
# wheels (pywhispercpp has them for amd64 and arm64), so no compiler is needed or left behind.
FROM python:3.12-slim AS python
ARG WHISPERCPP=1
WORKDIR /build
COPY backend/requirements-server.txt backend/requirements-whispercpp.txt ./
RUN python -m venv /venv \
 && reqs=$([ "$WHISPERCPP" = 1 ] && echo requirements-whispercpp.txt || echo requirements-server.txt) \
 && /venv/bin/pip install --no-cache-dir --no-compile --only-binary pywhispercpp -r "$reqs" \
 && /venv/bin/pip uninstall -y -q pip
# A static ffmpeg with the Opus encoder, from the imageio-ffmpeg wheel (as the Linux app bundles it): a tenth of
# the size of Debian's ffmpeg package and its libraries.
RUN pip install --no-cache-dir -q --no-deps --only-binary :all: --target /tmp/ff imageio-ffmpeg \
 && install -m 755 "$(ls /tmp/ff/imageio_ffmpeg/binaries/ffmpeg-linux* | tail -n 1)" /usr/local/bin/ffmpeg \
 && ffmpeg -hide_banner -encoders | grep -q ' libopus '

FROM python:3.12-slim
COPY --from=python /venv /venv
COPY --from=python /usr/local/bin/ffmpeg /usr/local/bin/ffmpeg
WORKDIR /app/backend
COPY backend/*.py ./
COPY backend/assets ./assets
COPY --from=web /web/dist /app/frontend/dist

ENV PATH=/venv/bin:$PATH \
    TRAILMIX_DATA_DIR=/data \
    TRAILMIX_UI_DIR=/app/frontend/dist \
    TRAILMIX_EXPORT_DIR=/data/exports \
    HF_HOME=/data/models \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/auth', timeout=5)" || exit 1
# With no engine chosen, an install from before the image transcribed by itself (a worker URL in .env), or an
# image built without whisper.cpp, sends audio to TRAILMIX_TRANSCRIBE_URL as before.
CMD ["sh", "-c", "if [ -z \"$TRAILMIX_TRANSCRIBE_ENGINE\" ] && { [ -n \"$TRAILMIX_TRANSCRIBE_URL\" ] || ! [ -d /venv/lib/python3.12/site-packages/pywhispercpp ]; }; then export TRAILMIX_TRANSCRIBE_ENGINE=remote; fi; exec uvicorn main:app --host 0.0.0.0 --port 8765 --proxy-headers --forwarded-allow-ips '*'"]
