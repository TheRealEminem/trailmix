import Foundation
import Security
import ServiceManagement

/// Where the meeting's other side comes from.
enum MeetingSource: Equatable {
    case allApps
    case none  // in person: your mic only
    case app(String)  // bundle ID

    init(_ raw: String) {
        switch raw {
        case "all", "": self = .allApps
        case "none": self = .none
        default: self = .app(raw)
        }
    }

    var raw: String {
        switch self {
        case .allApps: return "all"
        case .none: return "none"
        case .app(let id): return id
        }
    }
}

/// The helper's settings, remembered between launches. The access token lives in the Keychain.
final class Prefs: ObservableObject {
    static let shared = Prefs()
    private let defaults = UserDefaults.standard

    /// Baked into Info.plist by the build scripts: the local server's address (and, for the launcher build, the Trailmix folder).
    static let bundledServer = ProcessInfo.processInfo.environment["TRAILMIX_SERVER_URL"]
        ?? Bundle.main.object(forInfoDictionaryKey: "TrailmixServerURL") as? String ?? "http://127.0.0.1:8765"
    static let trailmixRoot = Bundle.main.object(forInfoDictionaryKey: "TrailmixRoot") as? String

    @Published var server: String { didSet { defaults.set(server, forKey: "server") } }
    @Published var micID: String { didSet { defaults.set(micID, forKey: "micID") } }
    @Published var source: MeetingSource { didSet { defaults.set(source.raw, forKey: "source") } }
    @Published var liveDraft: Bool { didSet { defaults.set(liveDraft, forKey: "liveDraft") } }
    @Published var sounds: Bool { didSet { defaults.set(sounds, forKey: "sounds") } }
    @Published var toggleShortcut: Shortcut { didSet { save(toggleShortcut, "toggleShortcut") } }
    @Published var markShortcut: Shortcut { didSet { save(markShortcut, "markShortcut") } }
    @Published var token: String { didSet { Keychain.set(token, account: "access-token") } }
    /// "Zoom call started. Record it?" (CallReminder), and the apps you said not to ask about.
    @Published var callReminders: Bool { didSet { defaults.set(callReminders, forKey: "callReminders") } }
    @Published var mutedCallApps: [String] { didSet { defaults.set(mutedCallApps, forKey: "mutedCallApps") } }

    private init() {
        server = defaults.string(forKey: "server") ?? Prefs.bundledServer
        micID = defaults.string(forKey: "micID") ?? ""
        source = MeetingSource(defaults.string(forKey: "source") ?? "all")
        liveDraft = defaults.object(forKey: "liveDraft") as? Bool ?? true
        sounds = defaults.object(forKey: "sounds") as? Bool ?? true
        toggleShortcut = Prefs.load("toggleShortcut", defaults) ?? .toggleDefault
        markShortcut = Prefs.load("markShortcut", defaults) ?? .markDefault
        token = Keychain.get(account: "access-token") ?? ""
        callReminders = defaults.object(forKey: "callReminders") as? Bool ?? true
        mutedCallApps = defaults.stringArray(forKey: "mutedCallApps") ?? []
    }

    private func save(_ shortcut: Shortcut, _ key: String) {
        defaults.set(try? JSONEncoder().encode(shortcut), forKey: key)
    }

    private static func load(_ key: String, _ defaults: UserDefaults) -> Shortcut? {
        defaults.data(forKey: key).flatMap { try? JSONDecoder().decode(Shortcut.self, from: $0) }
    }

    /// The server URL, tidied: a scheme added if missing, no trailing slash.
    var serverURL: URL? {
        var text = server.trimmingCharacters(in: .whitespaces)
        if !text.contains("://") { text = "http://" + text }
        while text.hasSuffix("/") { text.removeLast() }
        return URL(string: text)
    }

    /// True for this Mac's own server.
    var isLocalServer: Bool {
        guard let host = serverURL?.host else { return false }
        return ["127.0.0.1", "localhost", "::1"].contains(host)
    }

    /// This Mac's own server, and there's a way to start it: the one inside Trailmix.app, or the ./trailmix launcher.
    var canStartServer: Bool { isLocalServer && (Bundled.isBundled || Prefs.trailmixRoot != nil) }

    var openAtLogin: Bool {
        get { SMAppService.mainApp.status == .enabled }
        set {
            objectWillChange.send()
            if newValue { try? SMAppService.mainApp.register() } else { try? SMAppService.mainApp.unregister() }
        }
    }
}

enum Keychain {
    private static let service = "local.trailmix.helper"

    static func get(account: String) -> String? {
        let query: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword, kSecAttrService: service, kSecAttrAccount: account,
            kSecReturnData: true, kSecMatchLimit: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func set(_ value: String, account: String) {
        let query: [CFString: Any] = [kSecClass: kSecClassGenericPassword, kSecAttrService: service, kSecAttrAccount: account]
        SecItemDelete(query as CFDictionary)
        guard !value.isEmpty else { return }
        var item = query
        item[kSecValueData] = Data(value.utf8)
        SecItemAdd(item as CFDictionary, nil)
    }
}
