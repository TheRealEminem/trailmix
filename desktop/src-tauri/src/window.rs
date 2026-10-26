//! Trailmix's own window (MainWindow.swift): the web app in a WebView2 view, so no browser is involved.
//! Closing it leaves the tray icon; links to other sites open in your browser. The page reaches the shell
//! (Quit, Show in Explorer) through the trailmix:// protocol, which the init script wires up as the same
//! `webkit.messageHandlers.trailmix` the Mac app offers, so the web app needn't know which shell it's in.

use std::borrow::Cow;
use std::sync::{mpsc, Mutex, OnceLock};
use std::time::Duration;

use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_opener::OpenerExt;
use url::Url;

pub const LABEL: &str = "main";
/// Where the bundled loading page lives (Tauri serves `frontendDist` here on Windows).
const LOADING_PAGE: &str = "http://tauri.localhost/index.html";

const BRIDGE_SCRIPT: &str = r#"(() => {
  const send = (message) => {
    try { fetch('http://trailmix.localhost/message?m=' + encodeURIComponent(String(message)), { mode: 'no-cors' }).catch(() => {}); } catch (e) {}
  };
  window.webkit = window.webkit || {};
  window.webkit.messageHandlers = window.webkit.messageHandlers || {};
  window.webkit.messageHandlers.trailmix = { postMessage: send };
})();"#;

/// The server the window shows: navigations within it stay in the window, anything else opens in the browser.
fn allowed() -> &'static Mutex<Option<Url>> {
    static ALLOWED: OnceLock<Mutex<Option<Url>>> = OnceLock::new();
    ALLOWED.get_or_init(|| Mutex::new(None))
}

fn same_site(a: &Url, b: &Url) -> bool {
    a.scheme() == b.scheme() && a.host_str() == b.host_str() && a.port_or_known_default() == b.port_or_known_default()
}

fn is_ours(url: &Url) -> bool {
    if url.scheme() == "about" || url.as_str().starts_with(LOADING_PAGE) || url.host_str() == Some("tauri.localhost") {
        return true;
    }
    match allowed().lock().unwrap_or_else(|e| e.into_inner()).as_ref() {
        Some(site) => same_site(site, url),
        None => matches!(url.host_str(), Some("127.0.0.1") | Some("localhost") | Some("::1") | Some("[::1]")),
    }
}

/// Makes the window (hidden) showing the loading page. Closing it hides it; the app lives on in the tray.
pub fn create(app: &AppHandle) -> tauri::Result<WebviewWindow> {
    let opener = app.clone();
    let window = WebviewWindowBuilder::new(app, LABEL, WebviewUrl::App("index.html".into()))
        .title("Trailmix")
        .inner_size(1280.0, 860.0)
        .min_inner_size(420.0, 560.0)
        .center()
        .visible(false)
        .initialization_script(BRIDGE_SCRIPT)
        .on_navigation(move |url| {
            if is_ours(url) {
                return true;
            }
            let _ = opener.opener().open_url(url.as_str(), None::<&str>);
            false
        })
        .build()?;
    let hide = window.clone();
    window.on_window_event(move |event| {
        if let WindowEvent::CloseRequested { api, .. } = event {
            api.prevent_close();
            let _ = hide.hide();
        }
    });
    Ok(window)
}

fn window(app: &AppHandle) -> Option<WebviewWindow> {
    app.get_webview_window(LABEL)
}

/// Shows the window at `target`. If it already shows that server, only the part after # changes (a
/// meeting or a page), so the page doesn't reload.
pub fn show(app: &AppHandle, target: &str) {
    let Some(window) = window(app) else { return };
    if let Ok(url) = Url::parse(target) {
        let current = window.url().ok();
        let same = current.map(|c| same_site(&c, &url)).unwrap_or(false);
        if same {
            if let Some(fragment) = url.fragment() {
                let hash = serde_json::to_string(&format!("#{fragment}")).unwrap_or_default();
                let _ = window.eval(format!("location.hash = {hash}"));
            }
        } else {
            *allowed().lock().unwrap_or_else(|e| e.into_inner()) = Some(url.clone());
            let _ = window.navigate(url);
        }
    }
    let _ = window.show();
    let _ = window.unminimize();
    let _ = window.set_focus();
}

/// Shows the loading page (while the engine starts).
pub fn show_loading(app: &AppHandle) {
    let Some(window) = window(app) else { return };
    let showing_it = window.url().map(|u| u.as_str().starts_with(LOADING_PAGE)).unwrap_or(false);
    if !showing_it {
        if let Ok(url) = Url::parse(LOADING_PAGE) {
            let _ = window.navigate(url);
        }
    }
    let _ = window.show();
    let _ = window.set_focus();
}

pub fn loading_status(app: &AppHandle, text: &str) {
    if let Some(window) = window(app) {
        let text = serde_json::to_string(text).unwrap_or_default();
        let _ = window.eval(format!("window.trailmixStatus && window.trailmixStatus({text})"));
    }
}

pub fn loading_failed(app: &AppHandle, text: &str) {
    if let Some(window) = window(app) {
        let text = serde_json::to_string(text).unwrap_or_default();
        let _ = window.eval(format!("window.trailmixFailed && window.trailmixFailed({text})"));
    }
}

/// Runs JavaScript in the page and returns its result as JSON text ("null" if there's no page or it threw).
pub fn evaluate(window: &WebviewWindow, script: &str) -> String {
    let (tx, rx) = mpsc::channel::<String>();
    let wrapped = format!(
        "(() => {{ try {{ return JSON.stringify((() => {{ return {script} }})()) ?? 'null' }} catch (e) {{ return 'null' }} }})()"
    );
    let js = windows::core::HSTRING::from(wrapped);
    let run = window.with_webview(move |webview| unsafe {
        let Ok(core) = webview.controller().CoreWebView2() else { return };
        let handler = webview2_com::ExecuteScriptCompletedHandler::create(Box::new(move |outcome: windows::core::Result<()>, result: String| {
            let _ = tx.send(if outcome.is_ok() { result } else { "null".into() });
            Ok(())
        }));
        let _ = core.ExecuteScript(&js, &handler);
    });
    if run.is_err() {
        return "null".into();
    }
    // ExecuteScript gives the JSON of the JS value, and our wrapper returns a string of JSON: unwrap one layer.
    match rx.recv_timeout(Duration::from_secs(15)) {
        Ok(encoded) => serde_json::from_str::<String>(&encoded).unwrap_or_else(|_| "null".into()),
        Err(_) => "null".into(),
    }
}

/// Saves what the window shows as a PNG, after `wait`.
pub fn snapshot(window: &WebviewWindow, path: &str, wait: Duration) -> bool {
    std::thread::sleep(wait);
    let (tx, rx) = mpsc::channel::<bool>();
    let file = windows::core::HSTRING::from(path);
    let run = window.with_webview(move |webview| unsafe {
        use webview2_com::Microsoft::Web::WebView2::Win32::COREWEBVIEW2_CAPTURE_PREVIEW_IMAGE_FORMAT_PNG;
        use windows::Win32::System::Com::IStream;
        use windows::Win32::UI::Shell::SHCreateStreamOnFileEx;
        const STGM_WRITE_CREATE: u32 = 0x0000_0001 | 0x0000_1000;
        const FILE_ATTRIBUTE_NORMAL: u32 = 0x80;
        let Ok(core) = webview.controller().CoreWebView2() else {
            let _ = tx.send(false);
            return;
        };
        let Ok(stream) = SHCreateStreamOnFileEx(&file, STGM_WRITE_CREATE, FILE_ATTRIBUTE_NORMAL, true, None::<&IStream>) else {
            let _ = tx.send(false);
            return;
        };
        let keep = stream.clone();
        let handler = webview2_com::CapturePreviewCompletedHandler::create(Box::new(move |outcome: windows::core::Result<()>| {
            drop(keep); // the file is complete once the capture is
            let _ = tx.send(outcome.is_ok());
            Ok(())
        }));
        // If this fails the handler (and with it the sender) is dropped, which reads as "no".
        let _ = core.CapturePreview(COREWEBVIEW2_CAPTURE_PREVIEW_IMAGE_FORMAT_PNG, &stream, &handler);
    });
    if run.is_err() {
        return false;
    }
    rx.recv_timeout(Duration::from_secs(20)).unwrap_or(false)
}

/// Messages from the page: "quit", or "reveal:<path>" (Show in Explorer for an exported meeting).
pub fn bridge(app: &AppHandle, request: tauri::http::Request<Vec<u8>>) -> tauri::http::Response<Cow<'static, [u8]>> {
    let uri = request.uri();
    if uri.path() == "/message" {
        let message = uri
            .query()
            .and_then(|q| url::form_urlencoded::parse(q.as_bytes()).find(|(k, _)| k == "m").map(|(_, v)| v.into_owned()))
            .unwrap_or_default();
        handle_message(app, &message);
    }
    tauri::http::Response::builder()
        .status(200)
        .header("Access-Control-Allow-Origin", "*")
        .header("Content-Type", "text/plain")
        .body(Cow::Borrowed(b"ok" as &[u8]))
        .unwrap()
}

fn handle_message(app: &AppHandle, message: &str) {
    if message == "quit" {
        crate::app::quit(app);
    } else if let Some(path) = message.strip_prefix("reveal:") {
        if std::path::Path::new(path).exists() {
            let _ = app.opener().reveal_item_in_dir(path);
        }
    }
}
