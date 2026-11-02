//! A small card in the corner of the screen, above everything (card.rs on Windows, the panel in
//! CallReminder.swift): "Zoom call started. Record it?" with Record and Not now, a note after a shortcut was
//! pressed, or "Type the new shortcut". A GTK window (GTK is what Tauri's window is made of here) with no
//! frame, kept above the others, that never takes the focus from the call (it doesn't accept focus), except the
//! one for typing a shortcut, which needs the keyboard. One card at a time: a new one replaces the last. It goes
//! away by itself after a while.
//!
//! On X11 it sits in the bottom right corner; on Wayland the desktop decides where it goes (a program can't
//! place its windows there), usually the middle of the screen.

use std::cell::RefCell;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::thread;

use gtk::glib;
use gtk::glib::translate::IntoGlib;
use gtk::prelude::*;

use crate::hotkeys::Shortcut;
use crate::linux::keys::{mods_of, vk_of_keysym};
use crate::linux::{on_main, on_main_wait};

#[derive(Clone, Debug, Default)]
pub struct Card {
    pub title: String,
    pub detail: String,
    /// Left to right; with more than one, the first is the main one.
    pub buttons: Vec<String>,
    /// A small text button under them ("Don't ask for Zoom").
    pub link: Option<String>,
    /// Gone by itself after this long (0: stays until answered).
    pub seconds: u32,
    /// Takes the keyboard, for typing a new shortcut (centred on the screen). Every other card never takes focus.
    pub keys: bool,
    /// A sound when it appears (the desktop's bell, where it has one).
    pub sound: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Answer {
    Button(usize),
    Link,
    /// A shortcut typed on a `keys` card.
    Key(Shortcut),
    /// Not now: timed out, Esc, replaced by another card, or hidden.
    Dismissed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Target {
    Button(usize),
    Link,
}

type OnAnswer = Box<dyn FnOnce(u64, Answer) + Send>;

/// The card up now, on GTK's main thread.
struct Live {
    id: u64,
    window: gtk::Window,
    buttons: Vec<gtk::Button>,
    link: Option<gtk::Button>,
    on_answer: Option<OnAnswer>,
    timer: Option<glib::SourceId>,
}

thread_local! {
    static LIVE: RefCell<Option<Live>> = const { RefCell::new(None) };
}

/// (id, title, whether it asks something) of the card that's up or on its way, for any thread.
static CURRENT: Mutex<Option<(u64, String, bool)>> = Mutex::new(None);
static NEXT: AtomicU64 = AtomicU64::new(1);

fn current_id() -> Option<u64> {
    CURRENT.lock().unwrap_or_else(|e| e.into_inner()).as_ref().map(|c| c.0)
}

/// Shows a card (replacing the one up, if any); `on_answer(id, answer)` is called once it's gone, on a thread of
/// its own. Returns the card's id.
pub fn show(card: Card, on_answer: impl FnOnce(u64, Answer) + Send + 'static) -> u64 {
    let id = NEXT.fetch_add(1, Ordering::SeqCst);
    *CURRENT.lock().unwrap_or_else(|e| e.into_inner()) = Some((id, card.title.clone(), !card.buttons.is_empty()));
    let on_answer: OnAnswer = Box::new(on_answer);
    on_main(move || open(id, card, on_answer));
    id
}

/// A note that needs no answer ("Recording", "Moment marked"), unless a question is up: that stays.
pub fn note(title: &str, detail: &str, seconds: u32) {
    if CURRENT.lock().unwrap_or_else(|e| e.into_inner()).as_ref().map(|c| c.2).unwrap_or(false) {
        return;
    }
    let card = Card { title: title.into(), detail: detail.into(), seconds, ..Card::default() };
    show(card, |_, _| {});
}

/// Takes card `id` away (if it's still the one up).
pub fn hide(id: u64) {
    on_main(move || finish(id, Answer::Dismissed));
}

/// The card up now, for the self-test.
#[derive(Clone, Debug)]
pub struct Shown {
    pub id: u64,
    pub title: String,
    pub visible: bool,
    /// Whether it can take the focus (only the one for typing a shortcut should).
    pub accepts_focus: bool,
    /// Whether it has the keyboard now.
    pub focused: bool,
    pub keep_above: bool,
}

pub fn current() -> Option<Shown> {
    on_main_wait(|| {
        LIVE.with(|live| {
            let live = live.borrow();
            let l = live.as_ref()?;
            let title = l.window.title().map(|t| t.to_string()).unwrap_or_default();
            Some(Shown {
                id: l.id,
                title,
                visible: l.window.is_visible(),
                accepts_focus: l.window.accepts_focus(),
                focused: l.window.has_toplevel_focus() || l.window.is_active(),
                keep_above: true,
            })
        })
    })
    .flatten()
}

/// Clicks a button (or the link) of card `id`: with the real mouse (`real`, X11's XTest), or by clicking the
/// button directly. False if there's no such card or button, or the click couldn't be made.
pub fn press(id: u64, target: Target, real: bool) -> bool {
    let point = on_main_wait(move || {
        LIVE.with(|live| {
            let live = live.borrow();
            let l = live.as_ref().filter(|l| l.id == id)?;
            let button = match target {
                Target::Button(i) => l.buttons.get(i)?.clone(),
                Target::Link => l.link.clone()?,
            };
            if !real {
                button.clicked();
                return Some((0, 0));
            }
            let (_, ox, oy) = l.window.window()?.origin();
            let (x, y) = button.translate_coordinates(&l.window, 0, 0)?;
            let a = button.allocation();
            Some((ox + x + a.width() / 2, oy + y + a.height() / 2))
        })
    })
    .flatten();
    match point {
        Some(_) if !real => true,
        Some((x, y)) => crate::linux::keys::click_at(x, y),
        None => false,
    }
}

/// Draws the card up now to a PNG, to look at.
pub fn save_png(path: &Path) -> bool {
    let path = path.to_path_buf();
    on_main_wait(move || {
        LIVE.with(|live| {
            let live = live.borrow();
            let Some(l) = live.as_ref() else { return false };
            let (w, h) = (l.window.allocated_width(), l.window.allocated_height());
            let Ok(surface) = cairo::ImageSurface::create(cairo::Format::ARgb32, w.max(1), h.max(1)) else { return false };
            {
                let Ok(cr) = cairo::Context::new(&surface) else { return false };
                l.window.draw(&cr);
            }
            match std::fs::File::create(&path) {
                Ok(mut file) => surface.write_to_png(&mut file).is_ok(),
                Err(_) => false,
            }
        })
    })
    .unwrap_or(false)
}

// MARK: The window (all on GTK's main thread)

const STYLE: &str = "
window.trailmix-card { background-color: @theme_base_color; border: 1px solid alpha(@theme_fg_color, 0.18); border-radius: 10px; }
.trailmix-card .title { font-weight: bold; font-size: 1.05em; }
.trailmix-card .detail { opacity: 0.8; }
.trailmix-card button.link { padding: 0; min-height: 0; opacity: 0.75; }
";

/// Ends card `id` (if it's the one up) with `answer`.
fn finish(id: u64, answer: Answer) {
    let ended = LIVE.with(|live| {
        let mut live = live.borrow_mut();
        if live.as_ref().map(|l| l.id) != Some(id) {
            return None;
        }
        live.take()
    });
    {
        let mut current = CURRENT.lock().unwrap_or_else(|e| e.into_inner());
        if current.as_ref().map(|c| c.0) == Some(id) {
            *current = None;
        }
    }
    let Some(mut ended) = ended else { return };
    if let Some(timer) = ended.timer.take() {
        timer.remove();
    }
    ended.window.close();
    if let Some(on_answer) = ended.on_answer.take() {
        thread::spawn(move || on_answer(id, answer));
    }
}

fn open(id: u64, card: Card, on_answer: OnAnswer) {
    // The card before this one goes, as if dismissed.
    let previous = LIVE.with(|live| live.borrow().as_ref().map(|l| l.id));
    if let Some(previous) = previous {
        finish(previous, Answer::Dismissed);
    }
    if current_id() != Some(id) {
        // Hidden or replaced before it got here.
        thread::spawn(move || on_answer(id, Answer::Dismissed));
        return;
    }
    let window = gtk::Window::new(gtk::WindowType::Toplevel);
    window.set_title(&card.title);
    window.set_decorated(false);
    window.set_resizable(false);
    window.set_skip_taskbar_hint(true);
    window.set_skip_pager_hint(true);
    window.set_keep_above(true);
    window.set_type_hint(if card.keys { gtk::gdk::WindowTypeHint::Dialog } else { gtk::gdk::WindowTypeHint::Notification });
    window.set_accept_focus(card.keys);
    window.set_focus_on_map(card.keys);
    window.set_default_size(360, -1);
    window.style_context().add_class("trailmix-card");
    let css = gtk::CssProvider::new();
    if css.load_from_data(STYLE.as_bytes()).is_ok() {
        if let Some(screen) = gtk::gdk::Screen::default() {
            gtk::StyleContext::add_provider_for_screen(&screen, &css, gtk::STYLE_PROVIDER_PRIORITY_APPLICATION);
        }
    }

    let column = gtk::Box::new(gtk::Orientation::Vertical, 6);
    column.set_margin_top(14);
    column.set_margin_bottom(14);
    column.set_margin_start(16);
    column.set_margin_end(16);
    let title = gtk::Label::new(Some(&card.title));
    title.style_context().add_class("title");
    title.set_xalign(0.0);
    title.set_line_wrap(true);
    title.set_max_width_chars(42);
    column.pack_start(&title, false, false, 0);
    if !card.detail.is_empty() {
        let detail = gtk::Label::new(Some(&card.detail));
        detail.style_context().add_class("detail");
        detail.set_xalign(0.0);
        detail.set_line_wrap(true);
        detail.set_max_width_chars(46);
        column.pack_start(&detail, false, false, 0);
    }
    let mut buttons = Vec::new();
    if !card.buttons.is_empty() {
        let row = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        row.set_margin_top(6);
        for (i, text) in card.buttons.iter().enumerate() {
            let button = gtk::Button::with_label(text);
            if i == 0 && card.buttons.len() > 1 {
                button.style_context().add_class("suggested-action");
            }
            button.set_can_focus(card.keys);
            button.connect_clicked(move |_| finish(id, Answer::Button(i)));
            row.pack_start(&button, false, false, 0);
            buttons.push(button);
        }
        column.pack_start(&row, false, false, 0);
    }
    let link = card.link.as_ref().map(|text| {
        let button = gtk::Button::with_label(text);
        button.set_relief(gtk::ReliefStyle::None);
        button.style_context().add_class("link");
        button.set_halign(gtk::Align::Start);
        button.set_can_focus(false);
        button.connect_clicked(move |_| finish(id, Answer::Link));
        column.pack_start(&button, false, false, 0);
        button
    });
    window.add(&column);

    if card.keys {
        window.set_position(gtk::WindowPosition::Center);
        window.connect_key_press_event(move |_, event| {
            let sym: u32 = event.keyval().into_glib();
            if sym == 0xFF1B {
                finish(id, Answer::Dismissed);
                return glib::Propagation::Stop;
            }
            let mods = mods_of(event.state().bits() as u16);
            if let Some(shortcut) = vk_of_keysym(sym).and_then(|vk| Shortcut::from_key(vk, mods)) {
                finish(id, Answer::Key(shortcut));
            }
            glib::Propagation::Stop
        });
    }
    window.connect_delete_event(move |_, _| {
        finish(id, Answer::Dismissed);
        glib::Propagation::Proceed
    });
    window.show_all();
    if !card.keys {
        place_in_corner(&window);
    }
    if card.sound {
        if let Some(display) = gtk::gdk::Display::default() {
            display.beep();
        }
    }
    let timer = (card.seconds > 0).then(|| {
        glib::timeout_add_seconds_local(card.seconds, move || {
            // (The timer is done once this returns: forget it, so finishing doesn't remove it again.)
            LIVE.with(|live| {
                if let Some(l) = live.borrow_mut().as_mut().filter(|l| l.id == id) {
                    l.timer = None;
                }
            });
            finish(id, Answer::Dismissed);
            glib::ControlFlow::Break
        })
    });
    LIVE.with(|live| *live.borrow_mut() = Some(Live { id, window, buttons, link, on_answer: Some(on_answer), timer }));
}

/// Bottom right of the screen the pointer is on, inside its work area (clear of panels).
fn place_in_corner(window: &gtk::Window) {
    let Some(display) = gtk::gdk::Display::default() else { return };
    let monitor = display.primary_monitor().or_else(|| display.monitor(0));
    let Some(monitor) = monitor else { return };
    let area = monitor.workarea();
    let (w, h) = window.size();
    window.move_(area.x() + area.width() - w - 16, area.y() + area.height() - h - 16);
}
