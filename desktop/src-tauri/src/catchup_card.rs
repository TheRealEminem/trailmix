//! The recording card on Windows (catchup.rs has what it shows): the live draft, the moments flagged, and Catch me
//! up, the way the Mac menu shows them while a meeting records. Like the call reminder's card (card.rs) it's drawn
//! with plain Win32 and GDI on a thread of its own, sits above everything in the corner, and never takes the focus
//! from the call (WS_EX_NOACTIVATE, and a click answers MA_NOACTIVATE). The one exception is the box to type a
//! question in: clicking it takes the keyboard, and Enter (or Esc) hands it straight back to the window that had
//! it. Unlike a reminder it stays until it's closed, or the recording ends.

use std::ffi::c_void;
use std::path::Path;
use std::sync::{Mutex, MutexGuard, Once};
use std::thread;

use crate::catchup::{self, Target, View, ASK_PLACEHOLDER, READING};
use crate::gui::*;

const CLASS: &str = "TrailmixCatchUp";
const WM_REFRESH: u32 = WM_APP + 1;
const WM_SUBMIT: u32 = WM_APP + 2;
const WM_CANCEL: u32 = WM_APP + 3;
const WM_FOCUS_EDIT: u32 = WM_APP + 4;
const VK_ESC: usize = 0x1B;
const DWMWA_WINDOW_CORNER_PREFERENCE: u32 = 33;
const DWMWCP_ROUND: u32 = 2;
/// The tallest an answer gets before it scrolls (the Mac's 260 points).
const ANSWER_MAX: f64 = 260.0;

struct Slot {
    hwnd: Hwnd,
    edit: Hwnd,
    layout: Layout,
    origin: (i32, i32),
    view: View,
    typing: bool,
}

/// Some while the card's thread runs (hwnd 0 until its window is made).
static SLOT: Mutex<Option<Slot>> = Mutex::new(None);

fn lock() -> MutexGuard<'static, Option<Slot>> {
    SLOT.lock().unwrap_or_else(|e| e.into_inner())
}

/// Shows the card, or brings the one up to date.
pub fn show() {
    {
        let mut slot = lock();
        if let Some(s) = slot.as_ref() {
            if s.hwnd != 0 {
                unsafe { PostMessageW(s.hwnd, WM_REFRESH, 0, 0) };
            }
            return;
        }
        *slot = Some(Slot { hwnd: 0, edit: 0, layout: Layout::default(), origin: (0, 0), view: View::default(), typing: false });
    }
    if thread::Builder::new().name("trailmix-catch-up".into()).spawn(|| unsafe { run() }).is_err() {
        *lock() = None;
    }
}

/// Redraws it from what catch-up says now (nothing, if it isn't up).
pub fn refresh() {
    if let Some(s) = lock().as_ref().filter(|s| s.hwnd != 0) {
        unsafe { PostMessageW(s.hwnd, WM_REFRESH, 0, 0) };
    }
}

pub fn hide() {
    let mut slot = lock();
    let Some(s) = slot.as_ref() else { return };
    if s.hwnd != 0 {
        unsafe { PostMessageW(s.hwnd, WM_CLOSE, 0, 0) };
    } else {
        *slot = None; // not on the screen yet: its thread sees that and stops
    }
}

pub fn is_open() -> bool {
    lock().is_some()
}

/// The card up now, for the self-test.
pub struct Shown {
    pub visible: bool,
    pub above: bool,
    /// It can't be activated (so clicking it leaves the focus where it was).
    pub no_activate: bool,
    /// It has the keyboard now.
    pub focused: bool,
    /// What it says, a line each.
    pub text: String,
}

pub fn current() -> Option<Shown> {
    let slot = lock();
    let s = slot.as_ref().filter(|s| s.hwnd != 0)?;
    let (ex_style, visible) = unsafe { (GetWindowLongPtrW(s.hwnd, GWL_EXSTYLE) as u32, IsWindowVisible(s.hwnd) != 0) };
    Some(Shown {
        visible,
        above: ex_style & WS_EX_TOPMOST != 0,
        no_activate: ex_style & WS_EX_NOACTIVATE != 0,
        focused: foreground() == s.hwnd,
        text: s.view.text(),
    })
}

/// The window up now, to tell it apart from whatever has the focus.
pub fn window() -> Hwnd {
    lock().as_ref().map(|s| s.hwnd).unwrap_or(0)
}

/// Clicks something on the card: with the real mouse (`real`), or by handing the window the click's messages.
pub fn press(target: Target, real: bool) -> bool {
    let (hwnd, rect, origin) = {
        let slot = lock();
        let Some(s) = slot.as_ref().filter(|s| s.hwnd != 0) else { return false };
        let Some(item) = s.layout.items.iter().find(|i| i.target == Some(target)) else { return false };
        (s.hwnd, item.rect, s.origin)
    };
    let (x, y) = rect.center();
    if real {
        click_at(origin.0 + x, origin.1 + y)
    } else {
        unsafe { PostMessageW(hwnd, WM_LBUTTONDOWN, 1, lparam_of(x, y)) != 0 && PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam_of(x, y)) != 0 }
    }
}

/// Asks a question through the box: clicked and typed with the real mouse and keyboard (`real`), or put in the
/// box and Enter handed to it. False if the box isn't there or Windows didn't take the input.
pub fn ask_typed(text: &str, real: bool) -> bool {
    let (hwnd, edit, rect, origin) = {
        let slot = lock();
        let Some(s) = slot.as_ref().filter(|s| s.hwnd != 0 && s.edit != 0) else { return false };
        let Some(rect) = s.layout.edit else { return false };
        (s.hwnd, s.edit, rect, s.origin)
    };
    if real {
        let (x, y) = rect.center();
        if !click_at(origin.0 + x, origin.1 + y) {
            return false;
        }
        let typing = crate::selftest::wait_until(2.0, || lock().as_ref().map(|s| s.typing).unwrap_or(false) && foreground() == hwnd);
        return typing && type_text(text) && { thread::sleep(std::time::Duration::from_millis(300)); press_key(VK_RETURN as u16) };
    }
    let wide = wstr(text);
    unsafe { SetWindowTextW(edit, wide.as_ptr()) != 0 && PostMessageW(edit, WM_KEYDOWN, VK_RETURN as usize, 0) != 0 }
}

/// Whether the box to type in has the keyboard now.
pub fn typing() -> bool {
    lock().as_ref().map(|s| s.typing).unwrap_or(false)
}

/// Draws the card as it is now to a PNG, to look at.
pub fn save_png(path: &Path) -> bool {
    let Some(view) = lock().as_ref().filter(|s| s.hwnd != 0).map(|s| s.view.clone()) else { return false };
    unsafe { render_png(&view, path) }
}

// MARK: The window

struct State {
    view: View,
    layout: Layout,
    fonts: Fonts,
    colors: Colors,
    scale: f64,
    edit: Hwnd,
    field_brush: isize,
    hover: Option<Target>,
    pressed: Option<Target>,
    tracking: bool,
    /// How far the answer is scrolled.
    scroll: i32,
    /// Typing a question: the window that had the focus before, to hand it back to.
    typing: bool,
    before: Hwnd,
}

fn register_class() {
    static ONCE: Once = Once::new();
    ONCE.call_once(|| unsafe {
        let name = wstr(CLASS);
        let class = WndClassExW {
            size: std::mem::size_of::<WndClassExW>() as u32,
            style: CS_DROPSHADOW,
            proc_: window_proc,
            cls_extra: 0,
            wnd_extra: 0,
            instance: GetModuleHandleW(std::ptr::null()),
            icon: 0,
            cursor: LoadCursorW(0, IDC_ARROW as *const u16),
            background: 0,
            menu_name: std::ptr::null(),
            class_name: name.as_ptr(),
            icon_small: 0,
        };
        RegisterClassExW(&class);
    });
}

/// Where the card goes: bottom right, above the taskbar, where Windows' own flyouts and notifications appear.
fn place(layout: &Layout, scale: f64) -> (i32, i32) {
    let area = work_area();
    let margin = px(12.0, scale);
    (area.right - layout.width - margin, (area.bottom - layout.height - margin).max(area.top + margin))
}

unsafe fn run() {
    register_class();
    let Some(catch_up) = catchup::shared() else {
        *lock() = None;
        return;
    };
    let scale = scale();
    let fonts = Fonts::new(scale);
    let view = catch_up.view();
    let measuring = CreateCompatibleDC(0);
    let layout = layout(measuring, &fonts, &view, scale);
    DeleteDC(measuring);
    let (x, y) = place(&layout, scale);
    let colors = Colors::new(crate::win::dark_mode());
    let field_brush = CreateSolidBrush(colors.field);
    let mut state = Box::new(State {
        view: view.clone(),
        layout: layout.clone(),
        fonts,
        colors,
        scale,
        edit: 0,
        field_brush,
        hover: None,
        pressed: None,
        tracking: false,
        scroll: 0,
        typing: false,
        before: 0,
    });
    let (class, title) = (wstr(CLASS), wstr("Trailmix: Catch me up"));
    let hwnd = CreateWindowExW(
        WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        class.as_ptr(),
        title.as_ptr(),
        WS_POPUP | WS_CLIPCHILDREN,
        x,
        y,
        layout.width,
        layout.height,
        0,
        0,
        GetModuleHandleW(std::ptr::null()),
        std::ptr::null(),
    );
    if hwnd == 0 {
        state.fonts.delete();
        DeleteObject(field_brush);
        *lock() = None;
        return;
    }
    let (edit_class, empty) = (wstr("EDIT"), wstr(""));
    let edit = CreateWindowExW(
        0,
        edit_class.as_ptr(),
        empty.as_ptr(),
        WS_CHILD | ES_AUTOHSCROLL,
        0,
        0,
        10,
        10,
        hwnd,
        100,
        GetModuleHandleW(std::ptr::null()),
        std::ptr::null(),
    );
    if edit != 0 {
        SendMessageW(edit, WM_SETFONT, state.fonts.body as usize, 1);
        let cue = wstr(ASK_PLACEHOLDER);
        SendMessageW(edit, EM_SETCUEBANNER, 0, cue.as_ptr() as isize);
    }
    state.edit = edit;
    SetWindowLongPtrW(hwnd, GWLP_USERDATA, &mut *state as *mut State as isize);
    place_edit(&state);
    let claimed = {
        let mut slot = lock();
        match slot.as_mut() {
            Some(s) if s.hwnd == 0 => {
                s.hwnd = hwnd;
                s.edit = edit;
                s.layout = layout.clone();
                s.origin = (x, y);
                s.view = view;
                true
            }
            _ => false,
        }
    };
    if !claimed {
        DestroyWindow(hwnd);
        state.fonts.delete();
        DeleteObject(field_brush);
        return;
    }
    let round = DWMWCP_ROUND;
    DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, &round as *const u32 as *const c_void, 4);
    SetWindowPos(hwnd, HWND_TOPMOST, x, y, layout.width, layout.height, SWP_NOACTIVATE | SWP_SHOWWINDOW);
    let mut msg = Msg::zeroed();
    while GetMessageW(&mut msg, 0, 0, 0) > 0 {
        // Enter and Esc in the box: ask, or don't; either way the keyboard goes back to where it was.
        if msg.message == WM_KEYDOWN && msg.hwnd == edit && edit != 0 {
            if msg.wparam == VK_RETURN as usize {
                PostMessageW(hwnd, WM_SUBMIT, 0, 0);
                continue;
            }
            if msg.wparam == VK_ESC {
                PostMessageW(hwnd, WM_CANCEL, 0, 0);
                continue;
            }
        }
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    state.fonts.delete();
    DeleteObject(field_brush);
    let mut slot = lock();
    if slot.as_ref().map(|s| s.hwnd) == Some(hwnd) {
        *slot = None;
    }
}

/// The box goes where the layout puts it (inside the field's frame), or away.
unsafe fn place_edit(s: &State) {
    if s.edit == 0 {
        return;
    }
    match s.layout.edit {
        Some(r) => {
            let inset = px(8.0, s.scale);
            let line = px(18.0, s.scale);
            MoveWindow(s.edit, r.left + inset, r.top + (r.height() - line) / 2, r.width() - 2 * inset, line, 1);
            EnableWindow(s.edit, if s.view.ask_enabled { 1 } else { 0 });
            ShowWindow(s.edit, SW_SHOWNOACTIVATE);
        }
        None => {
            ShowWindow(s.edit, SW_HIDE);
        }
    }
}

/// Takes what catch-up says now: lays it out again, keeps the bottom where it was, and redraws.
unsafe fn update(hwnd: Hwnd, state: *mut State) {
    let Some(catch_up) = catchup::shared() else { return };
    let view = catch_up.view();
    let s = &mut *state;
    let answer_changed = s.view.answer != view.answer || s.view.pending != view.pending;
    let measuring = CreateCompatibleDC(0);
    let layout = layout(measuring, &s.fonts, &view, s.scale);
    DeleteDC(measuring);
    if answer_changed {
        s.scroll = 0;
    }
    let resized = layout.width != s.layout.width || layout.height != s.layout.height;
    s.view = view.clone();
    s.layout = layout.clone();
    s.scroll = s.scroll.min(layout.scroll_max());
    let (x, y) = place(&layout, s.scale);
    if resized {
        SetWindowPos(hwnd, HWND_TOPMOST, x, y, layout.width, layout.height, SWP_NOACTIVATE);
    }
    place_edit(s);
    if let Some(slot) = lock().as_mut().filter(|slot| slot.hwnd == hwnd) {
        slot.layout = layout;
        slot.view = view;
        slot.origin = (x, y);
        slot.typing = s.typing;
    }
    InvalidateRect(hwnd, std::ptr::null(), 0);
}

fn cursor_in_edit(hwnd: Hwnd, s: &State) -> bool {
    let Some(r) = s.layout.edit else { return false };
    if !s.view.ask_enabled {
        return false;
    }
    let mut point = Point::default();
    unsafe {
        if GetCursorPos(&mut point) == 0 || ScreenToClient(hwnd, &mut point) == 0 {
            return false;
        }
    }
    r.contains(point.x, point.y)
}

/// Clicked into the box: the card may take the keyboard, until Enter or Esc.
unsafe fn begin_typing(hwnd: Hwnd, s: &mut State) {
    if !s.typing {
        s.typing = true;
        let before = foreground();
        s.before = if before != hwnd { before } else { 0 };
        let style = GetWindowLongPtrW(hwnd, GWL_EXSTYLE) as u32;
        SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (style & !WS_EX_NOACTIVATE) as isize);
    }
    PostMessageW(hwnd, WM_FOCUS_EDIT, 0, 0);
}

/// Done typing: the card can't be activated again, and the window that had the keyboard gets it back.
unsafe fn end_typing(hwnd: Hwnd, s: &mut State, give_back: bool) {
    if !s.typing {
        return;
    }
    s.typing = false;
    let style = GetWindowLongPtrW(hwnd, GWL_EXSTYLE) as u32;
    SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (style | WS_EX_NOACTIVATE) as isize);
    if give_back && s.before != 0 && IsWindow(s.before) != 0 {
        SetForegroundWindow(s.before);
    }
    s.before = 0;
    if let Some(slot) = lock().as_mut().filter(|slot| slot.hwnd == hwnd) {
        slot.typing = false;
    }
    InvalidateRect(hwnd, std::ptr::null(), 0);
}

fn edit_text(edit: Hwnd) -> String {
    unsafe {
        let len = GetWindowTextLengthW(edit);
        if len <= 0 {
            return String::new();
        }
        let mut buffer = vec![0u16; len as usize + 1];
        let got = GetWindowTextW(edit, buffer.as_mut_ptr(), buffer.len() as i32);
        String::from_utf16_lossy(&buffer[..got.max(0) as usize])
    }
}

unsafe extern "system" fn window_proc(hwnd: Hwnd, msg: u32, wparam: usize, lparam: isize) -> isize {
    let state = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *mut State;
    if state.is_null() {
        return DefWindowProcW(hwnd, msg, wparam, lparam);
    }
    match msg {
        WM_MOUSEACTIVATE => {
            if cursor_in_edit(hwnd, &*state) {
                begin_typing(hwnd, &mut *state);
                MA_ACTIVATE
            } else {
                MA_NOACTIVATE
            }
        }
        WM_FOCUS_EDIT => {
            if (*state).typing {
                SetForegroundWindow(hwnd);
                SetFocus((*state).edit);
                if let Some(slot) = lock().as_mut().filter(|slot| slot.hwnd == hwnd) {
                    slot.typing = true;
                }
                InvalidateRect(hwnd, std::ptr::null(), 0);
            }
            0
        }
        WM_ACTIVATE => {
            if wparam & 0xFFFF == WA_INACTIVE {
                end_typing(hwnd, &mut *state, false); // clicked back into the call: nothing to hand back
            }
            DefWindowProcW(hwnd, msg, wparam, lparam)
        }
        WM_SUBMIT => {
            let text = edit_text((*state).edit);
            let empty = wstr("");
            SetWindowTextW((*state).edit, empty.as_ptr());
            end_typing(hwnd, &mut *state, true);
            if !text.trim().is_empty() {
                if let Some(c) = catchup::shared() {
                    thread::spawn(move || c.ask("question", &text));
                }
            }
            0
        }
        WM_CANCEL => {
            end_typing(hwnd, &mut *state, true);
            0
        }
        WM_REFRESH => {
            update(hwnd, state);
            0
        }
        WM_CTLCOLOREDIT => {
            let s = &*state;
            SetTextColor(wparam as isize, s.colors.text);
            SetBkColor(wparam as isize, s.colors.field);
            s.field_brush
        }
        WM_ERASEBKGND => 1,
        WM_PAINT => {
            let mut ps = PaintStruct { hdc: 0, erase: 0, paint: Rect::default(), restore: 0, inc_update: 0, reserved: [0; 32] };
            let hdc = BeginPaint(hwnd, &mut ps);
            let s = &*state;
            let (w, h) = (s.layout.width, s.layout.height);
            let buffer = CreateCompatibleDC(hdc);
            let bitmap = CreateCompatibleBitmap(hdc, w, h);
            let old = SelectObject(buffer, bitmap);
            paint(buffer, &s.layout, &s.fonts, &s.colors, s.hover, s.scroll, s.typing, false, s.scale);
            BitBlt(hdc, 0, 0, w, h, buffer, 0, 0, SRCCOPY);
            SelectObject(buffer, old);
            DeleteObject(bitmap);
            DeleteDC(buffer);
            EndPaint(hwnd, &ps);
            0
        }
        WM_MOUSEMOVE => {
            if !(*state).tracking {
                let mut track = TrackMouseEvent { size: std::mem::size_of::<TrackMouseEvent>() as u32, flags: TME_LEAVE, hwnd, hover_time: 0 };
                TrackMouseEvent(&mut track);
                (*state).tracking = true;
            }
            let (x, y) = point_of(lparam);
            let hit = (*state).layout.hit(x, y, &(*state).view);
            if hit != (*state).hover {
                (*state).hover = hit;
                InvalidateRect(hwnd, std::ptr::null(), 0);
            }
            0
        }
        WM_MOUSELEAVE => {
            (*state).tracking = false;
            if (*state).hover.is_some() {
                (*state).hover = None;
                InvalidateRect(hwnd, std::ptr::null(), 0);
            }
            0
        }
        WM_SETCURSOR if (*state).hover.is_some() => {
            SetCursor(LoadCursorW(0, IDC_HAND as *const u16));
            1
        }
        WM_MOUSEWHEEL => {
            let s = &mut *state;
            let delta = ((wparam >> 16) & 0xFFFF) as u16 as i16 as i32;
            let max = s.layout.scroll_max();
            if max > 0 {
                s.scroll = (s.scroll - delta * px(40.0, s.scale) / 120).clamp(0, max);
                InvalidateRect(hwnd, std::ptr::null(), 0);
            }
            0
        }
        WM_LBUTTONDOWN => {
            let (x, y) = point_of(lparam);
            (*state).pressed = (*state).layout.hit(x, y, &(*state).view);
            0
        }
        WM_LBUTTONUP => {
            let (x, y) = point_of(lparam);
            let hit = (*state).layout.hit(x, y, &(*state).view);
            let pressed = (*state).pressed.take();
            if let Some(target) = hit.filter(|_| hit == pressed) {
                if let Some(c) = catchup::shared() {
                    thread::spawn(move || c.clicked(target));
                }
            }
            0
        }
        WM_CLOSE => {
            DestroyWindow(hwnd);
            0
        }
        WM_DESTROY => {
            PostQuitMessage(0);
            0
        }
        WM_NCDESTROY => {
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
            DefWindowProcW(hwnd, msg, wparam, lparam)
        }
        _ => DefWindowProcW(hwnd, msg, wparam, lparam),
    }
}

// MARK: Laying it out

fn px(value: f64, scale: f64) -> i32 {
    (value * scale).round() as i32
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Font {
    Title,
    Clock,
    Body,
    Bold,
    Small,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Ink {
    Text,
    Soft,
    Faint,
    Accent,
    You,
    Them,
    Error,
    Warning,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Fill {
    Panel,
    Error,
    Warning,
}

#[derive(Clone, Debug, PartialEq)]
enum Draw {
    Text { text: String, font: Font, ink: Ink, flags: u32 },
    /// A rounded box behind what follows (the answer, a warning).
    Box(Fill),
    /// A speaker's tag ("You", "Them").
    Tag { text: String, ink: Ink },
    /// A quick question: its label, and its cost on the right.
    Button { label: String, cost: Option<String>, enabled: bool, busy: bool },
    /// "Catch me up" with its arrow.
    Toggle { expanded: bool },
    /// The frame around the box to type in (the box itself is an EDIT control on top).
    Field,
    /// The close button.
    Close,
}

#[derive(Clone, Debug, PartialEq)]
struct Item {
    rect: Rect,
    draw: Draw,
    target: Option<Target>,
    /// Inside the answer's scrolling part.
    scrolls: bool,
}

#[derive(Clone, Debug, Default, PartialEq)]
struct Layout {
    width: i32,
    height: i32,
    items: Vec<Item>,
    edit: Option<Rect>,
    /// The answer's scrolling part, and how tall its content is.
    viewport: Option<Rect>,
    content: i32,
}

impl Layout {
    fn scroll_max(&self) -> i32 {
        self.viewport.map(|v| (self.content - v.height()).max(0)).unwrap_or(0)
    }

    fn hit(&self, x: i32, y: i32, view: &View) -> Option<Target> {
        self.items.iter().find(|i| i.target.is_some() && !i.scrolls && i.rect.contains(x, y)).and_then(|i| {
            let target = i.target?;
            match target {
                Target::Quick(n) => view.quick.get(n).filter(|q| q.enabled).map(|_| target),
                _ => Some(target),
            }
        })
    }
}

struct Fonts {
    title: isize,
    clock: isize,
    body: isize,
    bold: isize,
    small: isize,
}

impl Fonts {
    unsafe fn new(scale: f64) -> Fonts {
        let face = wstr("Segoe UI");
        let make = |size: f64, weight: i32| CreateFontW(-px(size, scale), 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, face.as_ptr());
        Fonts { title: make(14.0, 600), clock: make(17.0, 600), body: make(13.0, 400), bold: make(13.0, 600), small: make(11.5, 400) }
    }

    fn get(&self, font: Font) -> isize {
        match font {
            Font::Title => self.title,
            Font::Clock => self.clock,
            Font::Body => self.body,
            Font::Bold => self.bold,
            Font::Small => self.small,
        }
    }

    unsafe fn delete(&self) {
        for font in [self.title, self.clock, self.body, self.bold, self.small] {
            if font != 0 {
                DeleteObject(font);
            }
        }
    }
}

/// Windows 11's flyout colours (card.rs's), with Trailmix's own for the speakers and timestamps (Brand.swift).
struct Colors {
    background: u32,
    border: u32,
    text: u32,
    soft: u32,
    faint: u32,
    accent: u32,
    you: u32,
    them: u32,
    error: u32,
    warning: u32,
    panel: u32,
    error_fill: u32,
    warning_fill: u32,
    button: u32,
    button_hover: u32,
    button_border: u32,
    field: u32,
}

impl Colors {
    fn new(dark: bool) -> Colors {
        if dark {
            Colors {
                background: rgb(0x2C, 0x2C, 0x2C),
                border: rgb(0x45, 0x45, 0x45),
                text: rgb(0xFF, 0xFF, 0xFF),
                soft: rgb(0xC5, 0xC5, 0xC5),
                faint: rgb(0x9A, 0x9A, 0x9A),
                accent: rgb(0x4F, 0xA2, 0x7A),
                you: rgb(0x7F, 0xB2, 0xD3),
                them: rgb(0xCB, 0x86, 0xA6),
                error: rgb(0xFF, 0x99, 0xA4),
                warning: rgb(0xF2, 0xD0, 0x8A),
                panel: rgb(0x36, 0x36, 0x36),
                error_fill: rgb(0x44, 0x27, 0x29),
                warning_fill: rgb(0x43, 0x39, 0x22),
                button: rgb(0x3A, 0x3A, 0x3A),
                button_hover: rgb(0x45, 0x45, 0x45),
                button_border: rgb(0x50, 0x50, 0x50),
                field: rgb(0x1F, 0x1F, 0x1F),
            }
        } else {
            Colors {
                background: rgb(0xFB, 0xFB, 0xFB),
                border: rgb(0xDE, 0xDE, 0xDE),
                text: rgb(0x1A, 0x1A, 0x1A),
                soft: rgb(0x5C, 0x5C, 0x5C),
                faint: rgb(0x8A, 0x8A, 0x8A),
                accent: rgb(0x23, 0x63, 0x47),
                you: rgb(0x4F, 0x86, 0xA8),
                them: rgb(0x9B, 0x55, 0x75),
                error: rgb(0xA4, 0x26, 0x2C),
                warning: rgb(0x7D, 0x52, 0x05),
                panel: rgb(0xF0, 0xF0, 0xF0),
                error_fill: rgb(0xFD, 0xE7, 0xE9),
                warning_fill: rgb(0xFF, 0xF4, 0xCE),
                button: rgb(0xFF, 0xFF, 0xFF),
                button_hover: rgb(0xF2, 0xF2, 0xF2),
                button_border: rgb(0xCF, 0xCF, 0xCF),
                field: rgb(0xFF, 0xFF, 0xFF),
            }
        }
    }

    fn ink(&self, ink: Ink) -> u32 {
        match ink {
            Ink::Text => self.text,
            Ink::Soft => self.soft,
            Ink::Faint => self.faint,
            Ink::Accent => self.accent,
            Ink::You => self.you,
            Ink::Them => self.them,
            Ink::Error => self.error,
            Ink::Warning => self.warning,
        }
    }
}

unsafe fn measure(hdc: isize, font: isize, text: &str, width: i32, wrap: bool) -> (i32, i32) {
    SelectObject(hdc, font);
    let chars: Vec<u16> = text.encode_utf16().collect();
    let mut rect = Rect::new(0, 0, width, 0);
    let flags = DT_CALCRECT | DT_NOPREFIX | if wrap { DT_WORDBREAK | DT_EDITCONTROL } else { DT_SINGLELINE };
    DrawTextW(hdc, chars.as_ptr(), chars.len() as i32, &mut rect, flags);
    (rect.right, rect.bottom)
}

/// Lays the card out top to bottom, as the Mac menu's recording panel: the title and clock, the live draft, the
/// moments flagged, then Catch me up (when it's open): the quick questions or why there are none, the box to type
/// in, notes and warnings, the question being answered or the last answer, and who answers.
unsafe fn layout(hdc: isize, fonts: &Fonts, view: &View, scale: f64) -> Layout {
    let p = |v: f64| px(v, scale);
    let width = p(360.0);
    let pad = p(16.0);
    let inner = width - 2 * pad;
    let mut items: Vec<Item> = Vec::new();
    let mut add = |rect: Rect, draw: Draw, target: Option<Target>, scrolls: bool| items.push(Item { rect, draw, target, scrolls });
    let wrap = DT_WORDBREAK | DT_EDITCONTROL;
    let mut y = p(12.0);

    // Title, clock, close.
    let close = p(24.0);
    let (_, title_h) = measure(hdc, fonts.title, &view.title, inner, false);
    let (clock_w, clock_h) = measure(hdc, fonts.clock, &view.clock, inner, false);
    let row = title_h.max(clock_h).max(close);
    add(Rect::new(width - pad - close + p(6.0), y, width - pad + p(6.0), y + close), Draw::Close, Some(Target::Close), false);
    let clock_right = width - pad - close;
    add(Rect::new(clock_right - clock_w, y, clock_right, y + row), Draw::Text { text: view.clock.clone(), font: Font::Clock, ink: Ink::Text, flags: DT_SINGLELINE | DT_VCENTER | DT_RIGHT }, None, false);
    add(Rect::new(pad + p(14.0), y, clock_right - clock_w - p(8.0), y + row), Draw::Text { text: view.title.clone(), font: Font::Title, ink: Ink::Text, flags: DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS }, None, false);
    add(Rect::new(pad, y + row / 2 - p(4.0), pad + p(8.0), y + row / 2 + p(4.0)), Draw::Tag { text: String::new(), ink: Ink::Error }, None, false);
    y += row + p(8.0);

    // The live draft.
    for line in &view.draft {
        let mut x = pad;
        if let Some(speaker) = &line.speaker {
            let (w, h) = measure(hdc, fonts.small, speaker, inner, false);
            let ink = if speaker == "You" { Ink::You } else { Ink::Them };
            add(Rect::new(x, y + p(1.0), x + w + p(10.0), y + h + p(3.0)), Draw::Tag { text: speaker.clone(), ink }, None, false);
            x += w + p(16.0);
        }
        let (_, h) = measure(hdc, fonts.body, &line.text, width - pad - x, true);
        let h = h.min(p(54.0)); // three lines at most
        add(Rect::new(x, y, width - pad, y + h), Draw::Text { text: line.text.clone(), font: Font::Body, ink: Ink::Soft, flags: wrap | DT_END_ELLIPSIS }, None, false);
        y += h + p(6.0);
    }
    if let Some(hint) = &view.draft_hint {
        let (_, h) = measure(hdc, fonts.small, hint, inner, true);
        add(Rect::new(pad, y, width - pad, y + h), Draw::Text { text: hint.clone(), font: Font::Small, ink: Ink::Faint, flags: wrap }, None, false);
        y += h + p(6.0);
    }
    if let Some(marks) = &view.marks {
        let (_, h) = measure(hdc, fonts.small, marks, inner, false);
        add(Rect::new(pad, y, width - pad, y + h), Draw::Text { text: marks.clone(), font: Font::Small, ink: Ink::Soft, flags: DT_SINGLELINE }, None, false);
        y += h + p(6.0);
    }

    // Catch me up.
    y += p(2.0);
    add(Rect::new(pad - p(8.0), y, width - pad + p(8.0), y + p(30.0)), Draw::Toggle { expanded: view.expanded }, Some(Target::Toggle), false);
    y += p(30.0) + p(6.0);
    let mut edit = None;
    let mut viewport = None;
    let mut content = 0;
    if view.expanded {
        if let Some(reason) = &view.reason {
            let (_, h) = measure(hdc, fonts.small, reason, inner, true);
            add(Rect::new(pad, y, width - pad, y + h), Draw::Text { text: reason.clone(), font: Font::Small, ink: Ink::Soft, flags: wrap }, None, false);
            y += h + p(8.0);
        }
        for (i, q) in view.quick.iter().enumerate() {
            let h = p(32.0);
            add(Rect::new(pad, y, width - pad, y + h), Draw::Button { label: q.label.clone(), cost: q.cost.clone(), enabled: q.enabled, busy: q.busy }, Some(Target::Quick(i)), false);
            y += h + p(6.0);
        }
        for (label, why) in &view.off {
            let text = format!("{label} {why}");
            let (_, h) = measure(hdc, fonts.small, &text, inner, true);
            add(Rect::new(pad, y, width - pad, y + h), Draw::Text { text, font: Font::Small, ink: Ink::Soft, flags: wrap }, None, false);
            y += h + p(6.0);
        }
        if view.ask_box {
            let h = p(32.0);
            let r = Rect::new(pad, y, width - pad, y + h);
            add(r, Draw::Field, None, false);
            edit = Some(r);
            y += h + p(8.0);
        }
        let mut message = |y: &mut i32, text: &str, fill: Option<Fill>, ink: Ink, target: Option<Target>| {
            let gap = if fill.is_some() { p(8.0) } else { 0 };
            let (_, h) = measure(hdc, fonts.small, text, inner - 2 * gap - if target.is_some() { p(16.0) } else { 0 }, true);
            if let Some(fill) = fill {
                add(Rect::new(pad, *y, width - pad, *y + h + 2 * gap), Draw::Box(fill), target, false);
            }
            add(Rect::new(pad + gap, *y + gap, width - pad - gap - if target.is_some() { p(16.0) } else { 0 }, *y + gap + h), Draw::Text { text: text.to_string(), font: Font::Small, ink, flags: wrap }, None, false);
            if target.is_some() {
                add(Rect::new(width - pad - p(22.0), *y + gap - p(2.0), width - pad - p(4.0), *y + gap + p(16.0)), Draw::Close, None, false);
            }
            *y += h + 2 * gap + p(8.0);
        };
        if let Some(note) = &view.note {
            message(&mut y, note, None, Ink::Soft, None);
        }
        if let Some(warning) = &view.warning {
            message(&mut y, warning, Some(Fill::Warning), Ink::Warning, None);
        }
        if let Some(error) = &view.error {
            message(&mut y, error, Some(Fill::Error), Ink::Error, Some(Target::DismissError));
        }
        let gap = p(10.0);
        if let Some(pending) = &view.pending {
            let (_, qh) = measure(hdc, fonts.bold, pending, inner - 2 * gap, true);
            let qh = qh.min(p(36.0));
            let (_, rh) = measure(hdc, fonts.body, READING, inner - 2 * gap, false);
            let total = gap + qh + p(6.0) + rh + gap;
            add(Rect::new(pad, y, width - pad, y + total), Draw::Box(Fill::Panel), None, false);
            add(Rect::new(pad + gap, y + gap, width - pad - gap, y + gap + qh), Draw::Text { text: pending.clone(), font: Font::Bold, ink: Ink::Text, flags: wrap | DT_END_ELLIPSIS }, None, false);
            add(Rect::new(pad + gap, y + gap + qh + p(6.0), width - pad - gap, y + gap + qh + p(6.0) + rh), Draw::Text { text: READING.into(), font: Font::Body, ink: Ink::Soft, flags: DT_SINGLELINE }, None, false);
            y += total + p(8.0);
        } else if let Some(answer) = &view.answer {
            let top = y;
            let left = pad + gap;
            let right = width - pad - gap;
            let mut at_w = 0;
            let mut at_h = 0;
            if let Some(at) = &answer.asked_at {
                (at_w, at_h) = measure(hdc, fonts.small, at, inner, false);
            }
            let q_width = right - left - if at_w > 0 { at_w + p(8.0) } else { 0 };
            let (_, qh) = measure(hdc, fonts.bold, &answer.q, q_width, true);
            let qh = qh.min(p(36.0));
            let mut items_y = top + gap;
            let mut boxed: Vec<Item> = Vec::new();
            boxed.push(Item { rect: Rect::new(left, items_y, left + q_width, items_y + qh), draw: Draw::Text { text: answer.q.clone(), font: Font::Bold, ink: Ink::Text, flags: wrap | DT_END_ELLIPSIS }, target: None, scrolls: false });
            if let Some(at) = &answer.asked_at {
                boxed.push(Item { rect: Rect::new(right - at_w, items_y + p(1.0), right, items_y + p(1.0) + at_h), draw: Draw::Text { text: at.clone(), font: Font::Small, ink: Ink::Soft, flags: DT_SINGLELINE | DT_RIGHT }, target: None, scrolls: false });
            }
            items_y += qh + p(6.0);
            // The answer, line by line, scrolling past ANSWER_MAX.
            let start = items_y;
            let mut ly = start;
            for line in &answer.lines {
                let indent = line.indent as i32 * p(12.0);
                let font = if line.heading { Font::Bold } else { Font::Body };
                let mut x = left + indent;
                if let Some(bullet) = &line.bullet {
                    let (bw, bh) = measure(hdc, fonts.body, bullet, inner, false);
                    boxed.push(Item { rect: Rect::new(x, ly, x + bw, ly + bh), draw: Draw::Text { text: bullet.clone(), font: Font::Body, ink: Ink::Soft, flags: DT_SINGLELINE }, target: None, scrolls: true });
                    x += bw + p(5.0);
                }
                let text: String = line.spans.iter().map(|s| s.text.as_str()).collect();
                let (_, h) = measure(hdc, fonts.get(font), &text, right - x, true);
                boxed.push(Item { rect: Rect::new(x, ly, right, ly + h), draw: Draw::Text { text, font, ink: Ink::Text, flags: wrap }, target: None, scrolls: true });
                ly += h + p(4.0);
            }
            content = ly - start;
            let shown = content.min(p(ANSWER_MAX));
            viewport = Some(Rect::new(left, start, right, start + shown));
            items_y = start + shown;
            if let Some(label) = &answer.label {
                let (_, h) = measure(hdc, fonts.small, label, inner, false);
                items_y += p(4.0);
                boxed.push(Item { rect: Rect::new(left, items_y, right, items_y + h), draw: Draw::Text { text: label.clone(), font: Font::Small, ink: Ink::Soft, flags: DT_SINGLELINE | DT_END_ELLIPSIS }, target: None, scrolls: false });
                items_y += h;
            }
            let bottom = items_y + gap;
            add(Rect::new(pad, top, width - pad, bottom), Draw::Box(Fill::Panel), None, false);
            for item in boxed {
                add(item.rect, item.draw, item.target, item.scrolls);
            }
            y = bottom + p(8.0);
        }
        // Who answers, and Open in Trailmix.
        let (open_w, open_h) = measure(hdc, fonts.small, "Open in Trailmix", inner, false);
        let row = open_h.max(p(18.0));
        if let Some(footer) = &view.footer {
            add(Rect::new(pad, y, width - pad - open_w - p(10.0), y + row), Draw::Text { text: footer.clone(), font: Font::Small, ink: Ink::Soft, flags: DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS }, None, false);
        }
        if view.can_open {
            add(Rect::new(width - pad - open_w, y, width - pad, y + row), Draw::Text { text: "Open in Trailmix".into(), font: Font::Small, ink: Ink::Accent, flags: DT_SINGLELINE | DT_VCENTER | DT_RIGHT }, Some(Target::Open), false);
        }
        y += row + p(4.0);
    }
    Layout { width, height: y + p(8.0), items, edit, viewport, content }
}

// MARK: Drawing

unsafe fn draw_text(hdc: isize, font: isize, color: u32, text: &str, rect: Rect, flags: u32) {
    SelectObject(hdc, font);
    SetTextColor(hdc, color);
    let chars: Vec<u16> = text.encode_utf16().collect();
    let mut rect = rect;
    DrawTextW(hdc, chars.as_ptr(), chars.len() as i32, &mut rect, flags | DT_NOPREFIX);
}

unsafe fn round_rect(hdc: isize, rect: Rect, fill: u32, edge: u32, radius: i32) {
    let brush = CreateSolidBrush(fill);
    let pen = CreatePen(0, 1, edge);
    let old_brush = SelectObject(hdc, brush);
    let old_pen = SelectObject(hdc, pen);
    RoundRect(hdc, rect.left, rect.top, rect.right, rect.bottom, radius, radius);
    SelectObject(hdc, old_brush);
    SelectObject(hdc, old_pen);
    DeleteObject(brush);
    DeleteObject(pen);
}

/// A small filled triangle: pointing right (closed) or down (open).
unsafe fn arrow(hdc: isize, cx: i32, cy: i32, size: i32, down: bool, color: u32) {
    let points = if down {
        [Point { x: cx - size, y: cy - size / 2 }, Point { x: cx + size, y: cy - size / 2 }, Point { x: cx, y: cy + size / 2 + 1 }]
    } else {
        [Point { x: cx - size / 2, y: cy - size }, Point { x: cx - size / 2, y: cy + size }, Point { x: cx + size / 2 + 1, y: cy }]
    };
    let brush = CreateSolidBrush(color);
    let pen = CreatePen(0, 1, color);
    let old_brush = SelectObject(hdc, brush);
    let old_pen = SelectObject(hdc, pen);
    Polygon(hdc, points.as_ptr(), 3);
    SelectObject(hdc, old_brush);
    SelectObject(hdc, old_pen);
    DeleteObject(brush);
    DeleteObject(pen);
}

#[allow(clippy::too_many_arguments)]
unsafe fn paint(hdc: isize, layout: &Layout, fonts: &Fonts, colors: &Colors, hover: Option<Target>, scroll: i32, typing: bool, picture: bool, scale: f64) {
    let whole = Rect::new(0, 0, layout.width, layout.height);
    let background = CreateSolidBrush(colors.background);
    FillRect(hdc, &whole, background);
    DeleteObject(background);
    let border = CreateSolidBrush(colors.border);
    FrameRect(hdc, &whole, border);
    DeleteObject(border);
    SetBkMode(hdc, TRANSPARENT);
    let old_font = SelectObject(hdc, fonts.body);
    let radius = px(8.0, scale);
    for item in &layout.items {
        let hovered = item.target.is_some() && hover == item.target;
        let mut rect = item.rect;
        let mut saved = 0;
        if item.scrolls {
            let Some(view) = layout.viewport else { continue };
            rect.top -= scroll;
            rect.bottom -= scroll;
            if rect.bottom <= view.top || rect.top >= view.bottom {
                continue;
            }
            saved = SaveDC(hdc);
            IntersectClipRect(hdc, view.left, view.top, view.right, view.bottom);
        }
        match &item.draw {
            Draw::Text { text, font, ink, flags } => {
                let color = if hovered { colors.text } else { colors.ink(*ink) };
                draw_text(hdc, fonts.get(*font), color, text, rect, *flags);
            }
            Draw::Box(fill) => {
                let color = match fill {
                    Fill::Panel => colors.panel,
                    Fill::Error => colors.error_fill,
                    Fill::Warning => colors.warning_fill,
                };
                round_rect(hdc, rect, color, color, radius);
            }
            Draw::Tag { text, ink } => {
                let color = colors.ink(*ink);
                if text.is_empty() {
                    round_rect(hdc, rect, color, color, rect.height()); // the recording dot
                } else {
                    round_rect(hdc, rect, colors.background, color, rect.height());
                    draw_text(hdc, fonts.small, color, text, rect, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
                }
            }
            Draw::Button { label, cost, enabled, busy } => {
                let fill = if hovered && *enabled { colors.button_hover } else { colors.button };
                round_rect(hdc, rect, fill, colors.button_border, radius);
                let ink = if *enabled || *busy { colors.text } else { colors.faint };
                let inset = px(12.0, scale);
                let mut text_rect = Rect::new(rect.left + inset, rect.top, rect.right - inset, rect.bottom);
                if *busy {
                    draw_text(hdc, fonts.body, colors.accent, "…", Rect::new(rect.left + inset, rect.top, rect.left + inset + px(14.0, scale), rect.bottom), DT_SINGLELINE | DT_VCENTER);
                    text_rect.left += px(16.0, scale);
                }
                if let Some(cost) = cost {
                    draw_text(hdc, fonts.small, colors.soft, cost, text_rect, DT_SINGLELINE | DT_VCENTER | DT_RIGHT);
                    let (w, _) = measure(hdc, fonts.small, cost, text_rect.width(), false);
                    text_rect.right -= w + px(8.0, scale);
                }
                draw_text(hdc, fonts.body, ink, label, text_rect, DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS);
            }
            Draw::Toggle { expanded } => {
                if hovered {
                    round_rect(hdc, rect, colors.button_hover, colors.button_hover, radius);
                }
                let inset = px(8.0, scale);
                draw_text(hdc, fonts.bold, colors.text, "Catch me up", Rect::new(rect.left + inset, rect.top, rect.right - inset, rect.bottom), DT_SINGLELINE | DT_VCENTER);
                arrow(hdc, rect.right - inset - px(5.0, scale), (rect.top + rect.bottom) / 2, px(4.0, scale), *expanded, colors.soft);
            }
            Draw::Field => {
                let edge = if typing { colors.accent } else { colors.button_border };
                round_rect(hdc, rect, colors.field, edge, radius);
                if picture {
                    // (The box itself is a window of its own, not in a picture of the card.)
                    let inset = px(8.0, scale);
                    draw_text(hdc, fonts.body, colors.faint, ASK_PLACEHOLDER, Rect::new(rect.left + inset, rect.top, rect.right - inset, rect.bottom), DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS);
                }
            }
            Draw::Close => {
                if hovered {
                    round_rect(hdc, rect, colors.button_hover, colors.button_hover, radius);
                }
                draw_text(hdc, fonts.title, colors.soft, "×", rect, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
            }
        }
        if saved != 0 {
            RestoreDC(hdc, saved);
        }
    }
    SelectObject(hdc, old_font);
}

unsafe fn render_png(view: &View, path: &Path) -> bool {
    let scale = scale();
    let fonts = Fonts::new(scale);
    let colors = Colors::new(crate::win::dark_mode());
    let dc = CreateCompatibleDC(0);
    let layout = layout(dc, &fonts, view, scale);
    let (w, h) = (layout.width, layout.height);
    let info = BitmapInfo {
        header: BitmapInfoHeader {
            size: std::mem::size_of::<BitmapInfoHeader>() as u32,
            width: w,
            height: -h, // top-down rows
            planes: 1,
            bit_count: 32,
            compression: 0,
            size_image: 0,
            x_ppm: 0,
            y_ppm: 0,
            clr_used: 0,
            clr_important: 0,
        },
        colors: [0],
    };
    let mut bits: *mut c_void = std::ptr::null_mut();
    let bitmap = CreateDIBSection(dc, &info, 0, &mut bits, 0, 0);
    if bitmap == 0 || bits.is_null() || w <= 0 || h <= 0 {
        DeleteDC(dc);
        fonts.delete();
        return false;
    }
    let old = SelectObject(dc, bitmap);
    paint(dc, &layout, &fonts, &colors, None, 0, false, true, scale);
    GdiFlush();
    let pixels = std::slice::from_raw_parts(bits as *const u8, (w * h * 4) as usize);
    let mut rgba = Vec::with_capacity(pixels.len());
    for p in pixels.chunks_exact(4) {
        rgba.extend_from_slice(&[p[2], p[1], p[0], 255]);
    }
    SelectObject(dc, old);
    DeleteObject(bitmap);
    DeleteDC(dc);
    fonts.delete();
    std::fs::write(path, crate::card::png(w as u32, h as u32, &rgba)).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::catchup::QuickView;

    #[test]
    fn clicks_find_what_they_land_on() {
        let view = View {
            quick: vec![QuickView { label: "What did I miss?".into(), enabled: true, ..QuickView::default() }, QuickView { label: "Explain that".into(), ..QuickView::default() }],
            ..View::default()
        };
        let item = |rect: Rect, target: Target| Item { rect, draw: Draw::Close, target: Some(target), scrolls: false };
        let layout = Layout {
            width: 360,
            height: 300,
            items: vec![item(Rect::new(320, 10, 344, 34), Target::Close), item(Rect::new(16, 100, 344, 132), Target::Quick(0)), item(Rect::new(16, 138, 344, 170), Target::Quick(1))],
            ..Layout::default()
        };
        assert_eq!(layout.hit(330, 20, &view), Some(Target::Close));
        assert_eq!(layout.hit(30, 110, &view), Some(Target::Quick(0)));
        assert_eq!(layout.hit(30, 150, &view), None, "a question that's off can't be clicked");
        assert_eq!(layout.hit(5, 5, &view), None);
    }

    #[test]
    fn a_long_answer_scrolls() {
        let layout = Layout { viewport: Some(Rect::new(0, 100, 300, 360)), content: 500, ..Layout::default() };
        assert_eq!(layout.scroll_max(), 240);
        assert_eq!(Layout { viewport: Some(Rect::new(0, 0, 10, 50)), content: 20, ..Layout::default() }.scroll_max(), 0);
    }
}
