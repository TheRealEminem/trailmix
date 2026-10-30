//! The global shortcuts on Wayland: the desktop's GlobalShortcuts portal (xdg-desktop-portal's
//! org.freedesktop.portal.GlobalShortcuts, which KDE Plasma 6, GNOME 48 and later and Hyprland have). On Wayland an
//! X11 key grab only sees keys typed into X11 apps, so where there's no portal the shortcuts stay off.
//!
//! The portal works differently from a grab: Trailmix asks for its two shortcuts (by name, "record" and "mark", with
//! the keys it would like), the desktop shows them to you and binds them (usually with a dialog the first time), and
//! from then on the desktop keeps them: you change them in its own settings, which version 2 of the portal opens
//! (ConfigureShortcuts). A session's shortcuts can only be bound once, so asking for different keys starts a new
//! session. Presses arrive as the Activated signal.
//!
//! It runs on a D-Bus connection and a thread of its own (with its own GLib main context, where the portal's answers
//! and the presses are delivered), not GTK's, and runs nothing privileged.

use std::cell::RefCell;
use std::collections::HashMap;
use std::sync::{mpsc, Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use gtk::gio;
use gtk::glib;
use gtk::glib::{Variant, VariantTy};

use super::Session;
use crate::hotkeys::{self, Shortcut, MARK, MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, RECORD};

const BUS_NAME: &str = "org.freedesktop.portal.Desktop";
const OBJECT: &str = "/org/freedesktop/portal/desktop";
const INTERFACE: &str = "org.freedesktop.portal.GlobalShortcuts";
const REQUEST: &str = "org.freedesktop.portal.Request";
const SESSION: &str = "org.freedesktop.portal.Session";
const REGISTRY: &str = "org.freedesktop.host.portal.Registry";
/// The app id the portal knows Trailmix by: the .desktop file the .deb and .rpm install
/// (/usr/share/applications/Trailmix.desktop). The desktop shows the shortcuts under that app's name.
const APP_ID: &str = "Trailmix";

// MARK: Which way the shortcuts work here

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Backend {
    /// X11: a key grab on the root window (keys.rs).
    X11Grab,
    /// Wayland with the GlobalShortcuts portal.
    Portal,
    /// Wayland without it, or no display: no global shortcuts.
    Off,
}

/// How the shortcuts work for this session, given the portal's GlobalShortcuts version (None: no portal, or one
/// without GlobalShortcuts, as on GNOME before 48). An X11 session grabs the keys even when there's a portal: a
/// grab works for every app and needs no dialog.
pub fn backend(session: Session, portal_version: Option<u32>) -> Backend {
    match session {
        Session::X11 => Backend::X11Grab,
        Session::Wayland if portal_version.unwrap_or(0) >= 1 => Backend::Portal,
        _ => Backend::Off,
    }
}

// MARK: Shortcuts as the portal writes them

/// The shortcut's name with the portal (the same every time, so the desktop remembers its keys).
pub fn shortcut_name(id: i32) -> Option<&'static str> {
    match id {
        RECORD => Some("record"),
        MARK => Some("mark"),
        _ => None,
    }
}

pub fn shortcut_id(name: &str) -> Option<i32> {
    match name {
        "record" => Some(RECORD),
        "mark" => Some(MARK),
        _ => None,
    }
}

/// What the desktop shows for the shortcut.
fn description(id: i32) -> &'static str {
    if id == RECORD {
        "Start or stop recording"
    } else {
        "Mark a moment in the recording"
    }
}

/// The key's xkb keysym name (as the shortcuts spec has it: "r", "F9", "space", "Page_Down").
fn key_name(vk: u32) -> Option<String> {
    let named = match vk {
        0x41..=0x5A => return Some(char::from((vk + 0x20) as u8).to_string()),
        0x30..=0x39 => return Some(char::from(vk as u8).to_string()),
        0x70..=0x87 => return Some(format!("F{}", vk - 0x70 + 1)),
        0x60..=0x69 => return Some(format!("KP_{}", vk - 0x60)),
        0x08 => "BackSpace",
        0x09 => "Tab",
        0x0D => "Return",
        0x13 => "Pause",
        0x20 => "space",
        0x21 => "Page_Up",
        0x22 => "Page_Down",
        0x23 => "End",
        0x24 => "Home",
        0x25 => "Left",
        0x26 => "Up",
        0x27 => "Right",
        0x28 => "Down",
        0x2C => "Print",
        0x2D => "Insert",
        0x2E => "Delete",
        0x6A => "KP_Multiply",
        0x6B => "KP_Add",
        0x6D => "KP_Subtract",
        0x6E => "KP_Decimal",
        0x6F => "KP_Divide",
        0xBA => "semicolon",
        0xBB => "equal",
        0xBC => "comma",
        0xBD => "minus",
        0xBE => "period",
        0xBF => "slash",
        0xC0 => "grave",
        0xDB => "bracketleft",
        0xDC => "backslash",
        0xDD => "bracketright",
        0xDE => "apostrophe",
        _ => return None,
    };
    Some(named.to_string())
}

/// A shortcut in the XDG shortcuts spec's notation, the portal's preferred_trigger: "CTRL+ALT+SHIFT+s".
pub fn trigger(shortcut: Shortcut) -> Option<String> {
    let key = key_name(shortcut.key)?;
    let mut parts: Vec<String> = [(MOD_CONTROL, "CTRL"), (MOD_ALT, "ALT"), (MOD_SHIFT, "SHIFT"), (MOD_WIN, "LOGO")]
        .iter()
        .filter(|(flag, _)| shortcut.mods & flag != 0)
        .map(|(_, name)| name.to_string())
        .collect();
    parts.push(key);
    Some(parts.join("+"))
}

/// Where the portal answers a request: /org/freedesktop/portal/desktop/request/SENDER/TOKEN, SENDER being this
/// connection's unique name without the ':' and with '.' as '_'.
pub fn request_path(unique_name: &str, token: &str) -> String {
    format!("{OBJECT}/request/{}/{token}", unique_name.trim_start_matches(':').replace('.', "_"))
}

/// A string in GVariant's text format.
fn quote(text: &str) -> String {
    let mut out = String::from("'");
    for c in text.chars() {
        if c == '\'' || c == '\\' {
            out.push('\\');
        }
        out.push(c);
    }
    out.push('\'');
    out
}

/// BindShortcuts' arguments, (oa(sa{sv})sa{sv}), in GVariant's text format.
pub fn bind_text(session: &str, wanted: &[(i32, Shortcut)], token: &str) -> String {
    let shortcuts: Vec<String> = wanted
        .iter()
        .filter_map(|(id, shortcut)| {
            let name = shortcut_name(*id)?;
            let mut props = format!("'description': <{}>", quote(description(*id)));
            if let Some(keys) = trigger(*shortcut) {
                props.push_str(&format!(", 'preferred_trigger': <{}>", quote(&keys)));
            }
            Some(format!("({}, {{{props}}})", quote(name)))
        })
        .collect();
    format!("(objectpath {}, [{}], '', {{'handle_token': <{}>}})", quote(session), shortcuts.join(", "), quote(token))
}

fn ty(text: &'static str) -> &'static VariantTy {
    VariantTy::new(text).expect("a valid D-Bus type")
}

fn parse(signature: &'static str, text: &str) -> Option<Variant> {
    Variant::parse(Some(ty(signature)), text).ok()
}

fn empty_dict() -> Variant {
    Variant::parse(Some(ty("a{sv}")), "{}").expect("an empty dictionary")
}

/// A value from an a{sv} (unwrapped from its variant), if it's there.
fn dict_get(dict: &Variant, key: &str) -> Option<Variant> {
    if !dict.is_type(ty("a{sv}")) {
        return None;
    }
    (0..dict.n_children()).map(|i| dict.child_value(i)).find(|entry| entry.child_value(0).str() == Some(key)).map(|entry| {
        let value = entry.child_value(1);
        value.as_variant().unwrap_or(value)
    })
}

/// The shortcuts the desktop bound, a(sa{sv}), as (id, how to type it: its trigger_description, "" if no keys).
pub fn bound_shortcuts(list: &Variant) -> Vec<(i32, String)> {
    if !list.is_type(ty("a(sa{sv})")) {
        return Vec::new();
    }
    (0..list.n_children())
        .map(|i| list.child_value(i))
        .filter_map(|item| {
            let id = item.child_value(0).str().and_then(shortcut_id)?;
            let keys = dict_get(&item.child_value(1), "trigger_description").and_then(|v| v.str().map(String::from)).unwrap_or_default();
            Some((id, keys))
        })
        .collect()
}

/// The shortcuts asked for that the desktop didn't bind to any keys (left out, or bound to nothing).
pub fn not_bound(wanted: &[(i32, Shortcut)], bound: &[(i32, String)]) -> Vec<i32> {
    wanted.iter().map(|(id, _)| *id).filter(|id| !bound.iter().any(|(b, keys)| b == id && !keys.trim().is_empty())).collect()
}

// MARK: The portal's thread

#[derive(Default)]
struct Shared {
    /// While a new shortcut is being typed: presses do nothing.
    paused: bool,
    wanted: Vec<(i32, Shortcut)>,
    bound: Vec<(i32, String)>,
    session: Option<String>,
    last: Option<(i32, Instant)>,
}

struct Portal {
    context: glib::MainContext,
    version: u32,
    shared: Mutex<Shared>,
    pressed: Arc<dyn Fn(i32) + Send + Sync>,
}

static PORTAL: OnceLock<Portal> = OnceLock::new();

/// The connection, on the portal's thread only, with the requests waiting for their answer (by path).
struct Link {
    conn: gio::DBusConnection,
    unique: String,
    serial: u32,
    pending: HashMap<String, Box<dyn FnOnce(u32, Variant)>>,
}

thread_local! {
    static LINK: RefCell<Option<Link>> = const { RefCell::new(None) };
}

fn shared(portal: &Portal) -> std::sync::MutexGuard<'_, Shared> {
    portal.shared.lock().unwrap_or_else(|e| e.into_inner())
}

fn no_cancel() -> Option<&'static gio::Cancellable> {
    None
}

/// A connection of its own to the session bus, Trailmix named to the portal (before anything else is asked of it:
/// an app not started from its .desktop file has no app id otherwise), and the portal's GlobalShortcuts version.
fn connect(address: Option<&str>) -> Option<(gio::DBusConnection, Option<u32>)> {
    let address = match address {
        Some(address) => address.to_string(),
        None => gio::dbus_address_get_for_bus_sync(gio::BusType::Session, no_cancel()).ok()?.to_string(),
    };
    let flags = gio::DBusConnectionFlags::AUTHENTICATION_CLIENT | gio::DBusConnectionFlags::MESSAGE_BUS_CONNECTION;
    let conn = gio::DBusConnection::for_address_sync(&address, flags, None, no_cancel()).ok()?;
    if let Some(args) = parse("(sa{sv})", &format!("({}, {{}})", quote(APP_ID))) {
        // (Fails where there's no Trailmix.desktop, as for an AppImage, or with a portal older than 1.19: then the
        // desktop goes by how the app was started.)
        let _ = conn.call_sync(Some(BUS_NAME), OBJECT, REGISTRY, "Register", Some(&args), None, gio::DBusCallFlags::NONE, 5000, no_cancel());
    }
    let version = parse("(ss)", &format!("({}, 'version')", quote(INTERFACE))).and_then(|args| {
        let reply = conn
            .call_sync(Some(BUS_NAME), OBJECT, "org.freedesktop.DBus.Properties", "Get", Some(&args), Some(ty("(v)")), gio::DBusCallFlags::NONE, 5000, no_cancel())
            .ok()?;
        reply.child_value(0).as_variant()?.get::<u32>()
    });
    Some((conn, version))
}

fn subscribe(conn: &gio::DBusConnection) {
    let none = gio::DBusSignalFlags::NONE;
    let _ = conn.signal_subscribe(Some(BUS_NAME), Some(REQUEST), Some("Response"), None, None, none, |_, _, path, _, _, params| {
        responded(path, params)
    });
    let _ = conn.signal_subscribe(Some(BUS_NAME), Some(INTERFACE), Some("Activated"), Some(OBJECT), None, none, |_, _, _, _, _, params| {
        activated(params)
    });
    let _ = conn.signal_subscribe(Some(BUS_NAME), Some(INTERFACE), Some("ShortcutsChanged"), Some(OBJECT), None, none, |_, _, _, _, _, params| {
        changed(params)
    });
}

/// A request's answer (Response: (u code, a{sv} results); code 0 is done, 1 you said no, 2 it failed).
fn responded(path: &str, params: &Variant) {
    let handler = LINK.with(|link| link.borrow_mut().as_mut().and_then(|l| l.pending.remove(path)));
    let Some(handler) = handler else { return };
    let code = params.try_child_value(0).and_then(|v| v.get::<u32>()).unwrap_or(2);
    let results = params.try_child_value(1).unwrap_or_else(empty_dict);
    handler(code, results);
}

/// Calls a portal method that answers later, through a Request; `then` gets the answer (or code 2 if the call
/// itself failed).
fn request(method: &str, signature: &'static str, args: impl FnOnce(&str) -> String, then: impl FnOnce(u32, Variant) + 'static) {
    let prepared = LINK.with(|link| {
        let mut link = link.borrow_mut();
        let link = link.as_mut()?;
        link.serial += 1;
        let token = format!("trailmix_{}_{}", std::process::id(), link.serial);
        let path = request_path(&link.unique, &token);
        link.pending.insert(path.clone(), Box::new(then));
        Some((link.conn.clone(), token, path))
    });
    let Some((conn, token, path)) = prepared else { return };
    let reply = parse(signature, &args(&token)).and_then(|args| {
        conn.call_sync(Some(BUS_NAME), OBJECT, INTERFACE, method, Some(&args), Some(ty("(o)")), gio::DBusCallFlags::NONE, 10000, no_cancel()).ok()
    });
    LINK.with(|link| {
        let mut link = link.borrow_mut();
        let Some(link) = link.as_mut() else { return None };
        match &reply {
            // (A portal that didn't take the handle_token answers on a path of its own.)
            Some(reply) => {
                let actual = reply.child_value(0).str().map(String::from).unwrap_or_default();
                if !actual.is_empty() && actual != path {
                    if let Some(handler) = link.pending.remove(&path) {
                        link.pending.insert(actual, handler);
                    }
                }
                None
            }
            None => link.pending.remove(&path),
        }
    })
    .map(|failed| failed(2, empty_dict()));
}

fn call_quietly(path: &str, interface: &str, method: &str, args: Option<&Variant>) {
    LINK.with(|link| {
        if let Some(link) = link.borrow().as_ref() {
            let _ = link.conn.call_sync(Some(BUS_NAME), path, interface, method, args, None, gio::DBusCallFlags::NONE, 5000, no_cancel());
        }
    });
}

/// Binds these shortcuts in a new session (closing the old one: a session binds once).
fn bind(wanted: Vec<(i32, Shortcut)>) {
    let Some(portal) = PORTAL.get() else { return };
    let old = shared(portal).session.take();
    if let Some(old) = old {
        call_quietly(&old, SESSION, "Close", None);
    }
    let session_args = |token: &str| format!("({{'handle_token': <{t}>, 'session_handle_token': <{t}>}},)", t = quote(token));
    request("CreateSession", "(a{sv})", session_args, move |code, results| {
        let session = dict_get(&results, "session_handle").and_then(|v| v.str().map(String::from));
        let (0, Some(session)) = (code, session) else {
            finished(&wanted, Vec::new());
            return;
        };
        if let Some(portal) = PORTAL.get() {
            shared(portal).session = Some(session.clone());
        }
        let (for_args, asked) = (wanted.clone(), wanted);
        request("BindShortcuts", "(oa(sa{sv})sa{sv})", move |token| bind_text(&session, &for_args, token), move |code, results| {
            let bound = if code == 0 { dict_get(&results, "shortcuts").map(|list| bound_shortcuts(&list)).unwrap_or_default() } else { Vec::new() };
            finished(&asked, bound);
        });
    });
}

/// The desktop's answer: which shortcuts it bound, and to what. The rest count as taken (the tray says so).
fn finished(wanted: &[(i32, Shortcut)], bound: Vec<(i32, String)>) {
    let Some(portal) = PORTAL.get() else { return };
    let mut shared = shared(portal);
    if shared.wanted != wanted {
        return; // asked again since
    }
    hotkeys::set_taken(not_bound(wanted, &bound));
    shared.bound = bound;
}

/// Activated: (o session, s shortcut, t timestamp, a{sv}).
fn activated(params: &Variant) {
    let Some(portal) = PORTAL.get() else { return };
    let session = params.try_child_value(0).and_then(|v| v.str().map(String::from));
    let Some(id) = params.try_child_value(1).and_then(|v| v.str().and_then(shortcut_id)) else { return };
    {
        let mut shared = shared(portal);
        if shared.paused || shared.session.is_none() || shared.session != session {
            return;
        }
        // A held key repeats: one press is one press.
        if let Some((was, at)) = shared.last {
            if was == id && at.elapsed() < Duration::from_millis(600) {
                shared.last = Some((id, Instant::now()));
                return;
            }
        }
        shared.last = Some((id, Instant::now()));
    }
    let pressed = portal.pressed.clone();
    thread::spawn(move || pressed(id));
}

/// ShortcutsChanged: (o session, a(sa{sv})), when you change the keys in the desktop's settings.
fn changed(params: &Variant) {
    let Some(portal) = PORTAL.get() else { return };
    let session = params.try_child_value(0).and_then(|v| v.str().map(String::from));
    let Some(list) = params.try_child_value(1) else { return };
    let mut shared = shared(portal);
    if shared.session.is_none() || shared.session != session {
        return;
    }
    let bound = bound_shortcuts(&list);
    hotkeys::set_taken(not_bound(&shared.wanted, &bound));
    shared.bound = bound;
}

// MARK: For keys.rs

/// Connects to the portal and starts its thread, if this is a Wayland session whose desktop has GlobalShortcuts;
/// `pressed(id)` is called (on a thread of its own) for each press. False otherwise. Nothing is bound until
/// `register`.
pub fn start(pressed: impl Fn(i32) + Send + Sync + 'static) -> bool {
    if PORTAL.get().is_some() {
        return true;
    }
    if super::session() != Session::Wayland || std::env::var_os("TRAILMIX_NO_PORTAL").is_some() {
        return false;
    }
    start_on(None, pressed)
}

/// `start` on the session bus at `address` (None: this session's).
fn start_on(address: Option<&str>, pressed: impl Fn(i32) + Send + Sync + 'static) -> bool {
    if PORTAL.get().is_some() {
        return true;
    }
    let address = address.map(String::from);
    let context = glib::MainContext::new();
    let (ready_tx, ready_rx) = mpsc::channel::<Option<u32>>();
    let thread_context = context.clone();
    let spawned = thread::Builder::new().name("trailmix-portal".into()).spawn(move || {
        let _ = thread_context.with_thread_default(|| {
            let Some((conn, version)) = connect(address.as_deref()) else {
                let _ = ready_tx.send(None);
                return;
            };
            if backend(Session::Wayland, version) != Backend::Portal {
                let _ = ready_tx.send(None);
                return;
            }
            subscribe(&conn);
            let unique = conn.unique_name().map(|name| name.to_string()).unwrap_or_default();
            LINK.with(|link| *link.borrow_mut() = Some(Link { conn, unique, serial: 0, pending: HashMap::new() }));
            let _ = ready_tx.send(version);
            loop {
                thread_context.iteration(true);
            }
        });
    });
    if spawned.is_err() {
        return false;
    }
    let Some(version) = ready_rx.recv_timeout(Duration::from_secs(8)).ok().flatten() else { return false };
    let portal = Portal { context, version, shared: Mutex::new(Shared::default()), pressed: Arc::new(pressed) };
    PORTAL.set(portal).is_ok() || PORTAL.get().is_some()
}

/// Whether the shortcuts go through the portal.
pub fn active() -> bool {
    PORTAL.get().is_some()
}

/// The portal's GlobalShortcuts version (2 can open the desktop's settings for them).
pub fn version() -> Option<u32> {
    PORTAL.get().map(|p| p.version)
}

/// Asks the desktop for these shortcuts (none: pauses them, while a new one is being typed). The desktop answers
/// later (often after showing you a dialog): what it didn't bind is reported then, through hotkeys::set_taken.
/// Returns what's known to be not bound now.
pub fn register(wanted: &[(i32, Shortcut)]) -> Vec<i32> {
    let Some(portal) = PORTAL.get() else { return wanted.iter().map(|(id, _)| *id).collect() };
    {
        let mut shared = shared(portal);
        if wanted.is_empty() {
            shared.paused = true;
            return Vec::new();
        }
        shared.paused = false;
        if shared.wanted == wanted {
            return if shared.session.is_some() { not_bound(wanted, &shared.bound) } else { Vec::new() };
        }
        shared.wanted = wanted.to_vec();
        shared.bound.clear();
    }
    let wanted = wanted.to_vec();
    portal.context.invoke(move || bind(wanted));
    Vec::new()
}

/// How the desktop says to type the shortcut ("Ctrl+Alt+Shift+S"), once it has bound it to keys.
pub fn trigger_description(id: i32) -> Option<String> {
    let portal = PORTAL.get()?;
    let shared = shared(portal);
    shared.bound.iter().find(|(b, keys)| *b == id && !keys.trim().is_empty()).map(|(_, keys)| keys.clone())
}

/// Opens the desktop's own settings for Trailmix's shortcuts (portal version 2). False where it can't.
pub fn configure() -> bool {
    let Some(portal) = PORTAL.get() else { return false };
    if portal.version < 2 {
        return false;
    }
    let Some(session) = shared(portal).session.clone() else { return false };
    portal.context.invoke(move || {
        if let Some(args) = parse("(osa{sv})", &format!("(objectpath {}, '', {{}})", quote(&session))) {
            call_quietly(OBJECT, INTERFACE, "ConfigureShortcuts", Some(&args));
        }
    });
    true
}

/// Delivers a press of shortcut `id` as the portal would (for the self-test).
pub fn post_press(id: i32) -> bool {
    let Some(portal) = PORTAL.get() else { return false };
    let pressed = portal.pressed.clone();
    thread::spawn(move || pressed(id));
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(key: u8, mods: u32) -> Shortcut {
        Shortcut { key: key as u32, mods }
    }

    #[test]
    fn the_portal_is_used_on_wayland_only_when_the_desktop_has_it() {
        assert_eq!(backend(Session::X11, None), Backend::X11Grab);
        assert_eq!(backend(Session::X11, Some(2)), Backend::X11Grab, "a grab works for every app: kept on X11");
        assert_eq!(backend(Session::Wayland, Some(1)), Backend::Portal);
        assert_eq!(backend(Session::Wayland, Some(2)), Backend::Portal);
        assert_eq!(backend(Session::Wayland, None), Backend::Off, "GNOME before 48: no GlobalShortcuts");
        assert_eq!(backend(Session::Wayland, Some(0)), Backend::Off);
        assert_eq!(backend(Session::None, Some(2)), Backend::Off);
    }

    #[test]
    fn shortcuts_are_written_as_the_spec_has_them() {
        assert_eq!(trigger(s(b'S', MOD_CONTROL | MOD_ALT | MOD_SHIFT)).as_deref(), Some("CTRL+ALT+SHIFT+s"));
        assert_eq!(trigger(s(b'M', MOD_CONTROL | MOD_ALT | MOD_SHIFT)).as_deref(), Some("CTRL+ALT+SHIFT+m"));
        assert_eq!(trigger(Shortcut { key: 0x78, mods: MOD_WIN | MOD_SHIFT }).as_deref(), Some("SHIFT+LOGO+F9"));
        assert_eq!(trigger(Shortcut { key: 0x78, mods: 0 }).as_deref(), Some("F9"));
        assert_eq!(trigger(Shortcut { key: 0x20, mods: MOD_CONTROL }).as_deref(), Some("CTRL+space"));
        assert_eq!(trigger(Shortcut { key: 0x22, mods: MOD_ALT }).as_deref(), Some("ALT+Page_Down"));
        assert_eq!(trigger(Shortcut { key: 0xBC, mods: MOD_CONTROL }).as_deref(), Some("CTRL+comma"));
        assert_eq!(trigger(Shortcut { key: 0x63, mods: MOD_CONTROL }).as_deref(), Some("CTRL+KP_3"));
        assert_eq!(trigger(Shortcut { key: b'7' as u32, mods: MOD_CONTROL }).as_deref(), Some("CTRL+7"));
        assert_eq!(trigger(Shortcut { key: 0xFF, mods: MOD_CONTROL }), None);
        // Every key a shortcut can be made of has a name.
        for vk in 0x08..=0xFFu32 {
            if crate::hotkeys::key_name(vk).is_some() && !(0xDF..=0xE2).contains(&vk) {
                assert!(key_name(vk).is_some(), "no keysym name for key {vk:#x}");
            }
        }
    }

    #[test]
    fn shortcuts_have_steady_names() {
        for id in [RECORD, MARK] {
            assert_eq!(shortcut_id(shortcut_name(id).unwrap()), Some(id));
        }
        assert_eq!(shortcut_name(RECORD), Some("record"));
        assert_eq!(shortcut_id("something-else"), None);
    }

    #[test]
    fn requests_are_answered_where_the_spec_says() {
        assert_eq!(request_path(":1.42", "trailmix_7_1"), "/org/freedesktop/portal/desktop/request/1_42/trailmix_7_1");
    }

    #[test]
    fn binding_asks_for_both_with_the_preferred_keys() {
        let wanted = [(RECORD, Shortcut::RECORD_DEFAULT), (MARK, Shortcut::MARK_DEFAULT)];
        let text = bind_text("/org/freedesktop/portal/desktop/session/1_42/t1", &wanted, "t2");
        let args = parse("(oa(sa{sv})sa{sv})", &text).expect("BindShortcuts' arguments parse");
        assert_eq!(args.child_value(0).str(), Some("/org/freedesktop/portal/desktop/session/1_42/t1"));
        let list = args.child_value(1);
        assert_eq!(list.n_children(), 2);
        let record = list.child_value(0);
        assert_eq!(record.child_value(0).str(), Some("record"));
        let preferred = dict_get(&record.child_value(1), "preferred_trigger").unwrap();
        assert_eq!(preferred.str().map(String::from), trigger(Shortcut::RECORD_DEFAULT));
        assert!(dict_get(&record.child_value(1), "description").unwrap().str().unwrap().contains("recording"));
        assert_eq!(dict_get(&args.child_value(3), "handle_token").unwrap().str(), Some("t2"));
    }

    #[test]
    fn the_desktops_answer_says_what_was_bound() {
        let answer = parse(
            "a(sa{sv})",
            "[('record', {'description': <'Start or stop recording'>, 'trigger_description': <'Ctrl+Alt+Shift+S'>}), \
              ('mark', {'description': <'Mark'>, 'trigger_description': <''>}), ('other', {})]",
        )
        .unwrap();
        let bound = bound_shortcuts(&answer);
        assert_eq!(bound, vec![(RECORD, "Ctrl+Alt+Shift+S".to_string()), (MARK, String::new())]);
        let wanted = [(RECORD, Shortcut::RECORD_DEFAULT), (MARK, Shortcut::MARK_DEFAULT)];
        assert_eq!(not_bound(&wanted, &bound), vec![MARK], "bound to no keys counts as not bound");
        assert_eq!(not_bound(&wanted, &[]), vec![RECORD, MARK], "you said no in the dialog");
        assert!(bound_shortcuts(&empty_dict()).is_empty(), "not a list of shortcuts");
    }

    #[test]
    fn text_is_quoted_for_gvariant() {
        assert_eq!(quote("it's"), r"'it\'s'");
        assert_eq!(parse("s", &quote(r"a'b\c")).unwrap().str(), Some(r"a'b\c"));
    }

    // MARK: End to end, against a stand-in portal on a private session bus

    const STAND_IN: &str = r#"<node>
      <interface name="org.freedesktop.portal.GlobalShortcuts">
        <method name="CreateSession"><arg type="a{sv}" direction="in"/><arg type="o" direction="out"/></method>
        <method name="BindShortcuts">
          <arg type="o" direction="in"/><arg type="a(sa{sv})" direction="in"/><arg type="s" direction="in"/>
          <arg type="a{sv}" direction="in"/><arg type="o" direction="out"/>
        </method>
        <method name="ConfigureShortcuts"><arg type="o" direction="in"/><arg type="s" direction="in"/><arg type="a{sv}" direction="in"/></method>
        <property name="version" type="u" access="read"/>
      </interface>
      <interface name="org.freedesktop.host.portal.Registry">
        <method name="Register"><arg type="s" direction="in"/><arg type="a{sv}" direction="in"/></method>
      </interface>
    </node>"#;

    /// What the stand-in was asked.
    #[derive(Default)]
    struct Asked {
        app_id: Option<String>,
        client: String,
        sessions: Vec<String>,
        /// Each BindShortcuts: (session, [(name, preferred trigger)]).
        binds: Vec<(String, Vec<(String, String)>)>,
        configured: u32,
    }

    fn session_path(sender: &str, token: &str) -> String {
        format!("{OBJECT}/session/{}/{token}", sender.trim_start_matches(':').replace('.', "_"))
    }

    fn token_of(options: &Variant) -> String {
        dict_get(options, "handle_token").and_then(|v| v.str().map(String::from)).unwrap_or_default()
    }

    /// A portal of its own on the bus at `address`, answering as KDE's does (binding record and mark to
    /// Ctrl+Alt+Shift+S and M whatever was asked), on a thread of its own. Returns its connection.
    fn stand_in(address: &str, asked: Arc<Mutex<Asked>>) -> gio::DBusConnection {
        let (tx, rx) = mpsc::channel();
        let address = address.to_string();
        thread::spawn(move || {
            let context = glib::MainContext::new();
            let _ = context.with_thread_default(|| {
                let flags = gio::DBusConnectionFlags::AUTHENTICATION_CLIENT | gio::DBusConnectionFlags::MESSAGE_BUS_CONNECTION;
                let conn = gio::DBusConnection::for_address_sync(&address, flags, None, no_cancel()).expect("the stand-in connects");
                let name = parse("(su)", &format!("({}, 4)", quote(BUS_NAME))).unwrap();
                conn.call_sync(Some("org.freedesktop.DBus"), "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName", Some(&name), Some(ty("(u)")), gio::DBusCallFlags::NONE, 5000, no_cancel())
                    .expect("the stand-in takes the portal's name");
                let node = gio::DBusNodeInfo::for_xml(STAND_IN).expect("the stand-in's interfaces");
                for interface in [INTERFACE, REGISTRY] {
                    let info = node.lookup_interface(interface).expect("the interface");
                    let asked = asked.clone();
                    conn.register_object(
                        OBJECT,
                        &info,
                        move |conn, sender, _path, _interface, method, params, invocation| {
                            let mut asked = asked.lock().unwrap();
                            match method {
                                "Register" => {
                                    asked.app_id = params.child_value(0).str().map(String::from);
                                    invocation.return_value(None);
                                }
                                "CreateSession" => {
                                    let options = params.child_value(0);
                                    let request = request_path(sender, &token_of(&options));
                                    let token = dict_get(&options, "session_handle_token").and_then(|v| v.str().map(String::from)).unwrap_or_default();
                                    let session = session_path(sender, &token);
                                    asked.client = sender.to_string();
                                    asked.sessions.push(session.clone());
                                    invocation.return_value(parse("(o)", &format!("(objectpath {})", quote(&request))).as_ref());
                                    let answer = parse("(ua{sv})", &format!("(0, {{'session_handle': <{}>}})", quote(&session))).unwrap();
                                    let _ = conn.emit_signal(Some(sender), &request, REQUEST, "Response", Some(&answer));
                                }
                                "BindShortcuts" => {
                                    let session = params.child_value(0).str().unwrap_or_default().to_string();
                                    let list = params.child_value(1);
                                    let wanted = (0..list.n_children())
                                        .map(|i| list.child_value(i))
                                        .map(|item| {
                                            let name = item.child_value(0).str().unwrap_or_default().to_string();
                                            let keys = dict_get(&item.child_value(1), "preferred_trigger").and_then(|v| v.str().map(String::from)).unwrap_or_default();
                                            (name, keys)
                                        })
                                        .collect::<Vec<_>>();
                                    let request = request_path(sender, &token_of(&params.child_value(3)));
                                    let bound: Vec<String> = wanted
                                        .iter()
                                        .map(|(name, _)| {
                                            let keys = if name == "record" { "Ctrl+Alt+Shift+S" } else { "Ctrl+Alt+Shift+M" };
                                            format!("({}, {{'description': <'x'>, 'trigger_description': <{}>}})", quote(name), quote(keys))
                                        })
                                        .collect();
                                    asked.binds.push((session, wanted));
                                    invocation.return_value(parse("(o)", &format!("(objectpath {})", quote(&request))).as_ref());
                                    let answer = parse("(ua{sv})", &format!("(0, {{'shortcuts': <@a(sa{{sv}}) [{}]>}})", bound.join(", "))).unwrap();
                                    let _ = conn.emit_signal(Some(sender), &request, REQUEST, "Response", Some(&answer));
                                }
                                "ConfigureShortcuts" => {
                                    asked.configured += 1;
                                    invocation.return_value(None);
                                }
                                _ => invocation.return_value(None),
                            }
                        },
                        |_, _, _, _, _| Variant::from(2u32),
                        |_, _, _, _, _, _| false,
                    )
                    .expect("the stand-in's object");
                }
                let _ = tx.send(conn.clone());
                loop {
                    context.iteration(true);
                }
            });
        });
        rx.recv_timeout(Duration::from_secs(10)).expect("the stand-in portal is up")
    }

    fn wait(seconds: f64, mut done: impl FnMut() -> bool) -> bool {
        let deadline = Instant::now() + Duration::from_secs_f64(seconds);
        while Instant::now() < deadline {
            if done() {
                return true;
            }
            thread::sleep(Duration::from_millis(50));
        }
        done()
    }

    fn press(portal: &gio::DBusConnection, to: &str, session: &str, name: &str) {
        let args = parse("(osta{sv})", &format!("(objectpath {}, {}, 1, {{}})", quote(session), quote(name))).unwrap();
        portal.emit_signal(Some(to), OBJECT, INTERFACE, "Activated", Some(&args)).expect("Activated is sent");
    }

    #[test]
    fn shortcuts_go_through_a_stand_in_portal() {
        // A private session bus, so nothing of this computer's is asked; skipped where there's no dbus-daemon.
        let daemon = std::process::Command::new("dbus-daemon")
            .args(["--session", "--nofork", "--print-address=1"])
            .stdout(std::process::Stdio::piped())
            .spawn();
        let Ok(mut daemon) = daemon else {
            eprintln!("no dbus-daemon here: skipped");
            return;
        };
        let mut address = String::new();
        std::io::BufRead::read_line(&mut std::io::BufReader::new(daemon.stdout.take().unwrap()), &mut address).unwrap();
        let address = address.trim().to_string();
        let asked = Arc::new(Mutex::new(Asked::default()));
        let portal = stand_in(&address, asked.clone());

        let (tx, rx) = mpsc::channel();
        let tx = Mutex::new(tx);
        assert!(start_on(Some(&address), move |id| {
            let _ = tx.lock().unwrap().send(id);
        }));
        assert_eq!(version(), Some(2));
        assert_eq!(asked.lock().unwrap().app_id.as_deref(), Some(APP_ID), "Trailmix said who it is first");

        // Binding: a session, then both shortcuts with the keys asked for; the desktop's keys come back.
        let wanted = [(RECORD, Shortcut::RECORD_DEFAULT), (MARK, Shortcut::MARK_DEFAULT)];
        assert!(register(&wanted).is_empty());
        assert!(wait(5.0, || trigger_description(RECORD).is_some()), "the desktop's answer arrives");
        assert_eq!(trigger_description(RECORD).as_deref(), Some("Ctrl+Alt+Shift+S"));
        assert_eq!(trigger_description(MARK).as_deref(), Some("Ctrl+Alt+Shift+M"));
        assert!(hotkeys::taken().is_empty());
        let (client, session) = {
            let asked = asked.lock().unwrap();
            assert_eq!(asked.binds.len(), 1);
            let (bound_in, keys) = &asked.binds[0];
            assert_eq!(bound_in, &asked.sessions[0]);
            assert_eq!(keys[0], ("record".to_string(), trigger(Shortcut::RECORD_DEFAULT).unwrap()));
            assert_eq!(keys[1], ("mark".to_string(), "CTRL+ALT+SHIFT+m".to_string()));
            (asked.client.clone(), asked.sessions[0].clone())
        };

        // Presses.
        press(&portal, &client, &session, "record");
        assert_eq!(rx.recv_timeout(Duration::from_secs(3)).ok(), Some(RECORD));
        press(&portal, &client, &session_path(&client, "someone_else"), "mark");
        assert!(rx.recv_timeout(Duration::from_millis(700)).is_err(), "another session's press isn't ours");
        press(&portal, &client, &session, "mark");
        assert_eq!(rx.recv_timeout(Duration::from_secs(3)).ok(), Some(MARK));

        // Paused while a new shortcut is typed, then back, without binding again.
        assert!(register(&[]).is_empty());
        press(&portal, &client, &session, "record");
        assert!(rx.recv_timeout(Duration::from_millis(700)).is_err(), "paused");
        assert!(register(&wanted).is_empty());
        press(&portal, &client, &session, "record");
        assert_eq!(rx.recv_timeout(Duration::from_secs(3)).ok(), Some(RECORD));
        assert_eq!(asked.lock().unwrap().binds.len(), 1);

        // Different keys: a session binds once, so a new one.
        let f9 = Shortcut { key: 0x78, mods: 0 };
        register(&[(RECORD, f9), (MARK, Shortcut::MARK_DEFAULT)]);
        assert!(wait(5.0, || asked.lock().unwrap().binds.len() == 2), "bound again");
        {
            let asked = asked.lock().unwrap();
            assert_eq!(asked.sessions.len(), 2);
            assert_eq!(asked.binds[1].0, asked.sessions[1]);
            assert_eq!(asked.binds[1].1[0].1, "F9");
        }
        assert!(wait(5.0, || trigger_description(RECORD).is_some()));

        // Version 2: the desktop's own settings for them.
        assert!(configure());
        assert!(wait(5.0, || asked.lock().unwrap().configured == 1), "ConfigureShortcuts was asked for");
        let _ = daemon.kill();
        let _ = daemon.wait();
    }
}
