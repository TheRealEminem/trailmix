# Self-hosting Trailmix

Trailmix runs entirely on your own computer by default. The same app can also live on your own server (a home
server, a NAS, a small cloud machine), so your meetings, search and tasks are reachable from any device. The server
transcribes by itself, or hands the work to a faster computer of yours or a cloud API.

```
     any browser (Chrome)                  your server (Docker)                     optional: a faster computer
 ┌─────────────────────────┐ HTTPS  ┌──────────────────────────────────┐ Tailscale ┌─────────────────────────────┐
 │ Trailmix UI             │──────▶ │ Trailmix app + SQLite + audio    │─────────▶ │ transcription worker  :8770 │
 │ mic + tab audio streamed│ (token)│ search · tasks · Q&A · export    │           │   Mac: MLX on the GPU       │
 └─────────────────────────┘        │ whisper.cpp (transcribes itself) │─────────▶ │   PC / Linux: whisper.cpp   │
                                    └──────────────────────────────────┘           │ ollama serve         :11434 │
                                                   │                               └─────────────────────────────┘
                                                   └──▶ optional cloud APIs (Claude, OpenAI, Gemini, DeepSeek, Groq…)
```

Transcription and summaries are **separate endpoints**, each set in **Settings**:

| Job | On the server | Elsewhere |
|---|---|---|
| Transcription (live draft + final) | whisper.cpp in the container, on the CPU | Any OpenAI-compatible `/audio/transcriptions` URL: Trailmix's worker on a Mac, PC or Linux machine, OpenAI, Groq, a self-hosted whisper server |
| Summaries, titles, Q&A | Ollama on the same machine | Ollama on another machine, or Claude / OpenAI / Gemini / DeepSeek / any OpenAI-compatible API |

## Recipe: the server on its own

All you need is a Linux machine with Docker (x86-64 or ARM64, so a Raspberry Pi 5 or most NAS boxes work).

```bash
git clone https://github.com/TheRealEminem/trailmix.git && cd trailmix
cp .env.example .env          # set TRAILMIX_ACCESS_TOKEN (openssl rand -base64 32)
docker compose up -d
tailscale serve --bg 8765     # HTTPS at https://<server>.<tailnet>.ts.net
```

Open the HTTPS address, enter the access token once, and record. The speech models download into the data volume
the first time (about 150 MB for the live draft and 200 to 600 MB for the transcript), so a restart or an update
doesn't fetch them again. A server with an Intel or AMD processor of fewer than 6 cores gets Whisper Small, which is
several times faster; anything bigger, and ARM chips, get Whisper Large v3 Turbo. **Settings → Transcription**
offers the others.

Browsers only allow microphone capture on HTTPS pages (or localhost), which is why
[Tailscale](https://tailscale.com) (free for personal use) or a reverse proxy like Caddy is part of the setup. The
container only listens on `127.0.0.1`.

For summaries, run [Ollama](https://ollama.com) on the server too, listening beyond localhost so the container
can reach it at `host.docker.internal:11434` (`OLLAMA_HOST=0.0.0.0`, as in step 4 below), or pick a cloud provider
in **Settings → AI providers**.

To update: `git pull && docker compose up -d --build`.

## Recipe: server + a faster computer for transcription

A small server's CPU transcribes slowly. A computer with more speed to spare, a Mac with Apple silicon or a PC
with a good processor, can do it for the server instead, with Trailmix's **transcription worker**: an
OpenAI-compatible endpoint that uses the same speech engine as the Trailmix app (MLX on an Apple silicon Mac,
whisper.cpp everywhere else).

1. **Put both machines on [Tailscale](https://tailscale.com).** It gives each a stable name like `my-pc` and
   keeps everything off the public internet.

2. **On that computer**, in a copy of this repository, start the worker with a token of your choosing (note it):

   - Mac or Linux (needs Python 3.11+ and ffmpeg):
     ```bash
     TRAILMIX_WORKER_TOKEN="$(openssl rand -hex 24)" ./trailmix worker
     ```
   - Windows (needs Python 3.11+, from python.org or `winget install Python.Python.3.12`), in PowerShell:
     ```powershell
     $env:TRAILMIX_WORKER_TOKEN = [guid]::NewGuid().ToString("N"); $env:TRAILMIX_WORKER_TOKEN
     powershell -ExecutionPolicy Bypass -File .\trailmix.ps1 worker
     ```
     Allow Python through Windows Firewall on private networks when it asks.

   The first run sets up a Python environment in `backend/.venv` (on Windows with its own ffmpeg). Anywhere else,
   `pip install -r requirements-whispercpp.txt` in `backend/` and `python -m transcription_worker` do the same.
   The worker listens on port 8770 (`TRAILMIX_WORKER_PORT`), loads the speech model in a child process and shuts it
   down after 2 idle minutes (`TRAILMIX_WORKER_IDLE_S`), so the computer gets its memory back between meetings.

   whisper.cpp from pip runs on the CPU. On a PC with an NVIDIA graphics card, rebuilding it with CUDA in the
   worker's environment makes it much faster (needs the CUDA toolkit, CMake and a C++ compiler):
   `GGML_CUDA=1 backend/.venv/bin/pip install --force-reinstall git+https://github.com/absadiki/pywhispercpp`
   (`backend\.venv\Scripts\pip` on Windows).

3. **On the server**, point Trailmix at the worker: in `.env`, set `TRAILMIX_TRANSCRIBE_URL=http://my-pc:8770/v1`
   and `TRAILMIX_TRANSCRIBE_API_KEY` to the worker's token, then `docker compose up -d`; or in
   **Settings → Transcription → Your own server**. **Test** there checks it.

4. **Optionally, summaries on that computer too:** let Ollama listen on the network and set `OLLAMA_URL` (or
   **Settings → AI providers**) to `http://my-pc:11434`. On a Mac:
   `launchctl setenv OLLAMA_HOST 0.0.0.0 && osascript -e 'quit app "Ollama"' && open -a Ollama`; on Windows, set
   the `OLLAMA_HOST` environment variable to `0.0.0.0` for your account and restart Ollama; on Linux, add
   `Environment="OLLAMA_HOST=0.0.0.0"` with `sudo systemctl edit ollama` and restart it.

## Recipe: server + cloud only

In Settings choose **Transcription → Groq** (or OpenAI) and add a key, and pick Claude, OpenAI, Gemini or DeepSeek
as the summary provider. The server then doesn't download the speech models. Everything else is the same.

## Recording desktop apps with a hosted Trailmix

Browsers can only capture a tab's audio. To record the Zoom or Teams desktop apps into a hosted Trailmix, run the
Trailmix app on your computer (on a Mac, just its menu bar recorder, `./trailmix helper`, is enough) and, in its
Settings, set the address to your server (for example `https://trailmix.your-tailnet.ts.net`) and paste the access
token. It streams your mic and the call's audio to the server exactly as the web app does, and the hotkeys work from
anywhere.

## Good to know

- **Bandwidth:** recording streams raw 16 kHz audio to the server: about 32 KB/s per track (≈ 230 MB/hour
  with meeting audio). Audio is compressed to Opus on the server when you stop (~21 MB/hour/track).
- **If the worker's computer is asleep** when a meeting ends, transcription fails with a clear error and a
  **Try again** button; nothing is lost. Setting a cloud provider as the summary **Fallback** covers Ollama being
  offline.
- **Memory checks** ("Needs OK") apply to the server's own speech model, as on a computer running the app, and are
  skipped for a worker or a cloud API. A server with very little memory asks before each transcript; pick a
  smaller model, or send transcription elsewhere.
- **Transcription elsewhere only:** with `TRAILMIX_TRANSCRIBE_URL` set and no `TRAILMIX_TRANSCRIBE_ENGINE`, the
  server sends audio to that URL, as it always did before it could transcribe by itself.
  `TRAILMIX_TRANSCRIBE_ENGINE=local` or `remote` in `.env` decides it by hand, and
  `docker build --build-arg WHISPERCPP=0` makes a smaller image without whisper.cpp.
- **Security:** set a long `TRAILMIX_ACCESS_TOKEN`; the browser gets an HttpOnly session cookie derived from it.
  API keys are stored in the server's database and are never sent back to the browser. Protect the worker
  with `TRAILMIX_WORKER_TOKEN`, and keep both behind Tailscale rather than exposing ports.
- **Data** lives in the `trailmix-data` Docker volume (`/data`: database, audio, exports, and the speech models in
  `/data/models`). Back it up; the models can be left out, since they download again.
