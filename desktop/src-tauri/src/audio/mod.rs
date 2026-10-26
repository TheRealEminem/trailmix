//! Audio sources for a recording: your mic and the meeting's other side, as wire-format audio.

pub mod apps;
pub mod convert;
pub mod wasapi;

use crate::prefs::{MeetingSource, Prefs};
use crate::wire::{AudioSource, MEETING, MIC};

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
