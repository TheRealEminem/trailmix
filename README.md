# Trailmix

Local-first meeting recorder, transcriber, and summarizer.

- **Capture:** the browser streams your mic and (optionally) a shared tab's audio to the backend as **two separate tracks** (AudioWorklet → WebSocket, 16 kHz mono PCM). Nothing is buffered in the browser.
- **Live draft:** while recording, a small Whisper model (`whisper-base`) transcribes in ~3–12 s chunks (cut at pauses) so you get a rough running overview. Optional — untick "Live draft" for the lightest footprint.
- **Final transcript:** after you stop, the large model (`whisper-large-v3-turbo`) transcribes each track over its speech regions only, in a **separate process** that exits when done, so its memory is fully returned to macOS. Mic = **You**, meeting audio = **Them**. With a single source (in person) segments are unlabeled.
- **Summary:** only after the transcription process has exited does a local Ollama model load, and it unloads right after (`keep_alive: 0`). Or use Claude, OpenAI, Gemini, DeepSeek or any OpenAI-compatible API, with an optional fallback.
- **Two-step approval:** transcript and summary are separate steps. Under **Settings → Automatic mode** you can switch each to auto or manual (manual = the meeting waits for a "Generate transcript" / "Generate summary" button). Settings are stored in the backend database.
- **Auto-title:** after the summary, the same model names the meeting; meetings you rename yourself are never retitled.
- **Export:** optional auto-export of the summary and/or transcript to a folder as Markdown, plain text or JSON (one document, or separate files). "Export now" in any meeting uses the same options.
- **Draft vs final:** the live draft is kept, and *Transcript → Draft vs final* shows it next to the final transcript with the differences highlighted.
- **Microphone picker:** choose the input device on the record screen (remembered in the browser); handy when an iPhone or headset grabs the system default.
- **RAM gate:** before each heavy stage that runs on this machine, the backend checks free RAM; if it's below the model's needs + headroom, the meeting waits as "Needs OK" until you press *Proceed anyway*. Heavy stages also never start while a recording is in progress.

## Install (Mac)

1. Download **Trailmix-x.y.z-arm64.dmg** from the project's Releases page. It needs an Apple silicon Mac
   (M1 or newer) on macOS 14.2 or later.
2. Open it and drag **Trailmix** to **Applications**, then open Trailmix.
3. **First open only:** this build isn't signed by an Apple developer yet, so macOS blocks it. Go to
   System Settings → Privacy & Security, scroll down, and press **Open Anyway** next to "Trailmix was blocked".
   (Terminal alternative: `xattr -dr com.apple.quarantine /Applications/Trailmix.app`.)
4. Allow **Microphone** when asked, and **System Audio Recording** the first time you record a call.

Trailmix lives in your menu bar and opens its window on launch. Everything it needs is inside the app: its own
Python, the server, and ffmpeg. On first run it downloads the speech models (about 1.7 GB, once) and shows the
progress; you can already record meanwhile. For summaries, install [Ollama](https://ollama.com) and pull a
model, or add an API key (Claude, OpenAI, Gemini, DeepSeek…) in Settings.

**Where things are kept:** `~/Library/Application Support/Trailmix` holds your meetings, settings and audio,
plus `logs/server.log`. Deleting the app leaves them alone; updating is just replacing the app.

**Moving over from a source install** (`./trailmix`, data in `backend/data`): quit both, then
`cp -R backend/data/. ~/Library/Application\ Support/Trailmix/`.

If a `./trailmix` server is already running on port 8765, the app uses that one instead of starting its own.

### Build the app yourself

```bash
scripts/build-app.sh          # -> dist/Trailmix.app
scripts/build-app.sh --dmg    # -> dist/Trailmix-<version>-arm64.dmg as well
```

Needs an Apple silicon Mac, the Xcode command line tools, Node 18+ and internet access (the Python runtime is
downloaded once into `build/cache`, checked against a pinned checksum). The version is in `VERSION`. To sign it
with an Apple Developer ID, set `TRAILMIX_SIGN_IDENTITY="Developer ID Application: …"` (that also turns on the
hardened runtime; notarizing is a separate `xcrun notarytool` step). The bundled ffmpeg is a static build from
the [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) package, which is GPL-licensed.

## Run from source

`./trailmix` starts the background server (if needed) and opens Trailmix in its own Chrome window. The first run
sets up Python and builds the web app, which takes a minute or two. Quit from **Settings → App → Quit**.

```bash
./trailmix stop | restart | status | logs
./trailmix autostart on      # start the server when you log in
./trailmix dev               # development: backend with reload + Vite on :5173
./trailmix worker            # transcription worker for a server install (see docs/self-hosting.md)
./trailmix app               # build Trailmix.app, a double-clickable launcher for this folder
./trailmix helper            # build and open Trailmix Helper, the menu bar recorder (below)
```

Requires Python 3.11+, Node 18+, `ffmpeg` (`brew install ffmpeg`), and either Ollama with a model pulled or an
API key for a cloud provider. First use downloads the Whisper models (base ≈ 150 MB, large-v3-turbo ≈ 1.6 GB).

**Self-hosting** on your own server, with your Mac (or a cloud API) doing transcription and summaries: see
[docs/self-hosting.md](docs/self-hosting.md).

## Features

- **Live draft** while recording, **final transcript** after, with **You / Them** from separate tracks. Rename
  speakers by clicking their names.
- **Click any transcript line to play the audio** from that moment (the audio is kept for 30 days by default).
- **Mark moments** while recording (button or **M** key) or afterwards (the flag on each line). Flagged moments
  get their own section in the summary.
- **Summaries with templates** (general, 1:1, standup, interview, sales call, or your own), an auto-generated
  title, and **action items you can tick off**, collected across all meetings in **Tasks**.
- **Ask** a single meeting or all of them; answers link back to the meetings they used.
- **Search** titles, transcripts and summaries from the sidebar.
- **Any AI provider**: Ollama (local), Claude, OpenAI, Gemini, DeepSeek, or any OpenAI-compatible API, with a
  fallback. Keys are entered in Settings and never shown again.
- **Export** to Markdown, text or JSON; **Day** and **Dusk** themes.
- **Trailmix Helper** in the menu bar: records Zoom, Teams or FaceTime audio directly, with global hotkeys.

## Audio on disk

```
recording:   data/audio/<id>/mic.pcm + system.pcm     raw 16 kHz int16, appended + flushed every ~250 ms  (~115 MB/hour/track)
stopped:     each track -> mic.ogg / system.ogg        Opus 48 kbps (~21 MB/hour/track); .pcm deleted only after the .ogg is verified
processed:   the .ogg files stay for TRAILMIX_AUDIO_RETENTION_DAYS (default 30), then the folder is deleted (checked at startup and every 6 h)
any time:    "Delete audio now" in the meeting view, or deleting the meeting
```

A crash or restart mid-recording loses nothing already flushed: on startup, interrupted meetings are compressed and processed automatically.

## Configuration (environment variables)

| Variable | Default | |
|---|---|---|
| `TRAILMIX_ACCESS_TOKEN` | – | require sign-in (set this for any non-local install) |
| `TRAILMIX_<SETTING>` | – | preset any Settings value, e.g. `TRAILMIX_TRANSCRIBE_ENGINE=remote` |
| `TRAILMIX_LIVE_MODEL` | `mlx-community/whisper-base-mlx` | live draft model |
| `TRAILMIX_WHISPER_MODEL` | `mlx-community/whisper-large-v3-turbo` | final transcript model |
| `TRAILMIX_LANGUAGE` | `en` | Whisper language; empty = auto-detect |
| `TRAILMIX_AUDIO_RETENTION_DAYS` | `30` | `0` = delete audio right after transcription, `-1` = keep forever |
| `TRAILMIX_RAM_HEADROOM_GB` | `2` | free RAM to keep on top of what a model needs |
| `TRAILMIX_WHISPER_RAM_GB` | `3.0` | estimated RAM for the final model (gate) |
| `OLLAMA_URL` / `OLLAMA_MODEL` / `OLLAMA_KEEP_ALIVE` | `localhost:11434` / first installed / `0` | also editable in Settings |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY` | – | used if no key is saved in Settings |

## Capturing the other side of a call

**With the menu bar recorder (recommended; built into Trailmix.app):** it records your mic plus the audio of other
apps (Zoom, Teams, Slack, FaceTime and iPhone calls on the Mac, or everything the Mac plays), with no
screen-share picker and no virtual audio device. (From a source install, `./trailmix helper` builds it as
"Trailmix Helper"; that needs macOS 14.2+ and the Xcode command line tools, `xcode-select --install`.)

- **⌃⌥⌘R** starts and stops a recording from anywhere; **⌃⌥⌘M** marks a moment. Both are changeable in its
  Settings, and both also work on a recording started in the Trailmix window.
- The menu shows the timer, input levels for You and Them, and the live draft. Pick the microphone, where
  the meeting audio comes from (all apps, one app, or none for in-person meetings) and whether to draft live.
- It streams to the same backend as the web app, so the Trailmix window shows the recording live and can
  mark or stop it. If Trailmix isn't running, starting a recording starts it.
- The first recording asks for **Microphone** and **System Audio Recording** permission. If "Them" stays
  silent, allow it under System Settings → Privacy & Security → Screen & System Audio Recording. macOS asks
  again after the helper is rebuilt (it's signed locally, so each build looks new).
- For a hosted Trailmix, set its address and access token in the helper's Settings.

**In the browser:** press **Start recording**, allow the microphone, then in the share picker choose a
**browser tab** running your meeting and tick **Share tab audio**. Chrome on macOS only shares audio from
tabs (not the whole screen or desktop apps such as Zoom), so for the Zoom desktop app use the helper. With
headphones you avoid your mic hearing the speakers; without them echo is suppressed as best it can.
