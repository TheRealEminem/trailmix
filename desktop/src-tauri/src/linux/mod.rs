//! What the Linux app needs of the system (win.rs's counterpart): the user's folders, processes, the session
//! (X11 or Wayland), running things on GTK's main thread, and what this recorder can do here. The cards
//! (card.rs) and open at login (login.rs) are here too, standing in for the Windows modules of the same names.

pub mod keys;
pub mod selftest;

use std::path::PathBuf;
use std::sync::mpsc;
use std::time::Duration;

use crate::audio::pulse;

/// $XDG_<name> (XDG_DATA_HOME, XDG_CONFIG_HOME), else that folder under home.
pub fn xdg_dir(var: &str, default: &str) -> PathBuf {
    match std::env::var_os(var).map(PathBuf::from).filter(|p| p.is_absolute()) {
        Some(dir) => dir,
        None => home().join(default),
    }
}

pub fn home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/tmp"))
}

pub fn hostname() -> String {
    std::fs::read_to_string("/proc/sys/kernel/hostname").map(|h| h.trim().to_string()).unwrap_or_default()
}

/// Wayland, X11, or neither (a terminal, a test without a display).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Session {
    X11,
    Wayland,
    None,
}

pub fn session() -> Session {
    let kind = std::env::var("XDG_SESSION_TYPE").unwrap_or_default().to_lowercase();
    if kind == "wayland" || std::env::var_os("WAYLAND_DISPLAY").is_some() {
        Session::Wayland
    } else if std::env::var_os("DISPLAY").is_some() {
        Session::X11
    } else {
        Session::None
    }
}

/// Before GTK starts: WebKitGTK's DMA-BUF renderer leaves the window blank on some graphics drivers (and in
/// virtual machines), so it's off unless asked for.
pub fn before_window() {
    if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
        std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
    }
}

/// What this recorder can do on this computer, for the web app (it hides the rest). Only what really works
/// here: other apps' audio needs a sound server, the shortcuts need X11 (or a desktop with the global
/// shortcuts portal), the call reminder needs the sound server's list of who's recording.
pub fn capabilities() -> Vec<&'static str> {
    let mut caps = Vec::new();
    let sound = pulse::available();
    if sound {
        caps.extend(["system_audio", "per_app_audio"]);
    }
    if keys::available() {
        caps.push("hotkeys");
    }
    if sound && session() != Session::None {
        caps.push("call_reminder");
    }
    if sound {
        caps.push("sound_check");
    }
    caps
}

// MARK: GTK's main thread

/// Runs `f` on GTK's main thread (where every window is made and changed), from any thread.
pub fn on_main(f: impl FnOnce() + Send + 'static) {
    gtk::glib::MainContext::default().invoke(f);
}

/// Runs `f` on GTK's main thread and waits for its answer (None if the main thread doesn't get to it).
pub fn on_main_wait<R: Send + 'static>(f: impl FnOnce() -> R + Send + 'static) -> Option<R> {
    let context = gtk::glib::MainContext::default();
    if context.is_owner() {
        return Some(f());
    }
    let (tx, rx) = mpsc::channel();
    context.invoke(move || {
        let _ = tx.send(f());
    });
    rx.recv_timeout(Duration::from_secs(10)).ok()
}

// MARK: Processes

/// Keeps the engine from outliving the app: on Windows a job object does it; here the engine itself watches
/// for its parent going (TRAILMIX_PARENT_PID, in main.py), so there's nothing to hold.
pub struct Job;

impl Job {
    pub fn new() -> Option<Job> {
        Some(Job)
    }

    pub fn assign(&self, _child: &std::process::Child) -> bool {
        true
    }
}

/// Asks the engine (and its process group) to stop: SIGTERM, which uvicorn takes as "finish up and quit".
pub fn ctrl_break(pid: u32) -> bool {
    unsafe { libc::kill(-(pid as i32), libc::SIGTERM) == 0 || libc::kill(pid as i32, libc::SIGTERM) == 0 }
}

/// Ends a process group at once (the engine's, when it didn't stop when asked).
pub fn kill_group(pid: u32) {
    unsafe {
        libc::kill(-(pid as i32), libc::SIGKILL);
    }
}
