//! Trailmix for Windows and Linux: the web app in its own window, a tray icon that records, and the Trailmix
//! server (with its own Python, speech engine and ffmpeg) started and watched over by this process. The same
//! shape as the Mac menu bar app (helper/Sources/TrailmixHelper), talking to the same server API.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod app;
mod audio;
// The cards and open at login have a Linux counterpart with the same API (in linux/).
#[cfg_attr(target_os = "linux", path = "linux/card.rs")]
mod card;
mod catchup;
#[cfg_attr(target_os = "linux", path = "linux/catchup_card.rs")]
mod catchup_card;
mod client;
#[cfg(windows)]
mod gui;
mod hotkeys;
#[cfg(target_os = "linux")]
mod linux;
#[cfg_attr(target_os = "linux", path = "linux/login.rs")]
mod login;
mod model;
mod prefs;
mod reminder;
mod selftest;
mod server;
mod soundcheck;
mod update;
#[cfg(windows)]
mod win;
mod window;
mod wire;

use tauri::{Manager, RunEvent};

enum Mode {
    Normal { at_login: bool },
    SelfTest(Vec<String>),
    WindowTest(Vec<String>),
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    // A copy of this program installing an update over the app (update.rs): no window, no tray.
    if args.iter().any(|a| a == update::APPLY_FLAG) {
        std::process::exit(update::apply(&args));
    }
    if args.iter().any(|a| a == "--list-audio") {
        selftest::list_audio();
        return;
    }
    #[cfg(target_os = "linux")]
    {
        if args.iter().any(|a| a == "--capture-test") {
            std::process::exit(linux::selftest::capture_test(&args));
        }
        linux::before_window();
    }
    let context: tauri::Context<tauri::Wry> = tauri::generate_context!();
    if args.iter().any(|a| a == "--version") {
        println!("{}", context.package_info().version);
        return;
    }
    let mode = if args.iter().any(|a| a == "--self-test") {
        Mode::SelfTest(args.clone())
    } else if args.iter().any(|a| a == "--window-test") {
        Mode::WindowTest(args.clone())
    } else {
        Mode::Normal { at_login: args.iter().any(|a| a == login::FLAG) }
    };

    let built = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .register_uri_scheme_protocol("trailmix", |ctx, request| window::bridge(ctx.app_handle(), request))
        .setup(move |app| {
            let handle = app.handle().clone();
            match mode {
                Mode::Normal { at_login } => app::launch(&handle, at_login)?,
                Mode::SelfTest(args) => selftest::run(handle, args),
                Mode::WindowTest(args) => selftest::window_test(handle, args),
            }
            Ok(())
        })
        .build(context)
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
