//! The plain Win32 window, message, input and drawing calls the reminder card (card.rs) and the hotkeys
//! (hotkeys.rs) need, declared here as win.rs does, so they don't depend on the `windows` crate's features.

#![allow(non_snake_case, dead_code)]

use std::ffi::c_void;

pub type Hwnd = isize;

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Rect {
    pub left: i32,
    pub top: i32,
    pub right: i32,
    pub bottom: i32,
}

impl Rect {
    pub fn new(left: i32, top: i32, right: i32, bottom: i32) -> Rect {
        Rect { left, top, right, bottom }
    }
    pub fn contains(&self, x: i32, y: i32) -> bool {
        x >= self.left && x < self.right && y >= self.top && y < self.bottom
    }
    pub fn center(&self) -> (i32, i32) {
        ((self.left + self.right) / 2, (self.top + self.bottom) / 2)
    }
    pub fn width(&self) -> i32 {
        self.right - self.left
    }
    pub fn height(&self) -> i32 {
        self.bottom - self.top
    }
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct Point {
    pub x: i32,
    pub y: i32,
}

#[repr(C)]
pub struct Msg {
    pub hwnd: Hwnd,
    pub message: u32,
    pub wparam: usize,
    pub lparam: isize,
    pub time: u32,
    pub pt: Point,
    pub private: u32,
}

impl Msg {
    pub fn zeroed() -> Msg {
        Msg { hwnd: 0, message: 0, wparam: 0, lparam: 0, time: 0, pt: Point::default(), private: 0 }
    }
}

pub type WndProc = unsafe extern "system" fn(Hwnd, u32, usize, isize) -> isize;

#[repr(C)]
pub struct WndClassExW {
    pub size: u32,
    pub style: u32,
    pub proc_: WndProc,
    pub cls_extra: i32,
    pub wnd_extra: i32,
    pub instance: isize,
    pub icon: isize,
    pub cursor: isize,
    pub background: isize,
    pub menu_name: *const u16,
    pub class_name: *const u16,
    pub icon_small: isize,
}

#[repr(C)]
pub struct PaintStruct {
    pub hdc: isize,
    pub erase: i32,
    pub paint: Rect,
    pub restore: i32,
    pub inc_update: i32,
    pub reserved: [u8; 32],
}

#[repr(C)]
pub struct TrackMouseEvent {
    pub size: u32,
    pub flags: u32,
    pub hwnd: Hwnd,
    pub hover_time: u32,
}

#[repr(C)]
pub struct BitmapInfoHeader {
    pub size: u32,
    pub width: i32,
    pub height: i32,
    pub planes: u16,
    pub bit_count: u16,
    pub compression: u32,
    pub size_image: u32,
    pub x_ppm: i32,
    pub y_ppm: i32,
    pub clr_used: u32,
    pub clr_important: u32,
}

#[repr(C)]
pub struct BitmapInfo {
    pub header: BitmapInfoHeader,
    pub colors: [u32; 1],
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct KeybdInput {
    pub vk: u16,
    pub scan: u16,
    pub flags: u32,
    pub time: u32,
    pub extra: usize,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct MouseInput {
    pub dx: i32,
    pub dy: i32,
    pub data: u32,
    pub flags: u32,
    pub time: u32,
    pub extra: usize,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub union InputUnion {
    pub ki: KeybdInput,
    pub mi: MouseInput,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Input {
    pub kind: u32,
    pub u: InputUnion,
}

pub const INPUT_MOUSE: u32 = 0;
pub const INPUT_KEYBOARD: u32 = 1;
pub const KEYEVENTF_EXTENDEDKEY: u32 = 0x1;
pub const KEYEVENTF_KEYUP: u32 = 0x2;
pub const MOUSEEVENTF_LEFTDOWN: u32 = 0x2;
pub const MOUSEEVENTF_LEFTUP: u32 = 0x4;

pub const WM_DESTROY: u32 = 0x0002;
pub const WM_PAINT: u32 = 0x000F;
pub const WM_CLOSE: u32 = 0x0010;
pub const WM_ERASEBKGND: u32 = 0x0014;
pub const WM_SETCURSOR: u32 = 0x0020;
pub const WM_MOUSEACTIVATE: u32 = 0x0021;
pub const WM_NCDESTROY: u32 = 0x0082;
pub const WM_KEYDOWN: u32 = 0x0100;
pub const WM_KEYUP: u32 = 0x0101;
pub const WM_CHAR: u32 = 0x0102;
pub const WM_SYSKEYDOWN: u32 = 0x0104;
pub const WM_SYSKEYUP: u32 = 0x0105;
pub const WM_SYSCHAR: u32 = 0x0106;
pub const WM_TIMER: u32 = 0x0113;
pub const WM_MOUSEMOVE: u32 = 0x0200;
pub const WM_LBUTTONDOWN: u32 = 0x0201;
pub const WM_LBUTTONUP: u32 = 0x0202;
pub const WM_MOUSELEAVE: u32 = 0x02A3;
pub const WM_HOTKEY: u32 = 0x0312;
pub const WM_USER: u32 = 0x0400;
pub const WM_APP: u32 = 0x8000;
pub const MA_NOACTIVATE: isize = 3;
pub const MA_ACTIVATE: isize = 1;
pub const WM_ACTIVATE: u32 = 0x0006;
pub const WA_INACTIVE: usize = 0;
pub const WM_SETFONT: u32 = 0x0030;
pub const WM_CTLCOLOREDIT: u32 = 0x0133;
pub const WM_MOUSEWHEEL: u32 = 0x020A;
pub const EM_SETCUEBANNER: u32 = 0x1501;
pub const KEYEVENTF_UNICODE: u32 = 0x4;
pub const VK_RETURN: u32 = 0x0D;
pub const PM_NOREMOVE: u32 = 0;

pub const WS_POPUP: u32 = 0x8000_0000;
pub const WS_CHILD: u32 = 0x4000_0000;
pub const WS_VISIBLE: u32 = 0x1000_0000;
pub const WS_CLIPCHILDREN: u32 = 0x0200_0000;
pub const ES_AUTOHSCROLL: u32 = 0x0080;
pub const SW_HIDE: i32 = 0;
pub const SWP_NOZORDER: u32 = 0x0004;
pub const WS_EX_TOPMOST: u32 = 0x0000_0008;
pub const WS_EX_TOOLWINDOW: u32 = 0x0000_0080;
pub const WS_EX_NOACTIVATE: u32 = 0x0800_0000;
pub const CS_DROPSHADOW: u32 = 0x0002_0000;
pub const GWL_EXSTYLE: i32 = -20;
pub const GWLP_USERDATA: i32 = -21;
pub const SW_SHOW: i32 = 5;
pub const SW_SHOWNOACTIVATE: i32 = 4;
pub const HWND_TOPMOST: Hwnd = -1;
pub const SWP_NOACTIVATE: u32 = 0x0010;
pub const SWP_SHOWWINDOW: u32 = 0x0040;
pub const SPI_GETWORKAREA: u32 = 0x0030;
pub const TME_LEAVE: u32 = 0x0000_0002;
pub const IDC_ARROW: usize = 32512;
pub const IDC_HAND: usize = 32649;
pub const HTCLIENT: isize = 1;

pub const DT_CENTER: u32 = 0x1;
pub const DT_RIGHT: u32 = 0x2;
pub const DT_EDITCONTROL: u32 = 0x2000;
pub const DT_END_ELLIPSIS: u32 = 0x8000;
pub const DT_VCENTER: u32 = 0x4;
pub const DT_WORDBREAK: u32 = 0x10;
pub const DT_SINGLELINE: u32 = 0x20;
pub const DT_CALCRECT: u32 = 0x400;
pub const DT_NOPREFIX: u32 = 0x800;
pub const TRANSPARENT: i32 = 1;
pub const SRCCOPY: u32 = 0x00CC_0020;
pub const NULL_BRUSH: i32 = 5;

#[link(name = "user32")]
extern "system" {
    pub fn RegisterClassExW(class: *const WndClassExW) -> u16;
    pub fn CreateWindowExW(
        ex_style: u32,
        class: *const u16,
        name: *const u16,
        style: u32,
        x: i32,
        y: i32,
        width: i32,
        height: i32,
        parent: Hwnd,
        menu: isize,
        instance: isize,
        param: *const c_void,
    ) -> Hwnd;
    pub fn DefWindowProcW(hwnd: Hwnd, msg: u32, wparam: usize, lparam: isize) -> isize;
    pub fn DestroyWindow(hwnd: Hwnd) -> i32;
    pub fn ShowWindow(hwnd: Hwnd, cmd: i32) -> i32;
    pub fn SetWindowPos(hwnd: Hwnd, after: Hwnd, x: i32, y: i32, cx: i32, cy: i32, flags: u32) -> i32;
    pub fn GetMessageW(msg: *mut Msg, hwnd: Hwnd, min: u32, max: u32) -> i32;
    pub fn PeekMessageW(msg: *mut Msg, hwnd: Hwnd, min: u32, max: u32, remove: u32) -> i32;
    pub fn TranslateMessage(msg: *const Msg) -> i32;
    pub fn DispatchMessageW(msg: *const Msg) -> isize;
    pub fn PostMessageW(hwnd: Hwnd, msg: u32, wparam: usize, lparam: isize) -> i32;
    pub fn PostThreadMessageW(thread: u32, msg: u32, wparam: usize, lparam: isize) -> i32;
    pub fn PostQuitMessage(code: i32);
    pub fn SetWindowLongPtrW(hwnd: Hwnd, index: i32, value: isize) -> isize;
    pub fn GetWindowLongPtrW(hwnd: Hwnd, index: i32) -> isize;
    pub fn BeginPaint(hwnd: Hwnd, paint: *mut PaintStruct) -> isize;
    pub fn EndPaint(hwnd: Hwnd, paint: *const PaintStruct) -> i32;
    pub fn InvalidateRect(hwnd: Hwnd, rect: *const Rect, erase: i32) -> i32;
    pub fn GetClientRect(hwnd: Hwnd, rect: *mut Rect) -> i32;
    pub fn GetWindowRect(hwnd: Hwnd, rect: *mut Rect) -> i32;
    pub fn SetTimer(hwnd: Hwnd, id: usize, millis: u32, func: *const c_void) -> usize;
    pub fn LoadCursorW(instance: isize, name: *const u16) -> isize;
    pub fn SetCursor(cursor: isize) -> isize;
    pub fn SystemParametersInfoW(action: u32, param: u32, pv: *mut c_void, ini: u32) -> i32;
    pub fn GetDpiForSystem() -> u32;
    pub fn GetForegroundWindow() -> Hwnd;
    pub fn SetForegroundWindow(hwnd: Hwnd) -> i32;
    pub fn IsWindowVisible(hwnd: Hwnd) -> i32;
    pub fn GetKeyState(vk: i32) -> i16;
    pub fn RegisterHotKey(hwnd: Hwnd, id: i32, modifiers: u32, vk: u32) -> i32;
    pub fn UnregisterHotKey(hwnd: Hwnd, id: i32) -> i32;
    pub fn SendInput(count: u32, inputs: *const Input, size: i32) -> u32;
    pub fn SetCursorPos(x: i32, y: i32) -> i32;
    pub fn TrackMouseEvent(event: *mut TrackMouseEvent) -> i32;
    pub fn MapVirtualKeyW(code: u32, map: u32) -> u32;
    pub fn FillRect(hdc: isize, rect: *const Rect, brush: isize) -> i32;
    pub fn FrameRect(hdc: isize, rect: *const Rect, brush: isize) -> i32;
    pub fn DrawTextW(hdc: isize, text: *const u16, count: i32, rect: *mut Rect, format: u32) -> i32;
    pub fn MessageBeep(kind: u32) -> i32;
    pub fn SendMessageW(hwnd: Hwnd, msg: u32, wparam: usize, lparam: isize) -> isize;
    pub fn SetWindowTextW(hwnd: Hwnd, text: *const u16) -> i32;
    pub fn GetWindowTextW(hwnd: Hwnd, text: *mut u16, max: i32) -> i32;
    pub fn GetWindowTextLengthW(hwnd: Hwnd) -> i32;
    pub fn SetFocus(hwnd: Hwnd) -> Hwnd;
    pub fn GetFocus() -> Hwnd;
    pub fn EnableWindow(hwnd: Hwnd, enable: i32) -> i32;
    pub fn IsWindow(hwnd: Hwnd) -> i32;
    pub fn GetCursorPos(point: *mut Point) -> i32;
    pub fn ScreenToClient(hwnd: Hwnd, point: *mut Point) -> i32;
    pub fn MoveWindow(hwnd: Hwnd, x: i32, y: i32, w: i32, h: i32, repaint: i32) -> i32;
}

#[link(name = "kernel32")]
extern "system" {
    pub fn GetCurrentThreadId() -> u32;
    pub fn GetModuleHandleW(name: *const u16) -> isize;
}

#[link(name = "gdi32")]
extern "system" {
    pub fn CreateFontW(
        height: i32,
        width: i32,
        escapement: i32,
        orientation: i32,
        weight: i32,
        italic: u32,
        underline: u32,
        strike: u32,
        charset: u32,
        out_precision: u32,
        clip_precision: u32,
        quality: u32,
        pitch: u32,
        face: *const u16,
    ) -> isize;
    pub fn SelectObject(hdc: isize, object: isize) -> isize;
    pub fn DeleteObject(object: isize) -> i32;
    pub fn GetStockObject(kind: i32) -> isize;
    pub fn CreateSolidBrush(color: u32) -> isize;
    pub fn CreatePen(style: i32, width: i32, color: u32) -> isize;
    pub fn RoundRect(hdc: isize, left: i32, top: i32, right: i32, bottom: i32, width: i32, height: i32) -> i32;
    pub fn SetTextColor(hdc: isize, color: u32) -> u32;
    pub fn SetBkMode(hdc: isize, mode: i32) -> i32;
    pub fn CreateCompatibleDC(hdc: isize) -> isize;
    pub fn CreateCompatibleBitmap(hdc: isize, width: i32, height: i32) -> isize;
    pub fn DeleteDC(hdc: isize) -> i32;
    pub fn CreateDIBSection(hdc: isize, info: *const BitmapInfo, usage: u32, bits: *mut *mut c_void, section: isize, offset: u32) -> isize;
    pub fn BitBlt(hdc: isize, x: i32, y: i32, w: i32, h: i32, src: isize, sx: i32, sy: i32, rop: u32) -> i32;
    pub fn GdiFlush() -> i32;
    pub fn SetBkColor(hdc: isize, color: u32) -> u32;
    pub fn SaveDC(hdc: isize) -> i32;
    pub fn RestoreDC(hdc: isize, saved: i32) -> i32;
    pub fn IntersectClipRect(hdc: isize, left: i32, top: i32, right: i32, bottom: i32) -> i32;
    pub fn Polygon(hdc: isize, points: *const Point, count: i32) -> i32;
}

#[link(name = "dwmapi")]
extern "system" {
    pub fn DwmSetWindowAttribute(hwnd: Hwnd, attribute: u32, value: *const c_void, size: u32) -> i32;
}

#[link(name = "winmm")]
extern "system" {
    pub fn PlaySoundW(sound: *const u16, module: isize, flags: u32) -> i32;
}

/// A string as Windows wants it: UTF-16, ending in a zero.
pub fn wstr(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

/// The x and y packed into a mouse message's lParam.
pub fn point_of(lparam: isize) -> (i32, i32) {
    ((lparam & 0xFFFF) as u16 as i16 as i32, ((lparam >> 16) & 0xFFFF) as u16 as i16 as i32)
}

pub fn lparam_of(x: i32, y: i32) -> isize {
    (((y as u16 as u32) << 16) | (x as u16 as u32)) as isize
}

/// A colour as GDI wants it (0x00BBGGRR).
pub const fn rgb(r: u8, g: u8, b: u8) -> u32 {
    r as u32 | (g as u32) << 8 | (b as u32) << 16
}

/// The screen's usable area (without the taskbar), on the main display.
pub fn work_area() -> Rect {
    let mut area = Rect::default();
    unsafe {
        if SystemParametersInfoW(SPI_GETWORKAREA, 0, &mut area as *mut Rect as *mut c_void, 0) == 0 {
            area = Rect::new(0, 0, 1280, 720);
        }
    }
    area
}

/// How much bigger than 96 dpi things are drawn (1.0, 1.25, 1.5…).
pub fn scale() -> f64 {
    let dpi = unsafe { GetDpiForSystem() };
    if dpi == 0 {
        1.0
    } else {
        dpi as f64 / 96.0
    }
}

pub fn foreground() -> Hwnd {
    unsafe { GetForegroundWindow() }
}

/// Types text as a keyboard would (each character as itself, whatever the layout). False if Windows didn't take it.
pub fn type_text(text: &str) -> bool {
    let mut inputs = Vec::new();
    for unit in text.encode_utf16() {
        for flags in [KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP] {
            inputs.push(Input { kind: INPUT_KEYBOARD, u: InputUnion { ki: KeybdInput { vk: 0, scan: unit, flags, time: 0, extra: 0 } } });
        }
    }
    unsafe { SendInput(inputs.len() as u32, inputs.as_ptr(), std::mem::size_of::<Input>() as i32) == inputs.len() as u32 }
}

/// Presses and lets go of a key (VK_RETURN, say).
pub fn press_key(vk: u16) -> bool {
    let key = |flags: u32| Input { kind: INPUT_KEYBOARD, u: InputUnion { ki: KeybdInput { vk, scan: 0, flags, time: 0, extra: 0 } } };
    let inputs = [key(0), key(KEYEVENTF_KEYUP)];
    unsafe { SendInput(2, inputs.as_ptr(), std::mem::size_of::<Input>() as i32) == 2 }
}

/// A real left click at a point on the screen, as if with the mouse. False if Windows didn't take it
/// (no interactive desktop, say).
pub fn click_at(x: i32, y: i32) -> bool {
    unsafe {
        if SetCursorPos(x, y) == 0 {
            return false;
        }
        let event = |flags: u32| Input { kind: INPUT_MOUSE, u: InputUnion { mi: MouseInput { dx: 0, dy: 0, data: 0, flags, time: 0, extra: 0 } } };
        let inputs = [event(MOUSEEVENTF_LEFTDOWN), event(MOUSEEVENTF_LEFTUP)];
        SendInput(inputs.len() as u32, inputs.as_ptr(), std::mem::size_of::<Input>() as i32) == inputs.len() as u32
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mouse_positions_pack_and_unpack() {
        assert_eq!(point_of(lparam_of(120, 48)), (120, 48));
        assert_eq!(point_of(lparam_of(-5, 7)), (-5, 7));
    }

    #[test]
    fn input_is_the_size_windows_expects() {
        // INPUT is 40 bytes on 64-bit Windows; SendInput refuses any other size.
        assert_eq!(std::mem::size_of::<Input>(), 40);
        assert_eq!(std::mem::size_of::<Msg>(), 48);
    }

    #[test]
    fn colours_are_bgr() {
        assert_eq!(rgb(0x23, 0x63, 0x47), 0x0047_6323);
    }
}
