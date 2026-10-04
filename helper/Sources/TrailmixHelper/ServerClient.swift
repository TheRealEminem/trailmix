import Foundation

/// A recording in progress on the server, whichever app is capturing it (see GET /api/live).
struct LiveRecording: Decodable, Equatable, Identifiable {
    let id: Int
    let title: String
    let client: String  // "web" (a Trailmix window) or "helper" (this app)
    let duration: Double
    let has_system: Bool
    let draft: Bool
    let drafts: [DraftLine]
    let bookmarks: [Bookmark]
}

struct DraftLine: Decodable, Equatable, Hashable {
    let start: Double
    let speaker: String?
    let text: String
}

struct Bookmark: Decodable, Equatable {
    let t: Double
    let note: String
}

enum ServerError: LocalizedError, Equatable {
    case offline
    case needsToken
    case outdated  // a Trailmix server from before the helper existed
    case rejected(String)

    var errorDescription: String? {
        switch self {
        case .offline: return "Trailmix isn't running."
        case .needsToken: return "This Trailmix needs an access token. Add it in Settings."
        case .outdated: return "Trailmix's server is older than the helper. Restart it to finish updating."
        case .rejected(let detail): return detail
        }
    }
}

/// Talks to the Trailmix backend: the same REST API and audio WebSocket the web app uses.
final class ServerClient {
    let base: URL
    private let token: String
    private let session: URLSession

    init(base: URL, token: String) {
        self.base = base
        self.token = token
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 8
        session = URLSession(configuration: config)
    }

    private func request(_ path: String, method: String = "GET", json: [String: Any]? = nil) -> URLRequest {
        var request = URLRequest(url: base.appendingPathComponent("api").appendingPathComponent(path))
        request.httpMethod = method
        if !token.isEmpty { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let json {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: json)
        }
        return request
    }

    private func call(_ path: String, method: String = "GET", json: [String: Any]? = nil) async throws -> Data {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request(path, method: method, json: json))
        } catch {
            throw ServerError.offline
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 401 { throw ServerError.needsToken }
        if status == 404 || status == 405 { throw ServerError.outdated }
        guard (200..<300).contains(status) else {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
            throw ServerError.rejected(detail ?? "Trailmix answered \(status)")
        }
        return data
    }

    /// Reachable, and signed in if the server needs a token.
    func check() async throws {
        let data = try await call("auth")
        let answer = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        if answer?["ok"] as? Bool == false { throw ServerError.needsToken }
    }

    func live() async throws -> [LiveRecording] {
        try JSONDecoder().decode([LiveRecording].self, from: await call("live"))
    }

    func startMeeting() async throws -> Int {
        let data = try await call("meetings", method: "POST", json: ["title": ""])
        guard let id = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["id"] as? Int else {
            throw ServerError.rejected("Trailmix didn't create the meeting")
        }
        return id
    }

    func mark(_ id: Int) async throws {
        _ = try await call("meetings/\(id)/mark", method: "POST", json: ["note": ""])
    }

    func stop(_ id: Int) async throws {
        _ = try await call("meetings/\(id)/stop", method: "POST")
    }

    /// Tells the server this recorder is here and what it would record; returns the commands waiting for it
    /// (the web app's Record button sends "start").
    func checkIn(_ info: [String: Any]) async throws -> [String] {
        let data = try await call("recorder/check-in", method: "POST", json: info)
        return ((try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["commands"] as? [String]) ?? []
    }

    /// The audio WebSocket for a meeting, already connecting.
    func openStream(id: Int, draft: Bool) -> URLSessionWebSocketTask {
        var components = URLComponents(url: base, resolvingAgainstBaseURL: false)!
        components.scheme = base.scheme == "https" ? "wss" : "ws"
        components.path = (components.path.hasSuffix("/") ? String(components.path.dropLast()) : components.path) + "/api/meetings/\(id)/stream"
        components.queryItems = [URLQueryItem(name: "draft", value: draft ? "1" : "0"), URLQueryItem(name: "client", value: "helper")]
        var request = URLRequest(url: components.url!)
        if !token.isEmpty { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        let task = session.webSocketTask(with: request)
        task.resume()
        return task
    }
}
