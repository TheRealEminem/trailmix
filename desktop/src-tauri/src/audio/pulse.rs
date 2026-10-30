//! Recording on Linux, through PulseAudio's client API, which PipeWire serves too (pipewire-pulse): your mic
//! from the default source, every app's audio from the default output's monitor, or one app's playback streams
//! on their own (a monitor stream per sink input, as pavucontrol's level meters do). The sound server converts
//! to the wire format itself (16 kHz mono int16), so nothing here resamples.
//!
//! Each recording runs a PulseAudio main loop on a thread of its own and hands the audio on by the clock: what
//! arrived since the last round goes out, and silence where a stream had nothing (a paused app, a monitor whose
//! output went idle), so a track never falls behind the recording's own time.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use libpulse_binding as pa;
use pa::callbacks::ListResult;
use pa::context::{Context, FlagSet as ContextFlags, State as ContextState};
use pa::def::BufferAttr;
use pa::mainloop::standard::{IterateResult, Mainloop};
use pa::operation::{Operation, State as OpState};
use pa::proplist::{properties, Proplist};
use pa::sample::{Format, Spec};
use pa::stream::{FlagSet as StreamFlags, PeekResult, State as StreamState, Stream};

use crate::wire::{AudioSource, Sink, SAMPLE_RATE};

/// A connection to the sound server, with its main loop (used from one thread only).
pub struct Conn {
    mainloop: Mainloop,
    context: Context,
}

fn failed<E: std::fmt::Debug>(what: &str) -> impl FnOnce(E) -> String + '_ {
    move |e| format!("{what} ({e:?})")
}

impl Conn {
    pub fn open(name: &str) -> Result<Conn, String> {
        let no_server = "Can't reach the sound server (PulseAudio or PipeWire)";
        let mainloop = Mainloop::new().ok_or(no_server)?;
        let mut props = Proplist::new().ok_or(no_server)?;
        let _ = props.set_str(properties::APPLICATION_NAME, "Trailmix");
        let _ = props.set_str(properties::APPLICATION_ID, "local.trailmix.app");
        let _ = props.set_str(properties::APPLICATION_ICON_NAME, "Trailmix");
        let mut context = Context::new_with_proplist(&mainloop, name, &props).ok_or(no_server)?;
        context.connect(None, ContextFlags::NOAUTOSPAWN, None).map_err(failed(no_server))?;
        let mut conn = Conn { mainloop, context };
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if !conn.turn() {
                return Err(no_server.into());
            }
            match conn.context.get_state() {
                ContextState::Ready => return Ok(conn),
                ContextState::Failed | ContextState::Terminated => return Err(no_server.into()),
                _ => {}
            }
            if Instant::now() > deadline {
                return Err(format!("{no_server}: it didn't answer"));
            }
            thread::sleep(Duration::from_millis(5));
        }
    }

    /// One turn of the main loop, without waiting. False once it's broken.
    fn turn(&mut self) -> bool {
        matches!(self.mainloop.iterate(false), IterateResult::Success(_))
    }

    fn ready(&self) -> bool {
        self.context.get_state() == ContextState::Ready
    }

    /// Turns the main loop until `op` is done (or 5 seconds go by).
    fn wait<F: ?Sized>(&mut self, mut op: Operation<F>) -> bool {
        let deadline = Instant::now() + Duration::from_secs(5);
        while op.get_state() == OpState::Running {
            if !self.turn() || Instant::now() > deadline {
                op.cancel();
                return false;
            }
            thread::sleep(Duration::from_millis(1));
        }
        op.get_state() == OpState::Done
    }

    pub fn snapshot(&mut self) -> Result<Snapshot, String> {
        let out = Rc::new(RefCell::new(Snapshot::default()));
        let introspect = self.context.introspect();
        let o = out.clone();
        let op = introspect.get_server_info(move |info| {
            let mut s = o.borrow_mut();
            s.default_sink = info.default_sink_name.as_deref().unwrap_or("").to_string();
            s.default_source = info.default_source_name.as_deref().unwrap_or("").to_string();
            s.server = info.server_name.as_deref().unwrap_or("").to_string();
        });
        if !self.wait(op) {
            return Err("The sound server didn't say what it has".into());
        }
        let o = out.clone();
        let op = introspect.get_sink_info_list(move |item| {
            if let ListResult::Item(info) = item {
                o.borrow_mut().sinks.push(Device {
                    index: info.index,
                    name: info.name.as_deref().unwrap_or("").to_string(),
                    description: info.description.as_deref().unwrap_or("").to_string(),
                    monitor: info.monitor_source_name.as_deref().map(String::from),
                });
            }
        });
        self.wait(op);
        let o = out.clone();
        let op = introspect.get_source_info_list(move |item| {
            if let ListResult::Item(info) = item {
                o.borrow_mut().sources.push(Device {
                    index: info.index,
                    name: info.name.as_deref().unwrap_or("").to_string(),
                    description: info.description.as_deref().unwrap_or("").to_string(),
                    // A source that's another output's monitor: what it plays, not a mic.
                    monitor: info.monitor_of_sink.map(|sink| sink.to_string()),
                });
            }
        });
        self.wait(op);
        let o = out.clone();
        let op = introspect.get_sink_input_info_list(move |item| {
            if let ListResult::Item(info) = item {
                o.borrow_mut().playing.push(StreamInfo::read(info.index, info.sink, info.corked, &info.proplist));
            }
        });
        self.wait(op);
        let o = out.clone();
        let op = introspect.get_source_output_info_list(move |item| {
            if let ListResult::Item(info) = item {
                o.borrow_mut().recording.push(StreamInfo::read(info.index, info.source, info.corked, &info.proplist));
            }
        });
        self.wait(op);
        let snapshot = out.borrow().clone();
        Ok(snapshot)
    }
}

impl Drop for Conn {
    fn drop(&mut self) {
        self.context.disconnect();
    }
}

#[derive(Clone, Debug, Default)]
pub struct Device {
    pub index: u32,
    pub name: String,
    pub description: String,
    /// A sink's monitor source; for a source, the sink it monitors (then it isn't a mic).
    pub monitor: Option<String>,
}

/// An app's stream: a sink input (playing) or a source output (recording).
#[derive(Clone, Debug, Default)]
pub struct StreamInfo {
    pub index: u32,
    /// The sink or source it's connected to.
    pub device: u32,
    pub corked: bool,
    /// application.process.binary, lower-case ("zoom", "firefox").
    pub binary: String,
    pub app_name: String,
    pub pid: u32,
    /// PulseAudio's own peak meters and the like, not an app.
    pub internal: bool,
}

impl StreamInfo {
    fn read(index: u32, device: u32, corked: bool, props: &Proplist) -> StreamInfo {
        let get = |key: &str| props.get_str(key).unwrap_or_default();
        let binary = get(properties::APPLICATION_PROCESS_BINARY).to_lowercase();
        let app_name = get(properties::APPLICATION_NAME);
        let pid = get(properties::APPLICATION_PROCESS_ID).parse().unwrap_or(0);
        let role = get(properties::MEDIA_ROLE);
        let app_id = get(properties::APPLICATION_ID);
        let internal = role == "abstract" || role == "filter" || app_id.starts_with("org.PulseAudio") || app_id == "org.pipewire.pavucontrol";
        StreamInfo { index, device, corked, binary, app_name, pid, internal }
    }

    /// Ours (the recorder, or anything else Trailmix runs).
    pub fn is_ours(&self) -> bool {
        self.pid == std::process::id() || self.binary == "trailmix" || self.app_name == "Trailmix"
    }
}

/// What the sound server has right now.
#[derive(Clone, Debug, Default)]
pub struct Snapshot {
    pub server: String,
    pub default_sink: String,
    pub default_source: String,
    pub sinks: Vec<Device>,
    pub sources: Vec<Device>,
    pub playing: Vec<StreamInfo>,
    pub recording: Vec<StreamInfo>,
}

impl Snapshot {
    /// The default source, if it's a mic (and not another output's monitor, as it is with no mic at all).
    pub fn default_mic(&self) -> Option<&Device> {
        self.sources.iter().find(|s| s.name == self.default_source && s.monitor.is_none())
    }

    pub fn default_output(&self) -> Option<&Device> {
        self.sinks.iter().find(|s| s.name == self.default_sink)
    }

    /// The apps recording from a mic (not from a monitor), with their streams' state.
    pub fn mic_users(&self) -> Vec<&StreamInfo> {
        let mics: Vec<u32> = self.sources.iter().filter(|s| s.monitor.is_none()).map(|s| s.index).collect();
        self.recording.iter().filter(|r| mics.contains(&r.device) && !r.internal && !r.is_ours()).collect()
    }
}

/// What the sound server has now, on a connection of its own.
pub fn snapshot() -> Result<Snapshot, String> {
    Conn::open("Trailmix (looking)")?.snapshot()
}

/// Whether there's a sound server to record from (looked at once a minute at most).
pub fn available() -> bool {
    static SEEN: Mutex<Option<(Instant, bool)>> = Mutex::new(None);
    let mut seen = SEEN.lock().unwrap_or_else(|e| e.into_inner());
    if let Some((at, ok)) = *seen {
        if at.elapsed() < Duration::from_secs(60) {
            return ok;
        }
    }
    let ok = Conn::open("Trailmix (checking)").is_ok();
    *seen = Some((Instant::now(), ok));
    ok
}

pub fn default_microphone() -> Option<String> {
    let snapshot = snapshot().ok()?;
    snapshot.default_mic().map(|d| if d.description.is_empty() { d.name.clone() } else { d.description.clone() })
}

/// Plays a 16-bit PCM WAV (the sound check's chime) on the default output, and returns once it's played.
pub fn play_wav(wav: &[u8]) -> bool {
    if wav.len() < 44 || &wav[..4] != b"RIFF" || &wav[8..12] != b"WAVE" {
        return false;
    }
    let channels = u16::from_le_bytes([wav[22], wav[23]]) as u8;
    let rate = u32::from_le_bytes([wav[24], wav[25], wav[26], wav[27]]);
    let pcm = &wav[44..];
    let Ok(mut conn) = Conn::open("Trailmix (sound check)") else { return false };
    let spec = Spec { format: Format::S16le, channels, rate };
    let Some(mut stream) = Stream::new(&mut conn.context, "Sound check", &spec, None) else { return false };
    if stream.connect_playback(None, None, StreamFlags::NOFLAGS, None, None).is_err() {
        return false;
    }
    let deadline = Instant::now() + Duration::from_secs(10);
    let frame = 2 * channels.max(1) as usize;
    let mut sent = 0;
    while Instant::now() < deadline {
        if !conn.turn() {
            return false;
        }
        match stream.get_state() {
            StreamState::Ready => {}
            StreamState::Failed | StreamState::Terminated => return false,
            _ => {
                thread::sleep(Duration::from_millis(2));
                continue;
            }
        }
        if sent < pcm.len() {
            let room = stream.writable_size().unwrap_or(0);
            let n = (room.min(pcm.len() - sent) / frame) * frame;
            if n > 0 {
                if stream.write(&pcm[sent..sent + n], None, 0, pa::stream::SeekMode::Relative).is_err() {
                    return false;
                }
                sent += n;
            }
        } else {
            let drained = stream.drain(None);
            let done = conn.wait(drained);
            let _ = stream.disconnect();
            return done;
        }
        thread::sleep(Duration::from_millis(2));
    }
    false
}

// MARK: Recording

#[derive(Clone, Debug, PartialEq)]
pub enum Target {
    /// The default source: your mic.
    Mic,
    /// Everything the default output plays (its monitor), following it when it changes.
    AllApps,
    /// One app's playback streams (by application.process.binary), with its name.
    App(String, String),
}

const SPEC: Spec = Spec { format: Format::S16le, channels: 1, rate: SAMPLE_RATE as u32 };
/// How often the audio is handed on.
const ROUND: Duration = Duration::from_millis(100);
/// How far behind the clock the output stays, so audio that's on its way isn't taken for silence.
const LAG: usize = SAMPLE_RATE / 4;
/// More than this waiting in a stream (its clock runs a little fast): the oldest goes.
const MOST: usize = SAMPLE_RATE * 2;

struct Feed {
    stream: Stream,
    buffer: Vec<i16>,
}

impl Feed {
    fn open(conn: &mut Conn, name: &str, device: Option<&str>, monitor_of: Option<u32>) -> Result<Feed, String> {
        let mut stream = Stream::new(&mut conn.context, name, &SPEC, None).ok_or("Couldn't make a recording stream")?;
        if let Some(input) = monitor_of {
            stream.set_monitor_stream(input).map_err(failed("Couldn't follow that app's sound"))?;
        }
        let attr = BufferAttr {
            maxlength: u32::MAX,
            tlength: u32::MAX,
            prebuf: u32::MAX,
            minreq: u32::MAX,
            fragsize: (SAMPLE_RATE / 10 * 2) as u32, // 100 ms
        };
        stream.connect_record(device, Some(&attr), StreamFlags::ADJUST_LATENCY).map_err(failed("Couldn't start recording"))?;
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if !conn.turn() {
                return Err("The sound server went away".into());
            }
            match stream.get_state() {
                StreamState::Ready => return Ok(Feed { stream, buffer: Vec::new() }),
                StreamState::Failed | StreamState::Terminated => return Err(format!("The sound server refused to record {name}")),
                _ => {}
            }
            if Instant::now() > deadline {
                return Err(format!("Recording {name} didn't start"));
            }
            thread::sleep(Duration::from_millis(5));
        }
    }

    fn alive(&self) -> bool {
        matches!(self.stream.get_state(), StreamState::Ready | StreamState::Creating)
    }

    /// Takes whatever the stream has.
    fn read(&mut self) {
        loop {
            let got = match self.stream.peek() {
                Ok(PeekResult::Data(bytes)) => Some(bytes.chunks_exact(2).map(|b| i16::from_le_bytes([b[0], b[1]])).collect::<Vec<i16>>()),
                Ok(PeekResult::Hole(_)) => None,
                Ok(PeekResult::Empty) | Err(_) => break,
            };
            let _ = self.stream.discard();
            if let Some(samples) = got {
                self.buffer.extend_from_slice(&samples);
            }
        }
        if self.buffer.len() > MOST {
            let extra = self.buffer.len() - MOST / 2;
            self.buffer.drain(..extra);
        }
    }
}

impl Drop for Feed {
    fn drop(&mut self) {
        let _ = self.stream.disconnect();
    }
}

/// What a recording follows: the device (for the mic or the monitor) or the app's streams.
struct Recording {
    target: Target,
    conn: Conn,
    /// For the mic and all apps, one feed (key 0); for an app, one per sink input it has.
    feeds: HashMap<u32, Feed>,
    /// The output whose monitor is being recorded (to notice when the default output changes).
    following: String,
    label: String,
}

impl Recording {
    fn open(target: Target) -> Result<Recording, String> {
        let conn = Conn::open("Trailmix (recording)")?;
        let mut recording = Recording { target, conn, feeds: HashMap::new(), following: String::new(), label: String::new() };
        recording.attach()?;
        Ok(recording)
    }

    /// Connects (again) to what the target is now.
    fn attach(&mut self) -> Result<(), String> {
        let snapshot = self.conn.snapshot()?;
        match self.target.clone() {
            Target::Mic => {
                if self.feeds.contains_key(&0) {
                    return Ok(());
                }
                let mic = snapshot.default_mic().ok_or("There's no microphone (the sound server has no input device)")?;
                self.label = if mic.description.is_empty() { mic.name.clone() } else { mic.description.clone() };
                // No device named: the stream follows the default mic when it changes (a headset plugged in).
                let feed = Feed::open(&mut self.conn, "Microphone", None, None)?;
                self.feeds.insert(0, feed);
            }
            Target::AllApps => {
                let output = snapshot.default_output().ok_or("There's no output device to record other apps from")?;
                if self.following == output.name && self.feeds.get(&0).map(Feed::alive).unwrap_or(false) {
                    return Ok(());
                }
                let monitor = output.monitor.clone().ok_or("The output device has no monitor to record from")?;
                self.feeds.clear();
                let feed = Feed::open(&mut self.conn, "Other apps", Some(&monitor), None)?;
                self.feeds.insert(0, feed);
                self.following = output.name.clone();
                self.label = output.description.clone();
            }
            Target::App(binary, name) => {
                self.label = name;
                self.feeds.retain(|_, feed| feed.alive());
                for input in snapshot.playing.iter().filter(|p| p.binary == binary && !p.is_ours()) {
                    if self.feeds.contains_key(&input.index) {
                        continue;
                    }
                    let Some(monitor) = snapshot.sinks.iter().find(|s| s.index == input.device).and_then(|s| s.monitor.clone()) else {
                        continue;
                    };
                    match Feed::open(&mut self.conn, "Meeting audio", Some(&monitor), Some(input.index)) {
                        Ok(feed) => {
                            self.feeds.insert(input.index, feed);
                        }
                        Err(e) => eprintln!("[audio] couldn't follow {binary}'s stream {}: {e}", input.index),
                    }
                }
            }
        }
        Ok(())
    }

    /// `n` samples: every feed's, added together, silence where a feed had none.
    fn mix(&mut self, n: usize) -> Vec<i16> {
        let mut out = vec![0i32; n];
        for feed in self.feeds.values_mut() {
            let take = feed.buffer.len().min(n);
            for (o, s) in out.iter_mut().zip(feed.buffer.drain(..take)) {
                *o += s as i32;
            }
        }
        out.into_iter().map(|s| s.clamp(i16::MIN as i32, i16::MAX as i32) as i16).collect()
    }
}

/// Runs a recording until `stop`: reads every feed, and hands on what's due by the clock.
fn run(mut recording: Recording, sink: Sink, stop: Arc<AtomicBool>) {
    let started = Instant::now();
    let mut handed: usize = 0;
    let mut last_round = Instant::now();
    let mut last_look = Instant::now();
    let mut broken_since: Option<Instant> = None;
    while !stop.load(Ordering::Relaxed) {
        if !recording.conn.turn() || !recording.conn.ready() {
            // The sound server went away (restarted): connect again, and keep the time with silence meanwhile.
            broken_since.get_or_insert_with(Instant::now);
            thread::sleep(Duration::from_millis(500));
            if let Ok(again) = Recording::open(recording.target.clone()) {
                eprintln!("[audio] the sound server is back; recording {} again", again.label);
                recording = again;
                broken_since = None;
            }
        } else {
            for feed in recording.feeds.values_mut() {
                feed.read();
            }
        }
        if last_look.elapsed() >= Duration::from_secs(2) && broken_since.is_none() {
            last_look = Instant::now();
            if let Err(e) = recording.attach() {
                eprintln!("[audio] {e}");
            }
        }
        if last_round.elapsed() >= ROUND {
            last_round = Instant::now();
            let due = (started.elapsed().as_secs_f64() * SAMPLE_RATE as f64) as usize;
            let due = due.saturating_sub(LAG);
            if due > handed {
                let samples = recording.mix(due - handed);
                handed = due;
                sink(&samples);
            }
        }
        thread::sleep(Duration::from_millis(10));
    }
}

/// A recording source on the sound server: the mic, all apps, or one app.
pub struct Capture {
    target: Target,
    label: Arc<Mutex<String>>,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl Capture {
    pub fn new(target: Target) -> Capture {
        Capture { target, label: Arc::new(Mutex::new(String::new())), stop: Arc::new(AtomicBool::new(false)), thread: None }
    }
}

impl AudioSource for Capture {
    fn name(&self) -> String {
        match &self.target {
            Target::Mic => self.label.lock().map(|n| n.clone()).unwrap_or_default(),
            Target::AllApps => "All apps".into(),
            Target::App(_, name) => name.clone(),
        }
    }

    fn start(&mut self, sink: Sink) -> Result<(), String> {
        let (tx, rx) = mpsc::channel::<Result<(), String>>();
        let (target, label, stop) = (self.target.clone(), self.label.clone(), self.stop.clone());
        self.thread = Some(thread::Builder::new().name("trailmix-audio".into()).spawn(move || {
            let recording = match Recording::open(target) {
                Ok(recording) => recording,
                Err(e) => {
                    let _ = tx.send(Err(e));
                    return;
                }
            };
            *label.lock().unwrap_or_else(|e| e.into_inner()) = recording.label.clone();
            let _ = tx.send(Ok(()));
            run(recording, sink, stop);
        }).map_err(|e| e.to_string())?);
        match rx.recv_timeout(Duration::from_secs(15)) {
            Ok(result) => result,
            Err(_) => Err("Recording didn't start (the sound server didn't answer)".into()),
        }
    }

    fn stop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}
