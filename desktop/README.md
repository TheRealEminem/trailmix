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

# Trailmix for Linux

The same Tauri app, built for Linux by `python3 scripts/build-linux.py` (on Linux: Python, Node.js, Rust and the
libraries Tauri builds against: `libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev libpulse-dev`)
into `dist/Trailmix-<version>-linux-<x86_64|arm64>.AppImage`, `.deb` and `.rpm`, each with a `.sha256`. Everything
shared stays shared; what's Windows-only is behind `cfg(windows)`, and Linux has its own counterparts in `linux/`
(behind `cfg(target_os = "linux")`):

- Data goes to `$XDG_DATA_HOME/Trailmix` (`~/.local/share/Trailmix`); the engine is `python/bin/python3 -m uvicorn`
  in a process group of its own, stopped with SIGTERM, and stops by itself if the app dies (`TRAILMIX_PARENT_PID`).
- `audio/pulse.rs` records through PulseAudio's client API, which PipeWire serves too (`pipewire-pulse`): the mic
  from the default source, all apps from the default output's monitor (following it when it changes), and one
  app's playback streams on their own (a monitor stream per sink input, as pavucontrol's meters do). The sound
  server converts to the wire format itself. `audio/apps_linux.rs` lists the apps playing (sink inputs, by
  `application.process.binary`) and the apps recording from a mic (source outputs), for the call reminder.
- `linux/keys.rs` is the global shortcuts: an X11 key grab on the root window (a combination another app has
  grabbed is reported as taken). On Wayland a grab only sees keys typed into X11 apps, so there the shortcuts are
  off and the web app doesn't offer them (the GlobalShortcuts portal isn't used yet).
- `linux/card.rs` draws the cards with GTK (no frame, kept above, never taking the focus except the shortcut
  card). On X11 they sit bottom right; on Wayland the desktop places them.
- `linux/login.rs` is open at login: `~/.config/autostart/trailmix.desktop`, starting the AppImage (or
  `/usr/bin/Trailmix`) with `--at-login`.
- `linux/update.rs`: an AppImage updates itself, putting `Trailmix-<version>-linux-<arch>.AppImage` (checked against
  its `.sha256`, and for being an AppImage) in place of the file you run, under the same name, then opening it.
  A `.deb` or `.rpm` install only says an update is out ("manual" in the report); Update opens the release page.
- The tray icon is an AppIndicator (StatusNotifierItem): KDE, Cinnamon, XFCE, MATE and Budgie show it; GNOME
  needs the "AppIndicator and KStatusNotifierItem Support" extension (Ubuntu has it on). It has no left click
  (AppIndicator only has a menu): Open Trailmix is in the menu.
- `linux/selftest.rs`: the self-test's Linux parts, `--list-audio`, and `--capture-test <seconds> <folder> [app]`
  (records the real mic, all apps and one app at once and saves them as WAVs).

`scripts/smoke-test-linux.py` runs the app headless in a private Xvfb and a private PulseAudio (a null speaker and
a mic fed from a null sink), so nothing of yours is touched or heard: the engine with and without the app, the
checks shared with the Mac, real recording of speech played into those devices, a stand-in Zoom holding the mic,
the self-test, a picture of the window, and (given a previous AppImage) updating and going back. It tests a .deb
or .rpm by installing it (as root or with sudo). The `linux` job in `release.yml` runs it on GitHub's x86_64 and
ARM runners, and the packages on Debian and Fedora too.
