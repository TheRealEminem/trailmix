# Trailmix

Local-first meeting recorder, transcriber and note-taker for the Mac. Record any call: the menu bar app captures
your microphone and the other side as separate tracks, the accurate speech model transcribes while you talk, and
the notes (summary, decisions, action items, a title, tags and a workspace) follow within about a minute of
stopping, written by a local model through Ollama or a cloud AI you choose. Your meetings stay on your computer
unless you send them somewhere. The full specs are under [Features](#features).

## Install (Mac)

1. Download **Trailmix-x.y.z-arm64.dmg** from the [latest release](https://github.com/TheRealEminem/trailmix/releases/latest). It needs an Apple silicon Mac
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

**Recording**
- Both sides of a call as separate tracks (your mic, and the call app's audio via Core Audio process taps), so the
  transcript knows **You** from **Them**. Menu bar recorder with global hotkeys (⌃⌥⌘R start/stop, ⌃⌥⌘M flag a
  moment); Record in the window works through it. A sound check walks you through the macOS permissions.
- Audio is written to disk as it arrives (a crash loses nothing), then compressed to Opus. See *Audio on disk*.

**Transcription**
- Whisper (large-v3-turbo) on Apple silicon with MLX, or any OpenAI-compatible endpoint. With 16 GB or more the
  accurate model transcribes *while you record* (chunks of at most 5 s, cut at pauses, each given the previous
  words and your vocabulary as context), so the transcript is ready about a second after Stop; with less, a
  small model drafts live and the accurate one runs after. Silero VAD, hallucination and echo cleanup.
- The live model is loaded at startup, after each meeting's processing, and when a call app opens audio.
- Each meeting records what transcribed it (`whisper-large-v3-turbo, while recording`).

**Notes**
- Summaries with templates (general, 1:1, standup, interview, sales call, or your own), long meetings summarized
  in parts first, an auto-generated title (or a new one on demand), action items collected in **Tasks**.
- Flagged moments get their own section, written from your real flags.
- Any AI: Ollama (local, installable from setup), Claude, OpenAI, Gemini, DeepSeek or any OpenAI-compatible API,
  with a fallback. Models only run when there's memory for them (or macOS can swap without it being dire).

**Organizing**
- **Workspaces** (e.g. a job, a committee, personal) with Default for the rest: meetings, tasks, search and Ask
  follow the one you're in; ⌃1…⌃9 switch. New meetings are **tagged** by topic and **sorted** from their notes
  (a workspace named in the title wins; the AI must call the fit good; meetings you place by hand are examples
  for it and never moved). Setup and Settings can **suggest workspaces** from your meetings' topics.
- **Quick questions** when the AI isn't sure: who was on the call, a name one letter off one you know, a word that
  sounds like a mishearing, a workspace it only partly fits. Answers fix the meeting and join a **vocabulary**
  the speech model and the notes AI use from then on.
- **Better models**: each meeting remembers which model wrote its notes, tags and workspace; a ranked list of
  models (`backend/assets/model-ranks.json`, refreshed daily from `docs/model-ranks.json`) tells Trailmix when
  the one you use now is clearly better, and Settings offers to redo that work.

**Finding things**
- Full-text search (titles, transcripts, notes, tags); **Ask** one meeting or all of them (or one workspace),
  with answers linking back to their sources; click any transcript line to play from that moment.

**Import and export**
- Import from **Granola** (API or pasted), **transcript files** (WebVTT and SRT from Zoom, Teams and Meet; Otter
  TXT and DOCX; text), **recordings** (any audio or video ffmpeg reads; your file is never changed) and
  **Trailmix exports**.
- Export a folder per meeting: notes and transcript as PDF, Word, OpenDocument, Markdown or text, `meeting.json`
  with everything (re-importable), and optionally the recording (hard-linked, so no extra space). "Keep forever"
  exempts a meeting's audio from the 30-day clean-up.

**The app**
- Auto-updates from GitHub releases (checksum and signature checked). **Beta updates** get new versions first;
  a beta reaches everyone after 3 days unless a blocking report is open.
- **Report a problem** from any screen: private content is replaced by placeholders, then patterns, known names
  and your local AI remove personal details; you check the text before posting it as a GitHub issue.
- Spell checking (macOS's own) in text fields; Day and Dusk themes.

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

## License

GNU Affero General Public License v3.0 (AGPL-3.0), copyright (C) 2026 Trailmix contributors; see [LICENSE](LICENSE). The app bundles a static ffmpeg build (GPL), the Silero VAD model weights
(MIT, `backend/assets/silero_vad.LICENSE`), the Geist font (SIL OFL, `backend/assets/fonts/Geist.LICENSE`) and
other open-source packages under their own licenses.
