import AppKit
import AVFoundation
@preconcurrency import UserNotifications

/// Input levels, kept apart from the main model so ~15 updates a second don't redraw the menu bar item.
final class Meters: ObservableObject {
    @Published var mic: Float = 0
    @Published var meeting: Float = 0
}

/// Small lock-protected state shared with the audio queues.
private final class AudioStats {
    private let lock = NSLock()
    private var lastLevel: [UInt8: Double] = [:]
    private var heard = false

    /// True at most ~15 times a second per track, so meters don't flood the main thread.
    func shouldReportLevel(_ channel: UInt8) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        let now = CACurrentMediaTime()
        guard now - (lastLevel[channel] ?? 0) > 0.066 else { return false }
        lastLevel[channel] = now
        return true
    }

    var heardMeetingAudio: Bool {
        get { lock.lock(); defer { lock.unlock() }; return heard }
        set { lock.lock(); heard = newValue; lock.unlock() }
    }
}

@MainActor
final class HelperModel: ObservableObject {
    enum Phase { case idle, starting, recording, stopping }
    enum Connection { case unknown, online, offline, needsToken, outdated }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var connection: Connection = .unknown
    /// A recording another app is capturing (normally the Trailmix window). The hotkeys work on it too.
    @Published private(set) var other: LiveRecording?
    @Published private(set) var meetingID: Int?
    @Published private(set) var now = Date()
    @Published private(set) var drafts: [DraftLine] = []
    @Published private(set) var marks: [Double] = []
    @Published private(set) var micName = ""
    @Published private(set) var capturingMeetingAudio = false
    @Published private(set) var lastMeetingID: Int?
    @Published private(set) var flash = false
    @Published private(set) var startingServer = false
    @Published private(set) var shortcutProblem: String?
    @Published var notice: String?
    @Published var error: String?
    /// Set by the menu while it's open; notifications are only for when it isn't.
    var menuOpen = false

    let prefs = Prefs.shared
    let meters = Meters()

    /// Makes the audio sources for a recording: your mic, plus other apps unless you're in person. Tests swap in files.
    var makeSources: (Prefs) throws -> [(UInt8, AudioSource)] = HelperModel.liveSources
    var needsMicPermission = true
    /// Tests point the model at a throwaway server instead of the one in Settings.
    var serverOverride: URL?

    private var startedAt: Date?
    private var otherFetchedAt = Date()
    private var sources: [(UInt8, AudioSource)] = []
    private var streamer: Streamer?
    private var socket: URLSessionWebSocketTask?
    private var stopRequested = false
    private var stopTimeout: Task<Void, Never>?
    private var timers: [Timer] = []
    private let stats = AudioStats()
    private var warnedSilence = false

    nonisolated static func liveSources(_ prefs: Prefs) throws -> [(UInt8, AudioSource)] {
        var sources: [(UInt8, AudioSource)] = [(Wire.mic, MicCapture(deviceID: prefs.micID))]
        switch prefs.source {
        case .none: break
        case .allApps: sources.append((Wire.meetingAudio, SystemAudioCapture(target: .allApps)))
        case .app(let id): sources.append((Wire.meetingAudio, SystemAudioCapture(target: .apps([id]))))
        }
        return sources
    }

    // MARK: Lifecycle

    func begin() {
        registerHotKeys()
        timers.append(Timer.scheduledTimer(withTimeInterval: 2.5, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in await self.poll() }
        })
        timers.append(Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in await self.checkIn() }
        })
        timers.append(Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self, self.elapsed != nil else { return }
                self.now = Date()
            }
        })
        Task { await poll() }
    }

    func registerHotKeys() {
        let toggleOK = HotKeys.shared.register(1, prefs.toggleShortcut) { [weak self] in
            MainActor.assumeIsolated { self?.toggle() }
        }
        let markOK = HotKeys.shared.register(2, prefs.markShortcut) { [weak self] in
            MainActor.assumeIsolated { self?.markMoment() }
        }
        let taken = [toggleOK ? nil : prefs.toggleShortcut.display, markOK ? nil : prefs.markShortcut.display].compactMap { $0 }
        shortcutProblem = taken.isEmpty ? nil : "\(taken.joined(separator: " and ")) is taken by another app. Pick another in Settings."
    }

    func pauseHotKeys() {
        HotKeys.shared.unregister(1)
        HotKeys.shared.unregister(2)
    }

    private func client() throws -> ServerClient {
        guard let url = serverOverride ?? prefs.serverURL else { throw ServerError.rejected("The server address in Settings isn't valid") }
        return ServerClient(base: url, token: serverOverride == nil ? prefs.token : "")
    }

    private var checkingIn = false
    private var micNameCache: (id: String, name: String)?

    /// Lets the server know this recorder is here (so the Trailmix window's Record button records through it),
    /// and carries out what the window asked for.
    func checkIn() async {
        guard !checkingIn, connection != .offline, let client = try? client() else { return }
        checkingIn = true
        defer { checkingIn = false }
        guard let commands = try? await client.checkIn(recorderInfo) else { return }
        if commands.contains("start"), phase == .idle, other == nil {
            start()
        }
        if commands.contains("sound-check"), phase == .idle, !soundChecking {
            soundChecking = true
            soundCheck = ["running": true]
            Task {
                let result = await SoundCheck.run(micID: prefs.micID)
                soundCheck = result
                soundChecking = false
            }
        }
    }

    private var soundChecking = false
    /// The last sound check's result, reported with each check-in so the window can show it.
    private var soundCheck: [String: Any]?

    private var recorderInfo: [String: Any] {
        if micNameCache?.id != prefs.micID {
            let chosen = prefs.micID.isEmpty ? nil : Microphone.all().first { $0.id == prefs.micID }?.name
            micNameCache = (prefs.micID, chosen ?? "")
        }
        let mic = micNameCache?.name.isEmpty == false ? micNameCache!.name : "System default (\(Microphone.defaultName))"
        let source: String
        switch prefs.source {
        case .allApps: source = "All apps' audio"
        case .none: source = "Your mic only (in person)"
        case .app(let id): source = Self.appName(id)
        }
        return [
            "mic": mic, "source": source, "machine": Host.current().localizedName ?? "",
            "version": Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "",
            "mic_allowed": AVCaptureDevice.authorizationStatus(for: .audio) != .denied,
            "sound_check": soundCheck ?? NSNull(),
        ]
    }

    private static func appName(_ bundleID: String) -> String {
        if bundleID == AudioApp.calls { return "FaceTime & iPhone calls" }
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) else { return bundleID }
        return FileManager.default.displayName(atPath: url.path).replacingOccurrences(of: ".app", with: "")
    }

    /// Keeps the connection status current and notices recordings started in the Trailmix window.
    func poll() async {
        do {
            let live = try await client().live()
            connection = .online
            other = live.first { $0.id != meetingID }
            otherFetchedAt = Date()
        } catch ServerError.needsToken {
            connection = .needsToken
            other = nil
        } catch ServerError.outdated {
            connection = .outdated  // it can still record; it just can't share live state yet
            other = nil
        } catch {
            connection = .offline
            other = nil
        }
    }

    // MARK: What the menu and the hotkeys do

    var isBusy: Bool { phase == .starting || phase == .stopping }

    /// Seconds recorded so far, for whichever recording is live.
    var elapsed: TimeInterval? {
        if let startedAt, phase == .recording || phase == .stopping { return now.timeIntervalSince(startedAt) }
        if let other { return other.duration + now.timeIntervalSince(otherFetchedAt) }
        return nil
    }

    /// Start/stop hotkey: stops whatever is recording (here or in the Trailmix window), otherwise starts.
    func toggle() {
        switch phase {
        case .idle: if let other { stopOther(other) } else { start() }
        case .recording: stop()
        case .starting, .stopping: break
        }
    }

    func start() {
        guard phase == .idle else { return }
        if other != nil {
            error = "Trailmix is already recording in its window. Use Stop to end it."
            return
        }
        error = nil
        notice = nil
        phase = .starting
        Task { await startRecording() }
    }

    private func startRecording() async {
        var started: [(UInt8, AudioSource)] = []
        do {
            try await ensureServer()
            if needsMicPermission { try await ensureMicAccess() }

            // Capture first (buffered until the socket is ready), so a mic problem never leaves an empty meeting.
            let streamer = Streamer()
            stats.heardMeetingAudio = false
            warnedSilence = false
            for (channel, source) in try makeSources(prefs) {
                source.onAudio = { [weak self, stats] samples in
                    streamer.push(samples, channel: channel)
                    self?.observe(samples, channel: channel, stats: stats)
                }
                if let mic = source as? MicCapture {
                    mic.onSwitched = { [weak self] name in
                        Task { @MainActor in
                            self?.micName = name
                            self?.notice = "Your microphone was disconnected, so Trailmix switched to \(name)."
                        }
                    }
                }
                do {
                    try await Task.detached { try source.start() }.value  // starting a capture session blocks
                    started.append((channel, source))
                } catch where channel == Wire.meetingAudio {
                    notice = "Couldn't listen to other apps (\(error.localizedDescription)). Recording your mic only."
                }
            }

            let client = try client()
            let id = try await client.startMeeting()
            let socket = client.openStream(id: id, draft: prefs.liveDraft)
            streamer.attach { data in socket.send(.data(data)) { _ in } }

            sources = started
            self.streamer = streamer
            self.socket = socket
            meetingID = id
            startedAt = Date()
            now = Date()
            drafts = []
            marks = []
            stopRequested = false
            capturingMeetingAudio = started.contains { $0.0 == Wire.meetingAudio }
            micName = started.first { $0.0 == Wire.mic }?.1.name ?? ""
            phase = .recording
            listen(socket, id: id)
            watchMeetingAudio(id)
            play("Pop")
        } catch {
            stopSources(started)
            phase = .idle
            self.error = error.localizedDescription
            alertIfHidden("Couldn't start recording", error.localizedDescription)
        }
    }

    func stop() {
        guard phase == .recording, let socket, let streamer else { return }
        phase = .stopping
        stopRequested = true
        let sources = self.sources
        Task.detached {
            for (_, source) in sources { source.stop() }
            streamer.finish { socket.send(.string(#"{"type":"stop"}"#)) { _ in } }
        }
        stopTimeout = Task { [weak self] in
            try? await Task.sleep(for: .seconds(10))
            if !Task.isCancelled { self?.finished(stoppedElsewhere: false) }
        }
    }

    func stopOther(_ other: LiveRecording) {
        Task {
            do {
                try await client().stop(other.id)
                play("Bottle")
                try? await Task.sleep(for: .milliseconds(600))
                await poll()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }

    /// Mark hotkey: flags the current moment of whatever is recording.
    func markMoment() {
        if phase == .recording, let socket {
            socket.send(.string(#"{"type":"mark"}"#)) { _ in }
            play("Tink")
        } else if let other {
            Task {
                do {
                    try await client().mark(other.id)
                    flashIcon()
                    play("Tink")
                    await poll()
                } catch {
                    self.error = error.localizedDescription
                }
            }
        } else {
            NSSound.beep()
        }
    }

    // MARK: The recording's WebSocket

    private func listen(_ socket: URLSessionWebSocketTask, id: Int) {
        Task { [weak self] in
            while true {
                let message: URLSessionWebSocketTask.Message
                do {
                    message = try await socket.receive()
                } catch {
                    let refused = (socket.response as? HTTPURLResponse).map { $0.statusCode >= 400 } ?? false
                    self?.socketEnded(id: id, refused: refused)
                    return
                }
                guard case .string(let text) = message,
                      let object = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any],
                      let type = object["type"] as? String else { continue }
                self?.received(type, object, id: id)
                if type == "stopped" { return }
            }
        }
    }

    private func received(_ type: String, _ object: [String: Any], id: Int) {
        guard id == meetingID else { return }
        switch type {
        case "draft":
            guard let text = object["text"] as? String else { return }
            drafts.append(DraftLine(start: object["start"] as? Double ?? 0, speaker: object["speaker"] as? String, text: text))
        case "marked":
            if let t = object["t"] as? Double { marks.append(t) }
            flashIcon()
        case "status":
            notice = object["message"] as? String
        case "stopped":
            finished(stoppedElsewhere: !stopRequested)
        default:
            break
        }
    }

    private func socketEnded(id: Int, refused: Bool) {
        guard id == meetingID, phase == .recording || phase == .stopping else { return }
        if stopRequested {
            finished(stoppedElsewhere: false)
            return
        }
        stopSources(sources)
        wrapUp()
        error = refused
            ? "Trailmix refused the audio connection. If it's a hosted Trailmix, check the access token in Settings."
            : "Lost the connection to Trailmix. What was recorded so far is being processed."
        alertIfHidden("Recording interrupted", error!)
    }

    private func finished(stoppedElsewhere: Bool) {
        guard phase == .recording || phase == .stopping else { return }
        if stoppedElsewhere {
            stopSources(sources)
            alertIfHidden("Recording stopped in Trailmix", "It's being transcribed now.")
        }
        wrapUp()
        play("Bottle")
        Task { await poll() }
    }

    private func wrapUp() {
        stopTimeout?.cancel()
        socket?.cancel(with: .normalClosure, reason: nil)
        socket = nil
        streamer = nil
        sources = []
        lastMeetingID = meetingID
        meetingID = nil
        startedAt = nil
        capturingMeetingAudio = false
        meters.mic = 0
        meters.meeting = 0
        phase = .idle
    }

    private func stopSources(_ list: [(UInt8, AudioSource)]) {
        Task.detached { for (_, source) in list { source.stop() } }
    }

    // MARK: Audio feedback

    nonisolated private func observe(_ samples: [Int16], channel: UInt8, stats: AudioStats) {
        if channel == Wire.meetingAudio, !stats.heardMeetingAudio, samples.contains(where: { $0 != 0 }) {
            stats.heardMeetingAudio = true
        }
        guard stats.shouldReportLevel(channel) else { return }
        let level = meterLevel(samples)
        DispatchQueue.main.async { [weak self] in
            MainActor.assumeIsolated {
                guard let self, self.phase == .recording else { return }
                if channel == Wire.mic { self.meters.mic = level } else { self.meters.meeting = level }
            }
        }
    }

    /// Without "System Audio Recording" permission the tap only ever delivers silence. If other apps are
    /// playing sound and we've heard none of it, say so, while there's still time to fix it.
    private func watchMeetingAudio(_ id: Int) {
        guard capturingMeetingAudio else { return }
        Task { [weak self] in
            for _ in 0..<12 {
                try? await Task.sleep(for: .seconds(10))
                guard let self, self.meetingID == id, self.phase == .recording, !self.warnedSilence else { return }
                if self.stats.heardMeetingAudio { return }
                if AudioApp.anythingPlaying() {
                    self.warnedSilence = true
                    self.notice = "\(Brand.appName) can't hear other apps. Allow it under System Settings → Privacy & Security → Screen & System Audio Recording, then stop and start again."
                    self.alertIfHidden("Can't hear the meeting", "Allow \(Brand.appName) to record system audio in System Settings.")
                    return
                }
            }
        }
    }

    private func flashIcon() {
        flash = true
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(1.2))
            self?.flash = false
        }
    }

    private func play(_ sound: String) {
        guard prefs.sounds, let cue = NSSound(named: sound) else { return }
        cue.volume = 0.35
        cue.play()
    }

    /// A notification, for when you started or stopped with a hotkey and the menu isn't open.
    private func alertIfHidden(_ title: String, _ body: String) {
        guard !menuOpen, Bundle.main.bundleIdentifier != nil else { return }  // the menu shows it; or a CLI test run
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert]) { granted, _ in
            guard granted else { return }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
        }
    }

    // MARK: Permissions and the local server

    private func ensureMicAccess() async throws {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            return
        case .notDetermined:
            if await AVCaptureDevice.requestAccess(for: .audio) { return }
        default:
            break
        }
        throw CaptureError("\(Brand.appName) isn't allowed to use the microphone. Turn it on in System Settings → Privacy & Security → Microphone.")
    }

    /// If the local Trailmix isn't running, start it (without opening a window) and wait for it.
    private func ensureServer() async throws {
        let client = try client()
        do {
            try await client.check()
            connection = .online
            return
        } catch ServerError.offline where prefs.canStartServer {
            try await startServer(waitingOn: client)
        } catch ServerError.needsToken {
            connection = .needsToken
            throw ServerError.needsToken
        }
    }

    private func startServer(waitingOn client: ServerClient) async throws {
        startingServer = true
        defer { startingServer = false }
        if Bundled.isBundled {
            try BundledServer.shared.start(port: prefs.serverURL?.port ?? 8765)
        } else {
            try launch("start", openWindow: false)
        }
        for _ in 0..<180 {  // a launcher start may build the app first, which takes a while
            try await Task.sleep(for: .milliseconds(500))
            if (try? await client.check()) != nil {
                connection = .online
                return
            }
            if let problem = BundledServer.shared.failure { throw ServerError.rejected(problem) }
        }
        throw ServerError.rejected(Bundled.isBundled
            ? "Trailmix didn't start. Its log is at \(Bundled.logFile.path)."
            : "Trailmix didn't start. Run ./trailmix in Terminal to see why.")
    }

    /// Called when the app opens: have the server up and waiting, so the first recording starts at once.
    func startServerIfNeeded() async {
        guard prefs.canStartServer, let client = try? client() else { return }
        if (try? await client.check()) != nil {
            connection = .online
            return
        }
        do { try await startServer(waitingOn: client) } catch { self.error = error.localizedDescription }
        await poll()
    }

    /// Runs the ./trailmix launcher (it starts the server if needed, and can open the app window).
    private func launch(_ command: String, openWindow: Bool) throws {
        guard let root = Prefs.trailmixRoot else { throw ServerError.offline }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [root + "/trailmix", command]
        var environment = ProcessInfo.processInfo.environment
        if !openWindow { environment["TRAILMIX_NO_OPEN"] = "1" }
        process.environment = environment
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
    }

    /// Opens Trailmix in its own window, optionally at a meeting. Starts the server first if it isn't running.
    func openTrailmix(meeting: Int? = nil) {
        guard let base = prefs.serverURL else { return }
        if connection != .online, prefs.canStartServer {
            if Bundled.isBundled {
                Task {
                    await startServerIfNeeded()
                    if connection == .online { showWindow(base, meeting: meeting) }
                }
            } else {
                try? launch("open", openWindow: true)
            }
            return
        }
        showWindow(base, meeting: meeting)
    }

    private func showWindow(_ base: URL, meeting: Int?) {
        let text = meeting.map { base.absoluteString + "/#meeting-\($0)" } ?? base.absoluteString
        if ProcessInfo.processInfo.environment["TRAILMIX_NO_OPEN"] != nil {  // tests: say it instead of opening a window
            FileHandle.standardError.write(Data("Would open \(text)\n".utf8))
            return
        }
        guard let url = URL(string: text) else { return }
        MainWindow.shared.show(url)
    }

    // MARK: Snapshots

    enum PreviewState: String, CaseIterable {
        case idle, offline, recording, window, problem
    }

    /// Puts the model in a representative state for `--snapshot` renders.
    func preview(_ state: PreviewState) {
        connection = state == .offline ? .offline : .online
        switch state {
        case .idle, .offline:
            lastMeetingID = state == .idle ? 12 : nil
        case .recording, .problem:
            phase = .recording
            startedAt = Date().addingTimeInterval(-754)
            now = Date()
            micName = "MacBook Pro Microphone"
            capturingMeetingAudio = true
            meters.mic = 0.62
            meters.meeting = 0.38
            drafts = [DraftLine(start: 740, speaker: "Them", text: "Agreed. I'll fix the login bug by Wednesday, and Priya will update the docs before the beta goes out.")]
            marks = [120, 402]
            if state == .problem {
                meters.meeting = 0
                notice = "\(Brand.appName) can't hear other apps. Allow it under System Settings → Privacy & Security → Screen & System Audio Recording, then stop and start again."
            }
        case .window:
            other = LiveRecording(id: 7, title: "Weekly sync", client: "web", duration: 1834, has_system: true, draft: true,
                                  drafts: [DraftLine(start: 1820, speaker: "You", text: "Let's pick this up again on Thursday.")], bookmarks: [])
            otherFetchedAt = Date()
            now = Date()
        }
    }
}
