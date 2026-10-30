//! Audio sources for a recording: your mic and the meeting's other side, as wire-format audio.

pub mod apps;
pub mod convert;
pub mod wasapi;

use std::sync::Arc;

use crate::prefs::{MeetingSource, Prefs};
use crate::soundcheck;
use crate::wire::{AudioSource, MEETING, MIC};

/// Part of the error when Windows' privacy settings keep Trailmix off the microphone (the sound check
/// looks for it, to say so and point at the switch).
pub const BLOCKED: &str = "Windows' privacy settings don't let Trailmix use it";

/// The sources for a recording: your mic, plus other apps unless you're in person. Tests swap in files.
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

/// What the sound check listens with: the default mic, and Trailmix's own audio (the chimes it plays) through
/// the per-process loopback a recording uses, pointed at Trailmix instead of away from it (a recording takes
/// every app but Trailmix, so it would never hear them). Where per-process loopback isn't available, the
/// whole output, as a recording takes it there. The self-test swaps in files.
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
