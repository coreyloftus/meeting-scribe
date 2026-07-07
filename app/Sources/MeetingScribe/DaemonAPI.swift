// HTTP + SSE client for the scribed daemon. Discovery mirrors the CLI:
// ~/.local/state/meeting-scribe/daemon.json (host/port) + daemon.token (bearer).
import Foundation

// MARK: - Models (decoded with convertFromSnakeCase)

struct SessionInfo: Codable, Equatable {
    var meetingId: String
    var startedAt: String?
    var elapsedSec: Int?
}

struct JobInfo: Codable, Equatable, Identifiable {
    var id: Int
    var type: String
    var meetingId: String
    var status: String
    var phase: String?
    var error: String?
}

struct DaemonStatus: Codable, Equatable {
    var recording: Bool
    var session: SessionInfo?
    var activeJob: JobInfo?
    var daemonVersion: String
}

struct OutputRow: Codable, Equatable, Identifiable {
    var id: Int
    var target: String
    var status: String        // ok | failed
    var url: String?
    var detail: String?
    var createdAt: String
}

struct Meeting: Codable, Equatable, Identifiable {
    var id: String
    var basePath: String?
    var startedAt: String?
    var endedAt: String?
    var status: String
    var title: String?
    var slug: String?
    var durationSec: Int?
    var summaryMd: String?
    var error: String?
    var outputs: [OutputRow]?
    var transcript: String?    // detail endpoint only
    var userNotes: String?     // detail endpoint only

    var displayTitle: String { title ?? id }
    var isProcessing: Bool {
        ["queued", "transcribing", "summarizing", "writing_outputs"].contains(status)
    }
}

struct IntegrationInfo: Codable, Equatable, Identifiable {
    var key: String
    var label: String
    var enabled: Bool
    var configured: Bool
    var id: String { key }
}

struct IntegrationsResponse: Codable, Equatable {
    struct GoogleInfo: Codable, Equatable {
        var connected: Bool
        var clientConfigured: Bool
    }
    var outputs: [IntegrationInfo]
    var google: GoogleInfo
}

struct DoctorCheck: Codable, Equatable, Identifiable {
    var key: String
    var ok: Bool
    var detail: String
    var id: String { key }
}

struct SSEEvent {
    var type: String
    var data: [String: Any]
}

// MARK: - Client

enum DaemonError: LocalizedError {
    case notDiscovered
    case http(Int, String)

    var errorDescription: String? {
        switch self {
        case .notDiscovered: return "scribed daemon not found — is it running?"
        case .http(let code, let detail): return "daemon error \(code): \(detail)"
        }
    }
}

struct DaemonClient {
    var baseURL: URL
    var token: String

    static var stateDir: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/state/meeting-scribe")
    }

    static func discover() -> DaemonClient? {
        let dir = stateDir
        guard
            let info = try? Data(contentsOf: dir.appendingPathComponent("daemon.json")),
            let obj = try? JSONSerialization.jsonObject(with: info) as? [String: Any],
            let host = obj["host"] as? String,
            let port = obj["port"] as? Int,
            let token = try? String(contentsOf: dir.appendingPathComponent("daemon.token"),
                                    encoding: .utf8)
                .trimmingCharacters(in: .whitespacesAndNewlines),
            !token.isEmpty,
            let url = URL(string: "http://\(host):\(port)")
        else { return nil }
        return DaemonClient(baseURL: url, token: token)
    }

    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    private func request(_ method: String, _ path: String,
                         body: [String: Any]? = nil) async throws -> Data {
        var req = URLRequest(url: baseURL.appendingPathComponent(path))
        req.httpMethod = method
        req.timeoutInterval = 30
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard code < 300 else {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"]
            throw DaemonError.http(code, "\(detail ?? String(data: data, encoding: .utf8) ?? "")")
        }
        return data
    }

    private func get<T: Decodable>(_ path: String, as type: T.Type) async throws -> T {
        try Self.decoder.decode(T.self, from: try await request("GET", path))
    }

    // MARK: endpoints

    func status() async throws -> DaemonStatus {
        try await get("v1/status", as: DaemonStatus.self)
    }

    struct MeetingsEnvelope: Codable { var meetings: [Meeting] }
    func meetings(limit: Int = 200, query: String? = nil) async throws -> [Meeting] {
        var path = "v1/meetings?limit=\(limit)"
        if let q = query, !q.isEmpty,
           let enc = q.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) {
            path += "&q=\(enc)"
        }
        // appendingPathComponent escapes "?", so build the URL directly here.
        var req = URLRequest(url: URL(string: baseURL.absoluteString + "/" + path)!)
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let code = (resp as? HTTPURLResponse)?.statusCode, code < 300 else {
            throw DaemonError.http((resp as? HTTPURLResponse)?.statusCode ?? 0, "")
        }
        return try Self.decoder.decode(MeetingsEnvelope.self, from: data).meetings
    }

    struct MeetingEnvelope: Codable { var meeting: Meeting }
    func meeting(_ id: String) async throws -> Meeting {
        try await get("v1/meetings/\(id)", as: MeetingEnvelope.self).meeting
    }

    func start() async throws { _ = try await request("POST", "v1/start") }
    func stop() async throws { _ = try await request("POST", "v1/stop") }

    func push(_ id: String, target: String) async throws {
        _ = try await request("POST", "v1/meetings/\(id)/push", body: ["target": target])
    }

    func reprocess(_ id: String) async throws {
        _ = try await request("POST", "v1/meetings/\(id)/reprocess", body: [:])
    }

    func delete(_ id: String, deleteFiles: Bool) async throws {
        _ = try await request("DELETE", "v1/meetings/\(id)?delete_files=\(deleteFiles)")
    }

    func saveNotes(_ id: String, notes: String) async throws {
        _ = try await request("PUT", "v1/meetings/\(id)/notes", body: ["notes": notes])
    }

    func saveSessionNotes(_ notes: String) async throws {
        _ = try await request("PUT", "v1/session/notes", body: ["notes": notes])
    }

    func integrations() async throws -> IntegrationsResponse {
        try await get("v1/integrations", as: IntegrationsResponse.self)
    }

    func connectGoogle() async throws {
        _ = try await request("POST", "v1/integrations/google/connect")
    }

    struct DoctorEnvelope: Codable { var checks: [DoctorCheck] }
    func doctor() async throws -> [DoctorCheck] {
        try await get("v1/doctor", as: DoctorEnvelope.self).checks
    }

    struct ConfigEnvelope: Codable { }
    func getConfigRaw() async throws -> [String: Any] {
        let data = try await request("GET", "v1/config")
        let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        return obj?["config"] as? [String: Any] ?? [:]
    }

    func putConfig(_ patch: [String: Any]) async throws {
        _ = try await request("PUT", "v1/config", body: patch)
    }

    // MARK: SSE

    func events() -> AsyncThrowingStream<SSEEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                var req = URLRequest(url: baseURL.appendingPathComponent("v1/events"))
                req.timeoutInterval = 3600 * 24
                req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                let (bytes, resp) = try await URLSession.shared.bytes(for: req)
                guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
                    throw DaemonError.http((resp as? HTTPURLResponse)?.statusCode ?? 0, "SSE")
                }
                for try await line in bytes.lines {
                    guard line.hasPrefix("data: ") else { continue }
                    let json = String(line.dropFirst(6))
                    if let data = json.data(using: .utf8),
                       let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                       let type = obj["type"] as? String {
                        continuation.yield(SSEEvent(type: type, data: obj))
                    }
                }
                continuation.finish()
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
