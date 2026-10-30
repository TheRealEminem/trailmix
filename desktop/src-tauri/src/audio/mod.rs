//! Audio sources for a recording: your mic and the meeting's other side, as wire-format audio. WASAPI on
//! Windows (wasapi.rs), the sound server on Linux (pulse.rs: PulseAudio, or PipeWire through pipewire-pulse).

#[cfg_attr(target_os = "linux", path = "apps_linux.rs")]
pub mod apps;
#[cfg_attr(not(windows), allow(dead_code))]
pub mod convert;
#[cfg(target_os = "linux")]
pub mod pulse;
#[cfg(windows)]
pub mod wasapi;

use std::sync::Arc;

use crate::prefs::{MeetingSource, Prefs};
use crate::soundcheck;
use crate::wire::{AudioSource, MEETING, MIC};

/// Part of the error when Windows' privacy settings keep Trailmix off the microphone (the sound check
/// looks for it, to say so and point at the switch).
pub const BLOCKED: &str = "Windows' privacy settings don't let Trailmix use it";

/// The sources for a recording: your mic, plus other apps unless you're in person. Tests swap in files.
#[cfg(windows)]
pub fn live_sources(prefs: &Prefs) -> Result<Vec<(u8, Box<dyn AudioSource>)>, String> {
    let mic: Box<dyn AudioSource> = Box::new(wasapi::MicCapture::new());
    let mut sources: Vec<(u8, Box<dyn AudioSource>)> = vec![(MIC, mic)];
    let target = match prefs.meeting_source() {
        MeetingSource::None => None,
        MeetingSource::AllApps => Some(wasapi::Target::AllApps),
        MeetingSource::App(exe) => Some(match apps::current().into_iter().find(|a| a.exe == exe) {
            Some(app) => wasapi::Target::Process(app.pid, app.name),
            // Not running: there's nothing of it to record, so take everything instead.
            None => wasapi::Target::AllApps,
        }),
    };
    if let Some(target) = target {
        let meeting: Box<dyn AudioSource> = Box::new(wasapi::LoopbackCapture::new(target));
        sources.push((MEETING, meeting));
    }
    Ok(sources)
}

/// The sources for a recording: your mic, plus other apps unless you're in person. Tests swap in files.
#[cfg(target_os = "linux")]
pub fn live_sources(prefs: &Prefs) -> Result<Vec<(u8, Box<dyn AudioSource>)>, String> {
    let mic: Box<dyn AudioSource> = Box::new(pulse::Capture::new(pulse::Target::Mic));
    let mut sources: Vec<(u8, Box<dyn AudioSource>)> = vec![(MIC, mic)];
    let target = match prefs.meeting_source() {
        MeetingSource::None => None,
        MeetingSource::AllApps => Some(pulse::Target::AllApps),
        // Its streams are picked up as they come (an app often makes one only once the call's sound starts).
        MeetingSource::App(exe) => {
            let name = apps::current().into_iter().find(|a| a.exe == exe).map(|a| a.name).unwrap_or_else(|| apps::display_name(&exe));
            Some(pulse::Target::App(exe, name))
        }
    };
    if let Some(target) = target {
        let meeting: Box<dyn AudioSource> = Box::new(pulse::Capture::new(target));
        sources.push((MEETING, meeting));
    }
    Ok(sources)
}

/// Whether one app's audio can be recorded on its own (the tray's per-app sources).
pub fn per_app_supported() -> bool {
    #[cfg(windows)]
    return wasapi::process_loopback_supported();
    #[cfg(target_os = "linux")]
    return pulse::available();
}

/// The default microphone's name.
pub fn default_microphone() -> Option<String> {
    #[cfg(windows)]
    return wasapi::default_microphone();
    #[cfg(target_os = "linux")]
    return pulse::default_microphone();
}

/// What the sound check listens with: the default mic, and Trailmix's own audio (the chimes it plays) through
/// the per-process loopback a recording uses, pointed at Trailmix instead of away from it (a recording takes
/// every app but Trailmix, so it would never hear them). Where per-process loopback isn't available, the
/// whole output, as a recording takes it there. The self-test swaps in files.
#[cfg(windows)]
pub fn sound_check_kit() -> soundcheck::Kit {
    let own = if wasapi::process_loopback_supported() {
        wasapi::Target::Process(std::process::id(), "Trailmix".into())
    } else {
        wasapi::Target::AllApps
    };
    soundcheck::Kit {
        mic: Box::new(wasapi::MicCapture::new()),
        system: Box::new(wasapi::LoopbackCapture::new(own)),
        chime: Arc::new(soundcheck::play_chime),
        mic_blocked: crate::win::mic_blocked(),
    }
}

/// What the sound check listens with on Linux: the default mic, and everything the default output plays (its
/// monitor, which hears Trailmix's own chimes too). There's no privacy switch to read.
#[cfg(target_os = "linux")]
pub fn sound_check_kit() -> soundcheck::Kit {
    soundcheck::Kit {
        mic: Box::new(pulse::Capture::new(pulse::Target::Mic)),
        system: Box::new(pulse::Capture::new(pulse::Target::AllApps)),
        chime: Arc::new(soundcheck::play_chime),
        mic_blocked: false,
    }
}
