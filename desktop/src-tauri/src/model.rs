//! What the recorder is doing (HelperModel.swift on the Mac): the connection to the server, the recorder
//! socket the window's Record button reaches it through, a recording's sources and audio socket, and the
//! state the tray shows. Nothing here knows about Tauri; the tray and window watch it through `observe`.

use std::sync::{mpsc, Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tungstenite::Message;

use crate::audio;
use crate::client::{set_read_timeout, timed_out, Client, DraftLine, LiveRecording, ServerError, Socket};
use crate::login;
use crate::prefs::{MeetingSource, Prefs};
use crate::server::Server;
use crate::update::Updater;
use crate::wire::{AudioSource, Sink, Streamer, MEETING, MIC};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    Idle,
    Starting,
    Recording,
    Stopping,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Connection {
    Unknown,
    Online,
    Offline,
    NeedsToken,
    Outdated,
}

/// What this computer is, as the recorder reports it (and releases are named).
#[cfg(all(windows, target_arch = "aarch64"))]
pub const PLATFORM: &str = "windows-arm64";
#[cfg(all(windows, not(target_arch = "aarch64")))]
pub const PLATFORM: &str = "windows-x64";
#[cfg(all(target_os = "linux", target_arch = "aarch64"))]
pub const PLATFORM: &str = "linux-arm64";
#[cfg(all(target_os = "linux", not(target_arch = "aarch64")))]
pub const PLATFORM: &str = "linux-x86_64";

pub type Sources = Vec<(u8, Box<dyn AudioSource>)>;
pub type MakeSources = Box<dyn Fn(&Prefs) -> Result<Sources, String> + Send>;

/// What goes down the audio socket, in order.
enum Out {
    Frame(Vec<u8>),
    Text(String),
}

enum RecorderMsg {
    /// Send what this recorder would record now (if it changed).
    Report,
}

struct State {
    phase: Phase,
    connection: Connection,
    /// A recording another app is capturing (normally the Trailmix window).
    other: Option<LiveRecording>,
    other_fetched_at: Instant,
    meeting_id: Option<i64>,
    started_at: Option<Instant>,
    drafts: Vec<DraftLine>,
    marks: Vec<f64>,
    mic_name: String,
    capturing_meeting_audio: bool,
    last_meeting_id: Option<i64>,
    error: Option<String>,
    notice: Option<String>,
    starting_server: bool,
    quitting: bool,
    sources: Sources,
    streamer: Option<Arc<Streamer>>,
    out: Option<mpsc::Sender<Out>>,
    stop_requested: bool,
    /// Bumped at every wrap-up, so a stop timeout from an earlier recording does nothing.
    generation: u64,
    recorder_tx: Option<mpsc::Sender<RecorderMsg>>,
    mic_cache: Option<(Instant, Option<String>)>,
    call_app_cache: Option<(Instant, Option<String>)>,
}

/// A copy of the state for the tray and tests.
#[derive(Clone, Debug)]
pub struct Snapshot {
    pub phase: Phase,
    pub connection: Connection,
    pub other: Option<LiveRecording>,
    pub meeting_id: Option<i64>,
    pub elapsed: Option<Duration>,
    pub mic_name: String,
    pub capturing_meeting_audio: bool,
    pub last_meeting_id: Option<i64>,
    pub error: Option<String>,
    pub notice: Option<String>,
    pub starting_server: bool,
    pub drafts: Vec<DraftLine>,
    pub marks: Vec<f64>,
}

pub struct Model {
    state: Mutex<State>,
    prefs: Mutex<Prefs>,
    server: Option<Arc<Server>>,
    make_sources: Mutex<MakeSources>,
    observers: Mutex<Vec<Arc<dyn Fn() + Send + Sync>>>,
    /// The updater, for an installed Trailmix (not a development build or a test).
    updater: Mutex<Option<Arc<Updater>>>,
    pub version: String,
    machine: String,
    /// Tests point the model at a throwaway server instead of the one in Settings.
    server_override: Option<String>,
}

fn stop_sources(sources: Sources) {
    if sources.is_empty() {
        return;
    }
    thread::spawn(move || {
        for (_, mut source) in sources {
            source.stop();
        }
    });
}

impl Model {
    pub fn new(prefs: Prefs, server: Option<Arc<Server>>, version: String, server_override: Option<String>) -> Arc<Model> {
        Arc::new(Model {
            state: Mutex::new(State {
                phase: Phase::Idle,
                connection: Connection::Unknown,
                other: None,
                other_fetched_at: Instant::now(),
                meeting_id: None,
                started_at: None,
                drafts: Vec::new(),
                marks: Vec::new(),
                mic_name: String::new(),
                capturing_meeting_audio: false,
                last_meeting_id: None,
                error: None,
                notice: None,
                starting_server: false,
                quitting: false,
                sources: Vec::new(),
                streamer: None,
                out: None,
                stop_requested: false,
                generation: 0,
                recorder_tx: None,
                mic_cache: None,
                call_app_cache: None,
            }),
            prefs: Mutex::new(prefs),
            server,
            make_sources: Mutex::new(Box::new(audio::live_sources)),
            observers: Mutex::new(Vec::new()),
            updater: Mutex::new(None),
            version,
            #[cfg(windows)]
            machine: std::env::var("COMPUTERNAME").unwrap_or_default(),
            #[cfg(target_os = "linux")]
            machine: crate::linux::hostname(),
            server_override,
        })
    }

    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn prefs(&self) -> Prefs {
        self.prefs.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    pub fn set_source(&self, source: MeetingSource) {
        self.update_prefs(|prefs| prefs.source = source.raw());
    }

    /// Changes the settings, saves them, and lets the server and the tray know.
    pub fn update_prefs(&self, change: impl FnOnce(&mut Prefs)) {
        let mut prefs = self.prefs.lock().unwrap_or_else(|e| e.into_inner());
        change(&mut prefs);
        prefs.save();
        drop(prefs);
        self.check_in_soon();
        self.notify();
    }

    pub fn set_make_sources(&self, make: MakeSources) {
        *self.make_sources.lock().unwrap_or_else(|e| e.into_inner()) = make;
    }

    /// Called (from whatever thread) whenever something the tray shows may have changed.
    pub fn observe(&self, f: impl Fn() + Send + Sync + 'static) {
        self.observers.lock().unwrap_or_else(|e| e.into_inner()).push(Arc::new(f));
    }

    /// Calls the observers without holding any lock: one of them may be waiting on the main thread.
    fn notify(&self) {
        let observers: Vec<Arc<dyn Fn() + Send + Sync>> = self.observers.lock().unwrap_or_else(|e| e.into_inner()).clone();
        for f in observers {
            f();
        }
    }

    pub fn snapshot(&self) -> Snapshot {
        let s = self.lock();
        Snapshot {
            phase: s.phase,
            connection: s.connection,
            other: s.other.clone(),
            meeting_id: s.meeting_id,
            elapsed: elapsed(&s),
            mic_name: s.mic_name.clone(),
            capturing_meeting_audio: s.capturing_meeting_audio,
            last_meeting_id: s.last_meeting_id,
            error: s.error.clone(),
            notice: s.notice.clone(),
            starting_server: s.starting_server,
            drafts: s.drafts.clone(),
            marks: s.marks.clone(),
        }
    }

    pub fn client(&self) -> Result<Client, ServerError> {
        match &self.server_override {
            Some(url) => Ok(Client::new(url, "")),
            None => {
                let prefs = self.prefs();
                Ok(Client::new(&prefs.server_url(), &prefs.token))
            }
        }
    }

    pub fn server_url(&self) -> String {
        match &self.server_override {
            Some(url) => url.clone(),
            None => self.prefs().server_url(),
        }
    }

    // MARK: The recorder socket and check-ins

    /// Keeps in touch with the server from now on: over the recorder socket, or for an older server by
    /// asking and checking in.
    pub fn begin(self: &Arc<Self>) {
        let model = self.clone();
        thread::Builder::new().name("trailmix-recorder".into()).spawn(move || model.recorder_loop()).expect("thread");
    }

    /// Which build of the recorder this is, and what it can do: the web app hides what's missing. (An
    /// installed Trailmix also opens at login and updates itself: `recorder_info` adds those.)
    pub fn capabilities() -> Vec<&'static str> {
        #[cfg(target_os = "linux")]
        return crate::linux::capabilities();
        #[cfg(windows)]
        {
            let mut caps = vec!["system_audio"];
            if audio::per_app_supported() {
                caps.push("per_app_audio");
            }
            caps.extend(["hotkeys", "call_reminder"]);
            caps
        }
    }

    pub fn set_updater(&self, updater: Arc<Updater>) {
        *self.updater.lock().unwrap_or_else(|e| e.into_inner()) = Some(updater);
    }

    pub fn updater(&self) -> Option<Arc<Updater>> {
        self.updater.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    /// This Trailmix.exe, when it's the installed app (with its own engine): the one open at login starts.
    fn installed_exe(&self) -> Option<std::path::PathBuf> {
        self.server.as_ref()?;
        std::env::current_exe().ok()
    }

    /// Recording here or in the Trailmix window: updating now would cut it short.
    pub fn recording_anywhere(&self) -> bool {
        let s = self.lock();
        s.phase != Phase::Idle || s.other.is_some()
    }

    /// What this recorder would record, for the server (RecorderCheckIn in main.py).
    pub fn recorder_info(&self) -> Value {
        let prefs = self.prefs();
        let mic = match self.default_mic() {
            Some(name) if !name.is_empty() => format!("System default ({name})"),
            _ => "System default (No microphone)".to_string(),
        };
        let source = match prefs.meeting_source() {
            MeetingSource::AllApps => "All apps' audio".to_string(),
            MeetingSource::None => "Your mic only (in person)".to_string(),
            MeetingSource::App(exe) => audio::apps::display_name(&exe),
        };
        let mut capabilities = Self::capabilities();
        let installed = self.installed_exe();
        let open_at_login = installed.as_deref().map(login::enabled);
        if open_at_login.is_some() {
            capabilities.push("open_at_login");
        }
        let updater = self.updater();
        // (A Linux .deb or .rpm install only says an update is out: the package manager installs it.)
        if updater.as_ref().map(|u| u.installs_itself()).unwrap_or(false) {
            capabilities.push("auto_update");
        }
        json!({
            "mic": mic,
            "source": source,
            "machine": self.machine,
            "version": self.version,
            "mic_allowed": true,
            "sound_check": null,
            "shortcut_record": prefs.shortcut_record.display(),
            "shortcut_mark": prefs.shortcut_mark.display(),
            "open_at_login": open_at_login,
            "call_reminders": prefs.call_reminders,
            "lockdown": updater.as_ref().map(|u| u.lockdown()),
            "update": updater.as_ref().map(|u| u.report()),
            "call_app": self.call_app(),
            "platform": PLATFORM,
            "capabilities": capabilities,
        })
    }

    /// The default microphone's name, looked up every few seconds at most (it's a COM call).
    fn default_mic(&self) -> Option<String> {
        {
            let s = self.lock();
            if let Some((at, name)) = &s.mic_cache {
                if at.elapsed() < Duration::from_secs(10) {
                    return name.clone();
                }
            }
        }
        let name = audio::default_microphone();
        self.lock().mic_cache = Some((Instant::now(), name.clone()));
        name
    }

    /// A call app playing sound right now (looked up every few seconds): the engine then gets the speech model ready.
    fn call_app(&self) -> Option<String> {
        {
            let s = self.lock();
            if let Some((at, name)) = &s.call_app_cache {
                if at.elapsed() < Duration::from_secs(5) {
                    return name.clone();
                }
            }
        }
        let name = audio::apps::call_in_progress();
        self.lock().call_app_cache = Some((Instant::now(), name.clone()));
        name
    }

    /// Carries out what the window asked for. The sound check isn't on Windows yet (its capability flag is
    /// off, so the window doesn't ask).
    fn perform(self: &Arc<Self>, commands: &[String]) {
        let updater = self.updater();
        for command in commands {
            match command.as_str() {
                "start" => {
                    let s = self.lock();
                    if s.phase == Phase::Idle && s.other.is_none() {
                        drop(s);
                        self.start();
                    }
                }
                // Turning reminders on again also asks about the apps you'd said not to (as on the Mac).
                "call-reminders" => self.update_prefs(|prefs| {
                    prefs.call_reminders = true;
                    prefs.muted_call_apps.clear();
                }),
                "no-call-reminders" => self.update_prefs(|prefs| prefs.call_reminders = false),
                "open-at-login" | "no-open-at-login" => {
                    if let Some(exe) = self.installed_exe() {
                        login::set(&exe, command == "open-at-login");
                        self.check_in_soon();
                    }
                }
                _ => {
                    let Some(updater) = &updater else { continue };
                    match command.as_str() {
                        "update" => updater.install(),
                        "check-update" => updater.check_soon(true),
                        "go-back" => updater.go_back(),
                        "unskip-update" => updater.set_skipped(None),
                        "beta-updates" => updater.set_beta(true),
                        "no-beta-updates" => updater.set_beta(false),
                        "lockdown" => updater.set_lockdown(true),
                        "no-lockdown" => updater.set_lockdown(false),
                        _ => {}
                    }
                }
            }
        }
    }

    /// Keeps the connection status current and notices recordings started in the Trailmix window. With the
    /// recorder socket open that arrives by itself; this then does nothing.
    pub fn poll(self: &Arc<Self>) {
        if self.lock().recorder_tx.is_some() {
            return;
        }
        let Ok(client) = self.client() else { return };
        let result = client.live();
        let mut s = self.lock();
        match result {
            Ok(live) => {
                s.connection = Connection::Online;
                let mine = s.meeting_id;
                s.other = live.into_iter().find(|r| Some(r.id) != mine);
                s.other_fetched_at = Instant::now();
            }
            Err(ServerError::NeedsToken) => {
                s.connection = Connection::NeedsToken;
                s.other = None;
            }
            Err(ServerError::Outdated) => {
                s.connection = Connection::Outdated; // it can still record; it just can't share live state yet
                s.other = None;
            }
            Err(_) => {
                s.connection = Connection::Offline;
                s.other = None;
            }
        }
        drop(s);
        self.notify();
    }

    /// Lets the server know this recorder is here and what it would record, now: a message down the
    /// recorder socket, or for an older server a check-in, which also brings back what the window asked for.
    pub fn check_in(self: &Arc<Self>) {
        let tx = self.lock().recorder_tx.clone();
        if let Some(tx) = tx {
            let _ = tx.send(RecorderMsg::Report);
            return;
        }
        if self.lock().connection == Connection::Offline {
            return;
        }
        let Ok(client) = self.client() else { return };
        if let Ok(commands) = client.check_in(&self.recorder_info()) {
            self.perform(&commands);
        }
    }

    fn check_in_soon(&self) {
        if let Some(tx) = self.lock().recorder_tx.clone() {
            let _ = tx.send(RecorderMsg::Report);
        }
    }

    fn recorder_loop(self: Arc<Self>) {
        let mut legacy_server = false;
        let mut rounds: u64 = 0;
        loop {
            if self.lock().quitting {
                return;
            }
            rounds += 1;
            let Ok(client) = self.client() else {
                thread::sleep(Duration::from_millis(2500));
                continue;
            };
            if !legacy_server {
                match client.open_recorder_socket() {
                    Ok(socket) => {
                        self.run_recorder_socket(socket);
                        thread::sleep(Duration::from_secs(1));
                        continue;
                    }
                    Err(ServerError::Outdated) => legacy_server = true, // a server from before the socket: ask and check in instead
                    Err(ServerError::NeedsToken) => {
                        let mut s = self.lock();
                        s.connection = Connection::NeedsToken;
                        s.other = None;
                        drop(s);
                        self.notify();
                        thread::sleep(Duration::from_millis(2500));
                        continue;
                    }
                    Err(_) => {
                        let mut s = self.lock();
                        if s.connection != Connection::Offline {
                            s.connection = Connection::Offline;
                            s.other = None;
                            drop(s);
                            self.notify();
                        }
                        thread::sleep(Duration::from_millis(2500));
                        continue;
                    }
                }
            }
            self.poll();
            self.check_in();
            thread::sleep(Duration::from_secs(1));
            if rounds % 30 == 0 {
                legacy_server = false; // the server may have been updated meanwhile
            }
        }
    }

    /// Runs the recorder socket until it's lost: sends the report whenever it changed (and a ping now and
    /// then, so a dead line is noticed), and acts on what comes down it.
    fn run_recorder_socket(self: &Arc<Self>, mut socket: Socket) {
        set_read_timeout(&mut socket, Duration::from_millis(200));
        let (tx, rx) = mpsc::channel::<RecorderMsg>();
        {
            let mut s = self.lock();
            s.recorder_tx = Some(tx);
            s.connection = Connection::Online;
        }
        self.notify();
        let mut last_report: Option<String> = None;
        let mut last_tick = Instant::now() - Duration::from_secs(1);
        let mut ticks: u64 = 0;
        loop {
            if self.lock().quitting {
                break;
            }
            let mut asked = false;
            while let Ok(RecorderMsg::Report) = rx.try_recv() {
                asked = true;
            }
            if asked || last_tick.elapsed() >= Duration::from_secs(1) {
                last_tick = Instant::now();
                ticks += 1;
                let report = self.recorder_info().to_string();
                if last_report.as_deref() != Some(report.as_str()) {
                    if socket.send(Message::text(report.clone())).is_err() {
                        break;
                    }
                    last_report = Some(report);
                }
                if ticks % 20 == 0 && socket.send(Message::Ping(Vec::new().into())).is_err() {
                    break;
                }
            }
            match socket.read() {
                Ok(Message::Text(text)) => self.heard(text.as_str()),
                Ok(Message::Close(_)) => break,
                Ok(_) => {}
                Err(e) if timed_out(&e) => {}
                Err(_) => break,
            }
        }
        let _ = socket.close(None);
        {
            let mut s = self.lock();
            s.recorder_tx = None;
            s.connection = Connection::Offline;
            s.other = None;
        }
        self.notify();
    }

    fn heard(self: &Arc<Self>, text: &str) {
        let Ok(object) = serde_json::from_str::<Value>(text) else { return };
        if let Some(commands) = object.get("commands").and_then(|c| c.as_array()) {
            let list: Vec<String> = commands.iter().filter_map(|c| c.as_str().map(String::from)).collect();
            self.perform(&list);
        }
        if let Some(live) = object.get("live") {
            if let Ok(recordings) = serde_json::from_value::<Vec<LiveRecording>>(live.clone()) {
                let mut s = self.lock();
                let (mine, phase) = (s.meeting_id, s.phase);
                // Our own recording can be announced before meeting_id is set: don't take it for someone else's.
                s.other = recordings.into_iter().find(|r| Some(r.id) != mine && !(phase == Phase::Starting && r.client == "helper"));
                s.other_fetched_at = Instant::now();
                drop(s);
                self.notify();
            }
        }
    }

    // MARK: What the tray does

    pub fn is_busy(&self) -> bool {
        matches!(self.lock().phase, Phase::Starting | Phase::Stopping)
    }

    /// Record/Stop: stops whatever is recording (here or in the Trailmix window), otherwise starts.
    pub fn toggle(self: &Arc<Self>) {
        let s = self.lock();
        match s.phase {
            Phase::Idle => {
                let other = s.other.clone();
                drop(s);
                match other {
                    Some(other) => self.stop_other(other),
                    None => self.start(),
                }
            }
            Phase::Recording => {
                drop(s);
                self.stop();
            }
            Phase::Starting | Phase::Stopping => {}
        }
    }

    pub fn start(self: &Arc<Self>) {
        {
            let mut s = self.lock();
            if s.phase != Phase::Idle {
                return;
            }
            if s.other.is_some() {
                s.error = Some("Trailmix is already recording in its window. Use Stop to end it.".into());
                drop(s);
                self.notify();
                return;
            }
            s.error = None;
            s.notice = None;
            s.phase = Phase::Starting;
        }
        self.notify();
        let model = self.clone();
        thread::spawn(move || model.start_recording());
    }

    fn start_recording(self: &Arc<Self>) {
        let mut started: Sources = Vec::new();
        let outcome: Result<(), String> = (|| {
            self.ensure_server()?;
            // Capture first (buffered until the socket is ready), so a mic problem never leaves an empty meeting.
            let streamer = Streamer::new();
            let prefs = self.prefs();
            let sources = (self.make_sources.lock().unwrap_or_else(|e| e.into_inner()))(&prefs)?;
            for (channel, mut source) in sources {
                let into = streamer.clone();
                let sink: Sink = Arc::new(move |samples: &[i16]| into.push(samples, channel));
                match source.start(sink) {
                    Ok(()) => started.push((channel, source)),
                    Err(e) if channel == MEETING => {
                        self.lock().notice = Some(format!("Couldn't listen to other apps ({e}). Recording your mic only."));
                    }
                    Err(e) => return Err(e),
                }
            }
            let client = self.client().map_err(|e| e.to_string())?;
            let id = client.start_meeting().map_err(|e| e.to_string())?;
            let socket = client.open_stream(id, prefs.live_draft).map_err(|e| e.to_string())?;
            let (tx, rx) = mpsc::channel::<Out>();
            let frames = tx.clone();
            streamer.attach(Box::new(move |data| {
                let _ = frames.send(Out::Frame(data));
            }));
            {
                let mut s = self.lock();
                s.mic_name = started.iter().find(|(c, _)| *c == MIC).map(|(_, source)| source.name()).unwrap_or_default();
                s.capturing_meeting_audio = started.iter().any(|(c, _)| *c == MEETING);
                s.sources = std::mem::take(&mut started);
                s.streamer = Some(streamer);
                s.out = Some(tx);
                s.meeting_id = Some(id);
                s.started_at = Some(Instant::now());
                s.drafts.clear();
                s.marks.clear();
                s.stop_requested = false;
                s.phase = Phase::Recording;
            }
            let model = self.clone();
            thread::Builder::new().name("trailmix-stream".into()).spawn(move || model.run_stream(socket, rx, id)).map_err(|e| e.to_string())?;
            Ok(())
        })();
        if let Err(e) = outcome {
            stop_sources(std::mem::take(&mut started));
            let mut s = self.lock();
            s.phase = Phase::Idle;
            s.error = Some(e);
        }
        self.notify();
    }

    /// Sends the audio as it comes and listens for the server's messages, until the recording ends.
    fn run_stream(self: Arc<Self>, mut socket: Socket, rx: mpsc::Receiver<Out>, id: i64) {
        set_read_timeout(&mut socket, Duration::from_millis(10));
        loop {
            loop {
                match rx.try_recv() {
                    Ok(Out::Frame(data)) => {
                        if socket.send(Message::binary(data)).is_err() {
                            self.socket_ended(id, false);
                            return;
                        }
                    }
                    Ok(Out::Text(text)) => {
                        if socket.send(Message::text(text)).is_err() {
                            self.socket_ended(id, false);
                            return;
                        }
                    }
                    Err(mpsc::TryRecvError::Empty) => break,
                    Err(mpsc::TryRecvError::Disconnected) => {
                        let _ = socket.close(None);
                        return;
                    }
                }
            }
            match socket.read() {
                Ok(Message::Text(text)) => {
                    if self.received(text.as_str(), id) {
                        return;
                    }
                }
                Ok(Message::Close(_)) => {
                    self.socket_ended(id, false);
                    return;
                }
                Ok(_) => {}
                Err(e) if timed_out(&e) => {}
                Err(_) => {
                    self.socket_ended(id, false);
                    return;
                }
            }
        }
    }

    /// A message from the server about the recording. True once it said the recording stopped.
    fn received(&self, text: &str, id: i64) -> bool {
        let Ok(object) = serde_json::from_str::<Value>(text) else { return false };
        let kind = object.get("type").and_then(|t| t.as_str()).unwrap_or("");
        let mut s = self.lock();
        if s.meeting_id != Some(id) {
            return kind == "stopped";
        }
        match kind {
            "draft" => {
                if let Some(text) = object.get("text").and_then(|t| t.as_str()) {
                    s.drafts.push(DraftLine {
                        start: object.get("start").and_then(|v| v.as_f64()).unwrap_or(0.0),
                        speaker: object.get("speaker").and_then(|v| v.as_str()).map(String::from),
                        text: text.to_string(),
                    });
                }
            }
            "marked" => {
                if let Some(t) = object.get("t").and_then(|v| v.as_f64()) {
                    s.marks.push(t);
                }
            }
            "status" => s.notice = object.get("message").and_then(|m| m.as_str()).map(String::from),
            "stopped" => {
                let elsewhere = !s.stop_requested;
                drop(s);
                self.finished(elsewhere);
                self.notify();
                return true;
            }
            _ => {}
        }
        drop(s);
        self.notify();
        false
    }

    fn socket_ended(&self, id: i64, refused: bool) {
        let mut s = self.lock();
        if s.meeting_id != Some(id) || !matches!(s.phase, Phase::Recording | Phase::Stopping) {
            return;
        }
        if s.stop_requested {
            drop(s);
            self.finished(false);
            self.notify();
            return;
        }
        let sources = std::mem::take(&mut s.sources);
        stop_sources(sources);
        wrap_up(&mut s);
        s.error = Some(if refused {
            "Trailmix refused the audio connection. If it's a hosted Trailmix, check the access token in Settings.".into()
        } else {
            "Lost the connection to Trailmix. What was recorded so far is being processed.".into()
        });
        drop(s);
        self.notify();
    }

    fn finished(&self, stopped_elsewhere: bool) {
        let mut s = self.lock();
        if !matches!(s.phase, Phase::Recording | Phase::Stopping) {
            return;
        }
        if stopped_elsewhere {
            let sources = std::mem::take(&mut s.sources);
            stop_sources(sources);
            s.notice = Some("Recording stopped in Trailmix. It's being transcribed now.".into());
        }
        wrap_up(&mut s);
    }

    pub fn stop(self: &Arc<Self>) {
        let mut s = self.lock();
        if s.phase != Phase::Recording {
            return;
        }
        s.phase = Phase::Stopping;
        s.stop_requested = true;
        let sources = std::mem::take(&mut s.sources);
        let streamer = s.streamer.clone();
        let out = s.out.clone();
        let generation = s.generation;
        drop(s);
        self.notify();
        thread::spawn(move || {
            for (_, mut source) in sources {
                source.stop();
            }
            if let Some(streamer) = streamer {
                streamer.finish();
            }
            if let Some(out) = out {
                let _ = out.send(Out::Text(r#"{"type":"stop"}"#.into()));
            }
        });
        let model = self.clone();
        thread::spawn(move || {
            thread::sleep(Duration::from_secs(10));
            let s = model.lock();
            if s.generation == generation && s.phase == Phase::Stopping {
                drop(s);
                model.finished(false);
                model.notify();
            }
        });
    }

    pub fn stop_other(self: &Arc<Self>, other: LiveRecording) {
        let model = self.clone();
        thread::spawn(move || {
            match model.client().and_then(|c| c.stop(other.id)) {
                Ok(()) => {
                    thread::sleep(Duration::from_millis(600));
                    model.poll();
                }
                Err(e) => {
                    model.lock().error = Some(e.to_string());
                    model.notify();
                }
            }
        });
    }

    /// Flags the current moment of whatever is recording.
    pub fn mark_moment(self: &Arc<Self>) {
        let s = self.lock();
        if s.phase == Phase::Recording {
            if let Some(out) = &s.out {
                let _ = out.send(Out::Text(r#"{"type":"mark"}"#.into()));
            }
        } else if let Some(other) = s.other.clone() {
            drop(s);
            let model = self.clone();
            thread::spawn(move || {
                if let Err(e) = model.client().and_then(|c| c.mark(other.id)) {
                    model.lock().error = Some(e.to_string());
                    model.notify();
                } else {
                    model.poll();
                }
            });
        }
    }

    // MARK: The local server

    pub fn can_start_server(&self) -> bool {
        self.server.is_some() && self.server_override.is_none() && self.prefs().is_local_server()
    }

    /// If the local Trailmix isn't running, start it and wait for it.
    fn ensure_server(&self) -> Result<(), String> {
        let client = self.client().map_err(|e| e.to_string())?;
        match client.check() {
            Ok(()) => {
                self.lock().connection = Connection::Online;
                Ok(())
            }
            Err(ServerError::Offline) if self.can_start_server() => self.start_server(&client),
            Err(ServerError::NeedsToken) => {
                self.lock().connection = Connection::NeedsToken;
                Err(ServerError::NeedsToken.to_string())
            }
            Err(e) => Err(e.to_string()),
        }
    }

    fn start_server(&self, client: &Client) -> Result<(), String> {
        let Some(server) = &self.server else { return Err(ServerError::Offline.to_string()) };
        self.lock().starting_server = true;
        self.notify();
        let result = (|| {
            server.start(self.prefs().server_port())?;
            for _ in 0..180 {
                thread::sleep(Duration::from_millis(500));
                if client.check().is_ok() {
                    self.lock().connection = Connection::Online;
                    return Ok(());
                }
                if let Some(problem) = server.failure() {
                    return Err(problem);
                }
            }
            Err(format!("Trailmix didn't start. Its log is at {}.", crate::server::log_file().display()))
        })();
        self.lock().starting_server = false;
        self.notify();
        result
    }

    /// Called when the app opens: have the server up and waiting, so the first recording starts at once.
    pub fn start_server_if_needed(&self) -> Result<(), String> {
        let client = self.client().map_err(|e| e.to_string())?;
        if client.check().is_ok() {
            self.lock().connection = Connection::Online;
            self.notify();
            return Ok(());
        }
        if !self.can_start_server() {
            return Err(match self.server {
                Some(_) => "Trailmix isn't running at the address in Settings.".into(),
                None => "This build has no Trailmix engine inside it. Start one with ./trailmix, or install Trailmix.".into(),
            });
        }
        let result = self.start_server(&client);
        if let Err(e) = &result {
            self.lock().error = Some(e.clone());
            self.notify();
        }
        result
    }

    /// Ends whatever is recording and waits (briefly) for it to be saved: for quitting.
    pub fn quit_recording(self: &Arc<Self>) {
        self.lock().quitting = true;
        if self.lock().phase == Phase::Starting {
            thread::sleep(Duration::from_secs(2));
        }
        self.stop();
        let deadline = Instant::now() + Duration::from_secs(11);
        while Instant::now() < deadline && self.lock().phase != Phase::Idle {
            thread::sleep(Duration::from_millis(100));
        }
        if let Some(tx) = self.lock().recorder_tx.take() {
            drop(tx);
        }
    }
}

fn wrap_up(s: &mut State) {
    s.generation += 1;
    s.out = None;
    s.streamer = None;
    let leftover = std::mem::take(&mut s.sources);
    stop_sources(leftover);
    s.last_meeting_id = s.meeting_id;
    s.meeting_id = None;
    s.started_at = None;
    s.capturing_meeting_audio = false;
    s.phase = Phase::Idle;
}

/// Seconds recorded so far, for whichever recording is live.
fn elapsed(s: &State) -> Option<Duration> {
    if let Some(started) = s.started_at {
        if matches!(s.phase, Phase::Recording | Phase::Stopping) {
            return Some(started.elapsed());
        }
    }
    s.other.as_ref().map(|other| Duration::from_secs_f64(other.duration.max(0.0)) + s.other_fetched_at.elapsed())
}
