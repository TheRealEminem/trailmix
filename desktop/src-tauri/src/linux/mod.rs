//! What the Linux app needs of the system (win.rs's counterpart): the user's folders, processes, the session
//! (X11 or Wayland), running things on GTK's main thread, and what this recorder can do here. The cards
//! (card.rs) and open at login (login.rs) are here too, standing in for the Windows modules of the same names.

pub mod keys;
pub mod portal;
pub mod scroll;
pub mod selftest;

use std::path::{Path, PathBuf};
use std::sync::{mpsc, OnceLock};
use std::time::Duration;

use crate::audio::pulse;

/// $XDG_<name> (XDG_DATA_HOME, XDG_CONFIG_HOME), else that folder under home.
pub fn xdg_dir(var: &str, default: &str) -> PathBuf {
    match std::env::var_os(var).map(PathBuf::from).filter(|p| p.is_absolute()) {
        Some(dir) => dir,
        None => home().join(default),
    }
}

pub fn home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/tmp"))
}

pub fn hostname() -> String {
    std::fs::read_to_string("/proc/sys/kernel/hostname").map(|h| h.trim().to_string()).unwrap_or_default()
}

/// Wayland, X11, or neither (a terminal, a test without a display).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Session {
    X11,
    Wayland,
    None,
}

pub fn session() -> Session {
    let kind = std::env::var("XDG_SESSION_TYPE").unwrap_or_default().to_lowercase();
    let set = |name: &str| std::env::var_os(name).map(|v| !v.is_empty()).unwrap_or(false);
    if kind == "wayland" || (kind != "x11" && set("WAYLAND_DISPLAY")) {
        Session::Wayland
    } else if set("DISPLAY") {
        Session::X11
    } else {
        Session::None
    }
}

/// How WebKitGTK draws the window here (`before_window` decides): "the GPU", or "the CPU (<why>)".
static DRAWING: OnceLock<String> = OnceLock::new();

/// Before GTK starts: how WebKitGTK draws the page. Its DMA-BUF renderer leaves the window blank on NVIDIA's own
/// driver and in virtual machines, so it's off there. But off isn't a slower GPU path: since WebKitGTK 2.44 it's
/// the only way GPU frames reach the window, so without it the page is painted on the CPU, every frame of a
/// scroll included (issue #14: a core or more while scrolling, on a computer with a working AMD GPU). So it's off
/// only where it's known to fail, and WEBKIT_DISABLE_DMABUF_RENDERER (=1 or =0) set by you decides instead.
pub fn before_window() {
    let set_off = |name: &str| std::env::var(name).map(|v| v != "0");
    let drawing = if let Ok(true) = set_off("WEBKIT_DISABLE_COMPOSITING_MODE") {
        "the CPU (WEBKIT_DISABLE_COMPOSITING_MODE is set)".to_string()
    } else if let Ok(off) = set_off("WEBKIT_DISABLE_DMABUF_RENDERER") {
        if off { "the CPU (WEBKIT_DISABLE_DMABUF_RENDERER is set)".to_string() } else { "the GPU".to_string() }
    } else if let Some(why) = dmabuf_fails() {
        std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
        format!("the CPU ({why})")
    } else {
        "the GPU".to_string()
    };
    let _ = DRAWING.set(drawing);
}

/// "the GPU", or "the CPU (<why>)": for the page (window.rs) and the self-test.
pub fn drawing() -> &'static str {
    DRAWING.get().map(String::as_str).unwrap_or("the GPU")
}

pub fn draws_on_cpu() -> bool {
    drawing().starts_with("the CPU")
}

/// Why WebKitGTK's DMA-BUF renderer won't work here, if it won't: where it left the window blank (a virtual
/// machine, NVIDIA's own driver), or where there's no GPU device for it to draw with (a container, no driver).
fn dmabuf_fails() -> Option<&'static str> {
    if virtual_machine() {
        return Some("a virtual machine");
    }
    if Path::new("/sys/module/nvidia").exists() || Path::new("/proc/driver/nvidia").exists() {
        return Some("NVIDIA's driver");
    }
    let render_node = std::fs::read_dir("/dev/dri")
        .map(|dir| dir.flatten().any(|e| e.file_name().to_string_lossy().starts_with("renderD")))
        .unwrap_or(false);
    if !render_node {
        return Some("no GPU device");
    }
    None
}

/// systemd's answer where it's there; otherwise the CPU's hypervisor flag (x86) and the firmware's maker.
fn virtual_machine() -> bool {
    if let Ok(status) = std::process::Command::new("systemd-detect-virt")
        .args(["--vm", "--quiet"])
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
    {
        return status.success();
    }
    let cpu = std::fs::read_to_string("/proc/cpuinfo").unwrap_or_default();
    if cpu.lines().any(|l| l.starts_with("flags") && l.split_whitespace().any(|f| f == "hypervisor")) {
        return true;
    }
    let firmware = ["sys_vendor", "product_name", "board_vendor"]
        .iter()
        .filter_map(|f| std::fs::read_to_string(format!("/sys/class/dmi/id/{f}")).ok())
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase();
    ["qemu", "kvm", "vmware", "virtualbox", "innotek", "xen", "parallels", "bochs", "bhyve", "hyper-v", "virtual machine"]
        .iter()
        .any(|v| firmware.contains(v))
        || Path::new("/sys/hypervisor/type").exists()
}

/// What this recorder can do on this computer, for the web app (it hides the rest). Only what really works
/// here: other apps' audio needs a sound server, the shortcuts need X11 or a desktop with the GlobalShortcuts
/// portal (and are known once the app has started them), the call reminder needs the sound server's list of
/// who's recording.
pub fn capabilities() -> Vec<&'static str> {
    let mut caps = Vec::new();
    let sound = pulse::available();
    if sound {
        caps.extend(["system_audio", "per_app_audio"]);
    }
    if keys::available() {
        caps.push("hotkeys");
    }
    if sound && session() != Session::None {
        caps.push("call_reminder");
    }
    if sound {
        caps.push("sound_check");
    }
    caps
}

// MARK: GTK's main thread

/// Runs `f` on GTK's main thread (where every window is made and changed), from any thread.
pub fn on_main(f: impl FnOnce() + Send + 'static) {
    gtk::glib::MainContext::default().invoke(f);
}

/// Runs `f` on GTK's main thread and waits for its answer (None if the main thread doesn't get to it).
pub fn on_main_wait<R: Send + 'static>(f: impl FnOnce() -> R + Send + 'static) -> Option<R> {
    let context = gtk::glib::MainContext::default();
    if context.is_owner() {
        return Some(f());
    }
    let (tx, rx) = mpsc::channel();
    context.invoke(move || {
        let _ = tx.send(f());
    });
    rx.recv_timeout(Duration::from_secs(10)).ok()
}

// MARK: Processes

/// Keeps the engine from outliving the app: on Windows a job object does it; here the engine itself watches
/// for its parent going (TRAILMIX_PARENT_PID, in main.py), so there's nothing to hold.
pub struct Job;

impl Job {
    pub fn new() -> Option<Job> {
        Some(Job)
    }

    pub fn assign(&self, _child: &std::process::Child) -> bool {
        true
    }
}

/// Asks the engine (and its process group) to stop: SIGTERM, which uvicorn takes as "finish up and quit".
pub fn ctrl_break(pid: u32) -> bool {
    unsafe { libc::kill(-(pid as i32), libc::SIGTERM) == 0 || libc::kill(pid as i32, libc::SIGTERM) == 0 }
}

/// Ends a process group at once (the engine's, when it didn't stop when asked).
pub fn kill_group(pid: u32) {
    unsafe {
        libc::kill(-(pid as i32), libc::SIGKILL);
    }
}
