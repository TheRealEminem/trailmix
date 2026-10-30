//! The sound check (SoundCheck.swift): about five seconds that make sure both sides of a call will be
//! recorded. It listens to the mic for 2 seconds, then plays two short chimes and listens for them through
//! the per-app capture a recording uses. Run from setup and the record screen in the Trailmix window.
//!
//! Windows asks no permission to hear other apps (macOS does), so the only thing that keeps Trailmix from
//! recording is the microphone's privacy switch, which is read first. The result goes up with every
//! check-in as "sound_check", the same fields the Mac app reports:
//! {"mic": ok|quiet|denied|error, "system": ok|silent|error, "mic_level", "system_level", "detail", "at"}.

use std::f32::consts::TAU;
use std::sync::atomic::{AtomicI32, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

use crate::wire::{AudioSource, Sink};

/// Peaks (of 32767) that count as heard: the Mac app's.
pub const MIC_HEARD: i32 = 400;
pub const SYSTEM_HEARD: i32 = 200;

pub type Chime = Arc<dyn Fn() + Send + Sync>;

/// What a sound check listens with.
pub struct Kit {
    pub mic: Box<dyn AudioSource>,
    /// Where the chimes should be heard.
    pub system: Box<dyn AudioSource>,
    /// Starts one chime playing (and returns at once).
    pub chime: Chime,
    /// The microphone's privacy switch is off.
    pub mic_blocked: bool,
}

pub type MakeKit = Box<dyn Fn() -> Kit + Send + Sync>;

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// Runs the check (about 5 seconds) and says how it went.
pub fn run(kit: Kit) -> Value {
    let Kit { mut mic, mut system, chime, mic_blocked } = kit;
    let mut result = json!({ "at": now() });
    let mut details: Vec<String> = Vec::new();

    // Your side: 2 seconds of the mic.
    if mic_blocked {
        result["mic"] = json!("denied");
    } else {
        match listen(mic.as_mut(), Duration::from_secs(2), None) {
            Ok(peak) => {
                result["mic"] = json!(if peak > MIC_HEARD { "ok" } else { "quiet" });
                result["mic_level"] = json!(peak);
            }
            Err(e) if e.contains(crate::audio::BLOCKED) => result["mic"] = json!("denied"),
            Err(e) => {
                result["mic"] = json!("error");
                details.push(e);
            }
        }
    }

    // The other side: two chimes, listened for.
    match listen(system.as_mut(), Duration::from_secs(3), Some(&chime)) {
        Ok(peak) => {
            result["system"] = json!(if peak > SYSTEM_HEARD { "ok" } else { "silent" });
            result["system_level"] = json!(peak);
        }
        Err(e) => {
            result["system"] = json!("error");
            details.push(e);
        }
    }
    result["detail"] = json!(details.join(" "));
    result
}

/// Records from a source for a while (playing the chime twice meanwhile, if given); returns the peak level.
fn listen(source: &mut dyn AudioSource, length: Duration, chime: Option<&Chime>) -> Result<i32, String> {
    let peak = Arc::new(AtomicI32::new(0));
    let meter = peak.clone();
    let sink: Sink = Arc::new(move |samples: &[i16]| {
        let top = samples.iter().map(|&s| (s as i32).abs()).max().unwrap_or(0);
        meter.fetch_max(top, Ordering::Relaxed);
    });
    source.start(sink)?;
    match chime {
        Some(chime) => {
            thread::sleep(Duration::from_millis(400));
            for _ in 0..2 {
                chime();
                thread::sleep(Duration::from_millis(1100));
            }
            thread::sleep(length.saturating_sub(Duration::from_millis(2600)));
        }
        None => thread::sleep(length),
    }
    source.stop();
    Ok(peak.load(Ordering::Relaxed))
}

/// A short glassy chime (about the Mac's Glass sound), as a 16-bit mono WAV.
pub fn chime_wav() -> Vec<u8> {
    const RATE: u32 = 44_100;
    let count = (RATE as f32 * 0.7) as usize;
    let mut pcm: Vec<u8> = Vec::with_capacity(count * 2);
    for i in 0..count {
        let t = i as f32 / RATE as f32;
        let attack = (t / 0.004).min(1.0);
        let tone = 0.6 * (TAU * 1318.5 * t).sin() * (-t * 6.0).exp()
            + 0.3 * (TAU * 2637.0 * t).sin() * (-t * 11.0).exp()
            + 0.1 * (TAU * 3951.1 * t).sin() * (-t * 16.0).exp();
        let sample = (tone * attack * 0.45 * 32767.0).clamp(-32767.0, 32767.0) as i16;
        pcm.extend_from_slice(&sample.to_le_bytes());
    }
    let mut wav = Vec::with_capacity(44 + pcm.len());
    wav.extend_from_slice(b"RIFF");
    wav.extend_from_slice(&(36 + pcm.len() as u32).to_le_bytes());
    wav.extend_from_slice(b"WAVEfmt ");
    wav.extend_from_slice(&16u32.to_le_bytes());
    wav.extend_from_slice(&1u16.to_le_bytes()); // PCM
    wav.extend_from_slice(&1u16.to_le_bytes()); // mono
    wav.extend_from_slice(&RATE.to_le_bytes());
    wav.extend_from_slice(&(RATE * 2).to_le_bytes());
    wav.extend_from_slice(&2u16.to_le_bytes());
    wav.extend_from_slice(&16u16.to_le_bytes());
    wav.extend_from_slice(b"data");
    wav.extend_from_slice(&(pcm.len() as u32).to_le_bytes());
    wav.extend_from_slice(&pcm);
    wav
}

/// Plays the chime on the default output, from a thread of its own (so it returns at once).
pub fn play_chime() {
    thread::spawn(|| {
        let wav = chime_wav();
        #[cfg(windows)]
        let played = crate::win::play_wav(&wav);
        #[cfg(target_os = "linux")]
        let played = crate::audio::pulse::play_wav(&wav);
        if !played {
            eprintln!("[sound check] couldn't play the chime (no audio output?)");
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;
    use std::sync::Mutex;

    /// Delivers a constant level every 100 ms (or fails to start).
    struct Steady {
        level: i16,
        fail: Option<String>,
        stop: Arc<std::sync::atomic::AtomicBool>,
        thread: Option<thread::JoinHandle<()>>,
    }

    impl Steady {
        fn new(level: i16) -> Box<Steady> {
            Box::new(Steady { level, fail: None, stop: Default::default(), thread: None })
        }
        fn failing(why: &str) -> Box<Steady> {
            Box::new(Steady { level: 0, fail: Some(why.into()), stop: Default::default(), thread: None })
        }
    }

    impl AudioSource for Steady {
        fn name(&self) -> String {
            "steady".into()
        }
        fn start(&mut self, sink: Sink) -> Result<(), String> {
            if let Some(why) = &self.fail {
                return Err(why.clone());
            }
            let (level, stop) = (self.level, self.stop.clone());
            self.thread = Some(thread::spawn(move || {
                while !stop.load(Ordering::Relaxed) {
                    sink(&[level, -level, 0]);
                    thread::sleep(Duration::from_millis(100));
                }
            }));
            Ok(())
        }
        fn stop(&mut self) {
            self.stop.store(true, Ordering::Relaxed);
            if let Some(t) = self.thread.take() {
                let _ = t.join();
            }
        }
    }

    #[test]
    fn both_sides_heard() {
        let chimes = Arc::new(AtomicUsize::new(0));
        let counted = chimes.clone();
        let result = run(Kit {
            mic: Steady::new(3000),
            system: Steady::new(900),
            chime: Arc::new(move || {
                counted.fetch_add(1, Ordering::SeqCst);
            }),
            mic_blocked: false,
        });
        assert_eq!(result["mic"], "ok");
        assert_eq!(result["system"], "ok");
        assert_eq!(result["mic_level"], 3000);
        assert_eq!(result["system_level"], 900);
        assert_eq!(result["detail"], "");
        assert!(result["at"].as_f64().unwrap() > 1.7e9);
        assert_eq!(chimes.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn quiet_blocked_and_broken_are_told_apart() {
        let played = Arc::new(Mutex::new(0));
        let p = played.clone();
        let result = run(Kit {
            mic: Steady::new(50),
            system: Steady::new(0),
            chime: Arc::new(move || *p.lock().unwrap() += 1),
            mic_blocked: false,
        });
        assert_eq!((result["mic"].as_str(), result["system"].as_str()), (Some("quiet"), Some("silent")));

        let result = run(Kit {
            mic: Steady::new(3000), // never listened to: the switch is off
            system: Steady::failing("There's no audio output device to listen to"),
            chime: Arc::new(|| {}),
            mic_blocked: true,
        });
        assert_eq!(result["mic"], "denied");
        assert_eq!(result["system"], "error");
        assert_eq!(result["detail"], "There's no audio output device to listen to");

        let blocked = format!("Couldn't use the microphone: {}", crate::audio::BLOCKED);
        let result = run(Kit { mic: Steady::failing(&blocked), system: Steady::new(900), chime: Arc::new(|| {}), mic_blocked: false });
        assert_eq!(result["mic"], "denied");
        assert_eq!(result["detail"], "");
    }

    #[test]
    fn the_chime_is_a_wav() {
        let wav = chime_wav();
        assert!(wav.starts_with(b"RIFF") && &wav[8..16] == b"WAVEfmt ");
        assert_eq!(u32::from_le_bytes(wav[4..8].try_into().unwrap()) as usize, wav.len() - 8);
        let peak = wav[44..].chunks_exact(2).map(|c| i16::from_le_bytes([c[0], c[1]]).unsigned_abs()).max().unwrap();
        assert!(peak > 8000 && peak < 32767, "{peak}");
    }
}
