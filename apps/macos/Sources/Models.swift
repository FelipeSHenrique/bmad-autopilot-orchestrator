import Foundation

// MARK: - JSON dinâmico (p/ payloads variáveis: question/decision)

enum JSONValue: Codable, Hashable {
    case string(String), number(Double), bool(Bool), null
    case array([JSONValue]), object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else if let o = try? c.decode([String: JSONValue].self) { self = .object(o) }
        else { self = .null }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let s): try c.encode(s)
        case .number(let n): try c.encode(n)
        case .bool(let b): try c.encode(b)
        case .null: try c.encodeNil()
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }

    /// Só o texto das perguntas (AskUserQuestion vem como lista de objetos
    /// {question, header, options, multiSelect}). Cai p/ `display` se não casar.
    var questionText: String {
        if case .array(let a) = self {
            let qs = a.compactMap { item -> String? in
                if case .object(let o) = item, case .string(let q)? = o["question"] { return q }
                return nil
            }
            if !qs.isEmpty { return qs.joined(separator: "\n") }
        }
        if case .object(let o) = self, case .string(let q)? = o["question"] { return q }
        return display
    }

    /// Só o(s) valor(es) escolhido(s) (decision vem como {pergunta: escolha}).
    var answerText: String {
        if case .object(let o) = self {
            let vals = o.values.map(\.display).filter { !$0.isEmpty }
            if !vals.isEmpty { return vals.joined(separator: ", ") }
        }
        return display
    }

    /// Renderização compacta e legível para a UI.
    var display: String {
        switch self {
        case .string(let s): return s
        case .number(let n): return n == n.rounded() ? String(Int(n)) : String(n)
        case .bool(let b): return b ? "true" : "false"
        case .null: return "—"
        case .array(let a): return a.map(\.display).joined(separator: ", ")
        case .object(let o):
            return o.map { "\($0.key): \($0.value.display)" }.joined(separator: " · ")
        }
    }
}

// MARK: - Evento vindo do backend (WebSocket)

struct AutoEvent: Identifiable, Decodable {
    let id = UUID()
    let kind: String
    let ts: Double
    // campos opcionais conforme o tipo de evento
    let role: String?
    let text: String?
    let skill: String?
    let target: String?
    let name: String?
    let message: String?
    let level: String?
    let op: String?
    let result: String?
    let key: String?
    let to: String?
    let from: String?
    let label: String?
    let scope: String?
    let dryRun: Bool?
    let ok: Bool?
    let reason: String?
    let rationale: String?
    let phase: String?
    let decision: JSONValue?
    let question: JSONValue?
    let resetsAt: Int?

    enum CodingKeys: String, CodingKey {
        case kind, ts, role, text, skill, target, name, message, level, op, result
        case key, to, from, label, scope, ok, reason, rationale, phase, decision, question
        case dryRun = "dry_run"
        case resetsAt = "resets_at"
    }
}

// MARK: - REST models

struct Project: Identifiable, Codable, Hashable {
    let id: String
    let path: String
    let name: String
}

struct StoryInfo: Identifiable, Codable, Hashable {
    var id: String { key }
    let key: String
    let num: Int
    let status: String
    let runnable: Bool?
    let runnableReason: String?

    enum CodingKeys: String, CodingKey {
        case key, num, status, runnable
        case runnableReason = "runnable_reason"
    }
}

struct EpicInfo: Identifiable, Codable, Hashable {
    var id: Int { epic }
    let epic: Int
    let stories: [StoryInfo]
    let epicStatus: String?
    let retrospective: String?
    let runnable: Bool?
    let runnableReason: String?

    enum CodingKeys: String, CodingKey {
        case epic, stories, runnable
        case epicStatus = "epic_status"
        case retrospective
        case runnableReason = "runnable_reason"
    }
}

struct DetectResult: Codable {
    let bmadInstalled: Bool
    let sprintStatusPath: String?
    let sprintStatusExists: Bool
    let epics: [EpicInfo]
    let warnings: [String]

    enum CodingKeys: String, CodingKey {
        case bmadInstalled = "bmad_installed"
        case sprintStatusPath = "sprint_status_path"
        case sprintStatusExists = "sprint_status_exists"
        case epics, warnings
    }
}

struct HealthResult: Codable {
    let ok: Bool
    let running: Bool
}

// MARK: - estado derivado p/ a UI

enum PhaseState: String { case pending, running, done }

struct PhaseRow: Identifiable {
    let id: String          // skill
    var title: String
    var state: PhaseState
    var target: String
}

struct DecisionRow: Identifiable {
    let id = UUID()
    let phase: String
    let question: String
    let decision: String
    let rationale: String
}

// Entrada da "conversa" (transcript) renderizada no console.
struct TranscriptEntry: Identifiable {
    enum Kind { case phase, message, tool, ask, decision, git, status, note, error, recovery }
    let id = UUID()
    var kind: Kind
    var role: String = ""        // "worker" | "advisor"
    var title: String = ""       // skill, nome da tool, pergunta, op de git, chave de status
    var subtitle: String = ""    // escolha do advisor, "de → para", resultado do git
    var text: String = ""        // corpo (mensagem em streaming, razão da decisão)
}

struct GitRow: Identifiable {
    let id = UUID()
    let op: String
    let result: String
}

// Recomendação de recuperação aguardando decisão humana (correct-course no modo tiered).
struct RecoveryItem: Identifiable {
    let id = UUID()
    let skill: String
    let reason: String
}
