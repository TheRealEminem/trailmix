//! The app around the model: the tray icon and its menu (MenuView.swift's Record/Stop, Mark, meeting-audio
//! source, the shortcuts and call reminders, Open and Quit), the global shortcuts and the call reminder,
//! starting the bundled server, and opening the window.

use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use tauri::image::Image;
use tauri::menu::{CheckMenuItem, IsMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::{MouseButton, MouseButtonState, TrayIcon, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, Wry};

use crate::audio::apps::{self, AudioApp};
use crate::card::{self, Answer, Card};
use crate::hotkeys::{self, Shortcut, MARK, RECORD};
use crate::model::{Connection, Model, Phase, Snapshot};
use crate::prefs::{MeetingSource, Prefs};
use crate::reminder::Reminder;
use crate::server::{self, Bundled, Server};
use crate::update::Updater;
use crate::window;

pub struct AppState {
    pub model: Arc<Model>,
    pub server: Option<Arc<Server>>,
    tray: Mutex<Option<Tray>>,
    reminder: Mutex<Option<Arc<Reminder>>>,
}

impl AppState {
    pub fn new(model: Arc<Model>, server: Option<Arc<Server>>) -> AppState {
        AppState { model, server, tray: Mutex::new(None), reminder: Mutex::new(None) }
    }
}

struct Tray {
    icon: TrayIcon,
    status: MenuItem<Wry>,
    record: MenuItem<Wry>,
    mark: MenuItem<Wry>,
    reminders: CheckMenuItem<Wry>,
    apps: Vec<AudioApp>,
    source: String,
}

/// The menu items that change while it's open.
struct Items {
    menu: Menu<Wry>,
    status: MenuItem<Wry>,
    record: MenuItem<Wry>,
    mark: MenuItem<Wry>,
    reminders: CheckMenuItem<Wry>,
}

/// Sets everything up for a normal run: the model and server, the updater, the hidden window, the tray,
/// the shortcuts and the call reminder; then opens the window (TRAILMIX_NO_OPEN set: says so instead, for
/// tests). Opened at login, it only starts the engine: the tray icon is there, and the window opens when
/// you click it.
pub fn launch(app: &AppHandle, at_login: bool) -> tauri::Result<()> {
    let data = server::data_dir();
    let _ = std::fs::create_dir_all(&data);
    let resources = app.path().resource_dir().unwrap_or_default();
    let server = Bundled::locate(&resources).map(Server::new);
    let prefs = Prefs::load(&data);
    let version = app.package_info().version.to_string();
    let model = Model::new(prefs, server.clone(), version.clone(), None);
    let exe = std::env::current_exe().unwrap_or_default();
    let updater = (server.is_some() && Updater::enabled_for(&exe)).then(|| Updater::new(version, data.clone(), exe));
    app.manage(AppState::new(model.clone(), server));
    window::create(app)?;
    if let Some(updater) = &updater {
        let weak = Arc::downgrade(&model);
        updater.set_busy(move || weak.upgrade().map(|m| m.recording_anywhere()).unwrap_or(false));
        let handle = app.clone();
        updater.set_quit(move || quit(&handle));
        model.set_updater(updater.clone());
    }
    start_hotkeys(app, &model);
    build_tray(app, &model)?;
    let handle = app.clone();
    model.observe(move || refresh_tray(&handle));
    if let Some(updater) = &updater {
        // The tray offers the update once there is one (and stops offering it once it's under way).
        let handle = app.clone();
        let shown: Mutex<Option<String>> = Mutex::new(None);
        updater.observe(move || {
            let Some(state) = handle.try_state::<AppState>() else { return };
            let now = state.model.updater().and_then(|u| u.available());
            let mut last = shown.lock().unwrap_or_else(|e| e.into_inner());
            if *last != now {
                *last = now;
                drop(last);
                let (handle, model) = (handle.clone(), state.model.clone());
                thread::spawn(move || rebuild_menu(&handle, &model, None));
            }
        });
        updater.begin();
    }
    model.begin();
    let reminder = Reminder::standard(model.clone());
    reminder.begin();
    *app.state::<AppState>().reminder.lock().unwrap_or_else(|e| e.into_inner()) = Some(reminder);
    start_ticker(app.clone());
    let handle = app.clone();
    if at_login {
        let model = model.clone();
        thread::spawn(move || {
            if let Err(problem) = model.start_server_if_needed() {
                eprintln!("Couldn't start Trailmix: {problem}");
            }
        });
    } else {
        thread::spawn(move || open_trailmix(&handle, None));
    }
    Ok(())
}

/// Opens Trailmix in its window, optionally at a page ("settings") or a meeting ("meeting-12"). Starts
/// the server first if it isn't running, showing the loading page meanwhile.
pub fn open_trailmix(app: &AppHandle, page: Option<&str>) {
    let state = app.state::<AppState>();
    let model = state.model.clone();
    let base = model.server_url();
    let target = match page {
        Some(page) => format!("{base}/#{page}"),
        None => base,
    };
    // Tests: start the server as usual, but say what would open instead of opening a window.
    let no_window = std::env::var_os("TRAILMIX_NO_OPEN").is_some();
    if model.snapshot().connection != Connection::Online {
        if !no_window {
            window::show_loading(app);
            window::loading_status(app, "The engine is loading. The first start takes a little longer.");
        }
        if let Err(problem) = model.start_server_if_needed() {
            if no_window {
                eprintln!("Couldn't start Trailmix: {problem}");
            } else {
                window::loading_failed(app, &problem);
            }
            return;
        }
    }
    if no_window {
        eprintln!("Would open {target}");
        return;
    }
    window::show(app, &target);
}

/// Stops whatever is recording, stops the server, and exits.
pub fn quit(app: &AppHandle) {
    let app = app.clone();
    thread::spawn(move || {
        let state = app.state::<AppState>();
        if let Some(reminder) = state.reminder.lock().unwrap_or_else(|e| e.into_inner()).as_ref() {
            reminder.stop();
        }
        state.model.quit_recording();
        if let Some(server) = &state.server {
            server.stop();
        }
        app.exit(0);
    });
}

// MARK: The shortcuts

/// The shortcuts work from any app; a press while the Trailmix window is in front shows no note (the
/// window itself shows what happened).
fn start_hotkeys(app: &AppHandle, model: &Arc<Model>) {
    let handle = app.clone();
    hotkeys::start(move |id| {
        let Some(state) = handle.try_state::<AppState>() else { return };
        let notes = !window_in_front(&handle);
        hotkeys::act(&state.model, id, notes);
    });
    hotkeys::apply(model);
}

fn window_in_front(app: &AppHandle) -> bool {
    match app.get_webview_window(window::LABEL) {
        Some(window) => window.is_visible().unwrap_or(false) && window.is_focused().unwrap_or(false),
        None => false,
    }
}

/// "Type the new shortcut": a card that takes the keyboard (the shortcuts are off meanwhile, so pressing
/// the current one doesn't start a recording, as on the Mac).
fn change_shortcut(app: &AppHandle, which: i32) {
    let model = app.state::<AppState>().model.clone();
    hotkeys::register(&[]);
    let what = if which == RECORD { "Start / stop" } else { "Mark moment" };
    let card = Card {
        title: format!("Type the new shortcut for {what}"),
        detail: "Hold Ctrl, Alt or Win (and Shift, if you like) and press a key, or press a function key on its own. Esc cancels."
            .into(),
        buttons: vec!["Cancel".into()],
        link: None,
        seconds: 30,
        keys: true,
        sound: false,
    };
    let app = app.clone();
    card::show(card, move |_, answer| match answer {
        Answer::Key(shortcut) => set_shortcut(&app, &model, which, shortcut),
        _ => {
            hotkeys::apply(&model);
        }
    });
}

/// Takes the new shortcut, unless it's the other one's or another app has it: then the old one stays.
fn set_shortcut(app: &AppHandle, model: &Arc<Model>, which: i32, shortcut: Shortcut) {
    let before = model.prefs();
    let other = if which == RECORD { before.shortcut_mark } else { before.shortcut_record };
    if shortcut == other {
        hotkeys::apply(model);
        card::note(&format!("{} is already the other shortcut", shortcut.display()), "Pick a different one.", 5);
        return;
    }
    model.update_prefs(|prefs| {
        if which == RECORD {
            prefs.shortcut_record = shortcut;
        } else {
            prefs.shortcut_mark = shortcut;
        }
    });
    if hotkeys::apply(model).contains(&which) {
        model.update_prefs(|prefs| {
            prefs.shortcut_record = before.shortcut_record;
            prefs.shortcut_mark = before.shortcut_mark;
        });
        hotkeys::apply(model);
        card::note(&format!("{} is taken by another app", shortcut.display()), "Pick another one in the tray menu: Settings.", 6);
    }
    rebuild_menu(app, model, None);
}

fn use_default_shortcuts(app: &AppHandle, model: &Arc<Model>) {
    model.update_prefs(|prefs| {
        prefs.shortcut_record = Shortcut::RECORD_DEFAULT;
        prefs.shortcut_mark = Shortcut::MARK_DEFAULT;
    });
    let taken = hotkeys::apply(model);
    if !taken.is_empty() {
        card::note("A shortcut is taken by another app", "Pick another one in the tray menu: Settings.", 6);
    }
    rebuild_menu(app, model, None);
}

// MARK: The tray

fn format_elapsed(elapsed: Duration) -> String {
    let secs = elapsed.as_secs();
    if secs >= 3600 {
        format!("{}:{:02}:{:02}", secs / 3600, secs / 60 % 60, secs % 60)
    } else {
        format!("{}:{:02}", secs / 60, secs % 60)
    }
}

fn status_text(snapshot: &Snapshot, prefs: &Prefs) -> String {
    match snapshot.phase {
        Phase::Starting => "Starting the recording…".into(),
        Phase::Recording => format!(
            "Recording {} · {}{}",
            snapshot.elapsed.map(format_elapsed).unwrap_or_default(),
            snapshot.mic_name,
            if snapshot.capturing_meeting_audio { " + other apps" } else { "" }
        ),
        Phase::Stopping => "Stopping…".into(),
        Phase::Idle => {
            if let Some(other) = &snapshot.other {
                return format!("Recording in the Trailmix window {} · {}", snapshot.elapsed.map(format_elapsed).unwrap_or_default(), other.title);
            }
            if snapshot.starting_server {
                return "Starting Trailmix…".into();
            }
            if let Some(error) = &snapshot.error {
                return error.clone();
            }
            match snapshot.connection {
                Connection::Online => match snapshot.last_meeting_id {
                    Some(id) => format!("Ready · last recording saved as meeting {id}"),
                    None => format!("Ready · {} to record", prefs.shortcut_record.display()),
                },
                Connection::Offline => "Trailmix isn't running".into(),
                Connection::NeedsToken => "Trailmix needs an access token (Settings)".into(),
                Connection::Outdated => "Trailmix's server is out of date".into(),
                Connection::Unknown => "Connecting to Trailmix…".into(),
            }
        }
    }
}

/// The Record item: its text (with the shortcut on the right, as Windows menus show them) and whether it works now.
fn record_text(snapshot: &Snapshot, prefs: &Prefs) -> (String, bool) {
    let shortcut = prefs.shortcut_record.display();
    match snapshot.phase {
        Phase::Starting => ("Starting…".into(), false),
        Phase::Recording => (format!("Stop Recording\t{shortcut}"), true),
        Phase::Stopping => ("Stopping…".into(), false),
        Phase::Idle => {
            if snapshot.other.is_some() {
                (format!("Stop Recording\t{shortcut}"), true)
            } else {
                (format!("Record\t{shortcut}"), true)
            }
        }
    }
}

fn mark_text(snapshot: &Snapshot, prefs: &Prefs) -> (String, bool) {
    let live = snapshot.phase == Phase::Recording || (snapshot.phase == Phase::Idle && snapshot.other.is_some());
    (format!("Mark Moment\t{}", prefs.shortcut_mark.display()), live)
}

fn shortcut_label(what: &str, shortcut: Shortcut, id: i32) -> String {
    let taken = if hotkeys::taken().contains(&id) { " (taken by another app)" } else { "" };
    format!("{what} shortcut: {}{taken}…", shortcut.display())
}

fn make_menu(app: &AppHandle, model: &Arc<Model>, apps: &[AudioApp]) -> tauri::Result<Items> {
    let snapshot = model.snapshot();
    let prefs = model.prefs();
    let status = MenuItem::with_id(app, "status", status_text(&snapshot, &prefs), false, None::<&str>)?;
    let (text, enabled) = record_text(&snapshot, &prefs);
    let record = MenuItem::with_id(app, "record", text, enabled, None::<&str>)?;
    let (text, enabled) = mark_text(&snapshot, &prefs);
    let mark = MenuItem::with_id(app, "mark", text, enabled, None::<&str>)?;
    let open = MenuItem::with_id(app, "open", "Open Trailmix", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Trailmix", true, None::<&str>)?;

    let source = prefs.meeting_source();
    let all = CheckMenuItem::with_id(app, "source:all", "All apps", true, source == MeetingSource::AllApps, None::<&str>)?;
    let none = CheckMenuItem::with_id(app, "source:none", "Your mic only (in person)", true, source == MeetingSource::None, None::<&str>)?;
    let per_app = crate::audio::per_app_supported();
    let mut app_items: Vec<CheckMenuItem<Wry>> = Vec::new();
    for a in apps {
        let label = if a.playing { format!("{} · playing", a.name) } else { a.name.clone() };
        app_items.push(CheckMenuItem::with_id(
            app,
            format!("source:app:{}", a.exe),
            label,
            per_app,
            source == MeetingSource::App(a.exe.clone()),
            None::<&str>,
        )?);
    }
    let separator = PredefinedMenuItem::separator(app)?;
    let mut entries: Vec<&dyn IsMenuItem<Wry>> = vec![&all, &none];
    if !app_items.is_empty() {
        entries.push(&separator);
        for item in &app_items {
            entries.push(item);
        }
    }
    let sources = Submenu::with_id_and_items(app, "sources", "Meeting audio", true, &entries)?;

    // Settings (MenuView.swift's): the shortcuts and the call reminders.
    let record_shortcut = MenuItem::with_id(app, "shortcut:record", shortcut_label("Start / stop", prefs.shortcut_record, RECORD), true, None::<&str>)?;
    let mark_shortcut = MenuItem::with_id(app, "shortcut:mark", shortcut_label("Mark moment", prefs.shortcut_mark, MARK), true, None::<&str>)?;
    let defaults = prefs.shortcut_record == Shortcut::RECORD_DEFAULT && prefs.shortcut_mark == Shortcut::MARK_DEFAULT;
    let reset = MenuItem::with_id(app, "shortcut:defaults", "Use the Default Shortcuts", !defaults, None::<&str>)?;
    let settings_separator = PredefinedMenuItem::separator(app)?;
    let reminders = CheckMenuItem::with_id(app, "reminders", "Remind Me to Record Calls", true, prefs.call_reminders, None::<&str>)?;
    let settings_items: Vec<&dyn IsMenuItem<Wry>> = vec![&record_shortcut, &mark_shortcut, &reset, &settings_separator, &reminders];
    let settings = Submenu::with_id_and_items(app, "settings", "Settings", true, &settings_items)?;

    let sep1 = PredefinedMenuItem::separator(app)?;
    let sep2 = PredefinedMenuItem::separator(app)?;
    let update = match model.updater().and_then(|u| u.available()) {
        Some(version) => Some(MenuItem::with_id(app, "update", format!("Update to Trailmix {version}"), true, None::<&str>)?),
        None => None,
    };
    let mut items: Vec<&dyn IsMenuItem<Wry>> = vec![&status, &sep1, &record, &mark, &sources, &settings, &sep2];
    if let Some(update) = &update {
        items.push(update);
    }
    items.push(&open);
    items.push(&quit);
    let menu = Menu::with_items(app, &items)?;
    Ok(Items { menu, status, record, mark, reminders })
}

fn build_tray(app: &AppHandle, model: &Arc<Model>) -> tauri::Result<()> {
    let apps = apps::current();
    let items = make_menu(app, model, &apps)?;
    let icon = TrayIconBuilder::with_id("main")
        .icon(Image::from_bytes(include_bytes!("../icons/32x32.png"))?)
        .tooltip("Trailmix")
        .menu(&items.menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| on_menu(app, event.id().0.as_str()))
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                let app = tray.app_handle().clone();
                thread::spawn(move || open_trailmix(&app, None));
            }
        })
        .build(app)?;
    let state = app.state::<AppState>();
    let source = model.prefs().source;
    *state.tray.lock().unwrap_or_else(|e| e.into_inner()) =
        Some(Tray { icon, status: items.status, record: items.record, mark: items.mark, reminders: items.reminders, apps, source });
    Ok(())
}

fn on_menu(app: &AppHandle, id: &str) {
    let state = app.state::<AppState>();
    let model = state.model.clone();
    match id {
        "record" => {
            thread::spawn(move || model.toggle());
        }
        "mark" => {
            thread::spawn(move || model.mark_moment());
        }
        "open" => {
            let app = app.clone();
            thread::spawn(move || open_trailmix(&app, None));
        }
        "quit" => quit(app),
        "update" => {
            if let Some(updater) = model.updater() {
                updater.install();
            }
        }
        "reminders" => {
            model.update_prefs(|prefs| {
                prefs.call_reminders = !prefs.call_reminders;
                if prefs.call_reminders {
                    prefs.muted_call_apps.clear(); // as when turned on in Settings
                }
            });
        }
        "shortcut:record" => change_shortcut(app, RECORD),
        "shortcut:mark" => change_shortcut(app, MARK),
        "shortcut:defaults" => {
            let app = app.clone();
            thread::spawn(move || use_default_shortcuts(&app, &model));
        }
        other => {
            if let Some(source) = other.strip_prefix("source:") {
                model.set_source(MeetingSource::parse(source));
                rebuild_menu(app, &model, None);
            }
        }
    }
}

/// Updates the menu's texts for what the model is doing now. The menu items are changed with the tray lock
/// released: changing one waits for the main thread, which may itself be waiting for that lock.
fn refresh_tray(app: &AppHandle) {
    let Some(state) = app.try_state::<AppState>() else { return };
    let snapshot = state.model.snapshot();
    let prefs = state.model.prefs();
    let (icon, status, record, mark, reminders) = {
        let tray = state.tray.lock().unwrap_or_else(|e| e.into_inner());
        let Some(tray) = tray.as_ref() else { return };
        (tray.icon.clone(), tray.status.clone(), tray.record.clone(), tray.mark.clone(), tray.reminders.clone())
    };
    let _ = status.set_text(status_text(&snapshot, &prefs));
    let (text, enabled) = record_text(&snapshot, &prefs);
    let _ = record.set_text(text);
    let _ = record.set_enabled(enabled);
    let (text, enabled) = mark_text(&snapshot, &prefs);
    let _ = mark.set_text(text);
    let _ = mark.set_enabled(enabled);
    let _ = reminders.set_checked(prefs.call_reminders);
    let tooltip = match snapshot.phase {
        Phase::Recording => format!("Trailmix · recording {}", snapshot.elapsed.map(format_elapsed).unwrap_or_default()),
        _ => "Trailmix".to_string(),
    };
    let _ = icon.set_tooltip(Some(tooltip));
}

/// Makes the menu afresh: the apps to pick from changed, the chosen source did, or a shortcut. Built with
/// the tray lock released (see `refresh_tray`).
fn rebuild_menu(app: &AppHandle, model: &Arc<Model>, apps: Option<Vec<AudioApp>>) {
    let state = app.state::<AppState>();
    let (icon, apps) = {
        let tray = state.tray.lock().unwrap_or_else(|e| e.into_inner());
        let Some(current) = tray.as_ref() else { return };
        (current.icon.clone(), apps.unwrap_or_else(|| current.apps.clone()))
    };
    let Ok(items) = make_menu(app, model, &apps) else { return };
    if icon.set_menu(Some(items.menu)).is_ok() {
        let mut tray = state.tray.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(current) = tray.as_mut() {
            current.status = items.status;
            current.record = items.record;
            current.mark = items.mark;
            current.reminders = items.reminders;
            current.apps = apps;
            current.source = model.prefs().source;
        }
    }
}

/// Every second while recording, the elapsed time; every few seconds, the apps the menu offers.
fn start_ticker(app: AppHandle) {
    thread::Builder::new()
        .name("trailmix-tray".into())
        .spawn(move || {
            let mut rounds: u64 = 0;
            loop {
                thread::sleep(Duration::from_secs(1));
                rounds += 1;
                let Some(state) = app.try_state::<AppState>() else { continue };
                let snapshot = state.model.snapshot();
                if snapshot.phase == Phase::Recording || snapshot.other.is_some() {
                    refresh_tray(&app);
                }
                if rounds % 5 == 0 {
                    let apps = apps::current();
                    let changed = {
                        let tray = state.tray.lock().unwrap_or_else(|e| e.into_inner());
                        tray.as_ref().map(|t| t.apps != apps || t.source != state.model.prefs().source).unwrap_or(false)
                    };
                    if changed {
                        rebuild_menu(&app, &state.model, Some(apps));
                    }
                }
            }
        })
        .expect("thread");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn idle() -> Snapshot {
        Snapshot {
            phase: Phase::Idle,
            connection: Connection::Online,
            other: None,
            meeting_id: None,
            elapsed: None,
            mic_name: String::new(),
            capturing_meeting_audio: false,
            last_meeting_id: None,
            error: None,
            notice: None,
            starting_server: false,
            drafts: Vec::new(),
            marks: Vec::new(),
        }
    }

    #[test]
    fn the_menu_shows_the_shortcuts() {
        let prefs = Prefs::default();
        assert_eq!(status_text(&idle(), &prefs), "Ready · Ctrl+Alt+Shift+R to record");
        assert_eq!(record_text(&idle(), &prefs), ("Record\tCtrl+Alt+Shift+R".to_string(), true));
        assert_eq!(mark_text(&idle(), &prefs), ("Mark Moment\tCtrl+Alt+Shift+M".to_string(), false));
        let recording = Snapshot { phase: Phase::Recording, ..idle() };
        assert_eq!(record_text(&recording, &prefs).0, "Stop Recording\tCtrl+Alt+Shift+R");
        assert!(mark_text(&recording, &prefs).1);
    }
}
