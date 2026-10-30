//! Updating on Linux (part of update.rs). An AppImage updates itself: the new one (Trailmix-<version>-linux-
//! <arch>.AppImage from the release, checked against its published SHA-256 and for being an AppImage) takes the
//! place of the file you run, under the same name, so your shortcuts and open at login still find it; then the
//! app quits and the new one opens. Linux lets a running program's file be replaced (the one running keeps the
//! old copy until it ends), so nothing needs to wait for the app to quit first, as on Windows.
//!
//! A .deb or .rpm install belongs to the package manager: it only says a new version is out, and Update opens
//! the release's page to download it.

use std::fs::{self, File};
use std::io::Read;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

use super::{log, Release, Saved, State, Updater, REPO};
use crate::server;

/// This computer's kind, as release files are named: Trailmix-<version>-linux-<arch>.AppImage.
#[cfg(target_arch = "aarch64")]
pub const ARCH: &str = "arm64";
#[cfg(not(target_arch = "aarch64"))]
pub const ARCH: &str = "x86_64";

/// The AppImage for this computer in a release.
pub fn installer_name(version: &str) -> String {
    format!("Trailmix-{version}-linux-{ARCH}.AppImage")
}

/// The AppImage this is running from, if it is one (the AppImage runtime says so in $APPIMAGE).
pub fn appimage() -> Option<PathBuf> {
    std::env::var_os("APPIMAGE").map(PathBuf::from).filter(|p| p.is_file())
}

/// Installed by a package (.deb, .rpm): the program is in the system's folders.
fn packaged(exe: &Path) -> bool {
    exe.starts_with("/usr/") || exe.starts_with("/opt/")
}

impl Updater {
    /// An AppImage updates itself; a packaged install only says when there's a new version. Either needs
    /// somewhere to look: the repo it was built from, or TRAILMIX_UPDATE_URL (tests).
    pub fn enabled_for(exe: &Path) -> bool {
        let somewhere = !REPO.is_empty() || std::env::var_os("TRAILMIX_UPDATE_URL").is_some();
        somewhere && (appimage().is_some() || packaged(exe))
    }

    pub fn installs_itself(&self) -> bool {
        appimage().is_some()
    }

    /// The folder the AppImage is in (the new one is downloaded next to it, then put in its place).
    pub(super) fn install_folder(&self) -> Option<PathBuf> {
        appimage()?.parent().map(Path::to_path_buf)
    }

    pub(super) fn download_and_hand_over(&self, release: &Release, folder: &Path, going_back: bool) -> Result<(), String> {
        let current = appimage().ok_or("This copy of Trailmix isn't an AppImage, so it can't update itself")?;
        let stamp = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
        // Next to the AppImage, so putting it in place is a rename on the same disk.
        let download = folder.join(format!(".trailmix-update-{}-{stamp}-{}", std::process::id(), release.name));
        log(&format!("downloading {} from {}", release.name, release.installer));
        let digest = match self.download(release, &download) {
            Ok(digest) => digest,
            Err(why) => {
                let _ = fs::remove_file(&download);
                return Err(why);
            }
        };
        let checked = (|| {
            let published = self.published_checksum(&release.checksum).ok_or("The update has no published checksum, so it wasn't installed")?;
            if digest != published {
                return Err("The update didn't match its published checksum, so it wasn't installed".to_string());
            }
            check_appimage(&download, &release.name)
        })();
        if let Err(why) = checked {
            let _ = fs::remove_file(&download);
            return Err(why);
        }
        self.set_state(State::Installing(release.version.clone()));
        let _ = fs::set_permissions(&download, fs::Permissions::from_mode(0o755));
        if let Err(e) = fs::rename(&download, &current) {
            let _ = fs::remove_file(&download);
            return Err(format!("Couldn't put the new version in place of {} ({e})", current.display()));
        }
        let data = server::data_dir();
        let mut saved = Saved::load(&data);
        if going_back {
            saved.skip_version = Some(self.current.clone()); // don't offer the one you left
            saved.previous_version = None;
        } else {
            saved.previous_version = Some(self.current.clone());
        }
        saved.last_error = None;
        saved.save(&data);
        log(&format!("installed {}", release.version));

        // Once this app has quit, the new one opens (unless TRAILMIX_UPDATE_NO_RELAUNCH, for tests).
        if std::env::var_os("TRAILMIX_UPDATE_NO_RELAUNCH").is_none() {
            let opened = Command::new("/bin/sh")
                .arg("-c")
                .arg("while kill -0 \"$0\" 2>/dev/null; do sleep 0.2; done; exec \"$1\"")
                .arg(std::process::id().to_string())
                .arg(&current)
                .env_remove("APPIMAGE")
                .env_remove("APPDIR")
                .env_remove("LD_LIBRARY_PATH")
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .process_group(0)
                .spawn();
            if let Err(e) = opened {
                log(&format!("couldn't open {} once this quits ({e})", current.display()));
            }
        }
        log(&format!("{} is in place; quitting", release.version));
        Ok(())
    }
}

/// Is this the AppImage it should be? Named as releases name it, a Linux program (ELF), and an AppImage
/// (type 2: "AI" and 2 after the ELF header's first eight bytes).
fn check_appimage(file: &Path, expected_name: &str) -> Result<(), String> {
    let not_trailmix = |why: &str| format!("The update isn't a Trailmix AppImage ({why}), so it wasn't installed");
    if !file.file_name().map(|n| n.to_string_lossy().ends_with(expected_name)).unwrap_or(false) {
        return Err(not_trailmix("wrong name"));
    }
    let mut head = [0u8; 11];
    File::open(file).and_then(|mut f| f.read_exact(&mut head)).map_err(|e| not_trailmix(&e.to_string()))?;
    if &head[..4] != b"\x7fELF" {
        return Err(not_trailmix("not a program"));
    }
    if &head[8..11] != b"AI\x02" {
        return Err(not_trailmix("not an AppImage"));
    }
    Ok(())
}

/// A packaged install's Update: the release's page, in the browser.
pub fn open_download(release: &Release) {
    let page = release.notes.clone().unwrap_or_else(|| format!("https://github.com/{REPO}/releases"));
    log(&format!("{} is out; opening {page} to download it", release.version));
    let _ = Command::new("xdg-open").arg(&page).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn();
}

/// `--apply-update` is the Windows installer's helper; on Linux an update is put in place by the app itself.
pub fn apply(_args: &[String]) -> i32 {
    log("--apply-update isn't used on Linux");
    2
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_an_appimage_of_that_name_is_taken() {
        let dir = std::env::temp_dir().join(format!("trailmix-update-test-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let name = installer_name("1.2.3");
        assert!(name.starts_with("Trailmix-1.2.3-linux-") && name.ends_with(".AppImage"));
        let good = dir.join(format!(".part-{name}"));
        fs::write(&good, b"\x7fELF\x02\x01\x01\x00AI\x02rest").unwrap();
        assert_eq!(check_appimage(&good, &name), Ok(()));
        let plain = dir.join(&name);
        fs::write(&plain, b"\x7fELF\x02\x01\x01\x00\x00\x00\x00rest").unwrap();
        assert!(check_appimage(&plain, &name).unwrap_err().contains("not an AppImage"));
        assert!(check_appimage(&good, "Trailmix-9.9.9-linux-x86_64.AppImage").unwrap_err().contains("wrong name"));
        let _ = fs::remove_dir_all(&dir);
    }
}
