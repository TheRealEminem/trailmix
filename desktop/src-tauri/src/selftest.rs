//! Checks the shell without a microphone or any audio device (SelfTest.swift): audio files stand in for
//! the mic and the meeting, a stand-in for Windows' microphone records for a call app, a stand-in AI for
//! Catch me up, and everything else is the real thing (model, shortcuts, sound check, reminder card, the
//! recording card with the live draft and Catch me up, recorder socket, audio socket, window, server).
//!
//!   Trailmix.exe --self-test http://127.0.0.1:8797 me.wav them.wav [--no-window] [--shots=<folder>]
//!   Trailmix.exe --window-test http://127.0.0.1:8765/#meeting-2 window.png [seconds]
//!   Trailmix.exe --list-audio
//!
//! The self-test registers the real shortcuts, so quit Trailmix first if it's running on this PC.

use std::path::{Path, PathBuf};
#[cfg(windows)]
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use serde_json::Value;
use tauri::{AppHandle, Manager};

use crate::app::AppState;
#[cfg(windows)]
use crate::audio;
#[cfg(windows)]
use crate::card::{self, Target};
use crate::catchup::{self, Target as CardTarget, READING};
use crate::catchup_card;
#[cfg(windows)]
use crate::gui::{self, WS_EX_NOACTIVATE, WS_EX_TOPMOST};
use crate::hotkeys::{MARK, RECORD};
#[cfg(windows)]
use crate::hotkeys::{self, Shortcut, MOD_ALT, MOD_CONTROL, MOD_SHIFT};
use crate::model::{Connection, Model, Phase, Sources};
use crate::prefs::{tidy_url, Prefs};
#[cfg(windows)]
use crate::reminder::{self, Reminder};
use crate::soundcheck;
#[cfg(windows)]
use crate::win;
use crate::window;
use crate::wire::{AudioSource, FileSource, MEETING, MIC};

// The shortcuts, the call reminder and the audio list, as Linux has them.
#[cfg(target_os = "linux")]
pub use crate::linux::selftest::list_audio;
#[cfg(target_os = "linux")]
use crate::linux::selftest::{press, reminders, shortcuts};

/// What the meeting-audio menu would offer right now, and what this Windows can do. Needs no permission.
#[cfg(windows)]
pub fn list_audio() {
    audio::wasapi::com_init();
    println!("Windows {} (build {})", crate::win::os_version(), crate::win::os_build());
    println!(
        "Per-process loopback: {}",
        if audio::wasapi::process_loopback_supported() { "available" } else { "not available (needs Windows 10 2004)" }
    );
    println!("Default microphone: {}", audio::wasapi::default_microphone().unwrap_or_else(|| "none".into()));
    println!("Default output: {}", audio::wasapi::default_output().unwrap_or_else(|| "none".into()));
    println!("Apps connected to audio:");
    for app in audio::apps::current() {
        println!("  {} [{} pid {}]{}", app.name, app.exe, app.pid, if app.playing { "  playing" } else { "" });
    }
    println!("Apps that have used the microphone (the ones using it now marked):");
    for entry in reminder::mic_entries(reminder::CONSENT_STORE) {
        let call = entry.call_name().map(|c| format!("  [{c}]")).unwrap_or_default();
        println!("  {}{}{}", entry.key, if entry.in_use() { "  IN USE" } else { "" }, call);
    }
    for (what, shortcut) in [("Start / stop", Shortcut::RECORD_DEFAULT), ("Mark moment", Shortcut::MARK_DEFAULT)] {
        let free = if hotkeys::is_free(shortcut) { "free" } else { "taken (by Trailmix, or another app)" };
        println!("{what} shortcut {}: {free}", shortcut.display());
    }
    println!("Recorder capabilities: {}", Model::capabilities().join(", "));
}

pub(crate) struct Test {
    pub failures: u32,
}

impl Test {
    pub fn expect(&mut self, ok: bool, what: &str) {
        println!("{}  {what}", if ok { "  ok  " } else { "  FAIL" });
        if !ok {
            self.failures += 1;
        }
    }
}

pub(crate) fn wait_until(seconds: f64, mut condition: impl FnMut() -> bool) -> bool {
    let deadline = Instant::now() + Duration::from_secs_f64(seconds);
    while Instant::now() < deadline {
        if condition() {
            return true;
        }
        thread::sleep(Duration::from_millis(100));
    }
    condition()
}

fn get_json(url: &str) -> Option<Value> {
    minreq::get(url).with_timeout(10).send().ok().filter(|r| r.status_code == 200).and_then(|r| r.json().ok())
}

fn get_text(url: &str) -> Option<String> {
    minreq::get(url).with_timeout(10).send().ok().filter(|r| r.status_code == 200).and_then(|r| r.as_str().ok().map(String::from))
}

fn post(url: &str) -> Option<i32> {
    minreq::post(url).with_timeout(10).send().ok().map(|r| r.status_code)
}

fn args_after<'a>(args: &'a [String], flag: &str) -> Vec<&'a String> {
    args.iter().skip_while(|a| *a != flag).skip(1).filter(|a| !a.starts_with("--")).collect()
}

/// --self-test <server> <mic audio> [<meeting audio>] [--no-window] [--shots=<folder>]
pub fn run(app: AppHandle, args: Vec<String>) {
    let rest = args_after(&args, "--self-test");
    if rest.len() < 2 {
        println!("usage: --self-test <server> <mic audio> [<meeting audio>] [--no-window] [--shots=<folder>]");
        std::process::exit(2);
    }
    let server = tidy_url(rest[0]);
    let me = PathBuf::from(rest[1]);
    let them = rest.get(2).map(PathBuf::from);
    let no_window = args.iter().any(|a| a == "--no-window");
    let shots = args.iter().find_map(|a| a.strip_prefix("--shots=")).map(PathBuf::from);

    let model = Model::new(Prefs::default(), None, app.package_info().version.to_string(), Some(server.clone()));
    // The sound check hears the same files: your side, then the meeting's (the chimes are played for real,
    // and go nowhere on a computer without speakers).
    let (check_me, check_them) = (me.clone(), them.clone().unwrap_or_else(|| me.clone()));
    model.set_make_sound_check(Box::new(move || {
        let source = |path: &PathBuf| -> Box<dyn AudioSource> {
            match FileSource::new(path) {
                Ok(file) => Box::new(file),
                Err(e) => Box::new(Broken(e)),
            }
        };
        soundcheck::Kit { mic: source(&check_me), system: source(&check_them), chime: Arc::new(soundcheck::play_chime), mic_blocked: false }
    }));
    model.set_make_sources(Box::new(move |_| {
        let mic: Box<dyn AudioSource> = Box::new(FileSource::new(&me)?);
        let mut sources: Sources = vec![(MIC, mic)];
        if let Some(them) = &them {
            let meeting: Box<dyn AudioSource> = Box::new(FileSource::new(them)?);
            sources.push((MEETING, meeting));
        }
        Ok(sources)
    }));
    app.manage(AppState::new(model.clone(), None));
    if let Err(e) = window::create(&app) {
        println!("couldn't make the window: {e}");
        std::process::exit(1);
    }
    model.begin();

    thread::spawn(move || {
        let mut t = Test { failures: 0 };
        shortcuts(&mut t, &model);
        check(&mut t, &model, &server);
        sound_check(&mut t, &model, &server);
        reminders(&mut t, &model, shots.as_deref());
        catch_up(&mut t, &model, &server, shots.as_deref());
        if !no_window {
            window_record(&mut t, &app, &model, &server);
        }
        println!("{}", if t.failures == 0 { "All checks passed.".to_string() } else { format!("{} check(s) failed.", t.failures) });
        std::process::exit(if t.failures == 0 { 0 } else { 1 });
    });
}

/// The shortcuts registered the way the app registers them (HotKeys.swift's register), and a taken one noticed.
#[cfg(windows)]
fn shortcuts(t: &mut Test, model: &Arc<Model>) {
    println!("0. The shortcuts");
    let pressed = model.clone();
    t.expect(hotkeys::start(move |id| hotkeys::act(&pressed, id, false)), "the shortcut thread is up");
    let prefs = model.prefs();
    let taken = hotkeys::apply(model);
    t.expect(
        taken.is_empty(),
        &format!(
            "registered {} and {}{}",
            prefs.shortcut_record.display(),
            prefs.shortcut_mark.display(),
            if taken.is_empty() { "" } else { " (FAILED: taken by another app; is Trailmix running?)" }
        ),
    );
    t.expect(!hotkeys::is_free(prefs.shortcut_record), "another app can't have it now (so a taken shortcut is noticed)");
    let spare = Shortcut { key: 0x87, mods: MOD_CONTROL | MOD_ALT | MOD_SHIFT };
    t.expect(hotkeys::is_free(spare), &format!("{} is free (so a free one reads as free)", spare.display()));
}

/// Presses a shortcut: typed on the keyboard, if Windows takes keystrokes here; else handed to the shortcut
/// thread the way Windows hands it a press. Says which.
#[cfg(windows)]
fn press(id: i32, shortcut: Shortcut) -> Option<&'static str> {
    let before = hotkeys::PRESSES.load(Ordering::SeqCst);
    let arrived = || hotkeys::PRESSES.load(Ordering::SeqCst) > before;
    if hotkeys::type_keys(shortcut) && wait_until(2.0, &arrived) {
        return Some("typed on the keyboard");
    }
    if hotkeys::post_press(id) && wait_until(2.0, &arrived) {
        return Some("handed to the shortcut thread, as Windows does (no keyboard input here)");
    }
    None
}

fn check(t: &mut Test, model: &Arc<Model>, base: &str) {
    model.poll();
    let online = wait_until(5.0, || model.snapshot().connection == Connection::Online);
    t.expect(online, &format!("reaches the server ({:?})", model.snapshot().connection));
    let prefs = model.prefs();
    let (record, mark) = (prefs.shortcut_record, prefs.shortcut_mark);

    println!("1. The start/stop shortcut ({}) starts a recording", record.display());
    let how = press(RECORD, record);
    t.expect(how.is_some(), &format!("pressed: {}", how.unwrap_or("it never arrived")));
    let recording = wait_until(20.0, || model.snapshot().phase == Phase::Recording);
    t.expect(recording, &format!("recording ({})", model.snapshot().error.unwrap_or_else(|| "no error".into())));
    let first = model.snapshot().meeting_id;
    thread::sleep(Duration::from_secs(5));
    println!("2. The mark shortcut ({}) flags a moment", mark.display());
    let how = press(MARK, mark);
    t.expect(how.is_some(), &format!("pressed: {}", how.unwrap_or("it never arrived")));
    t.expect(wait_until(5.0, || !model.snapshot().marks.is_empty()), &format!("server confirmed the mark: {:?}", model.snapshot().marks));
    let draft = wait_until(25.0, || !model.snapshot().drafts.is_empty());
    let first_line = model.snapshot().drafts.first().map(|d| d.text.chars().take(60).collect::<String>()).unwrap_or_else(|| "none".into());
    t.expect(draft, &format!("live draft arrived: {first_line}"));
    println!("3. The start/stop shortcut stops it");
    let how = press(RECORD, record);
    t.expect(how.is_some(), &format!("pressed: {}", how.unwrap_or("it never arrived")));
    t.expect(wait_until(15.0, || model.snapshot().phase == Phase::Idle), "stopped cleanly");
    let snap = model.snapshot();
    t.expect(snap.last_meeting_id == first && snap.error.is_none(), &format!("meeting {} saved, no error", first.unwrap_or(-1)));

    println!("4. A recording stopped from the Trailmix window ends here too");
    model.toggle();
    t.expect(wait_until(20.0, || model.snapshot().phase == Phase::Recording), "recording again");
    let second = model.snapshot().meeting_id.unwrap_or(-1);
    thread::sleep(Duration::from_secs(3));
    let _ = post(&format!("{base}/api/meetings/{second}/stop"));
    t.expect(wait_until(10.0, || model.snapshot().phase == Phase::Idle), "stopped from outside");
    let error = model.snapshot().error;
    t.expect(error.is_none(), &format!("no error shown ({})", error.unwrap_or_default()));

    println!("5. The Record button in the Trailmix window starts a recording here");
    model.check_in();
    thread::sleep(Duration::from_millis(500));
    let status = get_json(&format!("{base}/api/recorder")).unwrap_or(Value::Null);
    t.expect(
        status.get("available").and_then(|v| v.as_bool()) == Some(true),
        &format!(
            "the window sees the recorder ({}, {}, {})",
            status.get("mic").and_then(|v| v.as_str()).unwrap_or("?"),
            status.get("source").and_then(|v| v.as_str()).unwrap_or("?"),
            status.get("platform").and_then(|v| v.as_str()).unwrap_or("?")
        ),
    );
    let capabilities: Vec<&str> =
        status.get("capabilities").and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|c| c.as_str()).collect()).unwrap_or_default();
    t.expect(
        capabilities.contains(&"hotkeys") && capabilities.contains(&"call_reminder") && capabilities.contains(&"sound_check"),
        &format!("it offers the shortcuts, call reminders and the sound check ({})", capabilities.join(", ")),
    );
    let shown = status.get("shortcut_record").and_then(|v| v.as_str()).unwrap_or("");
    t.expect(
        shown == record.display() && status.get("call_reminders").and_then(|v| v.as_bool()) == Some(true),
        &format!("the window is told the shortcut ({shown}) and that reminders are on"),
    );
    let pressed = post(&format!("{base}/api/recorder/start"));
    t.expect(pressed == Some(202), "Record pressed in the window");
    model.check_in();
    t.expect(wait_until(20.0, || model.snapshot().phase == Phase::Recording), "recording started from the window");
    thread::sleep(Duration::from_secs(2));
    model.toggle();
    t.expect(wait_until(15.0, || model.snapshot().phase == Phase::Idle), "stopped cleanly");
}

/// A source that can't start (a stand-in file that couldn't be read).
struct Broken(String);

impl AudioSource for Broken {
    fn name(&self) -> String {
        "broken".into()
    }
    fn start(&mut self, _: crate::wire::Sink) -> Result<(), String> {
        Err(self.0.clone())
    }
    fn stop(&mut self) {}
}

/// The sound check asked for the way the Trailmix window asks (POST /api/recorder/sound-check): it reports
/// "running", then both sides heard, through the check-in the window reads.
fn sound_check(t: &mut Test, model: &Arc<Model>, base: &str) {
    println!("5b. The sound check from the Trailmix window");
    #[cfg(windows)]
    {
        let blocked = win::mic_blocked();
        println!("      (this PC's microphone privacy switch: {})", if blocked { "off" } else { "on" });
    }
    // Refused (409) while the server still counts the last recording as live: asked again until it isn't.
    let mut asked = None;
    wait_until(15.0, || {
        asked = post(&format!("{base}/api/recorder/sound-check"));
        asked == Some(202) || { thread::sleep(Duration::from_millis(400)); false }
    });
    t.expect(asked == Some(202), &format!("Run sound check pressed in the window ({asked:?})"));
    let reported = |model: &Arc<Model>| {
        model.check_in();
        get_json(&format!("{base}/api/recorder")).and_then(|s| s.get("sound_check").cloned()).unwrap_or(Value::Null)
    };
    let mut seen = Value::Null;
    let running = wait_until(5.0, || {
        seen = reported(model);
        seen.get("running").and_then(|v| v.as_bool()) == Some(true) || seen.get("at").is_some()
    });
    t.expect(running, &format!("the window is told it's running ({seen})"));
    let done = wait_until(20.0, || {
        seen = reported(model);
        seen.get("running").is_none() && seen.get("at").is_some()
    });
    let field = |name: &str| seen.get(name).map(|v| v.to_string().trim_matches('"').to_string()).unwrap_or_else(|| "?".into());
    t.expect(
        done && field("mic") == "ok" && field("system") == "ok",
        &format!(
            "the sound check heard both sides (mic {} at {}, other apps {} at {}{})",
            field("mic"),
            field("mic_level"),
            field("system"),
            field("system_level"),
            seen.get("detail").and_then(|d| d.as_str()).filter(|d| !d.is_empty()).map(|d| format!(": {d}")).unwrap_or_default()
        ),
    );
    let kept = model.sound_check_result().unwrap_or(Value::Null);
    t.expect(kept.get("mic") == seen.get("mic") && kept.get("system") == seen.get("system"), "and the recorder keeps the result");
}

/// A stand-in for Windows' microphone records (the ConsentStore), so a "call" can start and end on cue.
#[cfg(windows)]
const STAND_IN: &str = "Software\\Trailmix\\SelfTest\\ConsentStore\\microphone";
#[cfg(windows)]
const STAND_IN_ROOT: &str = "Software\\Trailmix\\SelfTest";

#[cfg(windows)]
fn zoom_call(on: bool) {
    let key = format!("{STAND_IN}\\NonPackaged\\C:#Program Files#Zoom#bin#Zoom.exe");
    let now = win::filetime_now();
    win::reg_set_qword(&key, "LastUsedTimeStart", now);
    win::reg_set_qword(&key, "LastUsedTimeStop", if on { 0 } else { now + 1 });
}

/// Waits for a card whose title starts so.
#[cfg(windows)]
fn card_titled(seconds: f64, title: &str) -> Option<card::Shown> {
    let mut found = None;
    wait_until(seconds, || {
        found = card::current().filter(|c| c.title.starts_with(title));
        found.is_some()
    });
    found
}

/// Clicks a card's button: with the real mouse if Windows takes it here, else by handing the card the
/// click. Says which (None: the card didn't go).
#[cfg(windows)]
fn click(id: u64, target: Target) -> Option<&'static str> {
    let gone = || card::current().map(|c| c.id != id).unwrap_or(true);
    if card::press(id, target, true) && wait_until(3.0, &gone) {
        return Some("clicked with the mouse");
    }
    if card::press(id, target, false) && wait_until(3.0, &gone) {
        return Some("clicked (handed the click: no mouse input here)");
    }
    None
}

#[cfg(windows)]
fn shot(t: &mut Test, shots: Option<&Path>, name: &str) {
    let Some(folder) = shots else { return };
    let path = folder.join(name);
    t.expect(card::save_png(&path), &format!("saved {}", path.display()));
}

/// CallReminder.swift's card, driven by a stand-in Zoom "taking the mic": asked to record, the card's
/// Record records; the call ending asks to stop, and Stop recording stops; Don't ask for Zoom sticks; and
/// with reminders off nothing asks. The card must never take the focus.
#[cfg(windows)]
fn reminders(t: &mut Test, model: &Arc<Model>, shots: Option<&Path>) {
    println!("6. A call app taking the mic brings up the reminder");
    let own = reminder::mic_entries(reminder::CONSENT_STORE);
    println!("      (this PC's own records: {} apps, {} using the mic now)", own.len(), own.iter().filter(|e| e.in_use()).count());
    win::reg_delete_tree(STAND_IN_ROOT);
    let now = win::filetime_now();
    let teams = format!("{STAND_IN}\\MSTeams_8wekyb3d8bbwe"); // used the mic, done now
    win::reg_set_qword(&teams, "LastUsedTimeStart", now - 50_000_000);
    win::reg_set_qword(&teams, "LastUsedTimeStop", now - 10_000_000);
    let trailmix = format!("{STAND_IN}\\NonPackaged\\C:#Program Files#Trailmix#Trailmix.exe"); // ourselves: never a call
    win::reg_set_qword(&trailmix, "LastUsedTimeStart", now);
    win::reg_set_qword(&trailmix, "LastUsedTimeStop", 0);
    zoom_call(true);
    let entries = reminder::mic_entries(STAND_IN);
    let using = reminder::call_using_mic(&entries, None);
    t.expect(entries.len() == 3 && using.as_deref() == Some("Zoom"), &format!("reads who's using the mic: {} entries, {:?}", entries.len(), using));

    let watcher = Reminder::new(model.clone(), STAND_IN, Duration::from_millis(500), Duration::from_secs(4), false, false);
    let before = gui::foreground();
    watcher.begin();
    let Some(asked) = card_titled(8.0, "Zoom call started") else {
        t.expect(false, &format!("the card asks: Zoom call started (it shows {:?})", card::current().map(|c| c.title)));
        watcher.stop();
        win::reg_delete_tree(STAND_IN_ROOT);
        return;
    };
    t.expect(true, "the card asks: Zoom call started");
    t.expect(
        asked.visible && asked.ex_style & WS_EX_TOPMOST != 0 && asked.ex_style & WS_EX_NOACTIVATE != 0,
        "it's on top of everything, and can't be activated",
    );
    thread::sleep(Duration::from_millis(500));
    let after = gui::foreground();
    t.expect(after != asked.hwnd, &format!("the call keeps the focus (in front: {before:#x}, then {after:#x}; the card is {:#x})", asked.hwnd));
    shot(t, shots, "smoke-reminder.png");
    let how = click(asked.id, Target::Button(0));
    t.expect(how.is_some(), &format!("Record: {}", how.unwrap_or("the card didn't take the click")));
    t.expect(
        wait_until(20.0, || model.snapshot().phase == Phase::Recording),
        &format!("the card's Record started a recording ({})", model.snapshot().error.unwrap_or_else(|| "no error".into())),
    );
    t.expect(gui::foreground() != asked.hwnd, "clicking it didn't take the focus either");

    println!("7. The call ending asks to stop the recording");
    thread::sleep(Duration::from_secs(2));
    zoom_call(false);
    match card_titled(15.0, "The call seems to have ended") {
        Some(ended) => {
            t.expect(true, "the card asks: The call seems to have ended");
            shot(t, shots, "smoke-reminder-stop.png");
            let how = click(ended.id, Target::Button(0));
            t.expect(how.is_some(), &format!("Stop recording: {}", how.unwrap_or("the card didn't take the click")));
            t.expect(wait_until(15.0, || model.snapshot().phase == Phase::Idle), "the card's Stop recording stopped it");
        }
        None => t.expect(false, &format!("the card asks: The call seems to have ended (it shows {:?})", card::current().map(|c| c.title))),
    }
    if model.snapshot().phase == Phase::Recording {
        model.stop();
        wait_until(15.0, || model.snapshot().phase == Phase::Idle);
    }

    println!("8. Don't ask for Zoom");
    thread::sleep(Duration::from_millis(1500)); // the call is over: the next one is asked about again
    zoom_call(true);
    match card_titled(8.0, "Zoom call started") {
        Some(again) => {
            t.expect(true, "the next call is asked about again");
            let how = click(again.id, Target::Link);
            t.expect(how.is_some(), &format!("Don't ask for Zoom: {}", how.unwrap_or("the card didn't take the click")));
            t.expect(
                wait_until(3.0, || model.prefs().muted_call_apps.iter().any(|a| a == "Zoom")),
                &format!("Zoom is left alone from now on ({:?})", model.prefs().muted_call_apps),
            );
        }
        None => t.expect(false, "the next call is asked about again"),
    }
    zoom_call(false);
    thread::sleep(Duration::from_millis(1500));
    zoom_call(true);
    let asked_again = card_titled(3.0, "Zoom").is_some();
    t.expect(!asked_again, "and its next call isn't asked about");

    println!("9. With reminders off, nothing asks");
    model.update_prefs(|prefs| {
        prefs.muted_call_apps.clear();
        prefs.call_reminders = false;
    });
    zoom_call(false);
    thread::sleep(Duration::from_millis(1500));
    zoom_call(true);
    let asked_anyway = card_titled(3.0, "Zoom").is_some();
    t.expect(!asked_anyway, "no card");
    model.update_prefs(|prefs| prefs.call_reminders = true);

    watcher.stop();
    win::reg_delete_tree(STAND_IN_ROOT);
}

// MARK: The live draft and Catch me up, from the tray

/// What the stand-in AI says to every question but "fail".
const STAND_IN_ANSWER: &str = "- The launch moved to **March** [0:04]\n- Priya updates the docs before the beta [0:09]";

/// A stand-in for an OpenAI-compatible AI on this computer (as Ollama or LM Studio would be): every question gets
/// the same two points, except "fail", which gets an error. Returns its port.
fn stand_in_ai() -> Option<u16> {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").ok()?;
    let port = listener.local_addr().ok()?.port();
    thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            thread::spawn(move || answer_like_an_ai(stream));
        }
    });
    Some(port)
}

fn answer_like_an_ai(mut stream: std::net::TcpStream) {
    use std::io::{Read, Write};
    let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
    let mut data: Vec<u8> = Vec::new();
    let mut buffer = [0u8; 16384];
    let mut need: Option<usize> = None;
    while need.map(|n| data.len() < n).unwrap_or(true) {
        let got = match stream.read(&mut buffer) {
            Ok(0) | Err(_) => break,
            Ok(got) => got,
        };
        data.extend_from_slice(&buffer[..got]);
        if need.is_none() {
            if let Some(end) = data.windows(4).position(|w| w == b"\r\n\r\n") {
                let head = String::from_utf8_lossy(&data[..end]).to_lowercase();
                let length = head.lines().find_map(|l| l.strip_prefix("content-length:")).and_then(|v| v.trim().parse::<usize>().ok()).unwrap_or(0);
                need = Some(end + 4 + length);
            }
        }
    }
    let text = String::from_utf8_lossy(&data);
    let (status, body) = if !text.contains("/chat/completions") {
        ("404 Not Found", r#"{"error": {"message": "not here"}}"#.to_string())
    } else if text.contains("QUESTION: fail") {
        ("500 Internal Server Error", r#"{"error": {"message": "the stand-in AI is down"}}"#.to_string())
    } else {
        let reply = serde_json::json!({
            "choices": [{"message": {"role": "assistant", "content": STAND_IN_ANSWER}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        });
        ("200 OK", reply.to_string())
    };
    let reply = format!("HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
    let _ = stream.write_all(reply.as_bytes());
}

fn put_settings(base: &str, body: &Value) -> Option<i32> {
    minreq::put(format!("{base}/api/settings"))
        .with_header("Content-Type", "application/json")
        .with_body(body.to_string())
        .with_timeout(10)
        .send()
        .ok()
        .map(|r| r.status_code)
}

/// Waits for the recording card to show something.
fn catch_up_card(seconds: f64, mut test: impl FnMut(&catchup_card::Shown) -> bool) -> Option<catchup_card::Shown> {
    let mut found = None;
    wait_until(seconds, || {
        found = catchup_card::current().filter(|s| test(s));
        found.is_some()
    });
    found
}

/// Clicks something on the recording card: with the real mouse if this computer takes it, else by handing the
/// card the click. Says which (None: `done` never came true).
fn tap(target: CardTarget, done: impl Fn() -> bool) -> Option<&'static str> {
    if catchup_card::press(target, true) && wait_until(3.0, &done) {
        return Some("clicked with the mouse");
    }
    if catchup_card::press(target, false) && wait_until(3.0, &done) {
        return Some("clicked (handed the click: no mouse input here)");
    }
    None
}

/// Whether the recording card has the keyboard.
#[cfg(windows)]
fn card_has_focus() -> bool {
    let card = catchup_card::window();
    card != 0 && gui::foreground() == card
}

#[cfg(not(windows))]
fn card_has_focus() -> bool {
    catchup_card::current().map(|s| s.focused).unwrap_or(false)
}

fn card_shot(t: &mut Test, shots: Option<&Path>, name: &str) {
    let Some(folder) = shots else { return };
    let path = folder.join(name);
    t.expect(catchup_card::save_png(&path), &format!("saved {}", path.display()));
}

/// The Mac menu's recording panel, from the tray (SelfTest.swift's --catch-up-test): while a recording goes, Live
/// Draft… shows its last lines; Catch me up with no AI set up says why, as the Mac does; with an AI (a stand-in on
/// this computer), "What did I miss?" is answered in the card, and a question typed in the box that fails says why.
/// The card never takes the focus, except while you type in it, and hands it back after. It goes when the recording
/// ends.
fn catch_up(t: &mut Test, model: &Arc<Model>, base: &str, shots: Option<&Path>) {
    println!("9b. Live Draft… from the tray shows the last lines while recording");
    let c = catchup::begin(model);
    model.toggle();
    if !wait_until(20.0, || model.snapshot().phase == Phase::Recording) {
        t.expect(false, &format!("recording ({})", model.snapshot().error.unwrap_or_else(|| "no error".into())));
        return;
    }
    let id = model.snapshot().meeting_id.unwrap_or(-1);
    t.expect(wait_until(30.0, || !model.snapshot().drafts.is_empty()), "the live draft is coming");
    #[cfg(windows)]
    let before = gui::foreground();
    c.open(false);
    let latest = || model.snapshot().drafts.iter().rev().take(3).map(|d| d.text.trim().to_string()).filter(|d| !d.is_empty()).collect::<Vec<_>>();
    let shown = catch_up_card(10.0, |s| latest().iter().any(|d| s.text.contains(d.as_str())));
    let Some(shown) = shown else {
        t.expect(false, &format!("the card shows the newest lines (it shows {:?})", catchup_card::current().map(|s| s.text)));
        catchup_card::hide();
        model.stop();
        wait_until(15.0, || model.snapshot().phase == Phase::Idle);
        return;
    };
    let first: String = shown.text.lines().nth(1).unwrap_or("").chars().take(60).collect();
    t.expect(true, &format!("the card shows the newest lines: {first}"));
    t.expect(shown.visible && shown.above && shown.no_activate, "it's above the other windows, and can't take the focus");
    thread::sleep(Duration::from_millis(500));
    #[cfg(windows)]
    t.expect(!card_has_focus(), &format!("the call keeps the focus (in front: {before:#x}, then {:#x})", gui::foreground()));
    #[cfg(not(windows))]
    t.expect(!card_has_focus(), "the call keeps the focus");
    card_shot(t, shots, "smoke-live-draft.png");

    println!("9c. Catch me up with no AI set up says why, as on the Mac");
    let how = tap(CardTarget::Toggle, || catchup_card::current().map(|s| s.text.contains("▾")).unwrap_or(false));
    t.expect(how.is_some(), &format!("Catch me up ▸: {}", how.unwrap_or("the card didn't take the click")));
    let plan = get_json(&format!("{base}/api/meetings/{id}/catch-up")).unwrap_or(Value::Null);
    let reason = plan.get("reason").and_then(|r| r.as_str()).unwrap_or("").to_string();
    let ready = plan.get("ready").and_then(|r| r.as_bool());
    if ready == Some(true) {
        // (Run on a computer with an AI of its own: there's no "no AI" to see.)
        println!("      (an AI is set up on this server already: {}; skipping the no-AI check)", plan.get("label").unwrap_or(&Value::Null));
    } else {
        t.expect(ready == Some(false) && !reason.is_empty(), &format!("the server says it can't answer: {reason}"));
        let says = catch_up_card(10.0, |s| !reason.is_empty() && s.text.contains(&reason));
        t.expect(says.is_some(), "the card says so, in the same words");
        if says.is_none() {
            println!("      (the card shows: {:?})", catchup_card::current().map(|s| s.text));
        }
        t.expect(says.map(|s| !s.text.contains("What did I miss?")).unwrap_or(false), "and offers no questions to ask");
        t.expect(!card_has_focus(), "the call still has the focus");
        card_shot(t, shots, "smoke-catch-up-no-ai.png");
    }

    println!("9d. With an AI set up, What did I miss? is answered in the card");
    let saved = get_json(&format!("{base}/api/settings")).unwrap_or(Value::Null);
    let restore = serde_json::json!({
        "summary_provider": saved.get("summary_provider").and_then(|v| v.as_str()).unwrap_or("ollama"),
        "custom_base_url": saved.get("custom_base_url").and_then(|v| v.as_str()).unwrap_or(""),
        "custom_model": saved.get("custom_model").and_then(|v| v.as_str()).unwrap_or("")
    });
    let Some(port) = stand_in_ai() else {
        t.expect(false, "a stand-in AI to ask");
        model.stop();
        wait_until(15.0, || model.snapshot().phase == Phase::Idle);
        return;
    };
    let set = put_settings(base, &serde_json::json!({
        "summary_provider": "custom", "custom_base_url": format!("http://127.0.0.1:{port}/v1"), "custom_model": "stand-in"
    }));
    t.expect(set == Some(200), &format!("notes AI set to a stand-in on this computer, port {port} ({set:?})"));
    c.load();
    let offered = catch_up_card(10.0, |s| s.text.contains("What did I miss?") && c.plan().map(|p| p.ready).unwrap_or(false));
    t.expect(offered.is_some(), &format!("the card offers the quick questions ({:?})", c.plan().map(|p| p.label.or(p.reason))));
    thread::sleep(Duration::from_millis(800)); // (it grew upwards: let it settle where it goes before clicking)
    let how = tap(CardTarget::Quick(0), || c.pending().is_some() || c.last_answer().is_some() || c.error().is_some());
    t.expect(how.is_some(), &format!("What did I miss?: {}", how.unwrap_or("the card didn't take the click")));
    let reading = catch_up_card(3.0, |s| s.text.contains(READING)).is_some();
    println!("      ({})", if reading { "it said \"Reading the meeting so far…\" meanwhile" } else { "the answer came before a look at the card" });
    let answered = catch_up_card(60.0, |s| s.text.contains("March") && s.text.contains("asked at "));
    let line = c.last_answer().map(|e| e.a.lines().next().unwrap_or("").to_string()).unwrap_or_default();
    t.expect(answered.is_some(), &format!("the answer is in the card: {line} ({})", c.error().unwrap_or_else(|| "no error".into())));
    t.expect(!card_has_focus(), "asking didn't take the focus");
    card_shot(t, shots, "smoke-catch-up.png");

    println!("9e. A question typed in the box; one that fails says why");
    #[cfg(windows)]
    let front = gui::foreground();
    thread::sleep(Duration::from_millis(800));
    let real = catchup_card::ask_typed("fail", true);
    let typed = real || catchup_card::ask_typed("fail", false);
    t.expect(
        typed,
        if real { "clicked into the box (it takes the keyboard), typed and pressed Enter" } else { "typed in the box (handed to it: no keyboard or mouse input here)" },
    );
    let failed = catch_up_card(30.0, |s| s.text.contains("stand-in AI is down"));
    t.expect(failed.is_some(), &format!("the card says why: {}", c.error().unwrap_or_else(|| "no error shown".into())));
    let back = wait_until(3.0, || !catchup_card::typing() && !card_has_focus());
    #[cfg(windows)]
    {
        if real && front == 0 {
            // Nothing had the keyboard before (no desktop session in front): there's nothing to hand it back to.
            t.expect(!catchup_card::typing(), "done typing (nothing had the keyboard before it)");
        } else {
            t.expect(back, &format!("the keyboard went back after Enter (in front: {front:#x}, now {:#x})", gui::foreground()));
        }
    }
    #[cfg(not(windows))]
    t.expect(back, "the keyboard went back after Enter");
    let _ = put_settings(base, &restore);

    println!("9f. The card closes, and goes by itself when the recording ends");
    let how = tap(CardTarget::Close, || !catchup_card::is_open());
    t.expect(how.is_some(), &format!("×: {}", how.unwrap_or("the card didn't take the click")));
    c.open(true);
    t.expect(catch_up_card(5.0, |s| s.visible).is_some(), "Catch Me Up… opens it again");
    model.toggle();
    t.expect(wait_until(15.0, || model.snapshot().phase == Phase::Idle), "stopped cleanly");
    t.expect(wait_until(5.0, || !catchup_card::is_open()), "and the card went with the recording");
}

/// The real Trailmix window and page, the way you use them: Record clicked in the window; the recording
/// must go through the recorder, never the page's own capture. Then the transcript (made while
/// recording) must arrive.
fn window_record(t: &mut Test, app: &AppHandle, model: &Arc<Model>, base: &str) {
    println!("10. Record in the real window");
    let _ = minreq::put(format!("{base}/api/settings"))
        .with_header("Content-Type", "application/json")
        .with_body(r#"{"live_final": true}"#)
        .with_timeout(10)
        .send();
    window::show(app, base);
    let Some(win) = app.get_webview_window(window::LABEL) else {
        t.expect(false, "the window exists");
        return;
    };
    let loaded = wait_until(40.0, || window::evaluate(&win, "!!document.querySelector('[data-testid=record]')") == "true");
    t.expect(loaded, "the window shows the Record button");

    // The page the window runs must be the one the server has now (not a cached older version).
    let html = get_text(base).unwrap_or_default();
    let script = window::evaluate(&win, "document.querySelector('script[src*=\"assets/\"]')?.getAttribute('src')");
    let src = serde_json::from_str::<String>(&script).unwrap_or_else(|_| "?".into());
    t.expect(src != "?" && html.contains(&src), &format!("the window runs the current web app ({src})"));

    thread::sleep(Duration::from_secs(6));
    let _ = window::evaluate(&win, "document.querySelector('[data-testid=record]').click()");
    t.expect(
        wait_until(30.0, || model.snapshot().phase == Phase::Recording),
        &format!("the recorder started ({})", model.snapshot().error.unwrap_or_else(|| "no error".into())),
    );
    let id = model.snapshot().meeting_id.unwrap_or(-1);
    let page_error = window::evaluate(&win, "document.body.innerText.includes(\"Couldn't start recording\")");
    t.expect(page_error == "false", "the window shows no recording error");
    thread::sleep(Duration::from_secs(8));
    let stop_shown = wait_until(10.0, || window::evaluate(&win, "!!document.querySelector('[data-testid=stop]')") == "true");
    t.expect(stop_shown, "the window shows the recording");
    let _ = window::evaluate(&win, "document.querySelector('[data-testid=stop]').click()");
    t.expect(wait_until(20.0, || model.snapshot().phase == Phase::Idle), "Stop in the window stopped the recorder");

    let mut transcript = String::new();
    let done = wait_until(360.0, || {
        // generous: a busy build machine is slow
        let Some(m) = get_json(&format!("{base}/api/meetings/{id}")) else { return false };
        if m.get("status").and_then(|s| s.as_str()) == Some("error") {
            transcript = format!("error: {}", m.get("error").and_then(|e| e.as_str()).unwrap_or(""));
            return true;
        }
        transcript = m.get("transcript").and_then(|s| s.as_str()).unwrap_or("").to_string();
        !transcript.is_empty()
    });
    let shown: String = transcript.chars().take(80).collect::<String>().replace('\n', " ");
    t.expect(done && !transcript.starts_with("error") && !transcript.is_empty(), &format!("the transcript arrived: {shown}"));
}

/// --window-test <url> <out.png> [seconds]: opens the window at a URL and saves what it shows.
pub fn window_test(app: AppHandle, args: Vec<String>) {
    let rest = args_after(&args, "--window-test");
    if rest.len() < 2 {
        println!("usage: --window-test <url> <out.png> [seconds]");
        std::process::exit(2);
    }
    let url = tidy_url(rest[0]);
    let out = rest[1].clone();
    let wait = rest.get(2).and_then(|s| s.parse::<f64>().ok()).unwrap_or(4.0);
    let model = Model::new(Prefs::default(), None, app.package_info().version.to_string(), Some(url.clone()));
    app.manage(AppState::new(model, None));
    if let Err(e) = window::create(&app) {
        println!("couldn't make the window: {e}");
        std::process::exit(1);
    }
    thread::spawn(move || {
        window::show(&app, &url);
        let Some(win) = app.get_webview_window(window::LABEL) else {
            println!("no window");
            std::process::exit(1);
        };
        let ok = window::snapshot(&win, &out, Duration::from_secs_f64(wait));
        println!("{}", if ok { format!("saved {out}") } else { "couldn't take a snapshot".into() });
        std::process::exit(if ok && Path::new(&out).is_file() { 0 } else { 1 });
    });
}
