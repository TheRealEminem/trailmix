//! Trailmix for Windows: the web app in its own window, a tray icon that records, and the Trailmix server
//! (with its own Python, speech engine and ffmpeg) started and watched over by this process. The same
//! shape as the Mac menu bar app (helper/Sources/TrailmixHelper), talking to the same server API.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod app;
mod audio;
mod client;
mod model;
mod prefs;
mod selftest;
mod server;
mod win;
mod window;
mod wire;

use tauri::{Manager, RunEvent};

enum Mode {
    Normal,
    SelfTest(Vec<String>),
    WindowTest(Vec<String>),
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|a| a == "--list-audio") {
        selftest::list_audio();
        return;
    }
    let mode = if args.iter().any(|a| a == "--self-test") {
        Mode::SelfTest(args.clone())
    } else if args.iter().any(|a| a == "--window-test") {
        Mode::WindowTest(args.clone())
    } else {
        Mode::Normal
    };

    let built = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .register_uri_scheme_protocol("trailmix", |ctx, request| window::bridge(ctx.app_handle(), request))
        .setup(move |app| {
            let handle = app.handle().clone();
            match mode {
                Mode::Normal => app::launch(&handle)?,
                Mode::SelfTest(args) => selftest::run(handle, args),
                Mode::WindowTest(args) => selftest::window_test(handle, args),
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Trailmix");

    built.run(|app, event| match event {
        // The window closing (or being hidden) isn't the end: the app lives in the tray until Quit.
        RunEvent::ExitRequested { code: None, api, .. } => api.prevent_exit(),
        RunEvent::Exit => {
            if let Some(state) = app.try_state::<app::AppState>() {
                if let Some(server) = &state.server {
                    server.stop();
                }
            }
        }
        _ => {}
    });
}
