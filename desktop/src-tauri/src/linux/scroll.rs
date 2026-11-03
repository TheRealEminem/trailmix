//! `--scroll-test`: how much CPU scrolling the real window takes here (issue #14: scrolling took Trailmix from 2% to
//! 10-12% of a 12-thread computer). The page scrolls its own content, a flick's speed each frame, and the processes'
//! CPU time is read from /proc before and after: the page's (WebKitWebProcess), the window's (this program) and the
//! X server's, which draws what the window hands it. Each look is measured idle first, then scrolling.
//!
//!   Trailmix --scroll-test http://127.0.0.1:8765/#settings [seconds] [--looks=shipped,glass,flat,flat-no-art]
//!                          [--before=<script run in the page first, such as clicking a tab>]
//!
//! The looks: shipped (as the app is), glass (frosted glass on), flat (glass off, as on the CPU), and flat-no-art
//! (also without the contour art behind the page), to see what each costs.

use std::collections::HashMap;
use std::thread;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager};

use crate::app::AppState;
use crate::model::Model;
use crate::prefs::{tidy_url, Prefs};
use crate::selftest::wait_until;
use crate::window;

/// CPU time (in clock ticks) so far, by who: the page, the window, other WebKit helpers, the X server.
#[derive(Default, Clone, Copy)]
struct Ticks {
    page: u64,
    window: u64,
    helpers: u64,
    x: u64,
}

/// (pid, parent, name, ticks) of every process.
fn processes() -> Vec<(u32, u32, String, u64)> {
    let mut all = Vec::new();
    let Ok(dir) = std::fs::read_dir("/proc") else { return all };
    for entry in dir.flatten() {
        let Ok(pid) = entry.file_name().to_string_lossy().parse::<u32>() else { continue };
        let Ok(stat) = std::fs::read_to_string(format!("/proc/{pid}/stat")) else { continue };
        // pid (name) state ppid ... utime(14) stime(15): the name may hold spaces and brackets, so split after it.
        let (Some(open), Some(close)) = (stat.find('('), stat.rfind(')')) else { continue };
        let name = stat[open + 1..close].to_string();
        let fields: Vec<&str> = stat[close + 1..].split_whitespace().collect();
        let field = |n: usize| fields.get(n - 3).and_then(|f| f.parse::<u64>().ok()).unwrap_or(0);
        all.push((pid, field(4) as u32, name, field(14) + field(15)));
    }
    all
}

fn ticks() -> Ticks {
    let me = std::process::id();
    let all = processes();
    let parents: HashMap<u32, u32> = all.iter().map(|(pid, parent, _, _)| (*pid, *parent)).collect();
    let ours = |mut pid: u32| {
        for _ in 0..16 {
            if pid == me {
                return true;
            }
            match parents.get(&pid) {
                Some(&parent) if parent > 1 => pid = parent,
                _ => return false,
            }
        }
        false
    };
    let mut t = Ticks::default();
    for (pid, _, name, ticks) in &all {
        if *pid == me {
            t.window += ticks;
        } else if name == "Xvfb" || name == "Xorg" || name == "Xwayland" {
            t.x += ticks;
        } else if ours(*pid) {
            if name.starts_with("WebKitWebProces") {
                t.page += ticks;
            } else {
                t.helpers += ticks;
            }
        }
    }
    t
}

/// Percent of one core each used between two readings.
fn percent(before: Ticks, after: Ticks, seconds: f64) -> [f64; 4] {
    let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) }.max(1) as f64;
    let p = |a: u64, b: u64| (b.saturating_sub(a)) as f64 / hz / seconds * 100.0;
    [
        p(before.page, after.page),
        p(before.window, after.window),
        p(before.helpers, after.helpers),
        p(before.x, after.x),
    ]
}

/// Sets a look: whether glass is drawn (the page's data-drawing), and whether the art behind the page is there.
fn look_script(look: &str, shipped: &str) -> String {
    let drawing = match look {
        "glass" => "gpu",
        "flat" | "flat-no-art" => "cpu",
        _ => shipped,
    };
    let no_art = look == "flat-no-art";
    format!(
        "(() => {{ document.documentElement.dataset.drawing = '{drawing}'; let s = document.getElementById('scroll-test-look'); \
         if (!s) {{ s = document.createElement('style'); s.id = 'scroll-test-look'; document.head.append(s); }} \
         s.textContent = {css:?}; return true; }})()",
        css = if no_art { ".pointer-events-none.fixed.inset-0.z-0 { display: none !important; }" } else { "" }
    )
}

const SCROLL: &str = "(() => { const m = document.querySelector('main'); const s = (window.__scroll = { frames: 0, run: true }); \
  let dir = 1; const step = () => { if (!s.run) return; const max = m.scrollHeight - m.clientHeight; let y = m.scrollTop + dir * 40; \
  if (y >= max) { y = max; dir = -1; } else if (y <= 0) { y = 0; dir = 1; } m.scrollTop = y; s.frames += 1; requestAnimationFrame(step); }; \
  requestAnimationFrame(step); return true; })()";

fn measure(win: &tauri::WebviewWindow, seconds: f64, scroll: bool) -> ([f64; 4], f64) {
    if scroll {
        let _ = window::evaluate(win, SCROLL);
    }
    let started = Instant::now();
    let before = ticks();
    thread::sleep(Duration::from_secs_f64(seconds));
    let after = ticks();
    let took = started.elapsed().as_secs_f64();
    let mut fps = 0.0;
    if scroll {
        let frames = window::evaluate(win, "(window.__scroll.run = false, window.__scroll.frames)");
        fps = frames.parse::<f64>().unwrap_or(0.0) / took;
        let _ = window::evaluate(win, "(document.querySelector('main').scrollTop = 0, true)");
    }
    (percent(before, after, took), fps)
}

fn show(name: &str, [page, window, helpers, x]: [f64; 4], fps: Option<f64>) -> String {
    let all = page + window + helpers;
    let frames = fps.map(|f| format!(", {f:.0} frames a second")).unwrap_or_default();
    format!("{name}: Trailmix {all:.0}% of a core (page {page:.0}%, window {window:.0}%, other {helpers:.0}%), X server {x:.0}%{frames}")
}

/// --scroll-test <url> [seconds] [--looks=...]
pub fn scroll_test(app: AppHandle, args: Vec<String>) {
    let rest: Vec<&String> = args.iter().skip_while(|a| *a != "--scroll-test").skip(1).filter(|a| !a.starts_with("--")).collect();
    let Some(url) = rest.first().map(|u| tidy_url(u)) else {
        println!("usage: --scroll-test <url> [seconds] [--looks=shipped,glass,flat,flat-no-art]");
        std::process::exit(2);
    };
    let seconds = rest.get(1).and_then(|s| s.parse::<f64>().ok()).unwrap_or(8.0);
    let looks: Vec<String> = args
        .iter()
        .find_map(|a| a.strip_prefix("--looks="))
        .unwrap_or("shipped")
        .split(',')
        .map(str::to_string)
        .collect();
    let before = args.iter().find_map(|a| a.strip_prefix("--before=")).map(str::to_string);
    let model = Model::new(Prefs::default(), None, app.package_info().version.to_string(), Some(url.clone()));
    app.manage(AppState::new(model, None));
    if let Err(e) = window::create(&app) {
        println!("couldn't make the window: {e}");
        std::process::exit(1);
    }
    thread::spawn(move || {
        window::show(&app, &url);
        let Some(win) = app.get_webview_window(window::LABEL) else {
            println!("no window");
            std::process::exit(1);
        };
        let ready = wait_until(60.0, || window::evaluate(&win, "!!document.querySelector('main') && document.querySelector('main').scrollHeight > 0") == "true");
        if !ready {
            println!("the page didn't show");
            std::process::exit(1);
        }
        thread::sleep(Duration::from_secs(3));
        if let Some(script) = &before {
            let _ = window::evaluate(&win, &format!("({script}, true)"));
        }
        thread::sleep(Duration::from_secs(2));
        let shipped = serde_json::from_str::<String>(&window::evaluate(&win, "document.documentElement.dataset.drawing ?? 'none'")).unwrap_or_default();
        let range = window::evaluate(&win, "(() => { const m = document.querySelector('main'); return [m.clientHeight, m.scrollHeight]; })()");
        println!("drawn on {} (the page has data-drawing={shipped}); main is [shown, all] {range} px tall", super::drawing());
        for look in &looks {
            let _ = window::evaluate(&win, &look_script(look, &shipped));
            thread::sleep(Duration::from_millis(1500));
            let (idle, _) = measure(&win, seconds / 2.0, false);
            let (busy, fps) = measure(&win, seconds, true);
            println!("  {}", show(&format!("{look}, idle"), idle, None));
            println!("  {}", show(&format!("{look}, scrolling"), busy, Some(fps)));
        }
        std::process::exit(0);
    });
}
