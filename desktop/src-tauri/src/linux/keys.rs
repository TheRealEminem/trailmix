//! The global shortcuts on Linux (hotkeys.rs's RegisterHotKey thread): a passive key grab on the X11 root
//! window, with a thread of its own that gets the key presses. A combination another app has grabbed can't be
//! had (X answers BadAccess), and is reported as taken. On Wayland a grab only sees keys typed into X11 apps,
//! so the shortcuts are off there (the web app then doesn't offer them).
//!
//! Shortcuts are kept as on Windows (virtual-key codes and MOD_* flags, so desktop.json reads the same) and
//! turned into X keysyms and modifier masks here. XTest types them and clicks the cards, for the self-test.

use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use x11rb::connection::Connection;
use x11rb::protocol::xproto::{ConnectionExt as _, GrabMode, ModMask, Window};
use x11rb::protocol::xtest::ConnectionExt as _;
use x11rb::protocol::Event;
use x11rb::rust_connection::RustConnection;

use crate::hotkeys::{Shortcut, MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN};

const SHIFT: u16 = 1;
const LOCK: u16 = 2;
const CONTROL: u16 = 4;
const MOD1: u16 = 8; // Alt
const MOD2: u16 = 16; // Num Lock
const MOD4: u16 = 64; // Super
/// Caps Lock and Num Lock don't change a shortcut: each grab is made with every mix of them.
const IGNORED: [u16; 4] = [0, LOCK, MOD2, LOCK | MOD2];

/// The X keysym for a virtual-key code (the keys hotkeys::key_name names).
pub fn keysym(vk: u32) -> Option<u32> {
    Some(match vk {
        0x41..=0x5A => vk + 0x20, // letters: the lower-case keysym
        0x30..=0x39 => vk,
        0x70..=0x87 => 0xFFBE + (vk - 0x70), // F1…F24
        0x60..=0x69 => 0xFFB0 + (vk - 0x60), // Num 0…9
        0x08 => 0xFF08,
        0x09 => 0xFF09,
        0x0D => 0xFF0D,
        0x13 => 0xFF13,
        0x20 => 0x20,
        0x21 => 0xFF55,
        0x22 => 0xFF56,
        0x23 => 0xFF57,
        0x24 => 0xFF50,
        0x25 => 0xFF51,
        0x26 => 0xFF52,
        0x27 => 0xFF53,
        0x28 => 0xFF54,
        0x2C => 0xFF61,
        0x2D => 0xFF63,
        0x2E => 0xFFFF,
        0x6A => 0xFFAA,
        0x6B => 0xFFAB,
        0x6D => 0xFFAD,
        0x6E => 0xFFAE,
        0x6F => 0xFFAF,
        0xBA => 0x3B,
        0xBB => 0x3D,
        0xBC => 0x2C,
        0xBD => 0x2D,
        0xBE => 0x2E,
        0xBF => 0x2F,
        0xC0 => 0x60,
        0xDB => 0x5B,
        0xDC => 0x5C,
        0xDD => 0x5D,
        0xDE => 0x27,
        0x1B => 0xFF1B,
        _ => return None,
    })
}

/// The virtual-key code for an X keysym (a key pressed on a card), if it's one a shortcut can use.
pub fn vk_of_keysym(sym: u32) -> Option<u32> {
    let sym = if (0x41..=0x5A).contains(&sym) { sym + 0x20 } else { sym }; // upper-case letters too
    (0x08..=0xFF).find(|vk| keysym(*vk) == Some(sym))
}

/// X modifier mask for MOD_* flags.
pub fn x_mods(mods: u32) -> u16 {
    let mut out = 0;
    for (flag, mask) in [(MOD_SHIFT, SHIFT), (MOD_CONTROL, CONTROL), (MOD_ALT, MOD1), (MOD_WIN, MOD4)] {
        if mods & flag != 0 {
            out |= mask;
        }
    }
    out
}

/// MOD_* flags for an X modifier mask.
pub fn mods_of(mask: u16) -> u32 {
    let mut out = 0;
    for (flag, bit) in [(MOD_SHIFT, SHIFT), (MOD_CONTROL, CONTROL), (MOD_ALT, MOD1), (MOD_WIN, MOD4)] {
        if mask & bit != 0 {
            out |= flag;
        }
    }
    out
}

struct Keys {
    conn: Arc<RustConnection>,
    root: Window,
    /// (id, keycode, X modifiers) for what's grabbed now.
    grabbed: Mutex<Vec<(i32, u8, u16)>>,
    pressed: Arc<dyn Fn(i32) + Send + Sync>,
}

static KEYS: OnceLock<Keys> = OnceLock::new();

fn connect() -> Option<(RustConnection, Window)> {
    let (conn, screen) = x11rb::connect(None).ok()?;
    let root = conn.setup().roots.get(screen)?.root;
    Some((conn, root))
}

/// The keycode that types this keysym on the keyboard as it's set up now.
fn keycode(conn: &RustConnection, sym: u32) -> Option<u8> {
    let setup = conn.setup();
    let (min, max) = (setup.min_keycode, setup.max_keycode);
    let map = conn.get_keyboard_mapping(min, max - min + 1).ok()?.reply().ok()?;
    let per = map.keysyms_per_keycode as usize;
    if per == 0 {
        return None;
    }
    map.keysyms.chunks(per).position(|syms| syms.contains(&sym)).map(|i| min + i as u8)
}

/// Whether the shortcuts work here: an X11 session (not Wayland, where a grab sees only X11 apps' keys).
pub fn available() -> bool {
    KEYS.get().is_some()
}

/// Starts the thread that gets the presses; `pressed(id)` is called (on a thread of its own) for each.
/// False where there are no global shortcuts (Wayland, or no display).
pub fn start(pressed: impl Fn(i32) + Send + Sync + 'static) -> bool {
    if KEYS.get().is_some() {
        return true;
    }
    if super::session() != super::Session::X11 {
        return false;
    }
    let Some((conn, root)) = connect() else { return false };
    let conn = Arc::new(conn);
    let keys = Keys { conn: conn.clone(), root, grabbed: Mutex::new(Vec::new()), pressed: Arc::new(pressed) };
    if KEYS.set(keys).is_err() {
        return true;
    }
    let spawned = thread::Builder::new().name("trailmix-hotkeys".into()).spawn(move || {
        let mut last: Option<(i32, Instant)> = None;
        loop {
            let event = match conn.wait_for_event() {
                Ok(event) => event,
                Err(_) => return, // the X server went away
            };
            let Event::KeyPress(press) = event else { continue };
            let Some(keys) = KEYS.get() else { continue };
            let state = u16::from(press.state) & !(LOCK | MOD2);
            let id = keys.grabbed.lock().unwrap_or_else(|e| e.into_inner()).iter().find(|(_, code, mods)| *code == press.detail && *mods == state).map(|g| g.0);
            let Some(id) = id else { continue };
            // A held key repeats: one press is one press.
            if let Some((was, at)) = last {
                if was == id && at.elapsed() < Duration::from_millis(600) {
                    last = Some((id, Instant::now()));
                    continue;
                }
            }
            last = Some((id, Instant::now()));
            let pressed = keys.pressed.clone();
            thread::spawn(move || pressed(id));
        }
    });
    spawned.is_ok()
}

/// Grabs a combination on `conn` with every mix of Caps Lock and Num Lock. False if another app has it (and
/// then nothing of it is kept).
fn grab(conn: &RustConnection, root: Window, code: u8, mods: u16) -> bool {
    for (n, extra) in IGNORED.iter().enumerate() {
        let ok = conn
            .grab_key(true, root, ModMask::from(mods | extra), code, GrabMode::ASYNC, GrabMode::ASYNC)
            .map(|cookie| cookie.check().is_ok())
            .unwrap_or(false);
        if !ok {
            for undo in &IGNORED[..n] {
                let _ = conn.ungrab_key(code, root, ModMask::from(mods | undo));
            }
            let _ = conn.flush();
            return false;
        }
    }
    let _ = conn.flush();
    true
}

fn ungrab(conn: &RustConnection, root: Window, code: u8, mods: u16) {
    for extra in IGNORED {
        let _ = conn.ungrab_key(code, root, ModMask::from(mods | extra));
    }
    let _ = conn.flush();
}

/// Grabs exactly these shortcuts (none: lets them all go). Returns the ids of those another app has.
pub fn register(wanted: &[(i32, Shortcut)]) -> Vec<i32> {
    let Some(keys) = KEYS.get() else { return wanted.iter().map(|(id, _)| *id).collect() };
    let mut grabbed = keys.grabbed.lock().unwrap_or_else(|e| e.into_inner());
    for (_, code, mods) in grabbed.drain(..) {
        ungrab(&keys.conn, keys.root, code, mods);
    }
    let mut taken = Vec::new();
    for (id, shortcut) in wanted {
        let code = keysym(shortcut.key).and_then(|sym| keycode(&keys.conn, sym));
        let mods = x_mods(shortcut.mods);
        match code {
            Some(code) if grab(&keys.conn, keys.root, code, mods) => grabbed.push((*id, code, mods)),
            _ => taken.push(*id),
        }
    }
    taken
}

/// Whether a shortcut is free: grabbed and let go at once on a connection of its own. (Grabbed by this app
/// already counts as not free.)
pub fn is_free(shortcut: Shortcut) -> bool {
    let Some((conn, root)) = connect() else { return false };
    let Some(code) = keysym(shortcut.key).and_then(|sym| keycode(&conn, sym)) else { return false };
    let mods = x_mods(shortcut.mods);
    if grab(&conn, root, code, mods) {
        ungrab(&conn, root, code, mods);
        true
    } else {
        false
    }
}

const KEY_PRESS: u8 = 2;
const KEY_RELEASE: u8 = 3;
const BUTTON_PRESS: u8 = 4;
const BUTTON_RELEASE: u8 = 5;
const MOTION: u8 = 6;

fn xtest(conn: &RustConnection, root: Window, kind: u8, detail: u8, x: i16, y: i16) -> bool {
    conn.xtest_fake_input(kind, detail, 0, root, x, y, 0).map(|c| c.check().is_ok()).unwrap_or(false)
}

/// Types the shortcut on the keyboard, as you would (XTest, for the self-test). False without XTest.
pub fn type_keys(shortcut: Shortcut) -> bool {
    let Some((conn, root)) = connect() else { return false };
    let held: Vec<u32> = [(MOD_CONTROL, 0xFFE3), (MOD_ALT, 0xFFE9), (MOD_SHIFT, 0xFFE1), (MOD_WIN, 0xFFEB)]
        .iter()
        .filter(|(flag, _)| shortcut.mods & flag != 0)
        .map(|(_, sym)| *sym)
        .collect();
    let Some(key) = keysym(shortcut.key).and_then(|sym| keycode(&conn, sym)) else { return false };
    let Some(held) = held.iter().map(|sym| keycode(&conn, *sym)).collect::<Option<Vec<u8>>>() else { return false };
    let mut ok = true;
    for code in &held {
        ok &= xtest(&conn, root, KEY_PRESS, *code, 0, 0);
    }
    ok &= xtest(&conn, root, KEY_PRESS, key, 0, 0);
    ok &= xtest(&conn, root, KEY_RELEASE, key, 0, 0);
    for code in held.iter().rev() {
        ok &= xtest(&conn, root, KEY_RELEASE, *code, 0, 0);
    }
    let _ = conn.flush();
    ok
}

/// Clicks the mouse at a point on the screen (XTest, for the self-test).
pub fn click_at(x: i32, y: i32) -> bool {
    if super::session() != super::Session::X11 {
        return false;
    }
    let Some((conn, root)) = connect() else { return false };
    let ok = xtest(&conn, root, MOTION, 0, x as i16, y as i16)
        && xtest(&conn, root, BUTTON_PRESS, 1, 0, 0)
        && xtest(&conn, root, BUTTON_RELEASE, 1, 0, 0);
    let _ = conn.flush();
    ok
}

/// Delivers a press of shortcut `id` as X would, for when there's no keyboard to type on.
pub fn post_press(id: i32) -> bool {
    let Some(keys) = KEYS.get() else { return false };
    let pressed = keys.pressed.clone();
    thread::spawn(move || pressed(id));
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_map_to_x_and_back() {
        assert_eq!(keysym(b'R' as u32), Some(b'r' as u32));
        assert_eq!(keysym(0x78), Some(0xFFC6)); // F9
        assert_eq!(vk_of_keysym(b'r' as u32), Some(b'R' as u32));
        assert_eq!(vk_of_keysym(b'R' as u32), Some(b'R' as u32));
        assert_eq!(vk_of_keysym(0xFFC6), Some(0x78));
        assert_eq!(vk_of_keysym(0xFE03), None); // ISO_Level3_Shift: no shortcut key
        let mods = MOD_CONTROL | MOD_ALT | MOD_SHIFT;
        assert_eq!(x_mods(mods), CONTROL | MOD1 | SHIFT);
        assert_eq!(mods_of(x_mods(mods | MOD_WIN)), mods | MOD_WIN);
    }
}
