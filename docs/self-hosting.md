# Self-hosting Trailmix

Trailmix runs entirely on your Mac by default. The same app can also live on your own server, so your
meetings, search and tasks are reachable from any device, while the heavy lifting stays wherever you want it.

```
        any browser (Chrome)                     your server (Docker)                 your Mac (Apple Silicon)
  ┌───────────────────────────┐  HTTPS   ┌──────────────────────────────┐  Tailscale  ┌────────────────────────────┐
  │ Trailmix UI               │ ───────▶ │ Trailmix app + SQLite + audio│ ──────────▶ │ ./trailmix worker  :8770   │
  │ mic + tab audio streamed  │  (token) │ search · tasks · Q&A · export│             │   mlx-whisper (GPU)        │
  └───────────────────────────┘          └──────────────────────────────┘ ──────────▶ │ ollama serve       :11434  │
                                                     │                                └────────────────────────────┘
                                                     └──▶ optional cloud APIs (Claude, OpenAI, Gemini, DeepSeek, Groq…)
```

Transcription and summaries are **separate endpoints**, each set in **Settings**:

| Job | Local on the Mac | Remote |
|---|---|---|
| Transcription (live draft + final) | MLX, in-process | Any OpenAI-compatible `/audio/transcriptions` URL: your Mac's worker, OpenAI, Groq, a self-hosted whisper server |
| Summaries, titles, Q&A | Ollama on `localhost` | Ollama on another machine, or Claude / OpenAI / Gemini / DeepSeek / any OpenAI-compatible API |

## Recipe: server + your Mac as the GPU (recommended)

1. **Put both machines on [Tailscale](https://tailscale.com)** (free for personal use). It gives each a stable
   name like `your-mac` and keeps everything off the public internet.

2. **On the Mac**, start the transcription worker and let Ollama listen on the network:
   ```bash
   TRAILMIX_WORKER_TOKEN="$(openssl rand -hex 24)" ./trailmix worker    # note the token
   launchctl setenv OLLAMA_HOST 0.0.0.0 && osascript -e 'quit app "Ollama"' && open -a Ollama
   ```
   The worker loads Whisper in a child process and shuts it down after 2 idle minutes
   (`TRAILMIX_WORKER_IDLE_S`), so the Mac gets its memory back between meetings.

3. **On the server** (any Linux box with Docker):
   ```bash
   git clone <your copy of this repo> trailmix && cd trailmix
   cp .env.example .env          # set TRAILMIX_ACCESS_TOKEN, the worker URL/token, OLLAMA_URL
   docker compose up -d
   tailscale serve --bg 8765     # HTTPS at https://<server>.<tailnet>.ts.net
   ```
   Browsers only allow microphone capture on HTTPS pages (or localhost), which is why `tailscale serve`
   (or a reverse proxy like Caddy) is part of the setup. The container only listens on `127.0.0.1`.

4. Open the HTTPS address, enter the access token once, then check **Settings → Transcription → Test** and
   **Settings → AI providers → Test connection**.

## Recipe: server + cloud only (no Mac needed)

Skip the Mac steps. In Settings choose **Transcription → Remote → Groq** (or OpenAI) and add a key, and pick
Claude, OpenAI, Gemini or DeepSeek as the summary provider. Everything else is the same.

## Recording desktop apps with a hosted Trailmix

Browsers can only capture a tab's audio. To record the Zoom or Teams desktop apps into a hosted Trailmix, run
the Trailmix app on your Mac (or just its menu bar recorder, `./trailmix helper`) and, in its Settings, set the address to your server
(for example `https://trailmix.your-tailnet.ts.net`) and paste the access token. It streams your mic and the
call's audio to the server exactly as the web app does, and the hotkeys work from anywhere.

## Good to know

- **Bandwidth:** recording streams raw 16 kHz audio to the server: about 32 KB/s per track (≈ 230 MB/hour
  with meeting audio). Audio is compressed to Opus on the server when you stop (~21 MB/hour/track).
- **If the Mac is asleep** when a meeting ends, transcription fails with a clear error and a **Try again**
  button; nothing is lost. Setting a cloud provider as the summary **Fallback** covers Ollama being offline.
- **Memory checks** ("Needs OK") only apply to models running on the same machine as Trailmix, so they're
  skipped for remote engines.
- **Security:** set a long `TRAILMIX_ACCESS_TOKEN`; the browser gets an HttpOnly session cookie derived from it.
  API keys are stored in the server's database and are never sent back to the browser. Protect the worker
  with `TRAILMIX_WORKER_TOKEN`, and keep both behind Tailscale rather than exposing ports.
- **Data** lives in the `trailmix-data` Docker volume (`/data`: database, audio, exports). Back that up.
