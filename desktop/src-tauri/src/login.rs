//! Open at login (Prefs.openAtLogin on the Mac): a "Trailmix" value under HKCU\...\Run that starts this
//! Trailmix.exe with --at-login, so it comes up in the tray with its engine running but no window.

use std::path::Path;

use crate::win;

const RUN: &str = "Software\\Microsoft\\Windows\\CurrentVersion\\Run";
/// Where Task Manager's Startup apps tab keeps the ones you turned off.
const APPROVED: &str = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\StartupApproved\\Run";
const NAME: &str = "Trailmix";
pub const FLAG: &str = "--at-login";

fn command(exe: &Path) -> String {
    format!("\"{}\" {FLAG}", exe.display())
}

/// On if Windows will start this Trailmix.exe at login: the Run value points here and it isn't turned off
/// in Task Manager (StartupApproved: an odd first byte means off).
pub fn enabled(exe: &Path) -> bool {
    let Some(value) = win::reg_get_string(RUN, NAME) else { return false };
    if !points_at(&value, exe) {
        return false;
    }
    match win::reg_get_binary(APPROVED, NAME) {
        Some(bytes) => bytes.first().map(|b| b & 1 == 0).unwrap_or(true),
        None => true,
    }
}

pub fn set(exe: &Path, on: bool) -> bool {
    // Task Manager's "off" would outlast turning it on here; forget it either way.
    win::reg_delete(APPROVED, NAME);
    if on {
        win::reg_set_string(RUN, NAME, &command(exe))
    } else {
        win::reg_delete(RUN, NAME)
    }
}

/// The Run value starts this executable (it's "\"C:\\...\\Trailmix.exe\" --at-login", or just a path).
fn points_at(value: &str, exe: &Path) -> bool {
    let value = value.trim();
    let path = match value.strip_prefix('"') {
        Some(rest) => rest.split('"').next().unwrap_or(""),
        None => value.split(" --").next().unwrap_or(value),
    };
    path.trim().eq_ignore_ascii_case(&exe.to_string_lossy())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn the_run_value_names_this_exe() {
        let exe = PathBuf::from("C:\\Users\\Sam Lee\\AppData\\Local\\Trailmix\\Trailmix.exe");
        assert!(points_at(&command(&exe), &exe));
        assert!(points_at("\"c:\\users\\sam lee\\appdata\\local\\trailmix\\trailmix.exe\"", &exe));
        assert!(points_at("C:\\Users\\Sam Lee\\AppData\\Local\\Trailmix\\Trailmix.exe --at-login", &exe));
        assert!(!points_at("\"C:\\Elsewhere\\Trailmix.exe\" --at-login", &exe));
    }
}
