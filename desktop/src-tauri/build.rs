use std::process::Command;

/// Bakes in where the app looks for updates (GitHub owner/repo), as scripts/build-app.sh does for the Mac:
/// TRAILMIX_UPDATE_REPO, else this checkout's origin. Empty (no updates) when neither says.
fn main() {
    println!("cargo:rerun-if-env-changed=TRAILMIX_UPDATE_REPO");
    let repo = std::env::var("TRAILMIX_UPDATE_REPO").ok().filter(|r| !r.trim().is_empty()).or_else(origin_repo).unwrap_or_default();
    println!("cargo:rustc-env=TRAILMIX_UPDATE_REPO={}", repo.trim());
    tauri_build::build()
}

fn origin_repo() -> Option<String> {
    let out = Command::new("git").args(["remote", "get-url", "origin"]).output().ok()?;
    if !out.status.success() {
        return None;
    }
    parse_repo(String::from_utf8(out.stdout).ok()?.trim())
}

/// "https://github.com/owner/name(.git)" or "git@github.com:owner/name(.git)" -> "owner/name".
fn parse_repo(url: &str) -> Option<String> {
    let at = url.find("github.com")?;
    let rest = url[at + "github.com".len()..].trim_start_matches([':', '/']);
    let rest = rest.strip_suffix(".git").unwrap_or(rest).trim_end_matches('/');
    let parts: Vec<&str> = rest.split('/').collect();
    (parts.len() == 2 && parts.iter().all(|p| !p.is_empty())).then(|| rest.to_string())
}
