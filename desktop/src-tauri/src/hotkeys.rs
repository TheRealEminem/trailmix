//! Global keyboard shortcuts (HotKeys.swift): Ctrl+Alt+Shift+R starts and stops a recording and
//! Ctrl+Alt+Shift+M marks a moment, from any app. RegisterHotKey on a thread of their own, which gets
//! WM_HOTKEY when one is pressed; a shortcut another app already has can't be registered, and is reported.

use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::card;
use crate::gui::*;
use crate::model::{Model, Phase, Snapshot};

pub const MOD_ALT: u32 = 0x1;
pub const MOD_CONTROL: u32 = 0x2;
pub const MOD_SHIFT: u32 = 0x4;
pub const MOD_WIN: u32 = 0x8;
const MOD_NOREPEAT: u32 = 0x4000;

/// The ids the two shortcuts are registered under.
pub const RECORD: i32 = 1;
pub const MARK: i32 = 2;

const VK_SHIFT: u32 = 0x10;
const VK_CONTROL: u32 = 0x11;
const VK_MENU: u32 = 0x12;
pub const VK_ESCAPE: u32 = 0x1B;
const VK_LWIN: u32 = 0x5B;
const VK_RWIN: u32 = 0x5C;
const VK_F1: u32 = 0x70;
const VK_F12: u32 = 0x7B;
const VK_F24: u32 = 0x87;

/// A shortcut: a virtual-key code and the MOD_* flags RegisterHotKey takes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Shortcut {
    pub key: u32,
    pub mods: u32,
}

impl Shortcut {
    pub const RECORD_DEFAULT: Shortcut = Shortcut { key: b'R' as u32, mods: MOD_CONTROL | MOD_ALT | MOD_SHIFT };
    pub const MARK_DEFAULT: Shortcut = Shortcut { key: b'M' as u32, mods: MOD_CONTROL | MOD_ALT | MOD_SHIFT };

    /// From a key press, if it makes a sensible global shortcut: Ctrl, Alt or Win with a key, or a function
    /// key on its own (not F12, which Windows keeps for debuggers). Shift alone would take a capital letter.
    pub fn from_key(key: u32, mods: u32) -> Option<Shortcut> {
        if is_modifier(key) || key == VK_ESCAPE || key_name(key).is_none() {
            return None;
        }
        let function = (VK_F1..=VK_F24).contains(&key);
        if mods & (MOD_CONTROL | MOD_ALT | MOD_WIN) == 0 && !(function && key != VK_F12) {
            return None;
        }
        Some(Shortcut { key, mods: mods & (MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_WIN) })
    }

    /// In Windows' own notation: "Ctrl+Alt+Shift+R", "Win+Shift+F9".
    pub fn display(&self) -> String {
        let mut parts: Vec<String> = Vec::new();
        for (flag, name) in [(MOD_WIN, "Win"), (MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift")] {
            if self.mods & flag != 0 {
                parts.push(name.into());
            }
        }
        parts.push(key_name(self.key).unwrap_or_else(|| format!("Key {}", self.key)));
        parts.join("+")
    }
}

fn is_modifier(key: u32) -> bool {
    matches!(key, VK_SHIFT | VK_CONTROL | VK_MENU | VK_LWIN | VK_RWIN | 0xA0..=0xA5)
}

/// The key's name as Windows writes it, or None for keys a shortcut can't use.
pub fn key_name(key: u32) -> Option<String> {
    let named = match key {
        0x08 => "Backspace",
        0x09 => "Tab",
        0x0D => "Enter",
        0x13 => "Pause",
        0x20 => "Space",
        0x21 => "Page Up",
        0x22 => "Page Down",
        0x23 => "End",
        0x24 => "Home",
        0x25 => "Left",
        0x26 => "Up",
        0x27 => "Right",
        0x28 => "Down",
        0x2C => "PrtScn",
        0x2D => "Insert",
        0x2E => "Delete",
        0x6A => "Num *",
        0x6B => "Num +",
        0x6D => "Num -",
        0x6E => "Num .",
        0x6F => "Num /",
        _ => "",
    };
    if !named.is_empty() {
        return Some(named.into());
    }
    match key {
        0x30..=0x39 | 0x41..=0x5A => Some(char::from(key as u8).to_string()),
        0x60..=0x69 => Some(format!("Num {}", key - 0x60)),
        VK_F1..=VK_F24 => Some(format!("F{}", key - VK_F1 + 1)),
        0xBA..=0xC0 | 0xDB..=0xDF | 0xE2 => {
            // Punctuation: whatever the key shows on this keyboard layout (MAPVK_VK_TO_CHAR).
            let mapped = unsafe { MapVirtualKeyW(key, 2) } & 0x7FFF;
            match char::from_u32(mapped).filter(|c| !c.is_control() && *c != '\0') {
                Some(c) => Some(c.to_uppercase().to_string()),
                None => fallback_punctuation(key).map(String::from),
            }
        }
        _ => None,
    }
}

fn fallback_punctuation(key: u32) -> Option<&'static str> {
    Some(match key {
        0xBA => ";",
        0xBB => "=",
        0xBC => ",",
        0xBD => "-",
        0xBE => ".",
        0xBF => "/",
        0xC0 => "`",
        0xDB => "[",
        0xDC => "\\",
        0xDD => "]",
        0xDE => "'",
        _ => return None,
    })
}

/// The modifier keys held right now, as MOD_* flags (for a key press in a window of ours).
pub fn mods_held() -> u32 {
    let down = |vk: u32| unsafe { GetKeyState(vk as i32) } < 0;
    let mut mods = 0;
    if down(VK_CONTROL) {
        mods |= MOD_CONTROL;
    }
    if down(VK_MENU) {
        mods |= MOD_ALT;
    }
    if down(VK_SHIFT) {
        mods |= MOD_SHIFT;
    }
    if down(VK_LWIN) || down(VK_RWIN) {
        mods |= MOD_WIN;
    }
    mods
}

// MARK: The hotkey thread

struct Request {
    wanted: Vec<(i32, Shortcut)>,
    reply: mpsc::Sender<Vec<i32>>,
}

struct HotKeyThread {
    id: u32,
    requests: Mutex<mpsc::Sender<Request>>,
}

static THREAD: OnceLock<HotKeyThread> = OnceLock::new();
static TAKEN: Mutex<Vec<i32>> = Mutex::new(Vec::new());

/// Starts the thread the shortcuts are registered on; `pressed(id)` is called (on a thread of its own)
/// whenever one is pressed. Once per process.
pub fn start(pressed: impl Fn(i32) + Send + Sync + 'static) -> bool {
    if THREAD.get().is_some() {
        return true;
    }
    let pressed: Arc<dyn Fn(i32) + Send + Sync> = Arc::new(pressed);
    let (ready_tx, ready_rx) = mpsc::channel::<u32>();
    let (tx, rx) = mpsc::channel::<Request>();
    let spawned = thread::Builder::new().name("trailmix-hotkeys".into()).spawn(move || unsafe {
        let mut msg = Msg::zeroed();
        PeekMessageW(&mut msg, 0, WM_USER, WM_USER, PM_NOREMOVE); // makes this thread's message queue
        let _ = ready_tx.send(GetCurrentThreadId());
        let mut registered: Vec<i32> = Vec::new();
        while GetMessageW(&mut msg, 0, 0, 0) > 0 {
            match msg.message {
                WM_HOTKEY => {
                    let id = msg.wparam as i32;
                    let pressed = pressed.clone();
                    thread::spawn(move || (*pressed)(id));
                }
                WM_APP => {
                    while let Ok(request) = rx.try_recv() {
                        for id in registered.drain(..) {
                            UnregisterHotKey(0, id);
                        }
                        let mut taken = Vec::new();
                        for (id, shortcut) in &request.wanted {
                            if RegisterHotKey(0, *id, shortcut.mods | MOD_NOREPEAT, shortcut.key) != 0 {
                                registered.push(*id);
                            } else {
                                taken.push(*id);
                            }
                        }
                        let _ = request.reply.send(taken);
                    }
                }
                _ => {}
            }
        }
    });
    if spawned.is_err() {
        return false;
    }
    let Ok(id) = ready_rx.recv_timeout(Duration::from_secs(5)) else { return false };
    THREAD.set(HotKeyThread { id, requests: Mutex::new(tx) }).is_ok()
}

/// Registers exactly these shortcuts (none: pauses them, while a new one is being typed). Returns the ids
/// of those that couldn't be registered, usually because another app has them.
pub fn register(wanted: &[(i32, Shortcut)]) -> Vec<i32> {
    let Some(thread) = THREAD.get() else { return wanted.iter().map(|(id, _)| *id).collect() };
    let (reply_tx, reply_rx) = mpsc::channel();
    let request = Request { wanted: wanted.to_vec(), reply: reply_tx };
    if thread.requests.lock().unwrap_or_else(|e| e.into_inner()).send(request).is_err() {
        return wanted.iter().map(|(id, _)| *id).collect();
    }
    unsafe { PostThreadMessageW(thread.id, WM_APP, 0, 0) };
    let taken = reply_rx.recv_timeout(Duration::from_secs(5)).unwrap_or_else(|_| wanted.iter().map(|(id, _)| *id).collect());
    *TAKEN.lock().unwrap_or_else(|e| e.into_inner()) = taken.clone();
    taken
}

/// The shortcuts the last `register` couldn't have.
pub fn taken() -> Vec<i32> {
    TAKEN.lock().unwrap_or_else(|e| e.into_inner()).clone()
}

/// Whether a shortcut is free: registered and let go at once from a thread of its own. (Registered here
/// already counts as not free: a combination can only be registered once.)
pub fn is_free(shortcut: Shortcut) -> bool {
    thread::spawn(move || unsafe {
        let ok = RegisterHotKey(0, 0xBFF0, shortcut.mods | MOD_NOREPEAT, shortcut.key) != 0;
        if ok {
            UnregisterHotKey(0, 0xBFF0);
        }
        ok
    })
    .join()
    .unwrap_or(false)
}

/// Types the shortcut on the keyboard, as you would (for the self-test). False if Windows didn't take the
/// keystrokes (no interactive desktop).
pub fn type_keys(shortcut: Shortcut) -> bool {
    let key = |vk: u32, up: bool| {
        let extended = matches!(vk, 0x21..=0x28 | 0x2D | 0x2E | VK_LWIN | VK_RWIN | 0x6F);
        let mut flags = if up { KEYEVENTF_KEYUP } else { 0 };
        if extended {
            flags |= KEYEVENTF_EXTENDEDKEY;
        }
        Input { kind: INPUT_KEYBOARD, u: InputUnion { ki: KeybdInput { vk: vk as u16, scan: 0, flags, time: 0, extra: 0 } } }
    };
    let held: Vec<u32> = [(MOD_CONTROL, VK_CONTROL), (MOD_ALT, VK_MENU), (MOD_SHIFT, VK_SHIFT), (MOD_WIN, VK_LWIN)]
        .iter()
        .filter(|(flag, _)| shortcut.mods & flag != 0)
        .map(|(_, vk)| *vk)
        .collect();
    let mut inputs: Vec<Input> = held.iter().map(|vk| key(*vk, false)).collect();
    inputs.push(key(shortcut.key, false));
    inputs.push(key(shortcut.key, true));
    inputs.extend(held.iter().rev().map(|vk| key(*vk, true)));
    let sent = unsafe { SendInput(inputs.len() as u32, inputs.as_ptr(), std::mem::size_of::<Input>() as i32) };
    sent == inputs.len() as u32
}

/// Delivers a press of shortcut `id` as Windows would (WM_HOTKEY), for when there's no keyboard to type on.
pub fn post_press(id: i32) -> bool {
    match THREAD.get() {
        Some(thread) => unsafe { PostThreadMessageW(thread.id, WM_HOTKEY, id as usize, 0) != 0 },
        None => false,
    }
}

/// Presses so far, for the self-test.
pub static PRESSES: AtomicU32 = AtomicU32::new(0);

// MARK: What the shortcuts do

/// Registers the two shortcuts from the settings. Returns the ids of those another app has.
pub fn apply(model: &Model) -> Vec<i32> {
    let prefs = model.prefs();
    register(&[(RECORD, prefs.shortcut_record), (MARK, prefs.shortcut_mark)])
}

fn wait_for(model: &Model, seconds: u64, done: impl Fn(&Snapshot) -> bool) -> Snapshot {
    let deadline = Instant::now() + Duration::from_secs(seconds);
    loop {
        let snapshot = model.snapshot();
        if done(&snapshot) || Instant::now() >= deadline {
            return snapshot;
        }
        thread::sleep(Duration::from_millis(100));
    }
}

/// A press (HelperModel's toggle and markMoment): Start / stop stops whatever is recording (here or in the
/// Trailmix window), otherwise starts; Mark flags the moment. With `notes`, a card in the corner says what
/// happened (the notification the Mac shows when its menu isn't open), for when the window isn't in front.
pub fn act(model: &Arc<Model>, id: i32, notes: bool) {
    PRESSES.fetch_add(1, Ordering::SeqCst);
    let prefs = model.prefs();
    let (record, mark) = (prefs.shortcut_record.display(), prefs.shortcut_mark.display());
    let before = model.snapshot();
    let live = before.phase == Phase::Recording || before.other.is_some();
    match id {
        RECORD => {
            if matches!(before.phase, Phase::Starting | Phase::Stopping) {
                return;
            }
            model.toggle();
            if !notes {
                return;
            }
            if live {
                wait_for(model, 15, |s| s.phase == Phase::Idle && s.other.is_none());
                card::note("Recording stopped", "The transcript and notes are on their way.", 4);
            } else {
                let now = wait_for(model, 40, |s| matches!(s.phase, Phase::Recording | Phase::Idle));
                if now.phase == Phase::Recording {
                    card::note("Recording", &format!("{record} stops it, {mark} marks a moment."), 4);
                } else if let Some(error) = now.error {
                    card::note("Couldn't start recording", &error, 8);
                }
            }
        }
        MARK => {
            if live {
                model.mark_moment();
                if notes {
                    card::note("Moment marked", "", 2);
                }
            } else if notes {
                card::note("Nothing is recording", &format!("{record} starts a recording."), 3);
            }
        }
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shortcuts_read_the_windows_way() {
        assert_eq!(Shortcut::RECORD_DEFAULT.display(), "Ctrl+Alt+Shift+R");
        assert_eq!(Shortcut::MARK_DEFAULT.display(), "Ctrl+Alt+Shift+M");
        assert_eq!(Shortcut { key: 0x78, mods: MOD_WIN | MOD_SHIFT }.display(), "Win+Shift+F9");
        assert_eq!(Shortcut { key: 0x20, mods: MOD_CONTROL | MOD_ALT }.display(), "Ctrl+Alt+Space");
        assert_eq!(Shortcut { key: 0x63, mods: MOD_CONTROL }.display(), "Ctrl+Num 3");
        assert_eq!(Shortcut { key: 0x22, mods: MOD_ALT }.display(), "Alt+Page Down");
    }

    #[test]
    fn only_sensible_shortcuts_are_taken() {
        let r = b'R' as u32;
        assert!(Shortcut::from_key(r, MOD_CONTROL | MOD_ALT).is_some());
        assert!(Shortcut::from_key(r, MOD_WIN | MOD_SHIFT).is_some());
        assert!(Shortcut::from_key(r, 0).is_none(), "a plain letter would stop you typing it");
        assert!(Shortcut::from_key(r, MOD_SHIFT).is_none(), "Shift+R is a capital R");
        assert!(Shortcut::from_key(0x78, 0).is_some(), "F9 on its own is fine");
        assert!(Shortcut::from_key(0x78, MOD_SHIFT).is_some());
        assert!(Shortcut::from_key(VK_F12, 0).is_none(), "F12 is the debugger's");
        assert!(Shortcut::from_key(VK_CONTROL, MOD_CONTROL).is_none(), "a modifier on its own isn't a shortcut");
        assert!(Shortcut::from_key(0xA2, MOD_CONTROL).is_none());
        assert!(Shortcut::from_key(VK_ESCAPE, MOD_CONTROL).is_none());
        assert!(Shortcut::from_key(0xFF, MOD_CONTROL).is_none(), "keys without a name aren't offered");
    }

    #[test]
    fn shortcuts_are_saved_as_numbers() {
        let text = serde_json::to_string(&Shortcut::RECORD_DEFAULT).unwrap();
        assert_eq!(text, r#"{"key":82,"mods":7}"#);
        assert_eq!(serde_json::from_str::<Shortcut>(&text).unwrap(), Shortcut::RECORD_DEFAULT);
    }
}
