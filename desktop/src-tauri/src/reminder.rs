//! "Zoom call started. Record it?" (CallReminder.swift): a card when a call app starts using the microphone
//! and nothing is recording; and during a recording, once the call app has let go of the mic and the
//! speakers for a minute, "The call seems to have ended. Stop recording?". Off with "Remind me to record
//! calls" (Settings in the web app, or the tray menu); "Don't ask for Zoom" stops it for one app.
//!
//! Which apps are using the mic comes from Windows' own privacy records, the ones behind the microphone
//! icon in the taskbar: HKCU\...\CapabilityAccessManager\ConsentStore\microphone has a key per Store app
//! and, under NonPackaged, one per program (its path with # for \). LastUsedTimeStop is 0 while it's in use.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, Weak};
use std::thread;
use std::time::{Duration, Instant};

use crate::audio;
use crate::card::{self, Answer, Card};
use crate::model::{Connection, Model, Phase};
use crate::win;

pub const CONSENT_STORE: &str = "Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\microphone";
pub const CHECK_EVERY: Duration = Duration::from_secs(3);
pub const ENDED_AFTER: Duration = Duration::from_secs(60);

/// Browsers people take calls in (Google Meet and the like): only counted while they use the microphone.
const BROWSERS: &[(&str, &str)] = &[
    ("chrome.exe", "Chrome"),
    ("msedge.exe", "Edge"),
    ("firefox.exe", "Firefox"),
    ("brave.exe", "Brave"),
    ("opera.exe", "Opera"),
    ("vivaldi.exe", "Vivaldi"),
    ("arc.exe", "Arc"),
];

/// Call apps from the Microsoft Store, by package family name (before the _publisher part), lower-case.
const PACKAGED_CALL_APPS: &[(&str, &str)] = &[
    ("msteams", "Microsoft Teams"),
    ("microsoftteams", "Microsoft Teams"),
    ("microsoft.skypeapp", "Skype"),
    ("5319275a.whatsappdesktop", "WhatsApp"),
    ("91750d7e.slack", "Slack"),
    ("microsoft.yourphone", "Phone Link"),
];

/// One app's entry in the ConsentStore.
#[derive(Clone, Debug, PartialEq)]
pub struct MicEntry {
    /// The key's name: a package family name, or a program's path with # for \.
    pub key: String,
    pub packaged: bool,
    pub start: u64,
    pub stop: u64,
}

impl MicEntry {
    pub fn in_use(&self) -> bool {
        self.stop == 0 && self.start != 0
    }

    /// A program's file name, lower-case ("zoom.exe"); None for a Store app.
    pub fn exe(&self) -> Option<String> {
        if self.packaged {
            return None;
        }
        self.key.rsplit(['#', '\\']).next().map(|s| s.to_lowercase())
    }

    /// What it's called as a call: "Zoom", "Microsoft Teams", "a call in Chrome"; None if it isn't a call app.
    pub fn call_name(&self) -> Option<String> {
        match self.exe() {
            Some(exe) => call_name_for_exe(&exe),
            None => {
                let family = self.key.split('_').next().unwrap_or("").to_lowercase();
                PACKAGED_CALL_APPS.iter().find(|(f, _)| *f == family).map(|(_, n)| n.to_string())
            }
        }
    }
}

/// "Zoom" for zoom.exe, "a call in Chrome" for chrome.exe, None for anything else (and for Trailmix itself).
pub fn call_name_for_exe(exe: &str) -> Option<String> {
    if exe == "trailmix.exe" {
        return None;
    }
    if let Some(name) = audio::apps::call_app_name(exe) {
        return Some(name.to_string());
    }
    BROWSERS.iter().find(|(e, _)| *e == exe).map(|(_, n)| format!("a call in {n}"))
}

/// Every app in the ConsentStore under `root`.
pub fn mic_entries(root: &str) -> Vec<MicEntry> {
    let read = |key: String, packaged: bool, path: String| MicEntry {
        start: win::reg_get_qword(&path, "LastUsedTimeStart").unwrap_or(0),
        stop: win::reg_get_qword(&path, "LastUsedTimeStop").unwrap_or(0),
        key,
        packaged,
    };
    let mut out = Vec::new();
    for name in win::reg_subkeys(root) {
        if !name.eq_ignore_ascii_case("NonPackaged") {
            let path = format!("{root}\\{name}");
            out.push(read(name, true, path));
        }
    }
    let programs = format!("{root}\\NonPackaged");
    for name in win::reg_subkeys(&programs) {
        let path = format!("{programs}\\{name}");
        out.push(read(name, false, path));
    }
    out
}

/// The call (or browser) using the microphone among these entries: a call app before a browser. With
/// `running`, a program counts only if it's running (a crash can leave an entry "in use").
pub fn call_using_mic(entries: &[MicEntry], running: Option<&[String]>) -> Option<String> {
    let live = |e: &&MicEntry| {
        e.in_use()
            && match (running, e.exe()) {
                (Some(running), Some(exe)) => running.iter().any(|r| *r == exe),
                _ => true,
            }
    };
    let calls: Vec<String> = entries.iter().filter(live).filter_map(|e| e.call_name()).collect();
    calls.iter().find(|c| !c.starts_with("a call in")).or_else(|| calls.first()).cloned()
}

// MARK: When to ask (the same rules as CallReminder.swift's tick, without the screen)

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Start,
    Stop,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Situation {
    /// Reminders off, or no connection to Trailmix.
    Off,
    /// Nothing recording, here or in the Trailmix window.
    Idle,
    /// Recording here.
    Recording,
    /// Starting, stopping, or recording in the Trailmix window.
    Busy,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Do {
    Show(Kind, String),
    Hide,
}

pub struct Watch {
    shown: Option<Kind>,
    /// The call already asked about, until it ends.
    asked_about: Option<String>,
    /// The call app in use during this recording.
    recording_call: Option<String>,
    /// When that app last let go of the mic and the speakers.
    quiet_since: Option<Instant>,
    asked_to_stop: bool,
    pub ended_after: Duration,
}

impl Watch {
    pub fn new(ended_after: Duration) -> Watch {
        Watch { shown: None, asked_about: None, recording_call: None, quiet_since: None, asked_to_stop: false, ended_after }
    }

    /// The card went away by itself (answered or timed out).
    pub fn closed(&mut self) {
        self.shown = None;
    }

    fn hide_if(&mut self, kind: Option<Kind>) -> Option<Do> {
        let hide = match kind {
            Some(kind) => self.shown == Some(kind),
            None => self.shown.is_some(),
        };
        if hide {
            self.shown = None;
            Some(Do::Hide)
        } else {
            None
        }
    }

    /// What to do now. `using_mic`: the call using the microphone; `still_in_call(name)`: whether that app
    /// still has the mic or the speakers.
    pub fn tick(
        &mut self,
        situation: Situation,
        now: Instant,
        muted: &[String],
        using_mic: Option<String>,
        still_in_call: impl Fn(&str) -> bool,
    ) -> Option<Do> {
        match situation {
            Situation::Off => self.hide_if(None),
            Situation::Idle => {
                self.recording_call = None;
                self.quiet_since = None;
                self.asked_to_stop = false;
                let Some(call) = using_mic else {
                    self.asked_about = None; // the call ended: the next one gets asked about again
                    return self.hide_if(None);
                };
                if self.asked_about.as_deref() == Some(call.as_str()) || muted.contains(&call) {
                    return self.hide_if(Some(Kind::Stop));
                }
                self.asked_about = Some(call.clone());
                self.shown = Some(Kind::Start);
                Some(Do::Show(Kind::Start, call))
            }
            Situation::Recording => {
                let hidden = self.hide_if(Some(Kind::Start));
                if let Some(call) = using_mic {
                    self.recording_call = Some(call);
                }
                let Some(call) = self.recording_call.clone() else { return hidden };
                if self.asked_to_stop {
                    return hidden;
                }
                if still_in_call(&call) {
                    self.quiet_since = None;
                    return hidden.or_else(|| self.hide_if(Some(Kind::Stop)));
                }
                let since = *self.quiet_since.get_or_insert(now);
                if now.duration_since(since) >= self.ended_after {
                    self.asked_to_stop = true;
                    self.shown = Some(Kind::Stop);
                    return Some(Do::Show(Kind::Stop, call));
                }
                hidden
            }
            Situation::Busy => self.hide_if(Some(Kind::Start)),
        }
    }
}

/// The card's words, as on the Mac.
pub fn card_for(kind: Kind, call: &str, sound: bool) -> Card {
    let is_call = !call.starts_with("a call in");
    match kind {
        Kind::Start => Card {
            title: if is_call { format!("{call} call started") } else { format!("Looks like {call}") },
            detail: "Record it with Trailmix? You'll get the notes when it ends.".into(),
            buttons: vec!["Record".into(), "Not now".into()],
            link: Some(if is_call { format!("Don't ask for {call}") } else { "Don't ask for calls in this browser".into() }),
            seconds: 45,
            keys: false,
            sound,
        },
        Kind::Stop => Card {
            title: "The call seems to have ended".into(),
            detail: format!("{} hasn't used the mic or speakers for a minute.", if is_call { call } else { "The call" }),
            buttons: vec!["Stop recording".into(), "Keep recording".into()],
            link: None,
            seconds: 90,
            keys: false,
            sound,
        },
    }
}

// MARK: Running it

pub struct Reminder {
    model: Arc<Model>,
    /// Where the ConsentStore is (the self-test points it at a stand-in).
    root: String,
    every: Duration,
    /// Only count programs that are running (the self-test's stand-in Zoom isn't).
    require_running: bool,
    sound: bool,
    watch: Mutex<Watch>,
    card: Mutex<Option<u64>>,
    stopped: AtomicBool,
}

impl Reminder {
    pub fn new(model: Arc<Model>, root: &str, every: Duration, ended_after: Duration, require_running: bool, sound: bool) -> Arc<Reminder> {
        Arc::new(Reminder {
            model,
            root: root.to_string(),
            every,
            require_running,
            sound,
            watch: Mutex::new(Watch::new(ended_after)),
            card: Mutex::new(None),
            stopped: AtomicBool::new(false),
        })
    }

    /// The one the app runs: Windows' own ConsentStore, every 3 seconds.
    pub fn standard(model: Arc<Model>) -> Arc<Reminder> {
        Reminder::new(model, CONSENT_STORE, CHECK_EVERY, ENDED_AFTER, true, true)
    }

    pub fn begin(self: &Arc<Self>) {
        let weak = Arc::downgrade(self);
        let every = self.every;
        let _ = thread::Builder::new().name("trailmix-reminder".into()).spawn(move || loop {
            thread::sleep(every);
            let Some(reminder) = weak.upgrade() else { return };
            if reminder.stopped.load(Ordering::SeqCst) {
                return;
            }
            reminder.tick();
        });
    }

    pub fn stop(&self) {
        self.stopped.store(true, Ordering::SeqCst);
        self.hide();
    }

    /// The call using the mic right now, if any.
    pub fn call_using_mic(&self) -> Option<String> {
        let entries = mic_entries(&self.root);
        if !self.require_running {
            return call_using_mic(&entries, None);
        }
        if !entries.iter().any(|e| e.in_use()) {
            return None;
        }
        let running: Vec<String> = win::processes().into_iter().map(|p| p.exe).collect();
        call_using_mic(&entries, Some(running.as_slice()))
    }

    /// Whether the app still has the mic or the speakers (muted calls often let go of the mic but keep playing).
    fn still_in_call(&self, name: &str) -> bool {
        let entries = mic_entries(&self.root);
        if entries.iter().any(|e| e.in_use() && e.call_name().as_deref() == Some(name)) {
            return true;
        }
        audio::apps::current().iter().any(|a| a.playing && call_name_for_exe(&a.exe).as_deref() == Some(name))
    }

    fn tick(self: &Arc<Self>) {
        let snapshot = self.model.snapshot();
        let prefs = self.model.prefs();
        let situation = if !prefs.call_reminders || snapshot.connection != Connection::Online {
            Situation::Off
        } else {
            match snapshot.phase {
                Phase::Idle if snapshot.other.is_none() => Situation::Idle,
                Phase::Recording => Situation::Recording,
                _ => Situation::Busy,
            }
        };
        let using_mic = match situation {
            Situation::Idle | Situation::Recording => self.call_using_mic(),
            _ => None,
        };
        let action = {
            let mut watch = self.watch.lock().unwrap_or_else(|e| e.into_inner());
            watch.tick(situation, Instant::now(), &prefs.muted_call_apps, using_mic, |name| self.still_in_call(name))
        };
        match action {
            Some(Do::Show(kind, call)) => self.show(kind, call),
            Some(Do::Hide) => self.hide(),
            None => {}
        }
    }

    fn hide(&self) {
        if let Some(id) = self.card.lock().unwrap_or_else(|e| e.into_inner()).take() {
            card::hide(id);
        }
    }

    fn show(self: &Arc<Self>, kind: Kind, call: String) {
        self.hide();
        let weak: Weak<Reminder> = Arc::downgrade(self);
        let name = call.clone();
        let mut card_slot = self.card.lock().unwrap_or_else(|e| e.into_inner());
        let id = card::show(card_for(kind, &call, self.sound), move |id, answer| {
            let Some(reminder) = weak.upgrade() else { return };
            reminder.answered(id, kind, &name, answer);
        });
        *card_slot = Some(id);
    }

    fn answered(&self, id: u64, kind: Kind, call: &str, answer: Answer) {
        {
            let mut current = self.card.lock().unwrap_or_else(|e| e.into_inner());
            if *current == Some(id) {
                *current = None;
                self.watch.lock().unwrap_or_else(|e| e.into_inner()).closed();
            }
        }
        match (kind, answer) {
            (Kind::Start, Answer::Button(0)) => self.model.start(),
            (Kind::Stop, Answer::Button(0)) => self.model.stop(),
            (Kind::Start, Answer::Link) => {
                let call = call.to_string();
                self.model.update_prefs(move |prefs| {
                    if !prefs.muted_call_apps.contains(&call) {
                        prefs.muted_call_apps.push(call);
                    }
                });
            }
            _ => {}
        }
    }

    /// The card this reminder has up, for the self-test.
    pub fn card_id(&self) -> Option<u64> {
        *self.card.lock().unwrap_or_else(|e| e.into_inner())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(key: &str, packaged: bool, start: u64, stop: u64) -> MicEntry {
        MicEntry { key: key.into(), packaged, start, stop }
    }

    #[test]
    fn consent_store_entries_are_named() {
        let zoom = entry("C:#Users#Sam Lee#AppData#Roaming#Zoom#bin#Zoom.exe", false, 5, 0);
        assert_eq!(zoom.exe().as_deref(), Some("zoom.exe"));
        assert_eq!(zoom.call_name().as_deref(), Some("Zoom"));
        assert!(zoom.in_use());
        let teams = entry("MSTeams_8wekyb3d8bbwe", true, 5, 9);
        assert_eq!(teams.exe(), None);
        assert_eq!(teams.call_name().as_deref(), Some("Microsoft Teams"));
        assert!(!teams.in_use(), "stopped using it");
        assert!(!entry("C:#x#Zoom.exe", false, 0, 0).in_use(), "never used");
        let chrome = entry("C:#Program Files#Google#Chrome#Application#chrome.exe", false, 5, 0);
        assert_eq!(chrome.call_name().as_deref(), Some("a call in Chrome"));
        assert_eq!(entry("C:#Program Files#Audacity#Audacity.exe", false, 5, 0).call_name(), None);
        assert_eq!(entry("C:#Program Files#Trailmix#Trailmix.exe", false, 5, 0).call_name(), None, "not ourselves");
        assert_eq!(entry("Microsoft.WindowsSoundRecorder_8wekyb3d8bbwe", true, 5, 0).call_name(), None);
    }

    #[test]
    fn a_call_app_beats_a_browser() {
        let entries = vec![
            entry("C:#Program Files#Google#Chrome#Application#chrome.exe", false, 5, 0),
            entry("C:#Zoom#bin#Zoom.exe", false, 5, 0),
            entry("MSTeams_8wekyb3d8bbwe", true, 5, 7),
        ];
        assert_eq!(call_using_mic(&entries, None).as_deref(), Some("Zoom"));
        assert_eq!(call_using_mic(&entries[..1], None).as_deref(), Some("a call in Chrome"));
        assert_eq!(call_using_mic(&entries[2..], None), None);
        let running = vec!["chrome.exe".to_string()];
        assert_eq!(call_using_mic(&entries, Some(running.as_slice())).as_deref(), Some("a call in Chrome"), "Zoom isn't running");
    }

    #[test]
    fn asks_once_per_call_and_again_for_the_next() {
        let mut w = Watch::new(Duration::from_secs(60));
        let now = Instant::now();
        let zoom = || Some("Zoom".to_string());
        assert_eq!(w.tick(Situation::Idle, now, &[], zoom(), |_| true), Some(Do::Show(Kind::Start, "Zoom".into())));
        w.closed(); // Not now
        assert_eq!(w.tick(Situation::Idle, now, &[], zoom(), |_| true), None, "not asked twice about one call");
        assert_eq!(w.tick(Situation::Idle, now, &[], None, |_| false), None, "the call ended");
        assert_eq!(w.tick(Situation::Idle, now, &[], zoom(), |_| true), Some(Do::Show(Kind::Start, "Zoom".into())), "a new call");
        assert_eq!(w.tick(Situation::Idle, now, &[], None, |_| false), Some(Do::Hide), "gone with the call");
    }

    #[test]
    fn muted_apps_and_reminders_off_ask_nothing() {
        let mut w = Watch::new(Duration::from_secs(60));
        let now = Instant::now();
        assert_eq!(w.tick(Situation::Idle, now, &["Zoom".into()], Some("Zoom".into()), |_| true), None);
        assert_eq!(w.tick(Situation::Off, now, &[], Some("Zoom".into()), |_| true), None);
        assert_eq!(w.tick(Situation::Idle, now, &[], Some("Zoom".into()), |_| true), Some(Do::Show(Kind::Start, "Zoom".into())));
        assert_eq!(w.tick(Situation::Off, now, &[], Some("Zoom".into()), |_| true), Some(Do::Hide), "turned off: the card goes");
    }

    #[test]
    fn recording_puts_the_question_away_and_asks_to_stop_once_the_call_is_quiet() {
        let mut w = Watch::new(Duration::from_secs(60));
        let t0 = Instant::now();
        let zoom = || Some("Zoom".to_string());
        assert!(matches!(w.tick(Situation::Idle, t0, &[], zoom(), |_| true), Some(Do::Show(Kind::Start, _))));
        assert_eq!(w.tick(Situation::Recording, t0, &[], zoom(), |_| true), Some(Do::Hide), "recording now");
        assert_eq!(w.tick(Situation::Recording, t0 + Duration::from_secs(5), &[], None, |_| false), None, "quiet, not for long");
        assert_eq!(w.tick(Situation::Recording, t0 + Duration::from_secs(30), &[], None, |_| true), None, "back (speakers)");
        assert_eq!(w.tick(Situation::Recording, t0 + Duration::from_secs(40), &[], None, |_| false), None, "quiet again");
        assert_eq!(w.tick(Situation::Recording, t0 + Duration::from_secs(99), &[], None, |_| false), None, "59 s quiet");
        assert_eq!(
            w.tick(Situation::Recording, t0 + Duration::from_secs(100), &[], None, |_| false),
            Some(Do::Show(Kind::Stop, "Zoom".into())),
            "a minute of quiet"
        );
        w.closed(); // Keep recording
        assert_eq!(w.tick(Situation::Recording, t0 + Duration::from_secs(200), &[], None, |_| false), None, "asked once");
        assert_eq!(w.tick(Situation::Idle, t0 + Duration::from_secs(201), &[], None, |_| false), None);
    }

    #[test]
    fn a_recording_without_a_call_asks_nothing() {
        let mut w = Watch::new(Duration::from_secs(1));
        let t0 = Instant::now();
        assert_eq!(w.tick(Situation::Recording, t0, &[], None, |_| false), None);
        assert_eq!(w.tick(Situation::Recording, t0 + Duration::from_secs(10), &[], None, |_| false), None);
    }

    #[test]
    fn the_cards_say_what_the_mac_says() {
        let start = card_for(Kind::Start, "Zoom", false);
        assert_eq!(start.title, "Zoom call started");
        assert_eq!(start.buttons, vec!["Record".to_string(), "Not now".to_string()]);
        assert_eq!(start.link.as_deref(), Some("Don't ask for Zoom"));
        assert_eq!(card_for(Kind::Start, "a call in Chrome", false).title, "Looks like a call in Chrome");
        let stop = card_for(Kind::Stop, "Zoom", false);
        assert_eq!(stop.title, "The call seems to have ended");
        assert_eq!(stop.detail, "Zoom hasn't used the mic or speakers for a minute.");
        assert_eq!(stop.buttons[0], "Stop recording");
    }
}
