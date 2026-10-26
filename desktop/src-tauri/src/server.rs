//! The Trailmix server that ships inside the app (ServerProcess.swift on the Mac): started with the app,
//! stopped when it quits, and started again (a few times) if it dies.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

use crate::win::{ctrl_break, Job};

const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// Where things live inside the installed app (next to Trailmix.exe), and where its data goes.
#[derive(Clone, Debug)]
pub struct Bundled {
    pub resources: PathBuf,
    pub python: PathBuf,
    pub backend: PathBuf,
    pub ui: PathBuf,
    pub bin: PathBuf,
}

impl Bundled {
    /// The bundled server, if this build has one (a `cargo run` from the source tree doesn't).
    pub fn locate(resources: &Path) -> Option<Bundled> {
        let python = resources.join("python").join("python.exe");
        if !python.is_file() {
            return None;
        }
        Some(Bundled {
            resources: resources.to_path_buf(),
            python,
            backend: resources.join("backend"),
            ui: resources.join("ui"),
            bin: resources.join("bin"),
        })
    }
}

/// Meetings, settings and recordings. Survives updating or uninstalling the app (the installer never
/// removes it). TRAILMIX_DATA_DIR overrides it (tests, or keeping your data elsewhere).
pub fn data_dir() -> PathBuf {
    if let Some(dir) = std::env::var_os("TRAILMIX_DATA_DIR") {
        return PathBuf::from(dir);
    }
    let local = std::env::var_os("LOCALAPPDATA").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."));
    local.join("Trailmix").join("data")
}

pub fn log_file() -> PathBuf {
    data_dir().join("logs").join("server.log")
}

struct Inner {
    child: Option<Child>,
    stopping: bool,
    crashes: Vec<Instant>,
    failure: Option<String>,
}

pub struct Server {
    bundled: Bundled,
    inner: Mutex<Inner>,
    job: Option<Job>,
}

impl Server {
    pub fn new(bundled: Bundled) -> Arc<Server> {
        Arc::new(Server {
            bundled,
            inner: Mutex::new(Inner { child: None, stopping: false, crashes: Vec::new(), failure: None }),
            job: Job::new(),
        })
    }

    pub fn bundled(&self) -> &Bundled {
        &self.bundled
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Why the server can't run, in words for the menu. None while all is well.
    pub fn failure(&self) -> Option<String> {
        self.lock().failure.clone()
    }

    pub fn is_running(&self) -> bool {
        let mut inner = self.lock();
        match inner.child.as_mut() {
            Some(child) => matches!(child.try_wait(), Ok(None)),
            None => false,
        }
    }

    pub fn start(self: &Arc<Self>, port: u16) -> Result<(), String> {
        let mut inner = self.lock();
        if let Some(child) = inner.child.as_mut() {
            if matches!(child.try_wait(), Ok(None)) {
                return Ok(());
            }
        }
        inner.stopping = false;
        inner.failure = None;

        let log_path = log_file();
        if let Some(logs) = log_path.parent() {
            std::fs::create_dir_all(logs).map_err(|e| format!("Couldn't make {}: {e}", logs.display()))?;
        }
        let mut log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .map_err(|e| format!("Couldn't open {}: {e}", log_path.display()))?;
        let _ = writeln!(log, "\n— Trailmix server starting {} —", chrono_now());
        let err_log: File = log.try_clone().map_err(|e| e.to_string())?;

        let b = &self.bundled;
        let system_root = std::env::var("SystemRoot").unwrap_or_else(|_| "C:\\Windows".into());
        let mut command = Command::new(&b.python);
        command
            .args(["-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", &port.to_string()])
            .current_dir(&b.backend)
            .env_clear();
        for (key, value) in std::env::vars_os() {
            if !key.to_string_lossy().to_uppercase().starts_with("PYTHON") {
                command.env(key, value);
            }
        }
        command
            .env("PATH", format!("{};{}\\System32;{}", b.bin.display(), system_root, system_root))
            .env("PYTHONDONTWRITEBYTECODE", "1") // the app folder is never written to while it runs
            .env("PYTHONNOUSERSITE", "1")
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONUTF8", "1")
            .env("TRAILMIX_DATA_DIR", data_dir())
            .env("TRAILMIX_UI_DIR", &b.ui)
            .env("TRAILMIX_PARENT_PID", std::process::id().to_string())
            .stdin(Stdio::null())
            .stdout(Stdio::from(log))
            .stderr(Stdio::from(err_log))
            .creation_flags(CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW);
        let child = command.spawn().map_err(|e| format!("Couldn't start the Trailmix engine ({e})"))?;
        if let Some(job) = &self.job {
            job.assign(&child);
        }
        inner.child = Some(child);
        drop(inner);

        let server = self.clone();
        thread::Builder::new()
            .name("trailmix-server-watch".into())
            .spawn(move || server.watch(port))
            .map_err(|e| e.to_string())?;
        Ok(())
    }

    /// Waits for the server to end, and starts it again unless it was stopped on purpose or keeps dying.
    fn watch(self: Arc<Self>, port: u16) {
        loop {
            thread::sleep(Duration::from_millis(250));
            let mut inner = self.lock();
            let Some(child) = inner.child.as_mut() else { return };
            match child.try_wait() {
                Ok(None) => continue,
                Ok(Some(_)) | Err(_) => {}
            }
            inner.child = None;
            if inner.stopping {
                return;
            }
            let now = Instant::now();
            inner.crashes.retain(|t| now.duration_since(*t) < Duration::from_secs(120));
            inner.crashes.push(now);
            if inner.crashes.len() >= 3 {
                inner.failure = Some(explain(port));
                return;
            }
            drop(inner);
            // start() spawns a new watcher; this one is done.
            let _ = self.start(port);
            return;
        }
    }

    /// Stops the server and waits (briefly) for it to finish what it's doing.
    pub fn stop(&self) {
        let mut inner = self.lock();
        inner.stopping = true;
        let Some(mut child) = inner.child.take() else { return };
        drop(inner);
        if matches!(child.try_wait(), Ok(Some(_))) {
            return;
        }
        let polite = ctrl_break(child.id());
        let deadline = Instant::now() + Duration::from_secs(if polite { 5 } else { 0 });
        while Instant::now() < deadline {
            if matches!(child.try_wait(), Ok(Some(_))) {
                return;
            }
            thread::sleep(Duration::from_millis(50));
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

/// The last useful line of the server log, in plain words where we recognise it.
fn explain(port: u16) -> String {
    let path = log_file();
    let text = std::fs::read_to_string(&path).unwrap_or_default();
    let tail: Vec<&str> = text.lines().rev().take(40).collect();
    if tail.iter().any(|l| l.contains("address already in use") || l.contains("10048") || l.contains("only one usage of each socket address")) {
        return format!("Another app is using port {port}, so Trailmix can't start. Quit it, or change the port.");
    }
    let last = tail.iter().find(|l| !l.trim().is_empty() && !l.starts_with('—')).copied().unwrap_or("no details");
    let last: String = last.chars().take(120).collect();
    format!("Trailmix's server stopped unexpectedly ({last}). Log: {}", path.display())
}

fn chrono_now() -> String {
    let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    format!("{secs} (unix)")
}
