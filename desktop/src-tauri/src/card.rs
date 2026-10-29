//! A small card in the corner of the screen, above everything (the panel in CallReminder.swift): "Zoom call
//! started. Record it?" with Record and Not now, a note after a shortcut was pressed, or "Press the new
//! shortcut". Drawn with plain Win32 and GDI on a thread of its own. It never takes the focus from the call
//! (WS_EX_NOACTIVATE, and a click answers MA_NOACTIVATE), except the one for typing a shortcut, which needs
//! the keyboard. One card at a time: a new one replaces the last. It goes away by itself after a while.

use std::ffi::c_void;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, MutexGuard, Once};
use std::thread;

use crate::gui::*;
use crate::hotkeys::{mods_held, Shortcut, VK_ESCAPE};

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
    /// The Windows notification sound when it appears.
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

#[derive(Clone, Debug, Default)]
struct Layout {
    width: i32,
    height: i32,
    title: Rect,
    detail: Rect,
    buttons: Vec<Rect>,
    link: Option<Rect>,
}

impl Layout {
    fn rect(&self, target: Target) -> Option<Rect> {
        match target {
            Target::Button(i) => self.buttons.get(i).copied(),
            Target::Link => self.link,
        }
    }

    fn hit(&self, x: i32, y: i32) -> Option<Target> {
        if let Some(i) = self.buttons.iter().position(|r| r.contains(x, y)) {
            return Some(Target::Button(i));
        }
        self.link.filter(|r| r.contains(x, y)).map(|_| Target::Link)
    }
}

struct Slot {
    id: u64,
    hwnd: Hwnd,
    card: Card,
    layout: Layout,
    /// Where the window is on the screen.
    origin: (i32, i32),
}

static SLOT: Mutex<Option<Slot>> = Mutex::new(None);
static NEXT: AtomicU64 = AtomicU64::new(1);

fn lock() -> MutexGuard<'static, Option<Slot>> {
    SLOT.lock().unwrap_or_else(|e| e.into_inner())
}

/// Shows a card (replacing the one up, if any); `on_answer(id, answer)` is called once it's gone, on the
/// card's thread. Returns the card's id.
pub fn show(card: Card, on_answer: impl FnOnce(u64, Answer) + Send + 'static) -> u64 {
    let id = NEXT.fetch_add(1, Ordering::SeqCst);
    {
        let mut slot = lock();
        if let Some(old) = slot.take() {
            if old.hwnd != 0 {
                unsafe { PostMessageW(old.hwnd, WM_CLOSE, 0, 0) };
            }
        }
        *slot = Some(Slot { id, hwnd: 0, card: card.clone(), layout: Layout::default(), origin: (0, 0) });
    }
    let spawned = thread::Builder::new().name("trailmix-card".into()).spawn(move || {
        let answer = unsafe { run(id, &card) };
        let mut slot = lock();
        if slot.as_ref().map(|s| s.id) == Some(id) {
            *slot = None;
        }
        drop(slot);
        on_answer(id, answer);
    });
    if spawned.is_err() {
        hide(id);
    }
    id
}

/// A note that needs no answer ("Recording", "Moment marked"), unless a question is up: that stays.
pub fn note(title: &str, detail: &str, seconds: u32) {
    if lock().as_ref().map(|s| !s.card.buttons.is_empty()).unwrap_or(false) {
        return;
    }
    let card = Card { title: title.into(), detail: detail.into(), seconds, ..Card::default() };
    show(card, |_, _| {});
}

/// Takes card `id` away (if it's still the one up).
pub fn hide(id: u64) {
    let mut slot = lock();
    let Some(current) = slot.as_ref() else { return };
    if current.id != id {
        return;
    }
    if current.hwnd != 0 {
        unsafe { PostMessageW(current.hwnd, WM_CLOSE, 0, 0) };
    } else {
        *slot = None; // not on the screen yet: its thread sees that and stops
    }
}

/// The card up now, for the self-test.
pub struct Shown {
    pub id: u64,
    pub hwnd: Hwnd,
    pub title: String,
    pub ex_style: u32,
    pub visible: bool,
}

pub fn current() -> Option<Shown> {
    let slot = lock();
    let s = slot.as_ref().filter(|s| s.hwnd != 0)?;
    let (ex_style, visible) = unsafe { (GetWindowLongPtrW(s.hwnd, GWL_EXSTYLE) as u32, IsWindowVisible(s.hwnd) != 0) };
    Some(Shown { id: s.id, hwnd: s.hwnd, title: s.card.title.clone(), ex_style, visible })
}

/// Clicks a button (or the link) of card `id`: with the real mouse (`real`), or by handing the window the
/// click's messages. False if there's no such card or button, or Windows didn't take the click.
pub fn press(id: u64, target: Target, real: bool) -> bool {
    let (hwnd, rect, origin) = {
        let slot = lock();
        let Some(s) = slot.as_ref().filter(|s| s.id == id && s.hwnd != 0) else { return false };
        let Some(rect) = s.layout.rect(target) else { return false };
        (s.hwnd, rect, s.origin)
    };
    let (x, y) = rect.center();
    if real {
        click_at(origin.0 + x, origin.1 + y)
    } else {
        unsafe { PostMessageW(hwnd, WM_LBUTTONDOWN, 1, lparam_of(x, y)) != 0 && PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam_of(x, y)) != 0 }
    }
}

/// Draws the card up now to a PNG, to look at.
pub fn save_png(path: &Path) -> bool {
    let Some(card) = lock().as_ref().map(|s| s.card.clone()) else { return false };
    unsafe { render_png(&card, path) }
}

// MARK: The window

const CLASS: &str = "TrailmixCard";
const SND_ASYNC: u32 = 0x1;
const SND_NODEFAULT: u32 = 0x2;
const SND_ALIAS: u32 = 0x1_0000;
const DWMWA_WINDOW_CORNER_PREFERENCE: u32 = 33;
const DWMWCP_ROUND: u32 = 2;

struct State {
    card: Card,
    layout: Layout,
    fonts: Fonts,
    colors: Colors,
    scale: f64,
    hover: Option<Target>,
    pressed: Option<Target>,
    tracking: bool,
    answer: Option<Answer>,
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

/// Makes the window, shows it, and runs it until it's answered or gone.
unsafe fn run(id: u64, card: &Card) -> Answer {
    register_class();
    let scale = scale();
    let fonts = Fonts::new(scale);
    let measuring = CreateCompatibleDC(0);
    let layout = layout(measuring, &fonts, card, scale);
    DeleteDC(measuring);
    let area = work_area();
    let margin = px(12.0, scale);
    let (x, y) = if card.keys {
        (area.left + (area.width() - layout.width) / 2, area.top + (area.height() - layout.height) / 3)
    } else {
        // Where Windows' own notifications appear: bottom right, above the taskbar.
        (area.right - layout.width - margin, area.bottom - layout.height - margin)
    };
    let ex_style = WS_EX_TOPMOST | WS_EX_TOOLWINDOW | if card.keys { 0 } else { WS_EX_NOACTIVATE };
    let mut state = Box::new(State {
        card: card.clone(),
        layout: layout.clone(),
        fonts,
        colors: Colors::new(crate::win::dark_mode()),
        scale,
        hover: None,
        pressed: None,
        tracking: false,
        answer: None,
    });
    let (class, title) = (wstr(CLASS), wstr("Trailmix"));
    let hwnd = CreateWindowExW(
        ex_style,
        class.as_ptr(),
        title.as_ptr(),
        WS_POPUP,
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
        return Answer::Dismissed;
    }
    SetWindowLongPtrW(hwnd, GWLP_USERDATA, &mut *state as *mut State as isize);
    let claimed = {
        let mut slot = lock();
        match slot.as_mut() {
            Some(s) if s.id == id => {
                s.hwnd = hwnd;
                s.layout = layout.clone();
                s.origin = (x, y);
                true
            }
            _ => false,
        }
    };
    if !claimed {
        // Hidden or replaced before it was up.
        DestroyWindow(hwnd);
        state.fonts.delete();
        return Answer::Dismissed;
    }
    let round = DWMWCP_ROUND;
    DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, &round as *const u32 as *const c_void, 4);
    if card.keys {
        ShowWindow(hwnd, SW_SHOW);
        SetForegroundWindow(hwnd);
    } else {
        SetWindowPos(hwnd, HWND_TOPMOST, x, y, layout.width, layout.height, SWP_NOACTIVATE | SWP_SHOWWINDOW);
    }
    let mut placed = Rect::default();
    if GetWindowRect(hwnd, &mut placed) != 0 {
        if let Some(s) = lock().as_mut().filter(|s| s.id == id) {
            s.origin = (placed.left, placed.top);
        }
    }
    if card.sound {
        let alias = wstr("Notification.Default");
        PlaySoundW(alias.as_ptr(), 0, SND_ALIAS | SND_ASYNC | SND_NODEFAULT);
    }
    if card.seconds > 0 {
        SetTimer(hwnd, 1, card.seconds.saturating_mul(1000), std::ptr::null());
    }
    let mut msg = Msg::zeroed();
    while GetMessageW(&mut msg, 0, 0, 0) > 0 {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    state.fonts.delete();
    state.answer.unwrap_or(Answer::Dismissed)
}

/// Ends the card with an answer (the first one given counts).
unsafe fn finish(hwnd: Hwnd, state: *mut State, answer: Answer) {
    if (*state).answer.is_none() {
        (*state).answer = Some(answer);
    }
    DestroyWindow(hwnd);
}

unsafe extern "system" fn window_proc(hwnd: Hwnd, msg: u32, wparam: usize, lparam: isize) -> isize {
    let state = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *mut State;
    if state.is_null() {
        return DefWindowProcW(hwnd, msg, wparam, lparam);
    }
    let keys = (*state).card.keys;
    match msg {
        WM_MOUSEACTIVATE if !keys => MA_NOACTIVATE,
        WM_ERASEBKGND => 1,
        WM_PAINT => {
            let mut ps = PaintStruct { hdc: 0, erase: 0, paint: Rect::default(), restore: 0, inc_update: 0, reserved: [0; 32] };
            let hdc = BeginPaint(hwnd, &mut ps);
            let s = &*state;
            let (w, h) = (s.layout.width, s.layout.height);
            let buffer = CreateCompatibleDC(hdc);
            let bitmap = CreateCompatibleBitmap(hdc, w, h);
            let old = SelectObject(buffer, bitmap);
            paint(buffer, &s.card, &s.layout, &s.fonts, &s.colors, s.hover, s.scale);
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
            let hit = (*state).layout.hit(x, y);
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
        WM_SETCURSOR if (*state).hover == Some(Target::Link) => {
            SetCursor(LoadCursorW(0, IDC_HAND as *const u16));
            1
        }
        WM_LBUTTONDOWN => {
            let (x, y) = point_of(lparam);
            (*state).pressed = (*state).layout.hit(x, y);
            0
        }
        WM_LBUTTONUP => {
            let (x, y) = point_of(lparam);
            let hit = (*state).layout.hit(x, y);
            let pressed = (*state).pressed.take();
            let s = &*state;
            if s.card.buttons.is_empty() && s.card.link.is_none() {
                finish(hwnd, state, Answer::Dismissed); // a note: a click puts it away
            } else if hit.is_some() && hit == pressed {
                let answer = match hit {
                    Some(Target::Button(i)) => Answer::Button(i),
                    _ => Answer::Link,
                };
                finish(hwnd, state, answer);
            }
            0
        }
        WM_KEYDOWN | WM_SYSKEYDOWN if keys => {
            let key = wparam as u32;
            if key == VK_ESCAPE {
                finish(hwnd, state, Answer::Dismissed);
            } else if let Some(shortcut) = Shortcut::from_key(key, mods_held()) {
                finish(hwnd, state, Answer::Key(shortcut));
            }
            0
        }
        // Not to Windows: Alt and F10 would otherwise go looking for a menu, and beep.
        WM_KEYUP | WM_SYSKEYUP | WM_CHAR | WM_SYSCHAR if keys => 0,
        WM_TIMER | WM_CLOSE => {
            finish(hwnd, state, Answer::Dismissed);
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

// MARK: Drawing

fn px(value: f64, scale: f64) -> i32 {
    (value * scale).round() as i32
}

struct Fonts {
    title: isize,
    detail: isize,
    button: isize,
    main_button: isize,
    link: isize,
}

impl Fonts {
    unsafe fn new(scale: f64) -> Fonts {
        let face = wstr("Segoe UI");
        let make = |size: f64, weight: i32| CreateFontW(-px(size, scale), 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, face.as_ptr());
        Fonts { title: make(15.0, 600), detail: make(13.0, 400), button: make(13.0, 400), main_button: make(13.0, 600), link: make(12.0, 400) }
    }

    unsafe fn delete(&self) {
        for font in [self.title, self.detail, self.button, self.main_button, self.link] {
            if font != 0 {
                DeleteObject(font);
            }
        }
    }
}

/// Windows 11's flyout colours, with Trailmix's green for the main button (Brand.forest).
struct Colors {
    background: u32,
    border: u32,
    text: u32,
    soft: u32,
    accent: u32,
    accent_hover: u32,
    accent_text: u32,
    button: u32,
    button_hover: u32,
    button_border: u32,
}

impl Colors {
    fn new(dark: bool) -> Colors {
        if dark {
            Colors {
                background: rgb(0x2C, 0x2C, 0x2C),
                border: rgb(0x45, 0x45, 0x45),
                text: rgb(0xFF, 0xFF, 0xFF),
                soft: rgb(0xC5, 0xC5, 0xC5),
                accent: rgb(0x4F, 0xA2, 0x7A),
                accent_hover: rgb(0x60, 0xB3, 0x8A),
                accent_text: rgb(0x0B, 0x1E, 0x15),
                button: rgb(0x3A, 0x3A, 0x3A),
                button_hover: rgb(0x45, 0x45, 0x45),
                button_border: rgb(0x50, 0x50, 0x50),
            }
        } else {
            Colors {
                background: rgb(0xFB, 0xFB, 0xFB),
                border: rgb(0xDE, 0xDE, 0xDE),
                text: rgb(0x1A, 0x1A, 0x1A),
                soft: rgb(0x5C, 0x5C, 0x5C),
                accent: rgb(0x23, 0x63, 0x47),
                accent_hover: rgb(0x1B, 0x50, 0x39),
                accent_text: rgb(0xFF, 0xFF, 0xFF),
                button: rgb(0xFF, 0xFF, 0xFF),
                button_hover: rgb(0xF2, 0xF2, 0xF2),
                button_border: rgb(0xCF, 0xCF, 0xCF),
            }
        }
    }
}

unsafe fn measure(hdc: isize, font: isize, text: &str, width: i32, wrap: bool) -> (i32, i32) {
    SelectObject(hdc, font);
    let chars: Vec<u16> = text.encode_utf16().collect();
    let mut rect = Rect::new(0, 0, width, 0);
    let flags = DT_CALCRECT | DT_NOPREFIX | if wrap { DT_WORDBREAK } else { DT_SINGLELINE };
    DrawTextW(hdc, chars.as_ptr(), chars.len() as i32, &mut rect, flags);
    (rect.right, rect.bottom)
}

unsafe fn draw_text(hdc: isize, font: isize, color: u32, text: &str, rect: Rect, flags: u32) {
    SelectObject(hdc, font);
    SetTextColor(hdc, color);
    let chars: Vec<u16> = text.encode_utf16().collect();
    let mut rect = rect;
    DrawTextW(hdc, chars.as_ptr(), chars.len() as i32, &mut rect, flags | DT_NOPREFIX);
}

unsafe fn layout(hdc: isize, fonts: &Fonts, card: &Card, scale: f64) -> Layout {
    let p = |v: f64| px(v, scale);
    let width = p(340.0);
    let pad = p(16.0);
    let inner = width - 2 * pad;
    let mut y = p(14.0);
    let (_, title_height) = measure(hdc, fonts.title, &card.title, inner, true);
    let title = Rect::new(pad, y, pad + inner, y + title_height);
    y += title_height;
    let mut detail = Rect::default();
    if !card.detail.is_empty() {
        y += p(4.0);
        let (_, height) = measure(hdc, fonts.detail, &card.detail, inner, true);
        detail = Rect::new(pad, y, pad + inner, y + height);
        y += height;
    }
    let mut buttons = Vec::new();
    if !card.buttons.is_empty() {
        y += p(12.0);
        let height = p(32.0);
        let mut x = pad;
        for (i, label) in card.buttons.iter().enumerate() {
            let font = if i == 0 && card.buttons.len() > 1 { fonts.main_button } else { fonts.button };
            let (text_width, _) = measure(hdc, font, label, inner, false);
            let w = (text_width + p(28.0)).max(p(84.0));
            buttons.push(Rect::new(x, y, x + w, y + height));
            x += w + p(8.0);
        }
        y += height;
    }
    let mut link = None;
    if let Some(label) = &card.link {
        y += p(8.0);
        let (w, h) = measure(hdc, fonts.link, label, inner, false);
        link = Some(Rect::new(pad, y, pad + w + p(2.0), y + h + p(4.0)));
        y += h + p(4.0);
    }
    Layout { width, height: y + p(14.0), title, detail, buttons, link }
}

unsafe fn paint(hdc: isize, card: &Card, layout: &Layout, fonts: &Fonts, colors: &Colors, hover: Option<Target>, scale: f64) {
    let whole = Rect::new(0, 0, layout.width, layout.height);
    let background = CreateSolidBrush(colors.background);
    FillRect(hdc, &whole, background);
    DeleteObject(background);
    let border = CreateSolidBrush(colors.border);
    FrameRect(hdc, &whole, border);
    DeleteObject(border);
    SetBkMode(hdc, TRANSPARENT);
    let old_font = SelectObject(hdc, fonts.title);
    draw_text(hdc, fonts.title, colors.text, &card.title, layout.title, DT_WORDBREAK);
    if !card.detail.is_empty() {
        draw_text(hdc, fonts.detail, colors.soft, &card.detail, layout.detail, DT_WORDBREAK);
    }
    let radius = px(8.0, scale);
    for (i, (label, rect)) in card.buttons.iter().zip(layout.buttons.iter()).enumerate() {
        let main = i == 0 && card.buttons.len() > 1;
        let hovered = hover == Some(Target::Button(i));
        let (fill, edge, ink, font) = if main {
            let fill = if hovered { colors.accent_hover } else { colors.accent };
            (fill, fill, colors.accent_text, fonts.main_button)
        } else {
            (if hovered { colors.button_hover } else { colors.button }, colors.button_border, colors.text, fonts.button)
        };
        let brush = CreateSolidBrush(fill);
        let pen = CreatePen(0, 1, edge);
        let old_brush = SelectObject(hdc, brush);
        let old_pen = SelectObject(hdc, pen);
        RoundRect(hdc, rect.left, rect.top, rect.right, rect.bottom, radius, radius);
        SelectObject(hdc, old_brush);
        SelectObject(hdc, old_pen);
        DeleteObject(brush);
        DeleteObject(pen);
        draw_text(hdc, font, ink, label, *rect, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
    }
    if let (Some(label), Some(rect)) = (&card.link, layout.link) {
        let ink = if hover == Some(Target::Link) { colors.text } else { colors.soft };
        draw_text(hdc, fonts.link, ink, label, rect, DT_SINGLELINE | DT_VCENTER);
    }
    SelectObject(hdc, old_font);
}

unsafe fn render_png(card: &Card, path: &Path) -> bool {
    let scale = scale();
    let fonts = Fonts::new(scale);
    let colors = Colors::new(crate::win::dark_mode());
    let dc = CreateCompatibleDC(0);
    let layout = layout(dc, &fonts, card, scale);
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
    paint(dc, card, &layout, &fonts, &colors, None, scale);
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
    std::fs::write(path, png(w as u32, h as u32, &rgba)).is_ok()
}

// MARK: A PNG, without a library (uncompressed: these are small)

fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &byte in data {
        crc ^= byte as u32;
        for _ in 0..8 {
            crc = if crc & 1 != 0 { (crc >> 1) ^ 0xEDB8_8320 } else { crc >> 1 };
        }
    }
    !crc
}

fn adler32(data: &[u8]) -> u32 {
    let (mut a, mut b) = (1u32, 0u32);
    for &byte in data {
        a = (a + byte as u32) % 65521;
        b = (b + a) % 65521;
    }
    (b << 16) | a
}

fn png(width: u32, height: u32, rgba: &[u8]) -> Vec<u8> {
    let mut raw = Vec::with_capacity(rgba.len() + height as usize);
    for row in rgba.chunks_exact((width * 4) as usize) {
        raw.push(0); // no filter
        raw.extend_from_slice(row);
    }
    let mut zlib = vec![0x78, 0x01];
    let blocks: Vec<&[u8]> = raw.chunks(65_535).collect();
    for (i, block) in blocks.iter().enumerate() {
        zlib.push(if i + 1 == blocks.len() { 1 } else { 0 });
        let len = block.len() as u16;
        zlib.extend_from_slice(&len.to_le_bytes());
        zlib.extend_from_slice(&(!len).to_le_bytes());
        zlib.extend_from_slice(block);
    }
    zlib.extend_from_slice(&adler32(&raw).to_be_bytes());

    let mut out = vec![0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A];
    let mut chunk = |kind: &[u8; 4], data: &[u8]| {
        out.extend_from_slice(&(data.len() as u32).to_be_bytes());
        let mut body = kind.to_vec();
        body.extend_from_slice(data);
        out.extend_from_slice(&body);
        out.extend_from_slice(&crc32(&body).to_be_bytes());
    };
    let mut header = Vec::new();
    header.extend_from_slice(&width.to_be_bytes());
    header.extend_from_slice(&height.to_be_bytes());
    header.extend_from_slice(&[8, 6, 0, 0, 0]); // 8 bits, RGBA
    chunk(b"IHDR", &header);
    chunk(b"IDAT", &zlib);
    chunk(b"IEND", &[]);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn checksums_match_the_standard_check_values() {
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
        assert_eq!(adler32(b"Wikipedia"), 0x11E6_0398);
    }

    #[test]
    fn a_png_is_well_formed() {
        let image = png(2, 2, &[255; 16]);
        assert_eq!(&image[..8], &[0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A]);
        assert_eq!(&image[12..16], b"IHDR");
        assert_eq!(u32::from_be_bytes(image[16..20].try_into().unwrap()), 2);
        assert_eq!(&image[image.len() - 8..image.len() - 4], b"IEND");
    }

    #[test]
    fn clicks_find_their_button() {
        let layout = Layout {
            width: 340,
            height: 120,
            buttons: vec![Rect::new(16, 60, 100, 92), Rect::new(108, 60, 192, 92)],
            link: Some(Rect::new(16, 100, 140, 116)),
            ..Layout::default()
        };
        assert_eq!(layout.hit(20, 70), Some(Target::Button(0)));
        assert_eq!(layout.hit(150, 91), Some(Target::Button(1)));
        assert_eq!(layout.hit(30, 105), Some(Target::Link));
        assert_eq!(layout.hit(200, 20), None);
        assert_eq!(layout.rect(Target::Button(1)).map(|r| r.center()), Some((150, 76)));
    }
}
