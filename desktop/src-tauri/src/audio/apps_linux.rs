//! The apps you can pick as the meeting-audio source on Linux: whatever has a playback stream on the sound
//! server right now (PulseAudio's sink inputs, which PipeWire has too), the ones playing sound first. An app is
//! named by its process's binary (application.process.binary): Chrome plays from many processes, all "chrome".
//! Also which apps are recording from a mic (source outputs), for the call reminder.

use super::pulse;

#[derive(Clone, Debug, PartialEq)]
pub struct AudioApp {
    /// One of its processes.
    pub pid: u32,
    /// Its binary's name, lower-case ("zoom", "firefox"): the key a preference remembers it by.
    pub exe: String,
    pub name: String,
    pub playing: bool,
}

/// Apps people take calls in, by their binary. Browsers aren't here: they play all sorts of sound.
const CALL_APPS: &[(&str, &str)] = &[
    ("zoom", "Zoom"),
    ("zoom.real", "Zoom"),
    ("zoomwebviewhost", "Zoom"),
    ("teams", "Microsoft Teams"),
    ("teams-for-linux", "Microsoft Teams"),
    ("slack", "Slack"),
    ("discord", "Discord"),
    ("webex", "Webex"),
    ("ciscocollabhost", "Webex"),
    ("whatsapp", "WhatsApp"),
    ("whatsapp-for-linux", "WhatsApp"),
    ("skypeforlinux", "Skype"),
    ("telegram-desktop", "Telegram"),
    ("telegram", "Telegram"),
    ("signal-desktop", "Signal"),
    ("element-desktop", "Element"),
    ("jitsi-meet", "Jitsi Meet"),
];

const KNOWN_NAMES: &[(&str, &str)] = &[
    ("chrome", "Google Chrome"),
    ("google-chrome", "Google Chrome"),
    ("chromium", "Chromium"),
    ("chromium-browser", "Chromium"),
    ("msedge", "Microsoft Edge"),
    ("firefox", "Firefox"),
    ("firefox-esr", "Firefox"),
    ("firefox-bin", "Firefox"),
    ("brave", "Brave"),
    ("brave-browser", "Brave"),
    ("vivaldi-bin", "Vivaldi"),
    ("opera", "Opera"),
    ("spotify", "Spotify"),
    ("vlc", "VLC"),
    ("mpv", "mpv"),
    ("totem", "Videos"),
    ("rhythmbox", "Rhythmbox"),
];

/// Browsers people take calls in (Google Meet and the like): only counted while they use the microphone.
pub const BROWSERS: &[(&str, &str)] = &[
    ("chrome", "Chrome"),
    ("google-chrome", "Chrome"),
    ("chromium", "Chromium"),
    ("chromium-browser", "Chromium"),
    ("msedge", "Edge"),
    ("firefox", "Firefox"),
    ("firefox-esr", "Firefox"),
    ("firefox-bin", "Firefox"),
    ("brave", "Brave"),
    ("brave-browser", "Brave"),
    ("vivaldi-bin", "Vivaldi"),
    ("opera", "Opera"),
    ("epiphany", "Web"),
];

pub fn call_app_name(exe: &str) -> Option<&'static str> {
    CALL_APPS.iter().find(|(e, _)| *e == exe).map(|(_, n)| *n)
}

/// A readable name for a binary: "Zoom" for zoom, "Obs" for obs.
pub fn display_name(exe: &str) -> String {
    if let Some(name) = call_app_name(exe).or_else(|| KNOWN_NAMES.iter().find(|(e, _)| *e == exe).map(|(_, n)| *n)) {
        return name.to_string();
    }
    let mut chars = exe.chars();
    match chars.next() {
        Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
        None => exe.to_string(),
    }
}

/// The apps among these playback streams, the ones playing sound first.
pub fn apps_of(streams: &[pulse::StreamInfo]) -> Vec<AudioApp> {
    let mut apps: Vec<AudioApp> = Vec::new();
    for s in streams.iter().filter(|s| !s.internal && !s.is_ours() && !s.binary.is_empty()) {
        let playing = !s.corked;
        match apps.iter_mut().find(|a| a.exe == s.binary) {
            Some(app) => app.playing |= playing,
            None => {
                let known = call_app_name(&s.binary).is_some() || KNOWN_NAMES.iter().any(|(e, _)| *e == s.binary);
                let name = if known || s.app_name.is_empty() { display_name(&s.binary) } else { s.app_name.clone() };
                apps.push(AudioApp { pid: s.pid, exe: s.binary.clone(), name, playing });
            }
        }
    }
    apps.sort_by(|a, b| (!a.playing, a.name.to_lowercase()).cmp(&(!b.playing, b.name.to_lowercase())));
    apps
}

/// Apps with a playback stream right now, the ones playing sound first.
pub fn current() -> Vec<AudioApp> {
    match pulse::snapshot() {
        Ok(snapshot) => apps_of(&snapshot.playing),
        Err(_) => Vec::new(),
    }
}

/// A call app playing sound right now (a call is under way), if any.
pub fn call_in_progress() -> Option<String> {
    current().into_iter().find(|a| a.playing && call_app_name(&a.exe).is_some()).map(|a| a.name)
}

/// Is anything other than us making sound?
#[allow(dead_code)]
pub fn anything_playing() -> bool {
    current().iter().any(|a| a.playing)
}

/// The apps recording from a mic right now, by binary (and whether each is really taking sound, not paused).
pub fn mic_users() -> Vec<(String, bool)> {
    let Ok(snapshot) = pulse::snapshot() else { return Vec::new() };
    let mut out: Vec<(String, bool)> = Vec::new();
    for user in snapshot.mic_users() {
        let binary = if user.binary.is_empty() { user.app_name.to_lowercase() } else { user.binary.clone() };
        match out.iter_mut().find(|(b, _)| *b == binary) {
            Some(entry) => entry.1 |= !user.corked,
            None => out.push((binary, !user.corked)),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stream(binary: &str, name: &str, corked: bool) -> pulse::StreamInfo {
        pulse::StreamInfo { binary: binary.into(), app_name: name.into(), corked, pid: 4242, ..Default::default() }
    }

    #[test]
    fn names_are_readable() {
        assert_eq!(display_name("zoom"), "Zoom");
        assert_eq!(display_name("teams-for-linux"), "Microsoft Teams");
        assert_eq!(display_name("chrome"), "Google Chrome");
        assert_eq!(display_name("obs"), "Obs");
    }

    #[test]
    fn streams_roll_up_to_their_app() {
        let streams = vec![
            stream("chrome", "Google Chrome", true),
            stream("chrome", "Google Chrome", false),
            stream("spotify", "Spotify", true),
            stream("zoom", "ZOOM VoiceEngine", false),
            stream("paplay", "paplay", true),
            pulse::StreamInfo { binary: "trailmix".into(), ..Default::default() },
        ];
        let apps = apps_of(&streams);
        let names: Vec<(&str, bool)> = apps.iter().map(|a| (a.name.as_str(), a.playing)).collect();
        assert_eq!(names, vec![("Google Chrome", true), ("Zoom", true), ("paplay", false), ("Spotify", false)]);
    }
}
