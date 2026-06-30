import Foundation
import SwiftUI

@MainActor
final class RunStore: ObservableObject {
    static let shared = RunStore()

    let port = 8765
    let api: APIClient
    private let stream: EventStream
    let backend: BackendController

    // conexão / projetos
    @Published var backendUp = false
    @Published var projects: [Project] = []
    @Published var selected: Project?
    @Published var epics: [EpicInfo] = []
    @Published var detectWarnings: [String] = []

    // controles
    @Published var scope: String = "epic"          // "story" | "epic"
    @Published var dryRun = true                     // seguro por padrão (fixture/primeiro uso)
    @Published var safeMode = true                   // branch + commits locais; sem push/PR/merge
    @Published var humanCheckpoint = "none"          // none | end-of-story | retrospective
    @Published var lastError: String?

    // estado do run
    @Published var running = false
    @Published var currentTarget = ""
    @Published var runningEpic: Int?                 // epic em execução (estável durante o run)
    @Published var phases: [PhaseRow] = RunStore.basePhases()
    @Published var workerLog = ""
    @Published var advisorLog = ""
    @Published var transcript: [TranscriptEntry] = []   // a "conversa" renderizada
    private var curMsgIndex: Int?                        // bolha de mensagem em streaming
    private var curRole: String?
    @Published var decisions: [DecisionRow] = []
    @Published var gitRows: [GitRow] = []
    @Published var messages: [String] = []
    @Published var liveStatus: [String: String] = [:]   // key -> status (sobrepõe detect)
    @Published var checkpointLabel: String?
    @Published var recovery: RecoveryItem?            // recuperação aguardando decisão
    @Published var paused = false                     // run pausado (gate)
    @Published var tokenLimitBanner: String?          // halt por limite de tokens
    @Published var online = true                      // conexão Claude/internet (por eventos)
    @Published var connectionBanner: String?          // halt por queda de rede
    @Published var showingSettings = false

    static func storyPhases() -> [PhaseRow] {
        [
            .init(id: "bmad-create-story", title: "Create Story", state: .pending, target: ""),
            .init(id: "bmad-dev-story", title: "Dev Story", state: .pending, target: ""),
            .init(id: "bmad-code-review", title: "Code Review", state: .pending, target: ""),
        ]
    }

    // Epic inclui a retrospective (que só roda ao concluir todas as stories da epic).
    static func epicPhases() -> [PhaseRow] {
        storyPhases() + [.init(id: "bmad-retrospective", title: "Retrospective", state: .pending, target: "")]
    }

    static func basePhases() -> [PhaseRow] { storyPhases() }

    init() {
        api = APIClient(port: port)
        stream = EventStream(port: port)
        backend = BackendController(port: port)
        stream.onEvent = { [weak self] ev in self?.apply(ev) }
        stream.onClose = { [weak self] in self?.backendUp = false }       // caiu (reconectando)
        stream.onReconnect = { [weak self] in                              // voltou
            guard let self else { return }
            self.backendUp = true
            if let p = self.selected { Task { await self.select(p) } }     // re-busca status/runnable
        }
    }

    func boot() { connect() }

    func connect() {
        Task {
            backendUp = await backend.ensureRunning()
            if backendUp {
                lastError = nil
                stream.connect()
                await refreshProjects()
            } else {
                lastError = "Backend offline. Rode `autopilot serve` ou clique em Reconectar."
            }
        }
    }

    func refreshProjects() async {
        projects = (try? await api.projects()) ?? []
        if selected == nil { selected = projects.first }
        if let s = selected { await select(s) }
    }

    func addProject(path: String) {
        Task {
            if let p = try? await api.addProject(path: path) {
                await refreshProjects()
                selected = p
                await select(p)
            }
        }
    }

    func deleteProject(_ p: Project) {
        Task {
            try? await api.deleteProject(p.id)
            if selected?.id == p.id { selected = nil; epics = [] }
            await refreshProjects()
        }
    }

    func select(_ p: Project) async {
        selected = p
        liveStatus = [:]
        if let d = try? await api.detect(p.id) {
            epics = d.epics
            detectWarnings = d.warnings
        }
    }

    func runStory(_ key: String, fresh: Bool = false) { startRun(scope: "story", id: key, fresh: fresh) }
    func runEpic(_ epic: Int, fresh: Bool = false) { startRun(scope: "epic", id: "\(epic)", fresh: fresh) }

    func startRun(scope: String, id: String, fresh: Bool = false) {
        guard let p = selected else { lastError = "Selecione um projeto primeiro."; return }
        guard backendUp else { lastError = "Backend offline."; return }
        guard !running else { lastError = "Já existe um run ativo. Pare-o antes de iniciar outro."; return }
        resetRun()
        Task {
            do {
                try await api.run(RunRequest(
                    projectId: p.id, scope: scope, id: id,
                    dryRun: dryRun, humanCheckpoint: humanCheckpoint, safe: safeMode, fresh: fresh))
            } catch {
                lastError = (error as? BackendError)?.message ?? error.localizedDescription
            }
        }
    }

    func control(_ action: String) {
        Task { try? await api.control(action) }
    }

    /// Chamado quando o app vai encerrar: para o run e mata o backend filho.
    func shutdownForQuit() {
        if running { api.stopSync() }   // best-effort, síncrono
        backend.shutdown()
    }

    // ---- Settings (config por projeto) ---------------------------------
    func loadConfigData() async -> Data? {
        guard let p = selected else { return nil }
        return try? await api.getConfigData(p.id)
    }

    func saveConfigData(_ data: Data) async -> Bool {
        guard let p = selected else { return false }
        do {
            try await api.setConfigData(p.id, data)
            return true
        } catch {
            lastError = (error as? BackendError)?.message ?? error.localizedDescription
            return false
        }
    }

    private func resetRun() {
        phases = RunStore.basePhases()
        workerLog = ""; advisorLog = ""
        transcript = []; curMsgIndex = nil; curRole = nil
        decisions = []; gitRows = []; messages = []
        checkpointLabel = nil; recovery = nil
        paused = false; tokenLimitBanner = nil
        online = true; connectionBanner = nil
    }

    // ---- montagem da conversa (transcript) -----------------------------
    private func streamDelta(_ role: String, _ text: String) {
        if curRole != role || curMsgIndex == nil {
            transcript.append(.init(kind: .message, role: role))
            curMsgIndex = transcript.count - 1
            curRole = role
        }
        if let i = curMsgIndex { transcript[i].text += text }
    }

    private func closeStream() { curMsgIndex = nil; curRole = nil }

    private func addEntry(_ e: TranscriptEntry) {
        closeStream()
        transcript.append(e)
        if transcript.count > 600 { transcript.removeFirst(transcript.count - 600) }
    }

    // MARK: - aplica eventos do backend ao estado

    private func setPhase(_ skill: String, _ state: PhaseState, target: String = "") {
        if let i = phases.firstIndex(where: { $0.id == skill }) {
            phases[i].state = state
            if !target.isEmpty { phases[i].target = target }
        }
    }

    private func append(_ keyPath: ReferenceWritableKeyPath<RunStore, String>, _ text: String) {
        self[keyPath: keyPath] += text
        let cap = 200_000
        if self[keyPath: keyPath].count > cap {
            self[keyPath: keyPath] = String(self[keyPath: keyPath].suffix(cap))
        }
    }

    func apply(_ ev: AutoEvent) {
        switch ev.kind {
        case "run_started":
            resetRun(); running = true
            online = true; connectionBanner = nil   // novo run -> rede presumida OK
            currentTarget = ev.target ?? ""
            scope = ev.scope ?? scope
            runningEpic = (ev.scope == "epic")
                ? Int(ev.target ?? "") : Self.epicOf(ev.target ?? "")
            dryRun = ev.dryRun ?? dryRun
            // story → 3 fases; epic → 3 + retrospective (que só roda no fim da epic)
            phases = (ev.scope == "epic") ? RunStore.epicPhases() : RunStore.storyPhases()
        case "run_ended":
            running = false
            paused = false
            runningEpic = nil
            recovery = nil   // dispensa qualquer sheet de recuperação pendente
            // para qualquer spinner: fase que ficou "running" volta a pending
            for i in phases.indices where phases[i].state == .running {
                phases[i].state = .pending
            }
            messages.append(ev.ok == true ? "✔ run concluído" : "■ run: \(ev.reason ?? "")")
            if let p = selected {     // atualiza status reais + flags de ordem
                Task { await select(p) }
            }
        case "phase_started":
            setPhase(ev.skill ?? "", .running, target: ev.target ?? "")
            currentTarget = ev.target ?? currentTarget
            addEntry(.init(kind: .phase, title: ev.skill ?? "", subtitle: ev.target ?? ""))
        case "phase_ended":
            setPhase(ev.skill ?? "", .done)
            closeStream()
        case "phase_resumed":
            addEntry(.init(kind: .note, title: "↻ retomando",
                           subtitle: "\(ev.skill ?? "") · \(ev.target ?? "") — continuando a sessão"))
        case "assistant_delta":
            let role = ev.role ?? "worker"
            // O advisor não vira bolha: a saída dele é JSON cru e já aparece
            // formatada no card "Advisor respondeu". Só o worker vira bolha.
            if role == "worker" { streamDelta("worker", ev.text ?? "") }
            if role == "advisor" { append(\.advisorLog, ev.text ?? "") }
            else { append(\.workerLog, ev.text ?? "") }
        case "tool_use":
            let role = ev.role ?? "worker", name = ev.name ?? ""
            // AskUserQuestion = o worker está perguntando ao advisor (destaque)
            if name == "AskUserQuestion" {
                addEntry(.init(kind: .ask, role: role,
                               title: "Worker perguntou ao Advisor",
                               subtitle: "aguardando decisão…"))
            } else {
                addEntry(.init(kind: .tool, role: role, title: name))
            }
        case "advisor_decision":
            let q = ev.question?.questionText ?? "", dec = ev.decision?.answerText ?? ""
            let rat = ev.rationale ?? ""
            decisions.append(.init(phase: ev.phase ?? "", question: q, decision: dec, rationale: rat))
            addEntry(.init(kind: .decision, role: "advisor",
                           title: q, subtitle: dec, text: rat))
        case "git_action":
            gitRows.append(.init(op: ev.op ?? "", result: ev.result ?? ""))
            addEntry(.init(kind: .git, title: ev.op ?? "", subtitle: ev.result ?? ""))
        case "status_changed":
            if let key = ev.key, let to = ev.to {
                liveStatus[key] = to
                addEntry(.init(kind: .status, title: key,
                               subtitle: "\(ev.from ?? "?") → \(to)"))
            }
        case "checkpoint_hit":
            checkpointLabel = ev.label
            addEntry(.init(kind: .note, title: "checkpoint", subtitle: ev.label ?? ""))
        case "recovery_recommended":
            let sk = ev.skill ?? "", rs = ev.reason ?? ""
            recovery = RecoveryItem(skill: sk, reason: rs)
            addEntry(.init(kind: .recovery, title: "Recuperação recomendada: \(sk)",
                           subtitle: "aguardando sua decisão…", text: rs))
        case "recovery_started":
            recovery = nil   // já resolvido (auto ou aprovado)
            addEntry(.init(kind: .recovery, title: "Recuperação iniciada: \(ev.skill ?? "")",
                           subtitle: "rodando…", text: ev.reason ?? ""))
        case "run_paused":
            paused = true
            addEntry(.init(kind: .note, title: "pausado", subtitle: ev.reason ?? ""))
        case "run_resumed":
            paused = false
            addEntry(.init(kind: .note, title: "retomado", subtitle: ""))
        case "token_limit":
            var msg = ev.message ?? "limite atingido"
            if let r = ev.resetsAt { msg += " (reseta \(Self.fmtReset(r)))" }
            tokenLimitBanner = msg
            addEntry(.init(kind: .error, text: "⏳ \(msg)"))
        case "connection_lost":
            online = false
            connectionBanner = ev.message?.isEmpty == false ? ev.message! : "sem conexão com o Claude"
            addEntry(.init(kind: .error, text: "📡 sem conexão: \(connectionBanner ?? "")"))
        case "log":
            messages.append(ev.message ?? "")
            addEntry(.init(kind: .note, text: ev.message ?? ""))
        case "error":
            messages.append("✖ \(ev.message ?? "")")
            addEntry(.init(kind: .error, text: ev.message ?? ""))
        default:
            break
        }
    }

    func status(for storyKey: String, fallback: String) -> String {
        liveStatus[storyKey] ?? fallback
    }

    /// Número da epic a partir de uma chave de story ("2-1-fix-shout" -> 2).
    static func epicOf(_ storyKey: String) -> Int? {
        Int(storyKey.split(separator: "-").first ?? "")
    }

    /// Formata um timestamp unix (resets_at do rate-limit) como hora local HH:mm.
    static func fmtReset(_ unix: Int) -> String {
        let f = DateFormatter()
        f.dateFormat = "dd/MM HH:mm"
        return f.string(from: Date(timeIntervalSince1970: TimeInterval(unix)))
    }
}
