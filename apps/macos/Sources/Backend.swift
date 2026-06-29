import Foundation

// MARK: - Cliente REST

struct RunRequest: Encodable {
    let projectId: String
    let scope: String
    let id: String
    let dryRun: Bool
    let humanCheckpoint: String
    let safe: Bool

    enum CodingKeys: String, CodingKey {
        case projectId = "project_id"
        case scope, id, safe
        case dryRun = "dry_run"
        case humanCheckpoint = "human_checkpoint"
    }
}

struct BackendError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

final class APIClient {
    let base: URL
    private let session = URLSession(configuration: .default)

    init(port: Int) {
        self.base = URL(string: "http://127.0.0.1:\(port)")!
    }

    private func decode<T: Decodable>(_ type: T.Type, _ data: Data) throws -> T {
        try JSONDecoder().decode(T.self, from: data)
    }

    private func check(_ data: Data, _ resp: URLResponse) throws {
        guard let http = resp as? HTTPURLResponse, http.statusCode >= 400 else { return }
        // FastAPI devolve {"detail": "..."}
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let detail = obj["detail"] as? String {
            throw BackendError(message: detail)
        }
        throw BackendError(message: "HTTP \(http.statusCode)")
    }

    private func get<T: Decodable>(_ path: String, _ type: T.Type) async throws -> T {
        let (data, resp) = try await session.data(from: base.appendingPathComponent(path))
        try check(data, resp)
        return try decode(type, data)
    }

    private func post<B: Encodable, T: Decodable>(_ path: String, _ body: B, _ type: T.Type) async throws -> T {
        var req = URLRequest(url: base.appendingPathComponent(path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(body)
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
        return try decode(type, data)
    }

    func health() async throws -> HealthResult { try await get("health", HealthResult.self) }
    func projects() async throws -> [Project] { try await get("projects", [Project].self) }

    func addProject(path: String) async throws -> Project {
        struct Body: Encodable { let path: String }
        return try await post("projects", Body(path: path), Project.self)
    }

    func deleteProject(_ id: String) async throws {
        var req = URLRequest(url: base.appendingPathComponent("projects/\(id)"))
        req.httpMethod = "DELETE"
        _ = try await session.data(for: req)
    }

    func detect(_ id: String) async throws -> DetectResult {
        try await get("projects/\(id)/detect", DetectResult.self)
    }

    struct OK: Decodable { let ok: Bool }

    func run(_ req: RunRequest) async throws {
        _ = try await post("run", req, OK.self)
    }

    func control(_ action: String) async throws {
        struct Body: Encodable { let action: String }
        _ = try await post("control", Body(action: action), OK.self)
    }

    func status(_ id: String) async throws -> [EpicInfo] {
        struct R: Decodable { let epics: [EpicInfo] }
        return try await get("projects/\(id)/status", R.self).epics
    }

    // Config bruto (JSON) — usado pela tela de Settings.
    func getConfigData(_ id: String) async throws -> Data {
        let (data, resp) = try await session.data(from: base.appendingPathComponent("projects/\(id)/config"))
        try check(data, resp)
        return data
    }

    func setConfigData(_ id: String, _ body: Data) async throws {
        var req = URLRequest(url: base.appendingPathComponent("projects/\(id)/config"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
    }

    // Stop síncrono e best-effort (usado ao encerrar o app).
    func stopSync(timeout: TimeInterval = 2) {
        var req = URLRequest(url: base.appendingPathComponent("control"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["action": "stop"])
        req.timeoutInterval = timeout
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { _, _, _ in sem.signal() }.resume()
        _ = sem.wait(timeout: .now() + timeout)
    }
}

// MARK: - Stream de eventos (WebSocket)

final class EventStream {
    private var task: URLSessionWebSocketTask?
    private let url: URL
    private let session = URLSession(configuration: .default)
    private var closedIntentionally = false
    var onEvent: ((AutoEvent) -> Void)?
    var onClose: (() -> Void)?      // perdeu conexão (vai tentar reconectar)
    var onReconnect: (() -> Void)?  // reconectou

    init(port: Int) {
        self.url = URL(string: "ws://127.0.0.1:\(port)/ws")!
    }

    func connect() {
        closedIntentionally = false
        let t = session.webSocketTask(with: url)
        task = t
        t.resume()
        receive()
    }

    func disconnect() {
        closedIntentionally = true
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
    }

    private func scheduleReconnect() {
        guard !closedIntentionally else { return }
        DispatchQueue.main.async { self.onClose?() }
        // tenta reconectar — mantém os eventos fluindo e evita o dead-man switch
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            guard let self, !self.closedIntentionally else { return }
            self.connect()
            self.onReconnect?()
        }
    }

    private func receive() {
        task?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure:
                self.scheduleReconnect()
            case .success(let message):
                if case .string(let text) = message, let data = text.data(using: .utf8),
                   let ev = try? JSONDecoder().decode(AutoEvent.self, from: data) {
                    DispatchQueue.main.async { self.onEvent?(ev) }
                }
                self.receive()
            }
        }
    }
}

// MARK: - Controlador do backend (spawn/connect)

@MainActor
final class BackendController {
    let port: Int
    private var process: Process?

    init(port: Int = 8765) { self.port = port }

    /// Conecta a um backend já rodando; se não houver, spawna um.
    func ensureRunning() async -> Bool {
        let api = APIClient(port: port)
        if (try? await api.health()) != nil { return true }   // já rodando (modo dev)
        spawn()
        // aguarda subir
        for _ in 0..<60 {
            if (try? await api.health()) != nil { return true }
            try? await Task.sleep(nanoseconds: 300_000_000)
        }
        return false
    }

    private func spawn() {
        let (exe, args, cwd) = backendCommand()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        if let cwd { p.currentDirectoryURL = URL(fileURLWithPath: cwd) }
        try? p.run()
        process = p
    }

    /// Resolve como lançar o backend:
    /// 1) binário embutido em Resources/ (release, PyInstaller)
    /// 2) AUTOPILOT_PYTHON + AUTOPILOT_REPO (env, dev)
    /// 3) <repo>/.venv/bin/python -m autopilot serve (dev)
    private func backendCommand() -> (String, [String], String?) {
        if let bundled = Bundle.main.url(forResource: "autopilot-backend", withExtension: nil) {
            return (bundled.path, ["serve", "--port", "\(port)"], nil)
        }
        let env = ProcessInfo.processInfo.environment
        let repo = env["AUTOPILOT_REPO"] ?? defaultRepoRoot()
        let python = env["AUTOPILOT_PYTHON"] ?? "\(repo)/.venv/bin/python"
        return (python, ["-m", "autopilot", "serve", "--port", "\(port)"], repo)
    }

    private func defaultRepoRoot() -> String {
        let fm = FileManager.default
        // candidatos em ordem: cwd, e o caminho conhecido do repo
        let candidates = [
            fm.currentDirectoryPath,
            "\(fm.homeDirectoryForCurrentUser.path)/codes/autopilot",
        ]
        for c in candidates where fm.fileExists(atPath: "\(c)/.venv/bin/python") {
            return c
        }
        return fm.currentDirectoryPath
    }

    func shutdown() {
        process?.terminate()
        process = nil
    }
}
