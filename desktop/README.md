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
- `update.rs` is the updater (`Updater.swift`): it looks at the GitHub releases at launch and every 6 hours for the
  newest one with a `Trailmix-<version>-windows-x64.exe` and its `.sha256`, and on request downloads it, checks the
  checksum and the installer's own version resource, quits, and lets a copy of `Trailmix.exe` (`--apply-update`) run
  it silently (`/S /UPDATE /D=<this folder>`) and open the new version. Go back installs the previous one the same way.
  It logs to `logs\update.log` in the data folder; the test overrides are those of the Mac (`TRAILMIX_UPDATE_URL`,
  `TRAILMIX_RELEASES_URL`, `TRAILMIX_UPDATE_DELAY`, `TRAILMIX_UPDATE_NO_RELAUNCH`). The repo it looks in is baked in
  by `build.rs` (`TRAILMIX_UPDATE_REPO`, else the checkout's origin).
- `login.rs` is open at login: a `Trailmix` value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` that
  starts the app with `--at-login` (tray and engine, no window).
- `selftest.rs`: `--self-test <url> <me.wav> <them.wav>`, `--window-test <url> <out.png>`, `--list-audio` (and
  `--version` in `main.rs`), for CI
  (`scripts/smoke-test-windows.py`) and for checking a real machine.

Build the installer with `python scripts/build-windows.py` (on Windows: Python, Node.js and Rust needed, plus Visual
Studio's C++ tools and the [Vulkan SDK](https://vulkan.lunarg.com/sdk/home#windows) for the x64 build's whisper.cpp,
which it compiles with GPU support once and keeps in `build/cache`). A plain
`cargo test` in `src-tauri` wants the staged resources to exist first: `python scripts/build-windows.py --stage-only`,
or empty `build/windows/{python,backend,ui,bin}` folders and a `build/windows/VERSION` as the CI job makes. The GitHub
`windows` job in `.github/workflows/release.yml` does that and runs the smoke test on every tag: as a fresh install,
and as an update from the current release (until there's a Windows release, from the same build made as 0.0.1), then
going back to it.

- `hotkeys.rs` is the global shortcuts (`HotKeys.swift`): `RegisterHotKey` on a thread of its own, Ctrl+Alt+Shift+R
  to start and stop and Ctrl+Alt+Shift+M to mark a moment, changed in the tray menu's Settings (a card that takes
  the next key press). A shortcut another app has is refused and the old one kept. A press while the window isn't in
  front shows a short note in the corner ("Recording", "Moment marked").
- `reminder.rs` is the call reminder (`CallReminder.swift`): every 3 seconds it reads which apps are using the mic
  from Windows' privacy records (`HKCU\...\CapabilityAccessManager\ConsentStore\microphone`, Store apps and
  `NonPackaged` programs; `LastUsedTimeStop` is 0 while in use) and asks "Zoom call started. Record it?"; once a
  recorded call has let go of the mic and the speakers for a minute, "The call seems to have ended". `card.rs`
  draws those cards (plain Win32 and GDI, bottom right, always on top, `WS_EX_NOACTIVATE` so they never take the
  focus from the call).
- `selftest.rs` also presses the shortcuts (typed with `SendInput` where there's a desktop, else handed to the
  shortcut thread as Windows would) and drives the reminder with a stand-in Zoom in a test registry key
  (`HKCU\Software\Trailmix\SelfTest`, removed afterwards), clicking the cards' buttons; `--shots=<folder>` saves
  pictures of the cards. It registers the real shortcuts, so quit Trailmix before running it on your own PC.

Not yet on Windows (its capability flag is off, so the web app hides it): the sound check. The web app's sound check
walks you through the macOS permission prompts and System Settings; on Windows it would need its own wording.
