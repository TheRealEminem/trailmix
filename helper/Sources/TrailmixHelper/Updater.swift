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
    private var timer: Timer?
    private(set) var checkedAt: Date?

    struct Release {
        let version: String
        let notes: URL?
        let dmg: URL
        let checksum: URL?
    }

    static let current = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
    /// Where releases are looked up: TRAILMIX_UPDATE_URL (tests), else the repo baked into Info.plist.
    private static var feed: URL? {
        if let override = ProcessInfo.processInfo.environment["TRAILMIX_UPDATE_URL"] { return URL(string: override) }
        guard let repo = Bundle.main.object(forInfoDictionaryKey: "TrailmixUpdateRepo") as? String, !repo.isEmpty else { return nil }
        return URL(string: "https://api.github.com/repos/\(repo)/releases/latest")
    }

    /// Only the full Trailmix.app updates itself (not a development build or the thin helper).
    static var enabled: Bool { Bundled.isBundled && feed != nil }

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
        var out: [String: Any] = ["current": Self.current, "checked_at": checkedAt.map { $0.timeIntervalSince1970 } ?? NSNull()]
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
        guard let feed = Self.feed else { return }
        if case .downloading = state { return }
        if case .installing = state { return }
        if manual, latest == nil { state = .checking }
        var request = URLRequest(url: feed)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let tag = json["tag_name"] as? String else {
            if latest == nil, manual || state == .checking { state = .offline }
            return
        }
        checkedAt = Date()
        let version = tag.hasPrefix("v") ? String(tag.dropFirst()) : tag
        let assets = (json["assets"] as? [[String: Any]]) ?? []
        func asset(_ suffix: String) -> URL? {
            assets.first { ($0["name"] as? String)?.hasSuffix(suffix) == true && ($0["name"] as? String)?.contains(version) == true }
                .flatMap { $0["browser_download_url"] as? String }.flatMap(URL.init(string:))
        }
        guard Self.isNewer(version, than: Self.current), let dmg = asset("-arm64.dmg") else {
            latest = nil
            state = .upToDate
            return
        }
        latest = Release(version: version, notes: (json["html_url"] as? String).flatMap(URL.init(string:)), dmg: dmg,
                         checksum: asset("-arm64.dmg.sha256"))
        state = .available(version)
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
        state = .downloading(release.version, nil)
        Task {
            do {
                let image = try await download(release)
                state = .installing(release.version)
                let fresh = try await Self.unpack(image, next: app)
                try Self.swap(app, with: fresh)
                Self.relaunch(app)
            } catch {
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
