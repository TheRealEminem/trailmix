//! Open at login on Linux (login.rs on Windows): ~/.config/autostart/trailmix.desktop ($XDG_CONFIG_HOME), the
//! freedesktop autostart entry GNOME, KDE and the rest read, starting this Trailmix with --at-login so it comes
//! up in the tray with its engine running but no window. For an AppImage it's the AppImage file that's started
//! (the program inside it lives in a temporary mount).

use std::path::{Path, PathBuf};

pub const FLAG: &str = "--at-login";

fn entry_path() -> PathBuf {
    crate::linux::xdg_dir("XDG_CONFIG_HOME", ".config").join("autostart").join("trailmix.desktop")
}

/// What to start: the AppImage, when this is one, else this program.
fn target(exe: &Path) -> PathBuf {
    match std::env::var_os("APPIMAGE").map(PathBuf::from) {
        Some(appimage) if appimage.is_file() => appimage,
        _ => exe.to_path_buf(),
    }
}

/// A path as a desktop entry's Exec wants it: in double quotes, with ", `, $ and \ escaped (and the
/// backslashes escaped again, as every string in the file is).
pub(crate) fn quoted(path: &Path) -> String {
    let mut out = String::from("\"");
    for c in path.to_string_lossy().chars() {
        match c {
            '"' | '`' | '$' => {
                out.push_str("\\\\");
                out.push(c);
            }
            '\\' => out.push_str("\\\\\\\\"),
            _ => out.push(c),
        }
    }
    out.push('"');
    out
}

fn entry(exe: &Path) -> String {
    format!(
        "[Desktop Entry]\nType=Application\nName=Trailmix\nComment=Records your meetings and writes the notes\nExec={} {FLAG}\nIcon=Trailmix\nTerminal=false\nX-GNOME-Autostart-enabled=true\n",
        quoted(&target(exe))
    )
}

/// On if the autostart entry starts this Trailmix and isn't turned off (Hidden, or GNOME's own switch).
pub fn enabled(exe: &Path) -> bool {
    let Ok(text) = std::fs::read_to_string(entry_path()) else { return false };
    let mut exec_ok = false;
    for line in text.lines().map(str::trim) {
        if let Some(exec) = line.strip_prefix("Exec=") {
            exec_ok = exec.starts_with(&quoted(&target(exe))) || exec.starts_with(&*target(exe).to_string_lossy());
        } else if line.eq_ignore_ascii_case("Hidden=true") || line.eq_ignore_ascii_case("X-GNOME-Autostart-enabled=false") {
            return false;
        }
    }
    exec_ok
}

pub fn set(exe: &Path, on: bool) -> bool {
    let path = entry_path();
    if on {
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        std::fs::write(&path, entry(exe)).is_ok()
    } else {
        match std::fs::remove_file(&path) {
            Ok(()) => true,
            Err(e) => e.kind() == std::io::ErrorKind::NotFound,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_entry_starts_this_program() {
        let exe = PathBuf::from("/home/sam lee/Apps/Trailmix.AppImage");
        assert_eq!(quoted(&exe), "\"/home/sam lee/Apps/Trailmix.AppImage\"");
        assert_eq!(quoted(Path::new("/a$b")), "\"/a\\\\$b\"");
        let text = entry(&exe);
        assert!(text.contains("Exec=\"/home/sam lee/Apps/Trailmix.AppImage\" --at-login\n") || std::env::var_os("APPIMAGE").is_some());
        assert!(text.starts_with("[Desktop Entry]\nType=Application\n"));
    }
}
