//! The shell's settings, remembered between launches in desktop.json in the data folder (Prefs.swift).

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::hotkeys::Shortcut;

pub const DEFAULT_SERVER: &str = "http://127.0.0.1:8765";
/// Prefs::shortcut_defaults now (1: Linux's Start / stop is Ctrl+Alt+Shift+S).
const SHORTCUT_DEFAULTS: u32 = 1;

/// Where the meeting's other side comes from.
#[derive(Clone, Debug, PartialEq)]
pub enum MeetingSource {
    AllApps,
    /// In person: your mic only.
    None,
    /// One app, by its executable's file name ("zoom.exe").
    App(String),
}

impl MeetingSource {
    pub fn parse(raw: &str) -> MeetingSource {
        match raw {
            "all" | "" => MeetingSource::AllApps,
            "none" => MeetingSource::None,
            other => MeetingSource::App(other.strip_prefix("app:").unwrap_or(other).to_string()),
        }
    }

    pub fn raw(&self) -> String {
        match self {
            MeetingSource::AllApps => "all".into(),
            MeetingSource::None => "none".into(),
            MeetingSource::App(exe) => format!("app:{exe}"),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Prefs {
    pub server: String,
    pub source: String,
    pub live_draft: bool,
    pub token: String,
    /// Start / stop and Mark moment, from any app.
    pub shortcut_record: Shortcut,
    pub shortcut_mark: Shortcut,
    /// "Zoom call started. Record it?"
    pub call_reminders: bool,
    /// The apps you said not to ask about ("Zoom", "a call in Chrome").
    pub muted_call_apps: Vec<String>,
    /// Which defaults the shortcuts were last moved to (Linux's Start / stop went from Ctrl+Alt+Shift+R to S, once).
    #[serde(default)] // (0 in settings saved before it, not Prefs::default()'s)
    pub shortcut_defaults: u32,
    #[serde(skip)]
    path: PathBuf,
}

impl Default for Prefs {
    fn default() -> Prefs {
        Prefs {
            server: DEFAULT_SERVER.into(),
            source: "all".into(),
            live_draft: true,
            token: String::new(),
            shortcut_record: Shortcut::RECORD_DEFAULT,
            shortcut_mark: Shortcut::MARK_DEFAULT,
            call_reminders: true,
            muted_call_apps: Vec::new(),
            shortcut_defaults: SHORTCUT_DEFAULTS,
            path: PathBuf::new(),
        }
    }
}

impl Prefs {
    pub fn load(data_dir: &Path) -> Prefs {
        let path = data_dir.join("desktop.json");
        let mut prefs: Prefs = std::fs::read_to_string(&path).ok().and_then(|text| serde_json::from_str(&text).ok()).unwrap_or_default();
        prefs.path = path;
        if prefs.move_to_new_defaults() {
            prefs.save();
        }
        prefs
    }

    /// Settings saved before Linux's Start / stop default became Ctrl+Alt+Shift+S (GNOME's screen recorder has R)
    /// still have R: moved to S, once (R picked again afterwards stays). True if anything changed.
    fn move_to_new_defaults(&mut self) -> bool {
        if self.shortcut_defaults >= SHORTCUT_DEFAULTS {
            return false;
        }
        if cfg!(target_os = "linux") && self.shortcut_record == Shortcut::RECORD_R && self.shortcut_mark != Shortcut::RECORD_DEFAULT {
            self.shortcut_record = Shortcut::RECORD_DEFAULT;
        }
        self.shortcut_defaults = SHORTCUT_DEFAULTS;
        true
    }

    pub fn save(&self) {
        if self.path.as_os_str().is_empty() {
            return;
        }
        if let Some(dir) = self.path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        if let Ok(text) = serde_json::to_string_pretty(self) {
            let _ = std::fs::write(&self.path, text);
        }
    }

    pub fn meeting_source(&self) -> MeetingSource {
        MeetingSource::parse(&self.source)
    }

    /// The server's address, tidied: TRAILMIX_SERVER_URL wins (tests), else the saved one with a scheme
    /// added if missing and no trailing slash.
    pub fn server_url(&self) -> String {
        let text = std::env::var("TRAILMIX_SERVER_URL").unwrap_or_else(|_| self.server.clone());
        tidy_url(&text)
    }

    /// True for this computer's own server.
    pub fn is_local_server(&self) -> bool {
        match url::Url::parse(&self.server_url()) {
            Ok(url) => matches!(url.host_str(), Some("127.0.0.1") | Some("localhost") | Some("::1") | Some("[::1]")),
            Err(_) => false,
        }
    }

    pub fn server_port(&self) -> u16 {
        url::Url::parse(&self.server_url()).ok().and_then(|u| u.port_or_known_default()).unwrap_or(8765)
    }
}

pub fn tidy_url(text: &str) -> String {
    let mut text = text.trim().to_string();
    if !text.contains("://") {
        text = format!("http://{text}");
    }
    while text.ends_with('/') {
        text.pop();
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn urls_are_tidied() {
        assert_eq!(tidy_url(" 127.0.0.1:8765/ "), "http://127.0.0.1:8765");
        assert_eq!(tidy_url("https://x.example//"), "https://x.example");
    }

    #[test]
    fn sources_round_trip() {
        for raw in ["all", "none", "app:zoom.exe"] {
            assert_eq!(MeetingSource::parse(raw).raw(), raw);
        }
        assert_eq!(MeetingSource::parse(""), MeetingSource::AllApps);
    }

    #[test]
    fn missing_fields_take_defaults() {
        let prefs: Prefs = serde_json::from_str(r#"{"source": "none"}"#).unwrap();
        assert_eq!(prefs.server, DEFAULT_SERVER);
        assert!(prefs.live_draft);
        assert_eq!(prefs.meeting_source(), MeetingSource::None);
        assert_eq!(prefs.shortcut_record, Shortcut::RECORD_DEFAULT);
        assert_eq!(prefs.shortcut_mark, Shortcut::MARK_DEFAULT);
        assert!(prefs.call_reminders);
        assert!(prefs.muted_call_apps.is_empty());
    }

    #[test]
    fn linux_moves_off_ctrl_alt_shift_r_once() {
        let mut old: Prefs = serde_json::from_str(r#"{"shortcut_record": {"key": 82, "mods": 7}}"#).unwrap();
        assert_eq!(old.shortcut_defaults, 0);
        assert!(old.move_to_new_defaults());
        assert_eq!(old.shortcut_record, Shortcut::RECORD_DEFAULT); // (S on Linux; R stays R elsewhere)
        assert_eq!(old.shortcut_defaults, SHORTCUT_DEFAULTS);
        old.shortcut_record = Shortcut::RECORD_R; // picked again: kept
        assert!(!old.move_to_new_defaults());
        assert_eq!(old.shortcut_record, Shortcut::RECORD_R);
        let mut own: Prefs = serde_json::from_str(r#"{"shortcut_record": {"key": 120, "mods": 0}}"#).unwrap();
        own.move_to_new_defaults();
        assert_eq!(own.shortcut_record.display(), "F9", "a shortcut of your own isn't touched");
        assert!(!Prefs::default().move_to_new_defaults(), "new settings have the new defaults already");
    }

    #[test]
    fn shortcuts_and_reminders_are_remembered() {
        let mut prefs = Prefs::default();
        prefs.shortcut_record = Shortcut { key: 0x78, mods: 0 };
        prefs.call_reminders = false;
        prefs.muted_call_apps.push("Zoom".into());
        let back: Prefs = serde_json::from_str(&serde_json::to_string(&prefs).unwrap()).unwrap();
        assert_eq!(back.shortcut_record.display(), "F9");
        assert!(!back.call_reminders);
        assert_eq!(back.muted_call_apps, vec!["Zoom".to_string()]);
    }
}
