//! What the Trailmix backend expects on the audio WebSocket: 16 kHz mono little-endian int16 PCM, each
//! message prefixed with one channel byte (0 = your mic, 1 = meeting audio). The same wire format as the
//! Mac app (AudioWire.swift), so the server can't tell the two apart.

use std::collections::HashMap;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, Weak};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

pub const SAMPLE_RATE: usize = 16_000;
pub const FRAME_SAMPLES: usize = 4096; // ~256 ms per message, like the web app
pub const MIC: u8 = 0;
pub const MEETING: u8 = 1;

/// Where a source delivers its wire-format audio (from whatever thread it captures on).
pub type Sink = Arc<dyn Fn(&[i16]) + Send + Sync>;

/// Something that produces wire-format audio: the mic, other apps, or a file (for tests).
pub trait AudioSource: Send {
    fn name(&self) -> String;
    /// Starts delivering audio to `sink` until `stop`. Returns once audio is flowing, or with why it can't.
    fn start(&mut self, sink: Sink) -> Result<(), String>;
    fn stop(&mut self);
}

/// Loudness 0…1 on the same scale as the web app's meters (-58 dB is silence, -12 dB is loud).
pub fn meter_level(samples: &[i16]) -> f32 {
    if samples.is_empty() {
        return 0.0;
    }
    let sum: f32 = samples.iter().map(|&s| (s as f32 / 32768.0).powi(2)).sum();
    let db = 20.0 * ((sum / samples.len() as f32).sqrt() + 1e-8).log10();
    ((db + 58.0) / 46.0).clamp(0.0, 1.0)
}

type Sender = Box<dyn Fn(Vec<u8>) + Send>;

#[derive(Default)]
struct Track {
    pending: Vec<i16>,
    sent: usize,
}

impl Track {
    fn total(&self) -> usize {
        self.sent + self.pending.len()
    }
}

/// The streamer's bookkeeping, with the clock passed in so it can be tested.
#[derive(Default)]
struct Inner {
    tracks: HashMap<u8, Track>,
    sender: Option<Sender>,
    unsent: Vec<Vec<u8>>,
}

impl Inner {
    /// `elapsed`: wire samples since the recording started, when this buffer arrived.
    fn push(&mut self, samples: &[i16], channel: u8, elapsed: usize) {
        let track = self.tracks.entry(channel).or_insert_with(|| {
            let mut track = Track::default();
            // This buffer began `lead` samples after the start: line the track up with the recording.
            let lead = elapsed as isize - samples.len() as isize;
            if lead > (SAMPLE_RATE / 20) as isize {
                track.pending = vec![0; lead as usize];
            }
            track
        });
        track.pending.extend_from_slice(samples);
        self.drain(channel, false);
    }

    /// A track that stalled (the output device changed mid-call, say) gets silence for the time it
    /// missed, so timestamps stay true.
    fn fill_gaps(&mut self, elapsed: usize) {
        let channels: Vec<u8> = self.tracks.keys().copied().collect();
        for channel in channels {
            let gap = elapsed as isize - self.tracks[&channel].total() as isize;
            // Only real stalls: under 1.5 s is ordinary buffering; over 10 s means the computer slept, so leave it.
            if gap > (SAMPLE_RATE * 3 / 2) as isize && gap < (SAMPLE_RATE * 10) as isize {
                let fill = gap as usize - SAMPLE_RATE * 3 / 10;
                self.tracks.get_mut(&channel).unwrap().pending.extend(std::iter::repeat(0).take(fill));
                self.drain(channel, false);
            }
        }
    }

    fn drain(&mut self, channel: u8, all: bool) {
        loop {
            let track = self.tracks.get_mut(&channel).unwrap();
            let count = track.pending.len();
            if !(count >= FRAME_SAMPLES || (all && count > 0)) {
                break;
            }
            let n = count.min(FRAME_SAMPLES);
            let mut data = Vec::with_capacity(1 + n * 2);
            data.push(channel);
            for sample in track.pending.drain(..n) {
                data.extend_from_slice(&sample.to_le_bytes());
            }
            track.sent += n;
            match &self.sender {
                Some(send) => send(data),
                None => self.unsent.push(data),
            }
        }
    }

    fn drain_all(&mut self) {
        let channels: Vec<u8> = self.tracks.keys().copied().collect();
        for channel in channels {
            self.drain(channel, true);
        }
    }
}

/// Packs both tracks into WebSocket messages and keeps them in step: each track's first sample lines up
/// with the start of the recording, and a track that stalls gets silence for the gap. Messages wait here
/// until `attach` gives them somewhere to go.
pub struct Streamer {
    inner: Arc<Mutex<Inner>>,
    started: Instant,
    alive: Arc<AtomicBool>,
}

impl Streamer {
    pub fn new() -> Arc<Streamer> {
        let streamer = Arc::new(Streamer {
            inner: Arc::new(Mutex::new(Inner::default())),
            started: Instant::now(),
            alive: Arc::new(AtomicBool::new(true)),
        });
        let inner: Weak<Mutex<Inner>> = Arc::downgrade(&streamer.inner);
        let alive = streamer.alive.clone();
        let started = streamer.started;
        thread::Builder::new()
            .name("trailmix-wire-gaps".into())
            .spawn(move || loop {
                thread::sleep(Duration::from_secs(1));
                if !alive.load(Ordering::Relaxed) {
                    return;
                }
                let Some(inner) = inner.upgrade() else { return };
                let elapsed = elapsed_samples(started);
                lock(&inner).fill_gaps(elapsed);
            })
            .expect("thread");
        streamer
    }

    /// Starts sending, beginning with everything captured so far, in order.
    pub fn attach(&self, send: Sender) {
        let mut inner = lock(&self.inner);
        inner.sender = Some(send);
        let unsent = std::mem::take(&mut inner.unsent);
        let send = inner.sender.as_ref().unwrap();
        for data in unsent {
            send(data);
        }
    }

    pub fn push(&self, samples: &[i16], channel: u8) {
        let elapsed = elapsed_samples(self.started);
        lock(&self.inner).push(samples, channel, elapsed);
    }

    /// Sends whatever is left. Nothing else is sent after this.
    pub fn finish(&self) {
        self.alive.store(false, Ordering::Relaxed);
        lock(&self.inner).drain_all();
    }
}

impl Drop for Streamer {
    fn drop(&mut self) {
        self.alive.store(false, Ordering::Relaxed);
    }
}

fn elapsed_samples(started: Instant) -> usize {
    (started.elapsed().as_secs_f64() * SAMPLE_RATE as f64) as usize
}

fn lock(inner: &Mutex<Inner>) -> std::sync::MutexGuard<'_, Inner> {
    inner.lock().unwrap_or_else(|e| e.into_inner())
}

/// Plays a raw 16 kHz int16 file (or a WAV) as if it were a live source, then silence. Used by the self-test.
pub struct FileSource {
    name: String,
    samples: Arc<Vec<i16>>,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl FileSource {
    pub fn new(path: &Path) -> Result<FileSource, String> {
        let data = std::fs::read(path).map_err(|e| format!("Couldn't read {}: {e}", path.display()))?;
        Ok(FileSource {
            name: path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default(),
            samples: Arc::new(pcm_of(&data)),
            stop: Arc::new(AtomicBool::new(false)),
            thread: None,
        })
    }
}

/// The int16 samples in a file: a WAV's data chunk, or the whole file when it's raw PCM.
fn pcm_of(data: &[u8]) -> Vec<i16> {
    let body = if data.starts_with(b"RIFF") {
        match data.windows(4).position(|w| w == b"data") {
            Some(at) => &data[(at + 8).min(data.len())..], // past "data" and the chunk's size
            None => data,
        }
    } else {
        data
    };
    body.chunks_exact(2).map(|c| i16::from_le_bytes([c[0], c[1]])).collect()
}

impl AudioSource for FileSource {
    fn name(&self) -> String {
        self.name.clone()
    }

    fn start(&mut self, sink: Sink) -> Result<(), String> {
        let samples = self.samples.clone();
        let stop = self.stop.clone();
        self.thread = Some(thread::spawn(move || {
            const CHUNK: usize = 1600; // 100 ms
            let mut position = 0;
            let started = Instant::now();
            let mut sent = 0;
            while !stop.load(Ordering::Relaxed) {
                let end = (position + CHUNK).min(samples.len());
                if end > position {
                    sink(&samples[position..end]);
                    position = end;
                } else {
                    sink(&[0; CHUNK]);
                }
                sent += 1;
                // Keep real time (sleeping 100 ms each round would drift).
                let due = started + Duration::from_millis(100 * sent);
                if let Some(wait) = due.checked_duration_since(Instant::now()) {
                    thread::sleep(wait);
                }
            }
        }));
        Ok(())
    }

    fn stop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;

    fn collector() -> (Sender, mpsc::Receiver<Vec<u8>>) {
        let (tx, rx) = mpsc::channel();
        (Box::new(move |data| tx.send(data).unwrap()), rx)
    }

    #[test]
    fn meter_scale_matches_the_web_app() {
        assert_eq!(meter_level(&[]), 0.0);
        assert_eq!(meter_level(&[0; 100]), 0.0);
        assert!(meter_level(&[i16::MAX; 100]) > 0.99);
        let quiet: Vec<i16> = (0..100).map(|i| if i % 2 == 0 { 300 } else { -300 }).collect();
        let level = meter_level(&quiet); // about -41 dB
        assert!(level > 0.3 && level < 0.45, "{level}");
    }

    #[test]
    fn frames_carry_the_channel_byte_and_little_endian_samples() {
        let mut inner = Inner::default();
        let (send, rx) = collector();
        inner.sender = Some(send);
        let samples: Vec<i16> = (0..FRAME_SAMPLES as i16).collect();
        inner.push(&samples, MEETING, FRAME_SAMPLES);
        let frame = rx.try_recv().unwrap();
        assert_eq!(frame.len(), 1 + FRAME_SAMPLES * 2);
        assert_eq!(frame[0], MEETING);
        assert_eq!(&frame[1..5], &[0, 0, 1, 0]); // 0 then 1, little-endian
        assert!(rx.try_recv().is_err());
    }

    #[test]
    fn a_late_first_buffer_is_padded_to_the_recording_start() {
        let mut inner = Inner::default();
        let (send, rx) = collector();
        inner.sender = Some(send);
        // The first buffer (1600 samples) arrives a second in: 14400 samples of silence come first.
        inner.push(&[7; 1600], MIC, SAMPLE_RATE);
        let mut got = Vec::new();
        while let Ok(frame) = rx.try_recv() {
            got.extend(frame[1..].chunks(2).map(|c| i16::from_le_bytes([c[0], c[1]])));
        }
        assert_eq!(got.len(), 3 * FRAME_SAMPLES); // 16000 samples total; 12288 in whole frames so far
        assert!(got.iter().all(|&s| s == 0));
        inner.drain_all();
        let rest: Vec<u8> = rx.try_recv().unwrap();
        let tail: Vec<i16> = rest[1..].chunks(2).map(|c| i16::from_le_bytes([c[0], c[1]])).collect();
        assert_eq!(tail.len(), 16000 - 3 * FRAME_SAMPLES);
        assert_eq!(tail.iter().filter(|&&s| s == 7).count(), 1600);
        // A buffer arriving within 50 ms of the start isn't padded.
        let mut inner = Inner::default();
        inner.push(&[1; 100], MEETING, 500);
        assert_eq!(inner.tracks[&MEETING].pending.len(), 100);
    }

    #[test]
    fn messages_wait_until_attached_then_go_in_order() {
        let mut inner = Inner::default();
        inner.push(&[1; FRAME_SAMPLES], MIC, FRAME_SAMPLES);
        inner.push(&[2; FRAME_SAMPLES], MEETING, FRAME_SAMPLES);
        assert_eq!(inner.unsent.len(), 2);
        let streamer = Streamer::new();
        *lock(&streamer.inner) = inner;
        let (send, rx) = collector();
        streamer.attach(send);
        assert_eq!(rx.try_recv().unwrap()[0], MIC);
        assert_eq!(rx.try_recv().unwrap()[0], MEETING);
        streamer.push(&[3; 10], MIC);
        streamer.finish();
        let last = rx.try_recv().unwrap();
        assert_eq!(last.len(), 1 + 10 * 2);
        assert!(rx.try_recv().is_err());
    }

    #[test]
    fn a_stalled_track_is_filled_with_silence_but_not_a_sleep() {
        let mut inner = Inner::default();
        let (send, rx) = collector();
        inner.sender = Some(send);
        inner.push(&[1; FRAME_SAMPLES], MIC, FRAME_SAMPLES);
        rx.try_recv().unwrap();
        inner.fill_gaps(FRAME_SAMPLES + SAMPLE_RATE); // a second behind: ordinary buffering
        assert!(rx.try_recv().is_err());
        inner.fill_gaps(FRAME_SAMPLES + 3 * SAMPLE_RATE); // three seconds behind: a stall
        let total = inner.tracks[&MIC].total();
        assert_eq!(total, FRAME_SAMPLES + 3 * SAMPLE_RATE - SAMPLE_RATE * 3 / 10);
        assert!(rx.try_recv().is_ok());
        inner.fill_gaps(FRAME_SAMPLES + 30 * SAMPLE_RATE); // the computer slept: left alone
        assert_eq!(inner.tracks[&MIC].total(), total);
    }

    #[test]
    fn wav_headers_are_skipped() {
        let mut wav = b"RIFF\0\0\0\0WAVEfmt \x10\0\0\0\x01\0\x01\0\x80\x3e\0\0\0\x7d\0\0\x02\0\x10\0data\x04\0\0\0".to_vec();
        wav.extend_from_slice(&[0x34, 0x12, 0xff, 0xff]);
        assert_eq!(pcm_of(&wav), vec![0x1234, -1]);
        assert_eq!(pcm_of(&[1, 0, 2, 0]), vec![1, 2]);
    }
}
