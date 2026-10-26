# Trailmix for Windows

The Windows shell: a [Tauri](https://tauri.app) app (Rust, WebView2) that does what the Mac menu bar app does
with the same server and the same web app.

- `src-tauri/src/server.rs` starts and watches over the bundled Trailmix server (`python/python.exe -m uvicorn`),
  as `ServerProcess.swift` does; data goes to `%LOCALAPPDATA%\Trailmix\data` (`TRAILMIX_DATA_DIR` overrides it).
- `model.rs` is the recorder: the recorder socket the window's Record button reaches it through, a recording's
  sources and audio socket (`wire.rs`: the same 16 kHz mono int16 frames with a channel byte as `AudioWire.swift`).
- `audio/wasapi.rs` records the mic and other apps' audio: per-process loopback (Windows 10 2004 and later;
  all apps but this one, or one app's process tree), else the whole output. `audio/apps.rs` lists the apps playing sound.
- `window.rs` shows the web app in a WebView2 window; `app.rs` is the tray icon and its menu.
- `selftest.rs`: `--self-test <url> <me.wav> <them.wav>`, `--window-test <url> <out.png>` and `--list-audio`, for CI
  (`scripts/smoke-test-windows.py`) and for checking a real machine.

Build the installer with `python scripts/build-windows.py` (on Windows: Python, Node.js and Rust needed). The GitHub
`windows` job in `.github/workflows/release.yml` does that and runs the smoke test on every tag.

Not yet on Windows (their capability flags are off, so the web app hides them): the updater, hotkeys, call
reminders, open at login, the sound check.
