//! The recording card on Linux (catchup_card.rs on Windows; catchup.rs has what it shows): the live draft, the
//! moments flagged, and Catch me up, the way the Mac menu shows them while a meeting records. A GTK window like the
//! call reminder's card (card.rs here): no frame, kept above the others, never taking the focus from the call (it
//! doesn't accept focus). The one exception is the box to type a question in: clicking it lets the card take the
//! keyboard, and Enter (or Esc) hands it back to the window that had it (on X11; on Wayland the desktop decides).
//! It stays until it's closed, or the recording ends.

use std::cell::RefCell;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;

use gtk::glib;
use gtk::glib::translate::IntoGlib;
use gtk::pango;
use gtk::prelude::*;

use crate::catchup::{self, AnswerView, DraftView, Line, QuickView, Target, View, ASK_PLACEHOLDER, READING};
use crate::linux::{on_main, on_main_wait};

/// Open, or on its way: for any thread.
static OPEN: AtomicBool = AtomicBool::new(false);

struct Live {
    window: gtk::Window,
    title: gtk::Label,
    clock: gtk::Label,
    arrow: gtk::Label,
    /// The parts made afresh when what they show changes: the live draft, what's above the box to type in (why
    /// nothing can be asked, or the quick questions), and what's below it (notes, the answer, who answers). The box
    /// itself stays, so typing in it isn't interrupted.
    draft: gtk::Box,
    above: gtk::Box,
    below: gtk::Box,
    entry: gtk::Entry,
    fixed: Vec<(Target, gtk::Button)>,
    above_targets: Vec<(Target, gtk::Button)>,
    below_targets: Vec<(Target, gtk::Button)>,
    view: Option<View>,
    typing: bool,
    /// While typing (X11): the window that had the keyboard (or none, or the pointer's), to hand it back to.
    before: Option<u32>,
    height: i32,
}

impl Live {
    fn button(&self, target: Target) -> Option<gtk::Button> {
        self.fixed.iter().chain(&self.above_targets).chain(&self.below_targets).find(|(t, _)| *t == target).map(|(_, b)| b.clone())
    }
}

thread_local! {
    static LIVE: RefCell<Option<Live>> = const { RefCell::new(None) };
}

/// Shows the card, or brings the one up to date.
pub fn show() {
    if OPEN.swap(true, Ordering::SeqCst) {
        refresh();
        return;
    }
    on_main(open);
}

/// Redraws it from what catch-up says now (nothing, if it isn't up).
pub fn refresh() {
    if OPEN.load(Ordering::SeqCst) {
        on_main(update);
    }
}

pub fn hide() {
    on_main(close);
}

pub fn is_open() -> bool {
    OPEN.load(Ordering::SeqCst)
}

/// The card up now, for the self-test.
#[derive(Clone, Debug)]
pub struct Shown {
    pub visible: bool,
    pub above: bool,
    /// It can't take the focus (so clicking it leaves the keyboard where it was).
    pub no_activate: bool,
    /// It has the keyboard now.
    pub focused: bool,
    /// What it says (its labels), a line each.
    pub text: String,
}

pub fn current() -> Option<Shown> {
    on_main_wait(|| {
        LIVE.with(|live| {
            let live = live.try_borrow().ok()?;
            let l = live.as_ref()?;
            let mut lines = Vec::new();
            collect_text(l.window.upcast_ref(), &mut lines);
            // On X11, whether the keyboard is the card's own: an X server with no window manager (or one where the
            // focus follows the mouse) sends the keys to whatever the pointer is over, which GTK counts as focus too.
            let focused = match (x11_id(&l.window), x11_focus()) {
                (Some(card), Some(focus)) => card == focus,
                _ => l.window.has_toplevel_focus() || l.window.is_active(),
            };
            Some(Shown { visible: l.window.is_visible(), above: true, no_activate: !l.window.accepts_focus(), focused, text: lines.join("\n") })
        })
    })
    .flatten()
}

fn collect_text(widget: &gtk::Widget, out: &mut Vec<String>) {
    if !widget.is_visible() {
        return;
    }
    if let Some(label) = widget.downcast_ref::<gtk::Label>() {
        let text = label.text().to_string();
        if !text.trim().is_empty() {
            out.push(text);
        }
        return;
    }
    if let Some(entry) = widget.downcast_ref::<gtk::Entry>() {
        out.push(entry.placeholder_text().map(|t| t.to_string()).unwrap_or_default());
        return;
    }
    if let Some(container) = widget.downcast_ref::<gtk::Container>() {
        for child in container.children() {
            collect_text(&child, out);
        }
    }
}

/// Clicks something on the card: with the real mouse (`real`, X11's XTest), or by clicking the button directly.
pub fn press(target: Target, real: bool) -> bool {
    let point = on_main_wait(move || {
        LIVE.with(|live| {
            let button = live.try_borrow().ok()?.as_ref()?.button(target)?;
            if !button.is_sensitive() {
                return None;
            }
            if !real {
                button.clicked();
                return Some((0, 0));
            }
            let window = live.try_borrow().ok()?.as_ref()?.window.clone();
            screen_center(&window, button.upcast_ref())
        })
    })
    .flatten();
    match point {
        Some(_) if !real => true,
        Some((x, y)) => crate::linux::keys::click_at(x, y),
        None => false,
    }
}

fn screen_center(window: &gtk::Window, widget: &gtk::Widget) -> Option<(i32, i32)> {
    let (_, ox, oy) = window.window()?.origin();
    let (x, y) = widget.translate_coordinates(window, 0, 0)?;
    let a = widget.allocation();
    Some((ox + x + a.width() / 2, oy + y + a.height() / 2))
}

/// Asks a question through the box. `real`: the box is clicked with the mouse first (XTest), so the card takes the
/// keyboard as it does for you; then the question goes in and Enter is pressed.
pub fn ask_typed(text: &str, real: bool) -> bool {
    if real {
        let point = on_main_wait(|| {
            LIVE.with(|live| {
                let live = live.try_borrow().ok()?;
                let l = live.as_ref()?;
                if !l.entry.is_sensitive() || !WidgetExt::is_visible(&l.entry) {
                    return None;
                }
                screen_center(&l.window, l.entry.upcast_ref())
            })
        })
        .flatten();
        let Some((x, y)) = point else { return false };
        if !crate::linux::keys::click_at(x, y) || !crate::selftest::wait_until(2.0, typing) {
            return false;
        }
    }
    let text = text.to_string();
    on_main_wait(move || {
        let entry = LIVE.with(|live| live.try_borrow().ok().and_then(|l| l.as_ref().map(|l| l.entry.clone())));
        let Some(entry) = entry.filter(|e| e.is_sensitive() && WidgetExt::is_visible(e)) else { return false };
        entry.set_text(&text);
        WidgetExt::activate(&entry);
        true
    })
    .unwrap_or(false)
}

/// Whether the box to type in has the keyboard now.
pub fn typing() -> bool {
    on_main_wait(|| LIVE.with(|live| live.try_borrow().ok().and_then(|l| l.as_ref().map(|l| l.typing)).unwrap_or(false))).unwrap_or(false)
}

/// Draws the card as it is now to a PNG, to look at.
pub fn save_png(path: &Path) -> bool {
    let path = path.to_path_buf();
    on_main_wait(move || {
        let window = LIVE.with(|live| live.try_borrow().ok().and_then(|l| l.as_ref().map(|l| l.window.clone())));
        let Some(window) = window else { return false };
        let (w, h) = (window.allocated_width(), window.allocated_height());
        let Ok(surface) = cairo::ImageSurface::create(cairo::Format::ARgb32, w.max(1), h.max(1)) else { return false };
        {
            let Ok(cr) = cairo::Context::new(&surface) else { return false };
            window.draw(&cr);
        }
        match std::fs::File::create(&path) {
            Ok(mut file) => surface.write_to_png(&mut file).is_ok(),
            Err(_) => false,
        }
    })
    .unwrap_or(false)
}

// MARK: The window (all on GTK's main thread)

const STYLE: &str = "
window.trailmix-catch-up { background-color: @theme_base_color; border: 1px solid alpha(@theme_fg_color, 0.18); border-radius: 10px; }
.trailmix-catch-up .title { font-weight: bold; }
.trailmix-catch-up .clock { font-weight: bold; font-size: 1.25em; }
.trailmix-catch-up .dot { color: #D1495B; }
.trailmix-catch-up .soft { opacity: 0.8; }
.trailmix-catch-up .faint { opacity: 0.6; }
.trailmix-catch-up .small { font-size: 0.9em; }
.trailmix-catch-up .bold { font-weight: bold; }
.trailmix-catch-up .panel { background-color: alpha(@theme_fg_color, 0.06); border-radius: 8px; padding: 10px; }
.trailmix-catch-up .warning { background-color: alpha(#E0A526, 0.18); border-radius: 8px; padding: 8px; }
.trailmix-catch-up .error { background-color: alpha(#D1495B, 0.16); border-radius: 8px; padding: 8px; }
.trailmix-catch-up button.flat-row { padding: 4px 8px; }
.trailmix-catch-up button.quick { padding: 6px 12px; }
.trailmix-catch-up button.link { padding: 0; min-height: 0; color: #2E8A60; }
";

fn open() {
    if !OPEN.load(Ordering::SeqCst) {
        return; // hidden again before it got here
    }
    if LIVE.with(|live| live.borrow().is_some()) {
        update();
        return;
    }
    let window = gtk::Window::new(gtk::WindowType::Toplevel);
    window.set_title("Trailmix: Catch me up");
    window.set_decorated(false);
    window.set_resizable(false);
    window.set_skip_taskbar_hint(true);
    window.set_skip_pager_hint(true);
    window.set_keep_above(true);
    window.set_type_hint(gtk::gdk::WindowTypeHint::Utility);
    window.set_accept_focus(false);
    window.set_focus_on_map(false);
    window.set_default_size(380, -1);
    window.style_context().add_class("trailmix-catch-up");
    let css = gtk::CssProvider::new();
    if css.load_from_data(STYLE.as_bytes()).is_ok() {
        if let Some(screen) = gtk::gdk::Screen::default() {
            gtk::StyleContext::add_provider_for_screen(&screen, &css, gtk::STYLE_PROVIDER_PRIORITY_APPLICATION);
        }
    }
    let content = gtk::Box::new(gtk::Orientation::Vertical, 6);
    content.set_margin_top(12);
    content.set_margin_bottom(12);
    content.set_margin_start(16);
    content.set_margin_end(16);
    window.add(&content);
    let mut fixed = Vec::new();

    // Title, clock, close.
    let header = row();
    header.pack_start(&label("●", &["dot"]), false, false, 0);
    let title = label("", &["title"]);
    title.set_line_wrap(false);
    title.set_ellipsize(pango::EllipsizeMode::End);
    header.pack_start(&title, true, true, 0);
    let clock = label("", &["clock"]);
    clock.set_line_wrap(false);
    header.pack_start(&clock, false, false, 0);
    let close_button = target_button(Target::Close, label("×", &["soft"]).upcast_ref(), &[], &mut fixed);
    header.pack_start(&close_button, false, false, 0);
    content.pack_start(&header, false, false, 0);

    let draft = gtk::Box::new(gtk::Orientation::Vertical, 6);
    content.pack_start(&draft, false, false, 0);

    // Catch me up ▸
    let toggle_row = row();
    toggle_row.pack_start(&label("Catch me up", &["bold"]), true, true, 0);
    let arrow = label("▸", &["soft"]);
    arrow.set_line_wrap(false);
    toggle_row.pack_start(&arrow, false, false, 0);
    let toggle = target_button(Target::Toggle, toggle_row.upcast_ref(), &["flat-row"], &mut fixed);
    content.pack_start(&toggle, false, false, 0);

    let above = gtk::Box::new(gtk::Orientation::Vertical, 6);
    content.pack_start(&above, false, false, 0);
    let entry = gtk::Entry::new();
    entry.set_placeholder_text(Some(ASK_PLACEHOLDER));
    entry.set_no_show_all(true);
    entry.connect_button_press_event(|_, _| {
        begin_typing();
        glib::Propagation::Proceed
    });
    entry.connect_activate(|entry| {
        let text = entry.text().to_string();
        entry.set_text("");
        end_typing(true);
        if !text.trim().is_empty() {
            if let Some(c) = catchup::shared() {
                thread::spawn(move || c.ask("question", &text));
            }
        }
    });
    entry.connect_key_press_event(|_, event| {
        let sym: u32 = event.keyval().into_glib();
        if sym == 0xFF1B {
            end_typing(true);
            return glib::Propagation::Stop;
        }
        glib::Propagation::Proceed
    });
    content.pack_start(&entry, false, false, 0);
    let below = gtk::Box::new(gtk::Orientation::Vertical, 6);
    content.pack_start(&below, false, false, 0);

    window.connect_focus_out_event(|_, _| {
        end_typing(false); // clicked back into the call: nothing to hand back
        glib::Propagation::Proceed
    });
    window.connect_delete_event(|window, _| {
        let ours = LIVE.with(|live| live.try_borrow().map(|l| l.as_ref().map(|l| &l.window == window).unwrap_or(false)).unwrap_or(false));
        if ours {
            close();
        }
        glib::Propagation::Proceed
    });
    // Its bottom stays in the corner as it grows and shrinks.
    window.connect_size_allocate(|window, allocation| {
        let changed = LIVE.with(|live| {
            let Ok(mut live) = live.try_borrow_mut() else { return false };
            match live.as_mut() {
                Some(l) if &l.window == window && l.height != allocation.height() => {
                    l.height = allocation.height();
                    true
                }
                _ => false,
            }
        });
        if changed {
            place_in_corner(window);
        }
    });
    LIVE.with(|live| {
        *live.borrow_mut() = Some(Live {
            window: window.clone(),
            title,
            clock,
            arrow,
            draft,
            above,
            below,
            entry,
            fixed,
            above_targets: Vec::new(),
            below_targets: Vec::new(),
            view: None,
            typing: false,
            before: None,
            height: 0,
        })
    });
    update();
    window.show_all();
    place_in_corner(&window);
}

fn close() {
    OPEN.store(false, Ordering::SeqCst);
    let ended = LIVE.with(|live| live.borrow_mut().take());
    if let Some(l) = ended {
        l.window.close();
    }
}

/// Clicked into the box: the card may take the keyboard, until Enter or Esc.
fn begin_typing() {
    let window = LIVE.with(|live| {
        let mut live = live.try_borrow_mut().ok()?;
        let l = live.as_mut()?;
        if !l.typing {
            l.typing = true;
            l.before = x11_focus();
            l.window.set_accept_focus(true);
        }
        Some((l.window.clone(), l.entry.clone()))
    });
    if let Some((window, entry)) = window {
        window.present();
        entry.grab_focus();
    }
}

/// Done typing: the card takes no focus again, and the window that had the keyboard gets it back.
fn end_typing(give_back: bool) {
    let before = LIVE.with(|live| {
        let mut live = live.try_borrow_mut().ok()?;
        let l = live.as_mut()?;
        if !l.typing {
            return None;
        }
        l.typing = false;
        l.window.set_accept_focus(false);
        Some(l.before.take())
    });
    if let Some(before) = before.flatten().filter(|_| give_back) {
        x11_set_focus(before);
    }
}

extern "C" {
    // GDK's own (libgdk-3, which GTK links): the X11 window behind a GDK one.
    fn gdk_x11_window_get_xid(window: *mut gtk::gdk::ffi::GdkWindow) -> std::os::raw::c_ulong;
}

/// The card's X11 window (X11 only).
fn x11_id(window: &gtk::Window) -> Option<u32> {
    use gtk::glib::translate::ToGlibPtr;
    if crate::linux::session() != crate::linux::Session::X11 {
        return None;
    }
    let gdk_window = window.window()?;
    let raw: *mut gtk::gdk::ffi::GdkWindow = gdk_window.to_glib_none().0;
    Some(unsafe { gdk_x11_window_get_xid(raw) } as u32)
}

/// The window with the keyboard now (X11 only).
fn x11_focus() -> Option<u32> {
    use x11rb::protocol::xproto::ConnectionExt as _;
    if crate::linux::session() != crate::linux::Session::X11 {
        return None;
    }
    let (conn, _) = x11rb::connect(None).ok()?;
    let reply = conn.get_input_focus().ok()?.reply().ok()?;
    Some(reply.focus)
}

fn x11_set_focus(window: u32) {
    use x11rb::connection::Connection as _;
    use x11rb::protocol::xproto::{ConnectionExt as _, InputFocus};
    let Ok((conn, _)) = x11rb::connect(None) else { return };
    let _ = conn.set_input_focus(InputFocus::PARENT, window, x11rb::CURRENT_TIME);
    let _ = conn.flush();
}

fn label(text: &str, classes: &[&str]) -> gtk::Label {
    let label = gtk::Label::new(Some(text));
    label.set_xalign(0.0);
    label.set_line_wrap(true);
    label.set_line_wrap_mode(pango::WrapMode::WordChar);
    label.set_max_width_chars(44);
    for class in classes {
        label.style_context().add_class(class);
    }
    label
}

fn row() -> gtk::Box {
    gtk::Box::new(gtk::Orientation::Horizontal, 6)
}

/// A flat button around `child` that does what clicking `target` does.
fn target_button(target: Target, child: &gtk::Widget, classes: &[&str], targets: &mut Vec<(Target, gtk::Button)>) -> gtk::Button {
    let b = gtk::Button::new();
    b.set_relief(gtk::ReliefStyle::None);
    b.set_can_focus(false);
    b.add(child);
    for class in classes {
        b.style_context().add_class(class);
    }
    b.connect_clicked(move |_| {
        if let Some(c) = catchup::shared() {
            thread::spawn(move || c.clicked(target));
        }
    });
    targets.push((target, b.clone()));
    b
}

fn clear(container: &gtk::Box) {
    for child in container.children() {
        container.remove(&child);
    }
}

/// An answer's line as Pango markup: bold where it's bold, timestamps in the Mac's amber.
fn markup(line: &Line) -> String {
    let mut out = String::new();
    if let Some(bullet) = &line.bullet {
        out.push_str(&"    ".repeat(line.indent));
        out.push_str(&glib::markup_escape_text(bullet));
        out.push(' ');
    }
    for span in &line.spans {
        let text = glib::markup_escape_text(&span.text);
        if span.stamp {
            out.push_str(&format!("<span foreground=\"#B07A12\">{text}</span>"));
        } else if span.bold || line.heading {
            out.push_str(&format!("<b>{text}</b>"));
        } else {
            out.push_str(&text);
        }
    }
    out
}

fn fill_draft(into: &gtk::Box, view: &View) {
    clear(into);
    for line in &view.draft {
        let r = row();
        if let Some(speaker) = &line.speaker {
            let tag = gtk::Label::new(None);
            let color = if speaker == "You" { "#4F86A8" } else { "#9B5575" };
            tag.set_markup(&format!("<span foreground=\"{color}\" weight=\"bold\" size=\"small\">{}</span>", glib::markup_escape_text(speaker)));
            tag.set_valign(gtk::Align::Start);
            r.pack_start(&tag, false, false, 0);
        }
        let text = label(&line.text, &["soft"]);
        text.set_lines(3);
        text.set_ellipsize(pango::EllipsizeMode::End);
        r.pack_start(&text, true, true, 0);
        into.pack_start(&r, false, false, 0);
    }
    if let Some(hint) = &view.draft_hint {
        into.pack_start(&label(hint, &["small", "faint"]), false, false, 0);
    }
    if let Some(marks) = &view.marks {
        into.pack_start(&label(marks, &["small", "soft"]), false, false, 0);
    }
    into.show_all();
}

/// Why nothing can be asked, or the quick questions (and the ones that are off, with why).
fn fill_above(into: &gtk::Box, view: &View) -> Vec<(Target, gtk::Button)> {
    clear(into);
    let mut targets = Vec::new();
    if view.expanded {
        if let Some(reason) = &view.reason {
            into.pack_start(&label(reason, &["small", "soft"]), false, false, 0);
        }
        for (i, q) in view.quick.iter().enumerate() {
            let r = row();
            let text = label(&q.label, &[]);
            text.set_line_wrap(false);
            r.pack_start(&text, true, true, 0);
            if q.busy {
                let spinner = gtk::Spinner::new();
                spinner.start();
                r.pack_start(&spinner, false, false, 0);
            }
            if let Some(cost) = &q.cost {
                r.pack_start(&label(cost, &["small", "soft"]), false, false, 0);
            }
            let b = target_button(Target::Quick(i), r.upcast_ref(), &["quick"], &mut targets);
            b.set_relief(gtk::ReliefStyle::Normal);
            b.set_sensitive(q.enabled);
            into.pack_start(&b, false, false, 0);
        }
        for (q, why) in &view.off {
            let off = gtk::Label::new(None);
            off.set_markup(&format!("<b>{}</b> {}", glib::markup_escape_text(q), glib::markup_escape_text(why)));
            off.set_xalign(0.0);
            off.set_line_wrap(true);
            off.set_max_width_chars(44);
            off.style_context().add_class("small");
            off.style_context().add_class("soft");
            into.pack_start(&off, false, false, 0);
        }
    }
    into.show_all();
    targets
}

/// Notes, warnings, what went wrong, the question being answered or the last answer, and who answers.
fn fill_below(into: &gtk::Box, view: &View) -> Vec<(Target, gtk::Button)> {
    clear(into);
    let mut targets = Vec::new();
    if !view.expanded {
        into.show_all();
        return targets;
    }
    if let Some(note) = &view.note {
        into.pack_start(&label(note, &["small", "soft"]), false, false, 0);
    }
    if let Some(warning) = &view.warning {
        let r = row();
        r.style_context().add_class("warning");
        r.pack_start(&label(warning, &["small"]), true, true, 0);
        into.pack_start(&r, false, false, 0);
    }
    if let Some(error) = &view.error {
        let r = row();
        r.style_context().add_class("error");
        r.pack_start(&label(error, &["small"]), true, true, 0);
        let dismiss = target_button(Target::DismissError, label("×", &["soft"]).upcast_ref(), &[], &mut targets);
        dismiss.set_valign(gtk::Align::Start);
        r.pack_start(&dismiss, false, false, 0);
        into.pack_start(&r, false, false, 0);
    }
    if let Some(pending) = &view.pending {
        let panel = gtk::Box::new(gtk::Orientation::Vertical, 6);
        panel.style_context().add_class("panel");
        let q = label(pending, &["bold"]);
        q.set_lines(2);
        q.set_ellipsize(pango::EllipsizeMode::End);
        panel.pack_start(&q, false, false, 0);
        let reading = row();
        let spinner = gtk::Spinner::new();
        spinner.start();
        reading.pack_start(&spinner, false, false, 0);
        reading.pack_start(&label(READING, &["soft"]), true, true, 0);
        panel.pack_start(&reading, false, false, 0);
        into.pack_start(&panel, false, false, 0);
    } else if let Some(answer) = &view.answer {
        let panel = gtk::Box::new(gtk::Orientation::Vertical, 6);
        panel.style_context().add_class("panel");
        let top = row();
        let q = label(&answer.q, &["bold"]);
        q.set_lines(2);
        q.set_ellipsize(pango::EllipsizeMode::End);
        top.pack_start(&q, true, true, 0);
        if let Some(at) = &answer.asked_at {
            let at = label(at, &["small", "soft"]);
            at.set_line_wrap(false);
            at.set_valign(gtk::Align::Start);
            top.pack_start(&at, false, false, 0);
        }
        panel.pack_start(&top, false, false, 0);
        let lines = gtk::Box::new(gtk::Orientation::Vertical, 4);
        for line in &answer.lines {
            let text = gtk::Label::new(None);
            text.set_markup(&markup(line));
            text.set_xalign(0.0);
            text.set_line_wrap(true);
            text.set_line_wrap_mode(pango::WrapMode::WordChar);
            text.set_max_width_chars(42);
            text.set_selectable(true);
            text.set_can_focus(false);
            lines.pack_start(&text, false, false, 0);
        }
        let scroll = gtk::ScrolledWindow::new(None::<&gtk::Adjustment>, None::<&gtk::Adjustment>);
        scroll.set_policy(gtk::PolicyType::Never, gtk::PolicyType::Automatic);
        scroll.set_max_content_height(260);
        scroll.set_propagate_natural_height(true);
        scroll.add(&lines);
        panel.pack_start(&scroll, false, false, 0);
        if let Some(who) = &answer.label {
            panel.pack_start(&label(who, &["small", "soft"]), false, false, 0);
        }
        into.pack_start(&panel, false, false, 0);
    }
    let footer = row();
    if let Some(who) = &view.footer {
        let who = label(who, &["small", "soft"]);
        who.set_line_wrap(false);
        who.set_ellipsize(pango::EllipsizeMode::End);
        footer.pack_start(&who, true, true, 0);
    } else {
        footer.pack_start(&gtk::Label::new(None), true, true, 0);
    }
    if view.can_open {
        let open = target_button(Target::Open, label("Open in Trailmix", &["small"]).upcast_ref(), &["link"], &mut targets);
        footer.pack_start(&open, false, false, 0);
    }
    into.pack_start(&footer, false, false, 0);
    into.show_all();
    targets
}

type DraftPart = (Vec<DraftView>, Option<String>, Option<String>);
type AbovePart = (bool, Option<String>, Vec<QuickView>, Vec<(String, String)>);
type BelowPart = (bool, Option<String>, Option<String>, Option<String>, Option<String>, Option<AnswerView>, Option<String>, bool);

fn draft_part(v: &View) -> DraftPart {
    (v.draft.clone(), v.draft_hint.clone(), v.marks.clone())
}

fn above_part(v: &View) -> AbovePart {
    (v.expanded, v.reason.clone(), v.quick.clone(), v.off.clone())
}

fn below_part(v: &View) -> BelowPart {
    (v.expanded, v.note.clone(), v.warning.clone(), v.error.clone(), v.pending.clone(), v.answer.clone(), v.footer.clone(), v.can_open)
}

/// Brings the card up to date with what catch-up says now, making afresh only the parts that changed.
fn update() {
    let Some(c) = catchup::shared() else { return };
    let view = c.view();
    LIVE.with(|live| {
        let Ok(mut live) = live.try_borrow_mut() else { return };
        let Some(l) = live.as_mut() else { return };
        let before = l.view.take();
        l.title.set_text(&view.title);
        l.clock.set_text(&view.clock);
        l.arrow.set_text(if view.expanded { "▾" } else { "▸" });
        if before.as_ref().map(draft_part) != Some(draft_part(&view)) {
            fill_draft(&l.draft, &view);
        }
        if before.as_ref().map(above_part) != Some(above_part(&view)) {
            l.above_targets = fill_above(&l.above, &view);
        }
        if before.as_ref().map(below_part) != Some(below_part(&view)) {
            l.below_targets = fill_below(&l.below, &view);
        }
        l.entry.set_sensitive(view.ask_enabled);
        if view.expanded && view.ask_box {
            l.entry.show();
        } else {
            l.entry.hide();
        }
        l.view = Some(view);
    });
}

/// Bottom right of the main screen, inside its work area (clear of panels). On Wayland the desktop places it.
fn place_in_corner(window: &gtk::Window) {
    let Some(display) = gtk::gdk::Display::default() else { return };
    let monitor = display.primary_monitor().or_else(|| display.monitor(0));
    let Some(monitor) = monitor else { return };
    let area = monitor.workarea();
    let (w, h) = window.size();
    window.move_(area.x() + area.width() - w - 16, (area.y() + area.height() - h - 16).max(area.y() + 16));
}
