//! The apps you can pick as the meeting-audio source: whatever has an audio session on the default output
//! right now, the ones playing sound first (as AudioApp in SystemAudioCapture.swift). An app is named by
//! the root of its process tree, which is what per-process loopback records: Teams plays its sound from
//! helper processes, so the session's own process is walked up to the app that started it.

use windows::core::Interface;
use windows::Win32::Media::Audio::{eConsole, eRender, AudioSessionStateActive, IAudioSessionControl2, IAudioSessionManager2};
use windows::Win32::System::Com::CLSCTX_ALL;

use super::wasapi::{com_init, enumerator};
use crate::win::{processes, Proc};

#[derive(Clone, Debug, PartialEq)]
pub struct AudioApp {
    /// The root of the app's process tree.
    pub pid: u32,
    /// Its executable's file name, lower-case: the key a preference remembers it by.
    pub exe: String,
    pub name: String,
    pub playing: bool,
}

/// Apps people take calls in. Browsers aren't here: they play all sorts of sound.
const CALL_APPS: &[(&str, &str)] = &[
    ("zoom.exe", "Zoom"),
    ("ms-teams.exe", "Microsoft Teams"),
    ("teams.exe", "Microsoft Teams"),
    ("slack.exe", "Slack"),
    ("discord.exe", "Discord"),
    ("webex.exe", "Webex"),
    ("ciscocollabhost.exe", "Webex"),
    ("atmgr.exe", "Webex"),
    ("whatsapp.exe", "WhatsApp"),
    ("skype.exe", "Skype"),
    ("telegram.exe", "Telegram"),
    ("signal.exe", "Signal"),
];

const KNOWN_NAMES: &[(&str, &str)] = &[
    ("chrome.exe", "Google Chrome"),
    ("msedge.exe", "Microsoft Edge"),
    ("firefox.exe", "Firefox"),
    ("brave.exe", "Brave"),
    ("spotify.exe", "Spotify"),
    ("vlc.exe", "VLC"),
];

fn call_app_name(exe: &str) -> Option<&'static str> {
    CALL_APPS.iter().find(|(e, _)| *e == exe).map(|(_, n)| *n)
}

/// A readable name for an executable: "Zoom" for zoom.exe, "Obs64" for obs64.exe.
pub fn display_name(exe: &str) -> String {
    if let Some(name) = call_app_name(exe).or_else(|| KNOWN_NAMES.iter().find(|(e, _)| *e == exe).map(|(_, n)| *n)) {
        return name.to_string();
    }
    let stem = exe.strip_suffix(".exe").unwrap_or(exe);
    let mut chars = stem.chars();
    match chars.next() {
        Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
        None => exe.to_string(),
    }
}

/// (pid, playing) for every audio session on the default output device.
fn sessions() -> Vec<(u32, bool)> {
    com_init();
    let mut out = Vec::new();
    unsafe {
        let Ok(device) = enumerator().and_then(|e| e.GetDefaultAudioEndpoint(eRender, eConsole)) else { return out };
        let Ok(manager) = device.Activate::<IAudioSessionManager2>(CLSCTX_ALL, None) else { return out };
        let Ok(list) = manager.GetSessionEnumerator() else { return out };
        for i in 0..list.GetCount().unwrap_or(0) {
            let Ok(session) = list.GetSession(i) else { continue };
            let Ok(session) = session.cast::<IAudioSessionControl2>() else { continue };
            let pid = session.GetProcessId().unwrap_or(0);
            if pid == 0 {
                continue; // the system sounds session
            }
            let playing = session.GetState().map(|s| s == AudioSessionStateActive).unwrap_or(false);
            out.push((pid, playing));
        }
    }
    out
}

/// The app a process belongs to: the top of the chain of parents with the same executable, or a call app
/// above it (Teams' helpers are msedgewebview2.exe under ms-teams.exe).
fn root_of(pid: u32, table: &[Proc]) -> Option<&Proc> {
    let find = |pid: u32| table.iter().find(|p| p.pid == pid);
    let mut current = find(pid)?;
    for _ in 0..16 {
        let Some(parent) = find(current.parent) else { break };
        if parent.pid == current.pid || parent.pid == 0 {
            break;
        }
        if parent.exe == current.exe || call_app_name(&parent.exe).is_some() {
            current = parent;
        } else {
            break;
        }
    }
    Some(current)
}

/// Apps connected to audio right now, the ones playing sound first.
pub fn current() -> Vec<AudioApp> {
    let table = processes();
    let me = std::process::id();
    let mut apps: Vec<AudioApp> = Vec::new();
    for (pid, playing) in sessions() {
        let Some(root) = root_of(pid, &table) else { continue };
        if root.pid == me || pid == me {
            continue;
        }
        match apps.iter_mut().find(|a| a.pid == root.pid) {
            Some(app) => app.playing |= playing,
            None => apps.push(AudioApp { pid: root.pid, exe: root.exe.clone(), name: display_name(&root.exe), playing }),
        }
    }
    apps.sort_by(|a, b| (!a.playing, a.name.to_lowercase()).cmp(&(!b.playing, b.name.to_lowercase())));
    apps
}

/// A call app playing sound right now (a call is under way), if any.
pub fn call_in_progress() -> Option<String> {
    current().into_iter().find(|a| a.playing && call_app_name(&a.exe).is_some()).map(|a| a.name)
}

/// Is anything other than us making sound? Used to tell "silence" from "not allowed to listen".
pub fn anything_playing() -> bool {
    current().iter().any(|a| a.playing)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_are_readable() {
        assert_eq!(display_name("zoom.exe"), "Zoom");
        assert_eq!(display_name("ms-teams.exe"), "Microsoft Teams");
        assert_eq!(display_name("chrome.exe"), "Google Chrome");
        assert_eq!(display_name("obs64.exe"), "Obs64");
    }

    #[test]
    fn helpers_roll_up_to_their_app() {
        let table = vec![
            Proc { pid: 1, parent: 0, exe: "explorer.exe".into() },
            Proc { pid: 10, parent: 1, exe: "ms-teams.exe".into() },
            Proc { pid: 11, parent: 10, exe: "msedgewebview2.exe".into() },
            Proc { pid: 12, parent: 11, exe: "msedgewebview2.exe".into() },
            Proc { pid: 20, parent: 1, exe: "chrome.exe".into() },
            Proc { pid: 21, parent: 20, exe: "chrome.exe".into() },
            Proc { pid: 30, parent: 1, exe: "spotify.exe".into() },
        ];
        assert_eq!(root_of(12, &table).unwrap().pid, 10); // Teams' web view → Teams
        assert_eq!(root_of(21, &table).unwrap().pid, 20); // a Chrome renderer → Chrome
        assert_eq!(root_of(30, &table).unwrap().pid, 30); // Spotify stays Spotify, not Explorer
        assert!(root_of(99, &table).is_none());
    }
}
