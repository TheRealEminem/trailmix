//! Checks the shell without a microphone or any audio device (SelfTest.swift): audio files stand in for
//! the mic and the meeting, and everything else is the real thing (model, recorder socket, audio socket,
//! window, server).
//!
//!   Trailmix.exe --self-test http://127.0.0.1:8797 me.wav them.wav [--no-window]
//!   Trailmix.exe --window-test http://127.0.0.1:8765/#meeting-2 window.png [seconds]
//!   Trailmix.exe --list-audio

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use serde_json::Value;
use tauri::{AppHandle, Manager};

use crate::app::AppState;
use crate::audio;
use crate::model::{Connection, Model, Phase, Sources};
use crate::prefs::{tidy_url, Prefs};
use crate::window;
use crate::wire::{AudioSource, FileSource, MEETING, MIC};

/// What the meeting-audio menu would offer right now, and what this Windows can do. Needs no permission.
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
    println!("Recorder capabilities: {}", Model::capabilities().join(", "));
}

struct Test {
    failures: u32,
}

impl Test {
    fn expect(&mut self, ok: bool, what: &str) {
        println!("{}  {what}", if ok { "  ok  " } else { "  FAIL" });
        if !ok {
            self.failures += 1;
        }
    }
}

fn wait_until(seconds: f64, mut condition: impl FnMut() -> bool) -> bool {
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

/// --self-test <server> <mic audio> [<meeting audio>] [--no-window]
pub fn run(app: AppHandle, args: Vec<String>) {
    let rest = args_after(&args, "--self-test");
    if rest.len() < 2 {
        println!("usage: --self-test <server> <mic audio> [<meeting audio>] [--no-window]");
        std::process::exit(2);
    }
    let server = tidy_url(rest[0]);
    let me = PathBuf::from(rest[1]);
    let them = rest.get(2).map(PathBuf::from);
    let no_window = args.iter().any(|a| a == "--no-window");

    let model = Model::new(Prefs::default(), None, app.package_info().version.to_string(), Some(server.clone()));
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
        check(&mut t, &model, &server);
        if !no_window {
            window_record(&mut t, &app, &model, &server);
        }
        println!("{}", if t.failures == 0 { "All checks passed.".to_string() } else { format!("{} check(s) failed.", t.failures) });
        std::process::exit(if t.failures == 0 { 0 } else { 1 });
    });
}

fn check(t: &mut Test, model: &Arc<Model>, base: &str) {
    model.poll();
    let online = wait_until(5.0, || model.snapshot().connection == Connection::Online);
    t.expect(online, &format!("reaches the server ({:?})", model.snapshot().connection));

    println!("1. Record starts a recording");
    model.toggle();
    let recording = wait_until(20.0, || model.snapshot().phase == Phase::Recording);
    t.expect(recording, &format!("recording ({})", model.snapshot().error.unwrap_or_else(|| "no error".into())));
    let first = model.snapshot().meeting_id;
    thread::sleep(Duration::from_secs(5));
    println!("2. Mark flags a moment");
    model.mark_moment();
    t.expect(wait_until(5.0, || !model.snapshot().marks.is_empty()), &format!("server confirmed the mark: {:?}", model.snapshot().marks));
    let draft = wait_until(25.0, || !model.snapshot().drafts.is_empty());
    let first_line = model.snapshot().drafts.first().map(|d| d.text.chars().take(60).collect::<String>()).unwrap_or_else(|| "none".into());
    t.expect(draft, &format!("live draft arrived: {first_line}"));
    println!("3. Stop stops it");
    model.toggle();
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
    let pressed = post(&format!("{base}/api/recorder/start"));
    t.expect(pressed == Some(202), "Record pressed in the window");
    model.check_in();
    t.expect(wait_until(20.0, || model.snapshot().phase == Phase::Recording), "recording started from the window");
    thread::sleep(Duration::from_secs(2));
    model.toggle();
    t.expect(wait_until(15.0, || model.snapshot().phase == Phase::Idle), "stopped cleanly");
}

/// The real Trailmix window and page, the way you use them: Record clicked in the window; the recording
/// must go through the recorder, never the page's own capture. Then the transcript (made while
/// recording) must arrive.
fn window_record(t: &mut Test, app: &AppHandle, model: &Arc<Model>, base: &str) {
    println!("6. Record in the real window");
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
