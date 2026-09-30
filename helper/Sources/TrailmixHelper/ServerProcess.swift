import Foundation

/// Where things live inside Trailmix.app. The thin "Trailmix Helper" build has no server inside, so `isBundled` is false there.
enum Bundled {
    static let resources = Bundle.main.resourceURL
    static let python = resources?.appendingPathComponent("python/bin/python3")
    static let isBundled = python.map { FileManager.default.isExecutableFile(atPath: $0.path) } ?? false

    /// Meetings, settings and recordings. Survives updating or deleting the app.
    /// TRAILMIX_DATA_DIR overrides it (tests, or keeping your data elsewhere).
    static let dataDirectory = ProcessInfo.processInfo.environment["TRAILMIX_DATA_DIR"].map { URL(fileURLWithPath: $0, isDirectory: true) }
        ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Trailmix", isDirectory: true)
    static let logFile = dataDirectory.appendingPathComponent("logs/server.log")
}

/// The Trailmix server that ships inside Trailmix.app: started with the app, stopped when it quits, and
/// started again (a few times) if it dies.
@MainActor
final class BundledServer {
    static let shared = BundledServer()

    /// Why the server can't run, in words for the menu. Nil while all is well.
    private(set) var failure: String?
    private var process: Process?
    private var stopping = false
    private var crashes: [Date] = []

    var isRunning: Bool { process?.isRunning ?? false }

    func start(port: Int) throws {
        guard Bundled.isBundled, let python = Bundled.python, let resources = Bundled.resources else { return }
        if isRunning { return }
        stopping = false
        failure = nil

        let logs = Bundled.logFile.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        if let size = (try? FileManager.default.attributesOfItem(atPath: Bundled.logFile.path)[.size]) as? Int, size > 5_000_000 {
            try? FileManager.default.removeItem(at: Bundled.logFile)  // keep the log from growing forever
        }
        if !FileManager.default.fileExists(atPath: Bundled.logFile.path) {
            FileManager.default.createFile(atPath: Bundled.logFile.path, contents: nil)
        }
        let log = try FileHandle(forWritingTo: Bundled.logFile)
        log.seekToEndOfFile()

        var environment = ProcessInfo.processInfo.environment.filter { !$0.key.hasPrefix("PYTHON") }
        environment["PATH"] = resources.appendingPathComponent("bin").path + ":/usr/bin:/bin:/usr/sbin:/sbin"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"  // the app is sealed; nothing may be written into it
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        environment["TRAILMIX_DATA_DIR"] = Bundled.dataDirectory.path
        environment["TRAILMIX_UI_DIR"] = resources.appendingPathComponent("ui").path
        environment["TRAILMIX_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)

        let backend = resources.appendingPathComponent("backend")
        let server = Process()
        server.executableURL = python
        server.arguments = ["-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", String(port)]
        server.currentDirectoryURL = backend
        server.environment = environment
        server.standardOutput = log
        server.standardError = log
        server.terminationHandler = { [weak self] finished in
            try? log.close()
            Task { @MainActor in self?.ended(finished, port: port) }
        }
        log.write(Data("\n— Trailmix server starting \(Date()) —\n".utf8))
        try server.run()
        process = server
    }

    /// Stops the server and waits (briefly) for it to finish what it's doing.
    func stop() {
        stopping = true
        guard let server = process, server.isRunning else { return }
        server.terminate()
        let deadline = Date().addingTimeInterval(5)
        while server.isRunning, Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        if server.isRunning { kill(server.processIdentifier, SIGKILL) }
    }

    private func ended(_ finished: Process, port: Int) {
        guard process === finished else { return }
        process = nil
        guard !stopping else { return }
        crashes = crashes.filter { Date().timeIntervalSince($0) < 120 } + [Date()]
        if crashes.count >= 3 {
            failure = Self.explain(port: port)
            return
        }
        try? start(port: port)
    }

    /// The last useful line of the server log, in plain words where we recognise it.
    private static func explain(port: Int) -> String {
        let tail = (try? String(contentsOf: Bundled.logFile, encoding: .utf8))?
            .split(separator: "\n").suffix(40).map(String.init) ?? []
        if tail.contains(where: { $0.contains("address already in use") || $0.contains("Errno 48") }) {
            return "Another app is using port \(port), so Trailmix can't start. Quit it, or change the port."
        }
        let last = tail.last { !$0.trimmingCharacters(in: .whitespaces).isEmpty && !$0.hasPrefix("—") } ?? "no details"
        return "Trailmix's server stopped unexpectedly (\(last.prefix(120))). Log: \(Bundled.logFile.path)"
    }
}
