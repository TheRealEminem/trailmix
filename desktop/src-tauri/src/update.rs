//! Keeps Trailmix up to date from its GitHub releases (Updater.swift on the Mac): checks at launch and every
//! few hours; on request downloads the new installer, checks it against the SHA-256 published with the
//! release, makes sure it's a Trailmix installer of that version, then quits and lets a copy of this program
//! (`--apply-update`) run it silently over this install once the app and its engine are gone, and open the
//! new version.
//!
//! The installer is unsigned, so there's no Authenticode check yet: what's checked is the file name, the
//! published checksum, and the installer's own version resource (product name and version). A file this app
//! downloads itself carries no mark of the web, so SmartScreen doesn't stop the silent install.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::server;
use crate::win;

/// The GitHub repo releases come from (owner/name), baked in by build.rs; empty for a build without one.
const REPO: &str = env!("TRAILMIX_UPDATE_REPO");

/// This computer's kind, as release installers are named: Trailmix-<version>-windows-<arch>.exe.
#[cfg(target_arch = "aarch64")]
pub const ARCH: &str = "arm64";
#[cfg(not(target_arch = "aarch64"))]
pub const ARCH: &str = "x64";

pub const APPLY_FLAG: &str = "--apply-update";

const DETACHED_PROCESS: u32 = 0x0000_0008;
const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const CREATE_BREAKAWAY_FROM_JOB: u32 = 0x0100_0000;

#[derive(Clone, Debug, PartialEq)]
pub enum State {
    Idle,
    Checking,
    UpToDate,
    /// Couldn't reach GitHub to check.
    Offline,
    Available(String),
    Downloading(String, Option<f64>),
    Installing(String),
    Failed(String),
}

#[derive(Clone, Debug, PartialEq)]
pub struct Release {
    pub version: String,
    pub notes: Option<String>,
    /// The installer's file name and where to download it and its checksum.
    pub name: String,
    pub installer: String,
    pub checksum: String,
    pub prerelease: bool,
}

/// What's remembered between launches, in updates.json in the data folder.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
struct Saved {
    /// Beta updates: also offer pre-releases.
    beta: bool,
    /// Lockdown mode's copy (the server keeps it in step): no checks on its own.
    lockdown: bool,
    /// The version this one replaced, remembered when an update installs: Go back offers it.
    previous_version: Option<String>,
    /// A version you went back from: not offered again until a newer one comes out.
    skip_version: Option<String>,
    /// Why the last install didn't happen (written by `--apply-update`), shown once.
    last_error: Option<String>,
}

impl Saved {
    fn path(data: &Path) -> PathBuf {
        data.join("updates.json")
    }

    fn load(data: &Path) -> Saved {
        fs::read_to_string(Self::path(data)).ok().and_then(|text| serde_json::from_str(&text).ok()).unwrap_or_default()
    }

    fn save(&self, data: &Path) {
        let _ = fs::create_dir_all(data);
        if let Ok(text) = serde_json::to_string_pretty(self) {
            let _ = fs::write(Self::path(data), text);
        }
    }
}

struct Inner {
    state: State,
    latest: Option<Release>,
    previous: Option<Release>,
    checked_at: Option<f64>,
    going_back: bool,
    saved: Saved,
}

type Hook = Arc<dyn Fn() + Send + Sync>;

#[derive(Default)]
struct Hooks {
    /// True while something is recording: updating would cut it short.
    busy: Option<Arc<dyn Fn() -> bool + Send + Sync>>,
    /// Stops recording, stops the engine and exits (the tray's Quit).
    quit: Option<Hook>,
    changed: Vec<Hook>,
}

pub struct Updater {
    inner: Mutex<Inner>,
    current: String,
    data: PathBuf,
    exe: PathBuf,
    checking: AtomicBool,
    hooks: Mutex<Hooks>,
}

impl Updater {
    pub fn new(current: String, data: PathBuf, exe: PathBuf) -> Arc<Updater> {
        let mut saved = Saved::load(&data);
        let mut state = State::Idle;
        if let Some(why) = saved.last_error.take() {
            state = State::Failed(why);
            saved.save(&data);
        }
        Arc::new(Updater {
            inner: Mutex::new(Inner { state, latest: None, previous: None, checked_at: None, going_back: false, saved }),
            current,
            data,
            exe,
            checking: AtomicBool::new(false),
            hooks: Mutex::new(Hooks::default()),
        })
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn hooks(&self) -> MutexGuard<'_, Hooks> {
        self.hooks.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn set_busy(&self, f: impl Fn() -> bool + Send + Sync + 'static) {
        self.hooks().busy = Some(Arc::new(f));
    }

    pub fn set_quit(&self, f: impl Fn() + Send + Sync + 'static) {
        self.hooks().quit = Some(Arc::new(f));
    }

    /// Called (from whatever thread) whenever the state changes.
    pub fn observe(&self, f: impl Fn() + Send + Sync + 'static) {
        self.hooks().changed.push(Arc::new(f));
    }

    fn notify(&self) {
        let hooks: Vec<Hook> = self.hooks().changed.clone();
        for f in hooks {
            f();
        }
    }

    fn set_state(&self, state: State) {
        let changed = {
            let mut inner = self.lock();
            let changed = inner.state != state;
            inner.state = state;
            changed
        };
        if changed {
            self.notify();
        }
    }

    pub fn state(&self) -> State {
        self.lock().state.clone()
    }

    /// A look for a new version is under way (asked for or not).
    pub fn is_checking(&self) -> bool {
        self.checking.load(Ordering::SeqCst)
    }

    /// The version on offer, if there is one.
    pub fn available(&self) -> Option<String> {
        match self.state() {
            State::Available(version) => Some(version),
            _ => None,
        }
    }

    /// Only an installed Trailmix (one the installer put there, with its uninstaller next to it) updates
    /// itself, and only with somewhere to look: the repo it was built from, or TRAILMIX_UPDATE_URL (tests).
    pub fn enabled_for(exe: &Path) -> bool {
        let installed = exe.parent().map(|dir| dir.join("uninstall.exe").is_file()).unwrap_or(false);
        installed && (!REPO.is_empty() || std::env::var_os("TRAILMIX_UPDATE_URL").is_some())
    }

    /// Where releases are looked up: TRAILMIX_UPDATE_URL (tests), else the repo's recent releases. They're
    /// listed rather than asking for GitHub's "latest": a release whose Windows build failed has no installer,
    /// and the newest one that has is offered instead.
    fn feed(&self) -> Option<String> {
        if let Ok(url) = std::env::var("TRAILMIX_UPDATE_URL") {
            return Some(url);
        }
        (!REPO.is_empty()).then(|| format!("https://api.github.com/repos/{REPO}/releases?per_page=30"))
    }

    /// All recent releases, for finding the one to go back to: TRAILMIX_RELEASES_URL (tests), else GitHub's list.
    fn releases_feed(&self) -> Option<String> {
        if let Ok(url) = std::env::var("TRAILMIX_RELEASES_URL") {
            return Some(url);
        }
        (!REPO.is_empty()).then(|| format!("https://api.github.com/repos/{REPO}/releases?per_page=30"))
    }

    /// Checks a little after launch (TRAILMIX_UPDATE_DELAY seconds, 20 by default) and every 6 hours.
    pub fn begin(self: &Arc<Self>) {
        clean_old_downloads();
        let delay: f64 = std::env::var("TRAILMIX_UPDATE_DELAY").ok().and_then(|d| d.parse().ok()).unwrap_or(20.0);
        let updater = self.clone();
        let _ = thread::Builder::new().name("trailmix-updater".into()).spawn(move || {
            thread::sleep(Duration::from_secs_f64(delay.max(0.0)));
            loop {
                updater.check(false);
                thread::sleep(Duration::from_secs(6 * 3600));
            }
        });
    }

    pub fn set_beta(self: &Arc<Self>, on: bool) {
        {
            let mut inner = self.lock();
            if inner.saved.beta == on {
                return;
            }
            inner.saved.beta = on;
            inner.saved.save(&self.data);
            if !on && matches!(inner.state, State::Available(_)) && inner.latest.as_ref().map(|l| l.prerelease).unwrap_or(false) {
                inner.latest = None;
                inner.state = State::Idle;
            }
        }
        self.notify();
        self.check_soon(false);
    }

    pub fn lockdown(&self) -> bool {
        self.lock().saved.lockdown
    }

    pub fn set_lockdown(&self, on: bool) {
        let mut inner = self.lock();
        if inner.saved.lockdown != on {
            inner.saved.lockdown = on;
            inner.saved.save(&self.data);
        }
    }

    /// Offers the version you went back from again (or, with a version, skips it).
    pub fn set_skipped(self: &Arc<Self>, version: Option<String>) {
        {
            let mut inner = self.lock();
            inner.saved.skip_version = version;
            inner.saved.save(&self.data);
        }
        self.check_soon(false);
    }

    /// Looks for a new version on another thread.
    pub fn check_soon(self: &Arc<Self>, manual: bool) {
        let updater = self.clone();
        thread::spawn(move || updater.check(manual));
    }

    /// For the window (the recorder check-in's "update"): {"current", "state", "version", "progress",
    /// "error", "notes", "checked_at", …}, as the Mac app reports it.
    pub fn report(&self) -> Value {
        let inner = self.lock();
        let mut out = json!({
            "current": self.current,
            "checked_at": inner.checked_at,
            "beta": inner.saved.beta,
            "prerelease": inner.latest.as_ref().map(|l| l.prerelease).unwrap_or(false),
            "previous": inner.previous.as_ref().map(|p| p.version.clone()),
            "skipped": inner.saved.skip_version,
            "going_back": inner.going_back,
            "lockdown": inner.saved.lockdown,
        });
        let latest_version = inner.latest.as_ref().map(|l| l.version.clone()).unwrap_or_default();
        match &inner.state {
            State::Idle => out["state"] = json!("idle"),
            State::Checking => out["state"] = json!("checking"),
            State::UpToDate => out["state"] = json!("up-to-date"),
            State::Offline => out["state"] = json!("offline"),
            State::Available(v) => {
                out["state"] = json!("available");
                out["version"] = json!(v);
                out["notes"] = json!(inner.latest.as_ref().and_then(|l| l.notes.clone()).unwrap_or_default());
            }
            State::Downloading(v, progress) => {
                out["state"] = json!("downloading");
                out["version"] = json!(v);
                out["progress"] = json!(progress);
            }
            State::Installing(v) => {
                out["state"] = json!("installing");
                out["version"] = json!(v);
            }
            State::Failed(why) => {
                out["state"] = json!("failed");
                out["version"] = json!(latest_version);
                out["error"] = json!(why);
            }
        }
        out
    }

    /// Looks for a new version. `manual`: you asked (Check for updates), so it shows "Checking…" meanwhile and
    /// happens even in Lockdown mode.
    pub fn check(&self, manual: bool) {
        let Some(feed) = self.feed() else { return };
        {
            let inner = self.lock();
            if !manual && inner.saved.lockdown {
                return;
            }
            if matches!(inner.state, State::Downloading(..) | State::Installing(_)) {
                return;
            }
        }
        if self.checking.swap(true, Ordering::SeqCst) {
            return; // one look at a time
        }
        struct Done<'a>(&'a AtomicBool);
        impl Drop for Done<'_> {
            fn drop(&mut self) {
                self.0.store(false, Ordering::SeqCst);
            }
        }
        let _done = Done(&self.checking);

        if manual && self.lock().latest.is_none() {
            self.set_state(State::Checking);
        }
        let Some(parsed) = self.fetch_json(&feed) else {
            let offline = {
                let inner = self.lock();
                inner.latest.is_none() && (manual || inner.state == State::Checking)
            };
            if offline {
                self.set_state(State::Offline);
            }
            log(&format!("couldn't check for updates at {feed}"));
            return;
        };
        let previous = self.find_previous();
        {
            let mut inner = self.lock();
            if matches!(inner.state, State::Downloading(..) | State::Installing(_)) {
                return; // an install started meanwhile
            }
            inner.checked_at = Some(now());
            if let Some(previous) = previous {
                inner.previous = previous;
            }
            let skipped = inner.saved.skip_version.clone();
            // A version you went back from stays skipped; a newer one than it is offered as usual.
            match newest(&parsed, inner.saved.beta) {
                Some(release)
                    if skipped.as_ref().map(|s| is_newer(&release.version, s)).unwrap_or(true) && is_newer(&release.version, &self.current) =>
                {
                    inner.state = State::Available(release.version.clone());
                    inner.latest = Some(release);
                }
                _ => {
                    inner.latest = None;
                    inner.state = State::UpToDate;
                }
            }
        }
        self.notify();
    }

    /// The release to go back to, if the list could be had (Some(None): there's none).
    fn find_previous(&self) -> Option<Option<Release>> {
        let url = self.releases_feed()?;
        let parsed = self.fetch_json(&url)?;
        let list = parsed.as_array()?;
        let inner = self.lock();
        Some(previous(list, &self.current, inner.saved.previous_version.as_deref(), inner.saved.beta))
    }

    fn user_agent(&self) -> String {
        format!("Trailmix/{} (Windows)", self.current)
    }

    fn fetch_json(&self, url: &str) -> Option<Value> {
        let response = minreq::get(url)
            .with_header("Accept", "application/vnd.github+json")
            .with_header("User-Agent", self.user_agent())
            .with_timeout(30)
            .send()
            .ok()?;
        if response.status_code != 200 {
            return None;
        }
        response.json::<Value>().ok()
    }

    // MARK: Installing

    pub fn install(self: &Arc<Self>) {
        let latest = self.lock().latest.clone();
        if let Some(release) = latest {
            self.install_release(release, false);
        }
    }

    /// Settings → Updates → Go back: installs the previous version the same checked way, and skips this one
    /// until a newer version comes out. Meetings stay (the database works with the older version too).
    pub fn go_back(self: &Arc<Self>) {
        let previous = self.lock().previous.clone();
        match previous {
            Some(release) => self.install_release(release, true),
            None => self.set_state(State::Failed("There's no earlier version to go back to.".into())),
        }
    }

    fn install_release(self: &Arc<Self>, release: Release, going_back: bool) {
        if matches!(self.state(), State::Downloading(..) | State::Installing(_)) {
            return;
        }
        let busy = self.hooks().busy.clone();
        if busy.map(|f| f()).unwrap_or(false) {
            self.set_state(State::Failed("Finish the recording first, then update.".into()));
            return;
        }
        let Some(folder) = self.exe.parent().map(Path::to_path_buf) else { return };
        if !writable(&folder) {
            self.set_state(State::Failed(format!(
                "Trailmix can't update itself where it's installed ({}). Download the new version and install it instead.",
                folder.display()
            )));
            return;
        }
        self.lock().going_back = going_back;
        self.set_state(State::Downloading(release.version.clone(), None));
        let updater = self.clone();
        thread::spawn(move || match updater.download_and_hand_over(&release, &folder, going_back) {
            Ok(()) => {
                let quit = updater.hooks().quit.clone();
                match quit {
                    Some(quit) => quit(),
                    None => std::process::exit(0),
                }
            }
            Err(why) => {
                log(&format!("couldn't update to {}: {why}", release.version));
                updater.lock().going_back = false;
                updater.set_state(State::Failed(why));
            }
        });
    }

    fn download_and_hand_over(&self, release: &Release, install_dir: &Path, going_back: bool) -> Result<(), String> {
        let stamp = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
        let folder = std::env::temp_dir().join(format!("trailmix-update-{}-{stamp}", std::process::id()));
        fs::create_dir_all(&folder).map_err(|e| format!("Couldn't make a folder for the download ({e})"))?;
        let installer = folder.join(&release.name);
        log(&format!("downloading {} from {}", release.name, release.installer));
        let digest = self.download(release, &installer)?;

        let published = self.published_checksum(&release.checksum).ok_or("The update has no published checksum, so it wasn't installed")?;
        if digest != published {
            return Err("The update didn't match its published checksum, so it wasn't installed".into());
        }
        self.set_state(State::Installing(release.version.clone()));
        check_installer(&installer, &release.name, &release.version)?;

        // The installer replaces Trailmix.exe, so a copy of it, elsewhere, waits for this app to quit and runs it.
        let helper = folder.join("trailmix-updater.exe");
        fs::copy(&self.exe, &helper).map_err(|e| format!("Couldn't get the update ready ({e})"))?;
        let mut command = Command::new(&helper);
        command
            .arg(APPLY_FLAG)
            .arg(&installer)
            .arg(std::process::id().to_string())
            .arg(install_dir)
            .arg(&release.version)
            .arg(&self.current)
            .arg(if going_back { "back" } else { "forward" })
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        let flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW;
        // Out of any job this app was started in, so it isn't ended along with the app (not every job allows it).
        let spawned = command.creation_flags(flags | CREATE_BREAKAWAY_FROM_JOB).spawn().or_else(|_| command.creation_flags(flags).spawn());
        spawned.map_err(|e| format!("Couldn't start installing the update ({e})"))?;
        log(&format!("handed {} over to the installer; quitting", release.version));
        Ok(())
    }

    /// Downloads straight to disk, showing progress, and returns the file's SHA-256.
    fn download(&self, release: &Release, target: &Path) -> Result<String, String> {
        let failed = |e: String| format!("Couldn't download the update ({e})");
        let mut response = minreq::get(&release.installer)
            .with_header("User-Agent", self.user_agent())
            .with_header("Accept", "application/octet-stream")
            .with_timeout(3600)
            .send_lazy()
            .map_err(|e| failed(e.to_string()))?;
        if response.status_code != 200 {
            return Err(failed(format!("the server answered {}", response.status_code)));
        }
        let total: Option<u64> = response.headers.get("content-length").and_then(|v| v.trim().parse().ok());
        let mut file = File::create(target).map_err(|e| failed(e.to_string()))?;
        let mut hasher = Sha256::new();
        let mut buffer = vec![0u8; 1 << 16];
        let mut done: u64 = 0;
        let mut shown = Instant::now();
        loop {
            let n = response.read(&mut buffer).map_err(|e| failed(e.to_string()))?;
            if n == 0 {
                break;
            }
            file.write_all(&buffer[..n]).map_err(|e| failed(e.to_string()))?;
            hasher.update(&buffer[..n]);
            done += n as u64;
            if shown.elapsed() >= Duration::from_millis(300) {
                shown = Instant::now();
                if let Some(total) = total.filter(|t| *t > 0) {
                    self.set_state(State::Downloading(release.version.clone(), Some((done as f64 / total as f64).min(1.0))));
                }
            }
        }
        file.flush().map_err(|e| failed(e.to_string()))?;
        if let Some(total) = total {
            if done != total {
                return Err(failed(format!("got {done} of {total} bytes")));
            }
        }
        Ok(hex(&hasher.finalize()))
    }

    fn published_checksum(&self, url: &str) -> Option<String> {
        let response = minreq::get(url).with_header("User-Agent", self.user_agent()).with_timeout(30).send().ok()?;
        if response.status_code != 200 {
            return None;
        }
        parse_checksum(response.as_str().ok()?)
    }
}

/// The first word of a .sha256 file ("<hex>  <name>"), if it's a SHA-256.
pub fn parse_checksum(text: &str) -> Option<String> {
    let word = text.split_whitespace().next()?.to_lowercase();
    (word.len() == 64 && word.chars().all(|c| c.is_ascii_hexdigit())).then_some(word)
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn writable(folder: &Path) -> bool {
    let probe = folder.join(".trailmix-update-check");
    match File::create(&probe) {
        Ok(_) => {
            let _ = fs::remove_file(&probe);
            true
        }
        Err(_) => false,
    }
}

/// Is this the Trailmix installer it should be? A Windows program (MZ), named as releases name it, whose
/// version resource (which the installer build writes) says Trailmix at that version.
fn check_installer(file: &Path, expected_name: &str, version: &str) -> Result<(), String> {
    let not_trailmix = |why: &str| format!("The update isn't a Trailmix installer ({why}), so it wasn't installed");
    if file.file_name().map(|n| n.to_string_lossy() != expected_name).unwrap_or(true) {
        return Err(not_trailmix("wrong name"));
    }
    let mut head = [0u8; 2];
    File::open(file).and_then(|mut f| f.read_exact(&mut head)).map_err(|e| not_trailmix(&e.to_string()))?;
    if &head != b"MZ" {
        return Err(not_trailmix("not a program"));
    }
    match win::version_string(file, "ProductName") {
        Some(name) if name == "Trailmix" => {}
        Some(name) => return Err(not_trailmix(&format!("it's {name}"))),
        None => return Err(not_trailmix("no product name")),
    }
    match win::version_string(file, "ProductVersion") {
        Some(v) if v == version => Ok(()),
        Some(v) => Err(not_trailmix(&format!("it's version {v}, not {version}"))),
        None => Err(not_trailmix("no version")),
    }
}

/// Downloads from earlier updates (each left its folder behind: the program that installed it was in it).
fn clean_old_downloads() {
    let Ok(entries) = fs::read_dir(std::env::temp_dir()) else { return };
    for entry in entries.flatten() {
        if entry.file_name().to_string_lossy().starts_with("trailmix-update-") {
            let _ = fs::remove_dir_all(entry.path());
        }
    }
}

/// Appends a line to logs/update.log in the data folder (started afresh when it gets big).
pub fn log(line: &str) {
    let path = server::data_dir().join("logs").join("update.log");
    if let Some(dir) = path.parent() {
        let _ = fs::create_dir_all(dir);
    }
    let big = fs::metadata(&path).map(|m| m.len() > 1 << 20).unwrap_or(false);
    let file = if big { File::create(&path) } else { OpenOptions::new().create(true).append(true).open(&path) };
    if let Ok(mut file) = file {
        let _ = writeln!(file, "{:.0} {line}", now());
    }
}

// MARK: Which release

/// The installer for this computer in a release: Trailmix-<version>-windows-<arch>.exe.
pub fn installer_name(version: &str) -> String {
    format!("Trailmix-{version}-windows-{ARCH}.exe")
}

/// A release from GitHub's JSON, if it has an installer for this computer and the checksum published next to
/// it. One without (its Windows build failed) is passed over.
pub fn release(json: &Value) -> Option<Release> {
    let tag = json.get("tag_name")?.as_str()?;
    let version = tag.strip_prefix('v').unwrap_or(tag).to_string();
    let name = installer_name(&version);
    let asset = |wanted: &str| -> Option<String> {
        json.get("assets")?.as_array()?.iter().find_map(|a| {
            (a.get("name")?.as_str()? == wanted).then(|| a.get("browser_download_url")?.as_str().map(String::from)).flatten()
        })
    };
    let installer = asset(&name)?;
    let checksum = asset(&format!("{name}.sha256"))?;
    Some(Release {
        version,
        notes: json.get("html_url").and_then(|u| u.as_str()).map(String::from),
        name,
        installer,
        checksum,
        prerelease: json.get("prerelease").and_then(|p| p.as_bool()).unwrap_or(false),
    })
}

fn is_draft(json: &Value) -> bool {
    json.get("draft").and_then(|d| d.as_bool()).unwrap_or(false)
}

/// The release to offer from GitHub's answer, a list of releases (or just one, as tests may give): the highest
/// version that has an installer for this computer and isn't a draft (nor, without beta updates, a pre-release).
pub fn newest(parsed: &Value, beta: bool) -> Option<Release> {
    let list: Vec<&Value> = match parsed {
        Value::Array(items) => items.iter().collect(),
        Value::Object(_) => vec![parsed],
        _ => Vec::new(),
    };
    list.into_iter()
        .filter(|r| !is_draft(r))
        .filter_map(release)
        .filter(|r| beta || !r.prerelease)
        .fold(None, |best: Option<Release>, r| match best {
            Some(b) if !is_newer(&r.version, &b.version) => Some(b),
            _ => Some(r),
        })
}

/// The release to go back to: the one this version replaced if it's still published, else the newest one
/// older than this (a pre-release only with beta updates on).
pub fn previous(list: &[Value], current: &str, replaced: Option<&str>, beta: bool) -> Option<Release> {
    let older: Vec<Release> = list.iter().filter(|r| !is_draft(r)).filter_map(release).filter(|r| is_newer(current, &r.version)).collect();
    if let Some(replaced) = replaced {
        if let Some(same) = older.iter().find(|r| r.version == replaced) {
            return Some(same.clone());
        }
    }
    older.into_iter().filter(|r| beta || !r.prerelease).fold(None, |best: Option<Release>, r| match best {
        Some(b) if !is_newer(&r.version, &b.version) => Some(b),
        _ => Some(r),
    })
}

/// Compares versions the way the Mac app does: the numbers in order ("0.19.0-build.7" is 0, 19, 0, 7), a
/// missing one counting as 0.
pub fn is_newer(a: &str, b: &str) -> bool {
    let numbers = |v: &str| -> Vec<u64> { v.split(['.', '-', '+']).filter_map(|p| p.parse::<u64>().ok()).collect() };
    let (x, y) = (numbers(a), numbers(b));
    for i in 0..x.len().max(y.len()) {
        let (p, q) = (x.get(i).copied().unwrap_or(0), y.get(i).copied().unwrap_or(0));
        if p != q {
            return p > q;
        }
    }
    false
}

// MARK: Installing it (a copy of Trailmix.exe, run with --apply-update)

/// `--apply-update <installer> <pid> <install folder> <version> <from version> <forward|back>`: waits for the
/// app (pid) and anything else running from the install folder to end, runs the installer silently over it,
/// remembers what to go back to, and opens the new version (unless TRAILMIX_UPDATE_NO_RELAUNCH, for tests).
pub fn apply(args: &[String]) -> i32 {
    let rest: Vec<&String> = args.iter().skip_while(|a| a.as_str() != APPLY_FLAG).skip(1).collect();
    if rest.len() < 6 {
        log("--apply-update needs <installer> <pid> <folder> <version> <from> <forward|back>");
        return 2;
    }
    let installer = PathBuf::from(rest[0]);
    let pid: u32 = rest[1].parse().unwrap_or(0);
    let folder = PathBuf::from(rest[2]);
    let (version, from, going_back) = (rest[3].as_str(), rest[4].as_str(), rest[5] == "back");
    log(&format!("installing {version} over {from} in {}", folder.display()));

    if pid != 0 && !win::wait_for_exit(pid, Duration::from_secs(90)) {
        log("Trailmix didn't quit in time; installing anyway");
    }
    // Its engine (Python) and ffmpeg end with it; anything still running from the folder would keep files locked.
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        let left = win::processes_in(&folder);
        if left.is_empty() {
            break;
        }
        if Instant::now() >= deadline {
            log(&format!("ending {} program(s) still running from the install", left.len()));
            for pid in left {
                win::terminate(pid);
            }
            thread::sleep(Duration::from_secs(1));
            break;
        }
        thread::sleep(Duration::from_millis(250));
    }

    // NSIS: /S silent, /UPDATE keeps your shortcuts as they are; /D= (last, unquoted) installs over this folder.
    let status = Command::new(&installer)
        .arg("/S")
        .arg("/UPDATE")
        .raw_arg(format!("/D={}", folder.display()))
        .creation_flags(CREATE_NO_WINDOW)
        .status();
    let data = server::data_dir();
    let mut saved = Saved::load(&data);
    let outcome = match status {
        Ok(s) if s.success() => Ok(()),
        Ok(s) => Err(format!("The installer for {version} stopped with code {}", s.code().unwrap_or(-1))),
        Err(e) => Err(format!("Couldn't run the installer for {version} ({e})")),
    };
    match &outcome {
        Ok(()) => {
            if going_back {
                saved.skip_version = Some(from.to_string()); // don't offer the one you left
                saved.previous_version = None;
            } else {
                saved.previous_version = Some(from.to_string());
            }
            saved.last_error = None;
        }
        Err(why) => saved.last_error = Some(why.clone()),
    }
    saved.save(&data);
    let _ = fs::remove_file(&installer);
    match &outcome {
        Ok(()) => log(&format!("installed {version}")),
        Err(why) => log(&format!("couldn't install {version}: {why}")),
    }

    if std::env::var_os("TRAILMIX_UPDATE_NO_RELAUNCH").is_none() {
        let exe = folder.join("Trailmix.exe");
        let opened = Command::new(&exe)
            .current_dir(&folder)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
            .spawn();
        if let Err(e) = opened {
            log(&format!("couldn't open {} ({e})", exe.display()));
        }
    }
    if outcome.is_ok() {
        0
    } else {
        1
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rel(version: &str, prerelease: bool, files: &[&str]) -> Value {
        let assets: Vec<Value> = files
            .iter()
            .map(|f| json!({"name": f, "browser_download_url": format!("http://127.0.0.1:1/{f}")}))
            .collect();
        json!({"tag_name": format!("v{version}"), "prerelease": prerelease, "draft": false, "html_url": "http://x/", "assets": assets})
    }

    fn windows(version: &str, prerelease: bool) -> Value {
        let name = installer_name(version);
        let checksum = format!("{name}.sha256");
        rel(version, prerelease, &[name.as_str(), checksum.as_str()])
    }

    #[test]
    fn versions_compare_by_their_numbers() {
        assert!(is_newer("0.20.0", "0.19.9"));
        assert!(is_newer("0.19.0-build.7", "0.19.0"));
        assert!(is_newer("0.19.0-build.7", "0.19.0-build.6"));
        assert!(is_newer("0.19.1", "0.19.0-build.900"));
        assert!(!is_newer("0.19.0", "0.19.0"));
        assert!(!is_newer("0.19.0", "0.19.0.0"));
        assert!(is_newer("0.19.0-build.1", "0.0.1"));
    }

    #[test]
    fn a_release_needs_this_computers_installer_and_its_checksum() {
        let r = release(&windows("0.20.0", false)).unwrap();
        assert_eq!(r.version, "0.20.0");
        assert_eq!(r.name, installer_name("0.20.0"));
        assert!(r.checksum.ends_with(".sha256"));
        let name = installer_name("0.20.0");
        assert!(release(&rel("0.20.0", false, &[name.as_str()])).is_none(), "no checksum: skipped");
        assert!(release(&rel("0.20.0", false, &["Trailmix-0.20.0-macos-arm64.dmg", "Trailmix-0.20.0-macos-arm64.dmg.sha256"])).is_none());
    }

    #[test]
    fn the_newest_with_an_installer_is_offered() {
        let list = json!([
            rel("999.0.0", false, &["Trailmix-999.0.0-macos-arm64.dmg"]),
            windows("0.21.0", true),
            windows("0.20.0", false),
            windows("0.19.0", false),
        ]);
        assert_eq!(newest(&list, false).unwrap().version, "0.20.0");
        assert_eq!(newest(&list, true).unwrap().version, "0.21.0");
        assert_eq!(newest(&windows("0.20.0", false), false).unwrap().version, "0.20.0", "one release on its own");
        let mut draft = windows("0.22.0", false);
        draft["draft"] = json!(true);
        assert_eq!(newest(&json!([draft]), true), None);
    }

    #[test]
    fn going_back_prefers_the_version_this_replaced() {
        let list = vec![windows("0.21.0", false), windows("0.20.0", true), windows("0.19.0", false), windows("0.18.0", false)];
        assert_eq!(previous(&list, "0.21.0", None, false).unwrap().version, "0.19.0");
        assert_eq!(previous(&list, "0.21.0", None, true).unwrap().version, "0.20.0");
        assert_eq!(previous(&list, "0.21.0", Some("0.18.0"), false).unwrap().version, "0.18.0");
        assert_eq!(previous(&list, "0.21.0", Some("0.17.0"), false).unwrap().version, "0.19.0");
        assert_eq!(previous(&list, "0.18.0", None, true), None);
    }

    #[test]
    fn checksums_are_read_from_sha256_files() {
        let sum = "a".repeat(64);
        assert_eq!(parse_checksum(&format!("{}  Trailmix-1.0.0-windows-x64.exe\n", sum.to_uppercase())), Some(sum));
        assert_eq!(parse_checksum("nope"), None);
        let mut hasher = Sha256::new();
        hasher.update(b"abc");
        assert_eq!(hex(&hasher.finalize()), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    }
}
