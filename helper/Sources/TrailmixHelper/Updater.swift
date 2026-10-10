import AppKit
import CryptoKit
import Foundation

/// Keeps Trailmix.app up to date from its GitHub releases: checks at launch and every few hours; on request
/// downloads the new disk image, checks it against the SHA-256 published with the release, makes sure the
/// app inside is Trailmix (same bundle ID, intact signature), swaps it in place and relaunches.
///
/// No Apple Developer ID is needed for this: a file the app downloads itself isn't quarantined, so macOS
/// doesn't block the new version. Until Trailmix is signed, though, macOS sees each version as a new app and
/// asks for the microphone and system-audio permissions again after an update.
@MainActor
final class Updater: ObservableObject {
    static let shared = Updater()

    enum State: Equatable {
        case idle
        case checking
        case upToDate
        case offline  // couldn't reach GitHub to check
        case available(String)
        case downloading(String, Double?)
        case installing(String)
        case failed(String)
    }

    @Published private(set) var state: State = .idle
    private var latest: Release?
    /// The version to go back to if this one lets you down (Settings → Updates → Go back).
    private(set) var previous: Release?
    private var timer: Timer?
    private(set) var checkedAt: Date?

    /// The version this one replaced, remembered when an update installs (older versions didn't, so without it
    /// the release just below this one is offered).
    private static var replacedVersion: String? {
        get { UserDefaults.standard.string(forKey: "previousVersion") }
        set { UserDefaults.standard.set(newValue, forKey: "previousVersion") }
    }

    /// A version you went back from: not offered again until a newer one comes out.
    var skipped: String? {
        get { UserDefaults.standard.string(forKey: "skipVersion") }
        set {
            UserDefaults.standard.set(newValue, forKey: "skipVersion")
            Task { await check() }
        }
    }

    struct Release {
        let version: String
        let notes: URL?
        let dmg: URL
        let checksum: URL?
        let prerelease: Bool
    }

    static let current = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"

    /// Beta updates: also offer pre-releases, the versions that go out to testers before everyone else.
    var beta: Bool {
        get { UserDefaults.standard.bool(forKey: "betaUpdates") }
        set {
            UserDefaults.standard.set(newValue, forKey: "betaUpdates")
            if !newValue, case .available = state, latest?.prerelease == true { latest = nil; state = .idle }
            Task { await check() }
        }
    }

    /// Where releases are looked up: TRAILMIX_UPDATE_URL (tests), else the repo baked into Info.plist. GitHub's
    /// "latest" never includes pre-releases; with beta updates the recent releases are listed instead.
    private var feed: URL? {
        if let override = ProcessInfo.processInfo.environment["TRAILMIX_UPDATE_URL"] { return URL(string: override) }
        guard let repo = Self.repo else { return nil }
        return URL(string: beta ? "https://api.github.com/repos/\(repo)/releases?per_page=20"
                                : "https://api.github.com/repos/\(repo)/releases/latest")
    }

    /// All recent releases, for finding the one to go back to: TRAILMIX_RELEASES_URL (tests), else GitHub's list.
    private var releasesFeed: URL? {
        if let override = ProcessInfo.processInfo.environment["TRAILMIX_RELEASES_URL"] { return URL(string: override) }
        guard let repo = Self.repo else { return nil }
        return URL(string: "https://api.github.com/repos/\(repo)/releases?per_page=30")
    }

    private static var repo: String? {
        (Bundle.main.object(forInfoDictionaryKey: "TrailmixUpdateRepo") as? String).flatMap { $0.isEmpty ? nil : $0 }
    }

    /// Only the full Trailmix.app updates itself (not a development build or the thin helper).
    static var enabled: Bool {
        Bundled.isBundled && (repo != nil || ProcessInfo.processInfo.environment["TRAILMIX_UPDATE_URL"] != nil)
    }

    func begin() {
        guard Self.enabled else { return }
        let delay = Double(ProcessInfo.processInfo.environment["TRAILMIX_UPDATE_DELAY"] ?? "") ?? 20  // tests: shorter
        Task {
            try? await Task.sleep(for: .seconds(delay))  // let the app settle first
            await check()
        }
        timer = Timer.scheduledTimer(withTimeInterval: 6 * 3600, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in await self.check() }
        }
    }

    /// For the window: {"current", "state", "version", "progress", "error", "notes", "checked_at"}, or nil for a
    /// build that doesn't update itself.
    var report: [String: Any]? {
        guard Self.enabled else { return nil }
        var out: [String: Any] = ["current": Self.current, "checked_at": checkedAt.map { $0.timeIntervalSince1970 } ?? NSNull(),
                                  "beta": beta, "prerelease": latest?.prerelease ?? false,
                                  "previous": previous?.version ?? NSNull(), "skipped": skipped ?? NSNull(),
                                  "going_back": goingBack]
        switch state {
        case .idle: out["state"] = "idle"
        case .checking: out["state"] = "checking"
        case .upToDate: out["state"] = "up-to-date"
        case .offline: out["state"] = "offline"
        case .available(let v): out.merge(["version": v, "state": "available", "notes": latest?.notes?.absoluteString ?? ""]) { $1 }
        case .downloading(let v, let p): out.merge(["version": v, "state": "downloading", "progress": p ?? NSNull()]) { $1 }
        case .installing(let v): out.merge(["version": v, "state": "installing"]) { $1 }
        case .failed(let why): out.merge(["version": latest?.version ?? "", "state": "failed", "error": why]) { $1 }
        }
        return out
    }

    /// Looks for a new version. `manual`: you asked (Help → Check for Updates…), so show "Checking…" meanwhile.
    func check(manual: Bool = false) async {
        guard let feed else { return }
        if case .downloading = state { return }
        if case .installing = state { return }
        if manual, latest == nil { state = .checking }
        var request = URLRequest(url: feed)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let parsed = try? JSONSerialization.jsonObject(with: data),
              let json = Self.newest(parsed, beta: beta),
              let tag = json["tag_name"] as? String else {
            if latest == nil, manual || state == .checking { state = .offline }
            return
        }
        checkedAt = Date()
        await findPrevious()
        // A version you went back from stays skipped; a newer one than it is offered as usual.
        if let skip = skipped, !Self.isNewer(String(tag.trimmingPrefix("v")), than: skip) {
            latest = nil
            state = .upToDate
            return
        }
        guard let release = Self.release(json), Self.isNewer(release.version, than: Self.current) else {
            latest = nil
            state = .upToDate
            return
        }
        latest = release
        state = .available(release.version)
    }

    /// A release from GitHub's JSON, if it has a disk image for this Mac.
    static func release(_ json: [String: Any]) -> Release? {
        guard let tag = json["tag_name"] as? String else { return nil }
        let version = String(tag.trimmingPrefix("v"))
        let assets = (json["assets"] as? [[String: Any]]) ?? []
        func asset(_ suffix: String) -> URL? {
            assets.first { ($0["name"] as? String)?.hasSuffix(suffix) == true && ($0["name"] as? String)?.contains(version) == true }
                .flatMap { $0["browser_download_url"] as? String }.flatMap(URL.init(string:))
        }
        guard let dmg = asset("-arm64.dmg") else { return nil }
        return Release(version: version, notes: (json["html_url"] as? String).flatMap(URL.init(string:)), dmg: dmg,
                       checksum: asset("-arm64.dmg.sha256"), prerelease: json["prerelease"] as? Bool ?? false)
    }

    /// The release to go back to: the one this version replaced if it's still published, else the newest
    /// one older than this (a pre-release only with beta updates on).
    private func findPrevious() async {
        guard let feed = releasesFeed else { return }
        var request = URLRequest(url: feed)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let list = (try? JSONSerialization.jsonObject(with: data)) as? [[String: Any]] else { return }
        previous = Self.previous(in: list, current: Self.current, replaced: Self.replacedVersion, beta: beta)
    }

    static func previous(in list: [[String: Any]], current: String, replaced: String?, beta: Bool) -> Release? {
        let older = list.filter { $0["draft"] as? Bool != true }.compactMap(release).filter { isNewer(current, than: $0.version) }
        if let replaced, let same = older.first(where: { $0.version == replaced }) { return same }
        return older.filter { beta || !$0.prerelease }.max { isNewer($1.version, than: $0.version) }
    }

    /// The release to offer from GitHub's answer: one release ("latest"), or a list (beta updates) from which
    /// the highest version that isn't a draft (and, without beta updates, isn't a pre-release).
    static func newest(_ parsed: Any, beta: Bool) -> [String: Any]? {
        if let one = parsed as? [String: Any] { return one }
        let usable = (parsed as? [[String: Any]] ?? []).filter {
            $0["draft"] as? Bool != true && (beta || $0["prerelease"] as? Bool != true) && $0["tag_name"] is String
        }
        func version(_ r: [String: Any]) -> String { String((r["tag_name"] as! String).trimmingPrefix("v")) }
        return usable.max { isNewer(version($1), than: version($0)) }
    }

    static func isNewer(_ a: String, than b: String) -> Bool {
        let x = a.split(separator: ".").map { Int($0) ?? 0 }, y = b.split(separator: ".").map { Int($0) ?? 0 }
        for i in 0..<max(x.count, y.count) {
            let p = i < x.count ? x[i] : 0, q = i < y.count ? y[i] : 0
            if p != q { return p > q }
        }
        return false
    }

    // MARK: Installing

    func install() {
        guard let release = latest else { return }
        install(release, goingBack: false)
    }

    /// Settings → Updates → Go back: installs the previous version the same checked way, and skips this one
    /// until a newer version comes out. Your meetings stay (the database works with the older version too).
    func goBack() {
        guard let release = previous else {
            state = .failed("There's no earlier version to go back to.")
            return
        }
        install(release, goingBack: true)
    }

    private(set) var goingBack = false

    private func install(_ release: Release, goingBack: Bool) {
        if case .downloading = state { return }
        if case .installing = state { return }
        if HelperModel.shared.phase != .idle || HelperModel.shared.other != nil {
            state = .failed("Finish the recording first, then update.")
            return
        }
        let app = Bundle.main.bundleURL
        if app.path.contains("/AppTranslocation/") || !FileManager.default.isWritableFile(atPath: app.deletingLastPathComponent().path) {
            state = .failed("Move Trailmix to your Applications folder first; it can't update itself where it is now.")
            return
        }
        self.goingBack = goingBack
        state = .downloading(release.version, nil)
        Task {
            do {
                let image = try await download(release)
                state = .installing(release.version)
                let fresh = try await Self.unpack(image, next: app)
                try Self.swap(app, with: fresh)
                if goingBack {
                    UserDefaults.standard.set(Self.current, forKey: "skipVersion")  // don't offer the one you left
                    Self.replacedVersion = nil
                } else {
                    Self.replacedVersion = Self.current
                }
                Self.relaunch(app)
            } catch {
                self.goingBack = false
                state = .failed((error as? CaptureError)?.message ?? error.localizedDescription)
            }
        }
    }

    private func download(_ release: Release) async throws -> URL {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("trailmix-update-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let target = folder.appendingPathComponent("Trailmix.dmg")

        // A plain download task (fast, straight to disk); its progress is shown while it runs.
        let downloaded: URL = try await withCheckedThrowingContinuation { done in
            let task = URLSession.shared.downloadTask(with: release.dmg) { file, response, error in
                if let error { return done.resume(throwing: error) }
                guard let file, (response as? HTTPURLResponse)?.statusCode == 200 else {
                    return done.resume(throwing: CaptureError("Couldn't download the update"))
                }
                do {
                    try FileManager.default.moveItem(at: file, to: target)  // the temporary file goes away after this callback
                    done.resume(returning: target)
                } catch {
                    done.resume(throwing: error)
                }
            }
            let progress = task.progress
            Task { @MainActor [weak self] in
                while !progress.isFinished, !progress.isCancelled {
                    if progress.totalUnitCount > 0 { self?.state = .downloading(release.version, progress.fractionCompleted) }
                    try? await Task.sleep(for: .milliseconds(300))
                }
            }
            task.resume()
        }

        guard let checksumURL = release.checksum,
              let (sumData, _) = try? await URLSession.shared.data(from: checksumURL),
              let published = String(data: sumData, encoding: .utf8)?.split(separator: " ").first.map(String.init)?.lowercased()
        else { throw CaptureError("The update has no published checksum, so it wasn't installed") }
        guard try Self.sha256(of: downloaded) == published else {
            throw CaptureError("The update didn't match its published checksum, so it wasn't installed")
        }
        return downloaded
    }

    private static func sha256(of file: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: file)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let chunk = try handle.read(upToCount: 4 << 20), !chunk.isEmpty {
            hasher.update(data: chunk)
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    /// Mounts the disk image, checks the app inside, and copies it next to the running one.
    private static func unpack(_ image: URL, next app: URL) async throws -> URL {
        let mount = image.deletingLastPathComponent().appendingPathComponent("mnt")
        try run("/usr/bin/hdiutil", "attach", "-nobrowse", "-readonly", "-noverify", "-mountpoint", mount.path, image.path)
        defer { try? run("/usr/bin/hdiutil", "detach", "-force", mount.path) }
        let inside = mount.appendingPathComponent("Trailmix.app")
        guard let bundle = Bundle(url: inside), bundle.bundleIdentifier == Bundle.main.bundleIdentifier else {
            throw CaptureError("The update doesn't contain Trailmix")
        }
        try run("/usr/bin/codesign", "--verify", "--strict", inside.path)
        let staged = app.deletingLastPathComponent().appendingPathComponent(".Trailmix-update.app")
        try? FileManager.default.removeItem(at: staged)
        try run("/usr/bin/ditto", inside.path, staged.path)
        return staged
    }

    private static func swap(_ app: URL, with fresh: URL) throws {
        _ = try FileManager.default.replaceItemAt(app, withItemAt: fresh, backupItemName: nil, options: [])
    }

    /// Opens the new version once this one has quit (and stopped its server).
    private static func relaunch(_ app: URL) {
        if ProcessInfo.processInfo.environment["TRAILMIX_UPDATE_NO_RELAUNCH"] != nil {  // tests: just quit
            NSApp.terminate(nil)
            return
        }
        let pid = ProcessInfo.processInfo.processIdentifier
        let script = "while kill -0 \(pid) 2>/dev/null; do sleep 0.2; done; /usr/bin/open \"\(app.path)\""
        let opener = Process()
        opener.executableURL = URL(fileURLWithPath: "/bin/sh")
        opener.arguments = ["-c", script]
        try? opener.run()
        NSApp.terminate(nil)
    }

    @discardableResult
    private static func run(_ tool: String, _ args: String...) throws -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: tool)
        process.arguments = args
        let out = Pipe()
        process.standardOutput = out
        process.standardError = out
        try process.run()
        process.waitUntilExit()
        let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        guard process.terminationStatus == 0 else {
            throw CaptureError("Updating failed at \((tool as NSString).lastPathComponent): \(text.prefix(200))")
        }
        return text
    }
}
