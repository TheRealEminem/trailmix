//! The self-test's Linux parts (selftest.rs has the rest): the shortcuts grabbed on X11 and typed with XTest,
//! the call reminder driven by a stand-in list of the apps using the mic, its cards clicked with XTest; what
//! the sound server has (`--list-audio`); and `--capture-test`, which records the real mic, all apps' audio
//! and one app's for a few seconds, for the smoke test to check against what it played into them.
//!
//!   Trailmix --capture-test <seconds> <out folder> [<app binary>]

use std::path::Path;
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use crate::audio::{self, pulse};
use crate::card::{self, Target};
use crate::hotkeys::{self, Shortcut, MOD_ALT, MOD_CONTROL, MOD_SHIFT};
use crate::model::{Model, Phase};
use crate::reminder::{self, Reminder};
use crate::selftest::{wait_until, Test};
use crate::wire::{meter_level, AudioSource, Sink, SAMPLE_RATE};

/// What the meeting-audio menu would offer right now, and what this computer can do. Needs no permission.
pub fn list_audio() {
    println!("Linux ({:?} session)", super::session());
    match pulse::snapshot() {
        Ok(s) => {
            println!("Sound server: {}", if s.server.is_empty() { "unknown" } else { &s.server });
            println!("Default microphone: {}", s.default_mic().map(|d| d.description.clone()).unwrap_or_else(|| "none".into()));
            println!("Default output: {}", s.default_output().map(|d| d.description.clone()).unwrap_or_else(|| "none".into()));
            println!("Apps playing to the sound server:");
            for app in audio::apps::apps_of(&s.playing) {
                println!("  {} [{} pid {}]{}", app.name, app.exe, app.pid, if app.playing { "  playing" } else { "" });
            }
            println!("Apps using a microphone:");
            for user in s.mic_users() {
                let call = reminder::call_name_for_exe(&user.binary).map(|c| format!("  [{c}]")).unwrap_or_default();
                println!("  {} [{} pid {}]{}{}", user.app_name, user.binary, user.pid, if user.corked { "  paused" } else { "  IN USE" }, call);
            }
        }
        Err(e) => println!("Sound server: {e}"),
    }
    let started = hotkeys::start(|_| {});
    for (what, shortcut) in [("Start / stop", Shortcut::RECORD_DEFAULT), ("Mark moment", Shortcut::MARK_DEFAULT)] {
        let free = if !started {
            "can't be had here (global shortcuts need an X11 session)"
        } else if hotkeys::is_free(shortcut) {
            "free"
        } else {
            "taken (by Trailmix, or another app)"
        };
        println!("{what} shortcut {}: {free}", shortcut.display());
    }
    println!("Recorder capabilities: {}", Model::capabilities().join(", "));
}

/// The shortcuts grabbed the way the app grabs them, and a taken one noticed.
pub fn shortcuts(t: &mut Test, model: &Arc<Model>) {
    println!("0. The shortcuts");
    let pressed = model.clone();
    t.expect(hotkeys::start(move |id| hotkeys::act(&pressed, id, false)), &format!("the shortcut thread is up ({:?} session)", super::session()));
    let prefs = model.prefs();
    let taken = hotkeys::apply(model);
    t.expect(
        taken.is_empty(),
        &format!(
            "grabbed {} and {}{}",
            prefs.shortcut_record.display(),
            prefs.shortcut_mark.display(),
            if taken.is_empty() { "" } else { " (FAILED: taken by another app; is Trailmix running?)" }
        ),
    );
    t.expect(!hotkeys::is_free(prefs.shortcut_record), "another app can't have it now (so a taken shortcut is noticed)");
    let spare = Shortcut { key: 0x87, mods: MOD_CONTROL | MOD_ALT | MOD_SHIFT };
    t.expect(hotkeys::is_free(spare), &format!("{} is free (so a free one reads as free)", spare.display()));
}

/// Presses a shortcut: typed with XTest, as a keyboard would; else handed to the shortcut thread. Says which.
pub fn press(id: i32, shortcut: Shortcut) -> Option<&'static str> {
    let before = hotkeys::PRESSES.load(Ordering::SeqCst);
    let arrived = || hotkeys::PRESSES.load(Ordering::SeqCst) > before;
    if hotkeys::type_keys(shortcut) && wait_until(2.0, &arrived) {
        return Some("typed on the keyboard (XTest)");
    }
    if hotkeys::post_press(id) && wait_until(2.0, &arrived) {
        return Some("handed to the shortcut thread (no keyboard input here)");
    }
    None
}

/// Waits for a card whose title starts so.
fn card_titled(seconds: f64, title: &str) -> Option<card::Shown> {
    let mut found = None;
    wait_until(seconds, || {
        found = card::current().filter(|c| c.title.starts_with(title));
        found.is_some()
    });
    found
}

/// Clicks a card's button: with the mouse (XTest) if there's an X server, else by clicking the button
/// directly. Says which (None: the card didn't go).
fn click(id: u64, target: Target) -> Option<&'static str> {
    let gone = || card::current().map(|c| c.id != id).unwrap_or(true);
    if card::press(id, target, true) && wait_until(3.0, &gone) {
        return Some("clicked with the mouse (XTest)");
    }
    if card::press(id, target, false) && wait_until(3.0, &gone) {
        return Some("clicked (the button pressed directly: no mouse here)");
    }
    None
}

fn shot(t: &mut Test, shots: Option<&Path>, name: &str) {
    let Some(folder) = shots else { return };
    let path = folder.join(name);
    t.expect(card::save_png(&path), &format!("saved {}", path.display()));
}

/// CallReminder.swift's card, driven by a stand-in list of the apps using the mic (as the sound server would
/// list them): asked to record, the card's Record records; the call ending asks to stop, and Stop recording
/// stops; Don't ask for Zoom sticks; and with reminders off nothing asks. The card must never take the focus.
pub fn reminders(t: &mut Test, model: &Arc<Model>, shots: Option<&Path>) {
    println!("6. A call app taking the mic brings up the reminder");
    let own = audio::apps::mic_users();
    println!("      (the sound server's own list: {} apps using a mic now)", own.len());
    let stand_in = std::env::temp_dir().join(format!("trailmix-selftest-mic-{}", std::process::id()));
    let root = stand_in.to_string_lossy().to_string();
    let zoom_call = |on: bool| {
        // Trailmix itself is on the list too: it's never a call.
        let _ = std::fs::write(&stand_in, if on { "trailmix\nzoom\n" } else { "trailmix\n" });
    };
    zoom_call(true);
    let users = reminder::mic_users(&root);
    let using = reminder::call_among(&users);
    t.expect(users.len() == 2 && using.as_deref() == Some("Zoom"), &format!("reads who's using the mic: {} apps, {:?}", users.len(), using));

    let watcher = Reminder::new(model.clone(), &root, Duration::from_millis(500), Duration::from_secs(4), false, false);
    watcher.begin();
    let Some(asked) = card_titled(8.0, "Zoom call started") else {
        t.expect(false, &format!("the card asks: Zoom call started (it shows {:?})", card::current().map(|c| c.title)));
        watcher.stop();
        let _ = std::fs::remove_file(&stand_in);
        return;
    };
    t.expect(true, "the card asks: Zoom call started");
    t.expect(asked.visible && asked.keep_above && !asked.accepts_focus, "it's above the other windows, and can't take the focus");
    thread::sleep(Duration::from_millis(500));
    let focused = card::current().map(|c| c.focused).unwrap_or(false);
    t.expect(!focused, "the call keeps the focus");
    shot(t, shots, "smoke-reminder.png");
    let how = click(asked.id, Target::Button(0));
    t.expect(how.is_some(), &format!("Record: {}", how.unwrap_or("the card didn't take the click")));
    t.expect(
        wait_until(20.0, || model.snapshot().phase == Phase::Recording),
        &format!("the card's Record started a recording ({})", model.snapshot().error.unwrap_or_else(|| "no error".into())),
    );

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
    let _ = std::fs::remove_file(&stand_in);
}

// MARK: --capture-test

fn write_wav(path: &Path, samples: &[i16]) -> std::io::Result<()> {
    let data = (samples.len() * 2) as u32;
    let mut out = Vec::with_capacity(44 + data as usize);
    out.extend_from_slice(b"RIFF");
    out.extend_from_slice(&(36 + data).to_le_bytes());
    out.extend_from_slice(b"WAVEfmt ");
    out.extend_from_slice(&16u32.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes()); // PCM
    out.extend_from_slice(&1u16.to_le_bytes()); // mono
    out.extend_from_slice(&(SAMPLE_RATE as u32).to_le_bytes());
    out.extend_from_slice(&(SAMPLE_RATE as u32 * 2).to_le_bytes());
    out.extend_from_slice(&2u16.to_le_bytes());
    out.extend_from_slice(&16u16.to_le_bytes());
    out.extend_from_slice(b"data");
    out.extend_from_slice(&data.to_le_bytes());
    for s in samples {
        out.extend_from_slice(&s.to_le_bytes());
    }
    std::fs::write(path, out)
}

/// `--capture-test <seconds> <out folder> [<app binary>]`: records the default mic, all apps' audio, and (given
/// a binary) that app's on its own, all at once, through the same sources a recording uses. Saves mic.wav,
/// apps.wav (and app.wav) and prints each one's length and loudness. Exit 0 if every one recorded for as long
/// as asked.
pub fn capture_test(args: &[String]) -> i32 {
    let rest: Vec<&String> = args.iter().skip_while(|a| *a != "--capture-test").skip(1).collect();
    if rest.len() < 2 {
        println!("usage: --capture-test <seconds> <out folder> [<app binary>]");
        return 2;
    }
    let seconds: f64 = rest[0].parse().unwrap_or(5.0);
    let folder = Path::new(rest[1].as_str());
    let _ = std::fs::create_dir_all(folder);
    let mut targets = vec![("mic", pulse::Target::Mic), ("apps", pulse::Target::AllApps)];
    if let Some(binary) = rest.get(2) {
        targets.push(("app", pulse::Target::App(binary.to_string(), audio::apps::display_name(binary))));
    }
    let mut running = Vec::new();
    let mut ok = true;
    for (name, target) in targets {
        let got: Arc<Mutex<Vec<i16>>> = Arc::new(Mutex::new(Vec::new()));
        let into = got.clone();
        let sink: Sink = Arc::new(move |samples: &[i16]| into.lock().unwrap_or_else(|e| e.into_inner()).extend_from_slice(samples));
        let mut capture = pulse::Capture::new(target);
        match capture.start(sink) {
            Ok(()) => {
                println!("{name}: recording from {}", capture.name());
                running.push((name, capture, got));
            }
            Err(e) => {
                println!("{name}: FAILED to start: {e}");
                ok = false;
            }
        }
    }
    thread::sleep(Duration::from_secs_f64(seconds));
    for (name, mut capture, got) in running {
        capture.stop();
        let samples = got.lock().unwrap_or_else(|e| e.into_inner()).clone();
        let level = meter_level(&samples);
        let peak = samples.iter().map(|s| s.unsigned_abs()).max().unwrap_or(0);
        let long_enough = samples.len() as f64 >= (seconds - 0.6) * SAMPLE_RATE as f64;
        println!(
            "{name}: {:.2} s, level {level:.2}, peak {peak}{}",
            samples.len() as f64 / SAMPLE_RATE as f64,
            if long_enough { "" } else { "  (FAILED: too short)" }
        );
        ok &= long_enough;
        if let Err(e) = write_wav(&folder.join(format!("{name}.wav")), &samples) {
            println!("{name}: couldn't save it: {e}");
            ok = false;
        }
    }
    if ok {
        0
    } else {
        1
    }
}
