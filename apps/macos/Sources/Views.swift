import SwiftUI
import AppKit

// MARK: - Janela principal

struct ContentView: View {
    @EnvironmentObject var store: RunStore

    var body: some View {
        NavigationSplitView {
            SidebarView()
                .frame(minWidth: 240)
        } content: {
            RunCenterView()
                .frame(minWidth: 460)
        } detail: {
            InspectorView()
                .frame(minWidth: 280)
        }
        .toolbar { RunToolbar() }
        .sheet(item: Binding(
            get: { store.checkpointLabel.map { CheckpointItem(label: $0) } },
            set: { _ in store.checkpointLabel = nil }
        )) { item in
            CheckpointSheet(label: item.label)
        }
        .sheet(isPresented: $store.showingSettings) {
            SettingsView().environmentObject(store)
        }
        .onAppear { store.boot() }
    }
}

struct CheckpointItem: Identifiable { let id = UUID(); let label: String }

// MARK: - Toolbar

struct RunToolbar: ToolbarContent {
    @EnvironmentObject var store: RunStore

    var body: some ToolbarContent {
        ToolbarItemGroup {
            Toggle("Dry-run", isOn: $store.dryRun)
                .toggleStyle(.button)
            Toggle("Modo seguro", isOn: $store.safeMode)
                .toggleStyle(.button)
                .help("Branch dedicada + commits locais; sem push, PR ou merge. Pausa ao fim de cada story.")

            if store.running {
                Button { store.control("pause") } label: { Image(systemName: "pause.fill") }
                    .clickableCursor()
                Button { store.control("stop") } label: { Image(systemName: "stop.fill") }
                    .foregroundStyle(.red).clickableCursor()
            }

            Button { store.showingSettings = true } label: { Image(systemName: "gearshape") }
                .clickableCursor()
                .help("Settings do projeto (prompt do advisor, fluxo git)")
                .disabled(store.selected == nil)

            Spacer()
            HStack(spacing: 6) {
                Circle().fill(store.backendUp ? .green : .red).frame(width: 8, height: 8)
                Text("opus-4-8").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

// MARK: - Sidebar (projetos + árvore epics/stories)

struct SidebarView: View {
    @EnvironmentObject var store: RunStore

    var body: some View {
        List {
            Section("Projetos") {
                ForEach(store.projects) { p in
                    HStack {
                        Image(systemName: "folder")
                        VStack(alignment: .leading) {
                            Text(p.name).fontWeight(store.selected?.id == p.id ? .semibold : .regular)
                            Text(p.path).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                    .contentShape(Rectangle())
                    .onTapGesture { Task { await store.select(p) } }
                    .contextMenu {
                        Button("Remover", role: .destructive) { store.deleteProject(p) }
                    }
                }
                Button { pickProject() } label: {
                    Label("Adicionar projeto…", systemImage: "plus")
                }
            }

            if let sel = store.selected {
                Section("Epics — \(sel.name)") {
                    ForEach(store.epics) { epic in
                        EpicRow(epic: epic)
                    }
                    if store.epics.isEmpty {
                        Text("nenhuma epic detectada").font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .listStyle(.sidebar)
    }

    private func pickProject() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url {
            store.addProject(path: url.path)
        }
    }
}

struct EpicRow: View {
    @EnvironmentObject var store: RunStore
    let epic: EpicInfo
    @State private var expanded = true

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            ForEach(epic.stories) { s in
                let runnable = s.runnable ?? true
                HStack {
                    Image(systemName: "circle.fill")
                        .font(.system(size: 6))
                        .foregroundStyle(Theme.color(for: store.status(for: s.key, fallback: s.status)))
                    Text(s.key).font(.callout).lineLimit(1)
                    Spacer()
                    StatusBadge(status: store.status(for: s.key, fallback: s.status))
                    Button { store.runStory(s.key) } label: {
                        Image(systemName: "play.circle")
                    }
                    .buttonStyle(.borderless)
                    .disabled(!store.backendUp || store.running || !runnable)
                    .clickableCursor()
                    .help(runnable ? "Rodar esta story" : (s.runnableReason ?? "fora de ordem"))
                }
            }
        } label: {
            HStack {
                Text("Epic \(epic.epic)").fontWeight(.semibold)
                if let es = epic.epicStatus { StatusBadge(status: es) }
                Spacer()
                Button { store.runEpic(epic.epic) } label: {
                    Image(systemName: "play.circle.fill")
                }
                .buttonStyle(.borderless)
                .disabled(!store.backendUp || store.running || !(epic.runnable ?? true))
                .clickableCursor()
                .help((epic.runnable ?? true) ? "Rodar a epic inteira" : (epic.runnableReason ?? "fora de ordem"))
            }
        }
    }
}

// MARK: - Centro (timeline + console streaming)

struct RunCenterView: View {
    @EnvironmentObject var store: RunStore

    var body: some View {
        VStack(spacing: 0) {
            if !store.backendUp {
                Banner(color: .orange, icon: "bolt.horizontal.circle",
                       text: "Backend offline — o app não roda nada sem ele.") {
                    Button("Reconectar") { store.connect() }
                }
            }
            if let err = store.lastError, store.backendUp {
                Banner(color: .red, icon: "exclamationmark.triangle", text: err) {
                    Button("OK") { store.lastError = nil }
                }
            }
            if store.dryRun {
                Banner(color: .blue, icon: "eye", text: "Dry-run: simula o ciclo sem chamar as skills nem mexer no git.") { EmptyView() }
            } else if store.safeMode {
                Banner(color: .green, icon: "lock.shield", text: "Modo seguro: branch autopilot/<story> + commits locais. Sem push/PR/merge; pausa ao fim de cada story.") { EmptyView() }
            } else {
                Banner(color: .red, icon: "exclamationmark.octagon", text: "Run REAL sem modo seguro: vai editar arquivos, commitar, abrir PR e dar MERGE na main.") { EmptyView() }
            }
            PhaseTimeline()
                .padding()
            Divider()
            StreamConsole()
        }
    }
}

struct Banner<Trailing: View>: View {
    let color: Color
    let icon: String
    let text: String
    @ViewBuilder var trailing: Trailing
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: icon)
            Text(text).font(.callout)
            Spacer()
            trailing
        }
        .padding(.horizontal, 12).padding(.vertical, 7)
        .background(color.opacity(0.14))
        .foregroundStyle(color)
    }
}

struct PhaseTimeline: View {
    @EnvironmentObject var store: RunStore

    var body: some View {
        HStack(spacing: 10) {
            ForEach(Array(store.phases.enumerated()), id: \.element.id) { idx, phase in
                PhaseCard(phase: phase)
                if idx < store.phases.count - 1 {
                    Image(systemName: "arrow.right").foregroundStyle(.secondary)
                }
            }
        }
        .animation(.easeInOut, value: store.phases.map(\.state.rawValue))
    }
}

struct PhaseCard: View {
    let phase: PhaseRow
    var body: some View {
        VStack(spacing: 6) {
            ZStack {
                Circle().fill(Theme.color(for: phase.state).opacity(0.15)).frame(width: 40, height: 40)
                if phase.state == .running {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: phase.state == .done ? "checkmark" : Theme.symbol(forSkill: phase.id))
                        .foregroundStyle(Theme.color(for: phase.state))
                }
            }
            Text(phase.title).font(.caption).fontWeight(phase.state == .running ? .bold : .regular)
        }
        .frame(width: 96)
    }
}

struct StreamConsole: View {
    @EnvironmentObject var store: RunStore
    @State private var filter = "all"   // all | worker | advisor | decisions

    private var entries: [TranscriptEntry] {
        store.transcript.filter { e in
            switch filter {
            case "worker":   return (e.role == "worker") || e.kind == .ask || e.kind == .phase
            case "advisor":  return (e.role == "advisor") || e.kind == .decision
            case "decisions":return e.kind == .decision || e.kind == .ask
            default:         return true
            }
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Picker("", selection: $filter) {
                    Text("Tudo").tag("all")
                    Text("Worker").tag("worker")
                    Text("Advisor").tag("advisor")
                    Text("Decisões").tag("decisions")
                }
                .pickerStyle(.segmented).frame(width: 320)
                Spacer()
                Text("\(store.decisions.count) decisões").font(.caption).foregroundStyle(.secondary)
            }
            .padding(8)
            Divider()

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        ForEach(entries) { TranscriptRow(entry: $0) }
                        Color.clear.frame(height: 1).id("end")
                    }
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .background(Color(nsColor: .textBackgroundColor))
                .onChange(of: store.transcript.count) { proxy.scrollTo("end", anchor: .bottom) }
            }
        }
    }
}

struct TranscriptRow: View {
    let entry: TranscriptEntry

    var body: some View {
        switch entry.kind {
        case .phase:
            HStack(spacing: 8) {
                Image(systemName: Theme.symbol(forSkill: entry.title))
                Text("\(entry.title)  ·  \(entry.subtitle)").fontWeight(.semibold)
                Rectangle().fill(.quaternary).frame(height: 1)
            }
            .font(.callout).foregroundStyle(.secondary).padding(.top, 6)

        case .message:
            VStack(alignment: .leading, spacing: 4) {
                Label(Theme.roleLabel(entry.role), systemImage: Theme.roleIcon(entry.role))
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Theme.roleColor(entry.role))
                Text(entry.text.isEmpty ? "…" : entry.text)
                    .font(.callout).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(10)
            .background(Theme.roleColor(entry.role).opacity(0.08),
                        in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10)
                .stroke(Theme.roleColor(entry.role).opacity(0.25)))

        case .tool:
            Label("\(Theme.roleLabel(entry.role)) usou \(entry.title)", systemImage: "wrench.and.screwdriver")
                .font(.caption).foregroundStyle(.secondary)
                .padding(.horizontal, 8).padding(.vertical, 3)
                .background(.quaternary.opacity(0.4), in: Capsule())

        case .ask:
            HStack(spacing: 8) {
                Image(systemName: "questionmark.bubble.fill")
                VStack(alignment: .leading, spacing: 1) {
                    Text(entry.title).fontWeight(.semibold)
                    Text(entry.subtitle).font(.caption)
                }
                Spacer()
            }
            .font(.callout).foregroundStyle(.orange)
            .padding(10)
            .background(.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))

        case .decision:
            VStack(alignment: .leading, spacing: 6) {
                Label("Advisor respondeu", systemImage: "checkmark.bubble.fill")
                    .font(.caption.weight(.bold)).foregroundStyle(.purple)
                if !entry.title.isEmpty {
                    Text(entry.title).font(.caption).foregroundStyle(.secondary)
                }
                Text(entry.subtitle).font(.callout.weight(.semibold))
                if !entry.text.isEmpty {
                    Text(entry.text).font(.caption).foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.purple.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(.purple.opacity(0.35)))

        case .git:
            Label("git \(entry.title): \(entry.subtitle)", systemImage: "arrow.triangle.branch")
                .font(.caption).foregroundStyle(.green)

        case .status:
            Label("\(entry.title): \(entry.subtitle)", systemImage: "arrow.right.circle")
                .font(.caption).foregroundStyle(.teal)

        case .note:
            Text(entry.text.isEmpty ? entry.subtitle : entry.text)
                .font(.caption).foregroundStyle(.secondary)

        case .error:
            Label(entry.text, systemImage: "exclamationmark.triangle.fill")
                .font(.caption).foregroundStyle(.red)
        }
    }
}

// MARK: - Inspector (decisões + git + mensagens)

struct InspectorView: View {
    @EnvironmentObject var store: RunStore

    var body: some View {
        List {
            Section("Conversa — decisões do advisor") {
                if store.decisions.isEmpty {
                    Text("nenhuma ainda").font(.caption).foregroundStyle(.secondary)
                }
                ForEach(store.decisions) { d in
                    DisclosureGroup {
                        VStack(alignment: .leading, spacing: 6) {
                            if !d.question.isEmpty {
                                Text("Pergunta (skill)").font(.caption2).foregroundStyle(.secondary)
                                Text(d.question).font(.caption).textSelection(.enabled)
                            }
                            if !d.rationale.isEmpty {
                                Text("Razão (advisor)").font(.caption2).foregroundStyle(.secondary)
                                Text(d.rationale).font(.caption).textSelection(.enabled)
                            }
                        }.padding(.top, 4)
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            if !d.phase.isEmpty {
                                Text(d.phase).font(.caption2).foregroundStyle(.secondary)
                            }
                            Text(d.decision).fontWeight(.semibold).lineLimit(2)
                        }
                    }
                }
            }
            Section("Git") {
                if store.gitRows.isEmpty {
                    Text("nenhuma ação").font(.caption).foregroundStyle(.secondary)
                }
                ForEach(store.gitRows) { g in
                    Label("\(g.op): \(g.result)", systemImage: "arrow.triangle.branch")
                        .font(.callout).lineLimit(2)
                }
            }
            if !store.messages.isEmpty {
                Section("Log") {
                    ForEach(Array(store.messages.enumerated()), id: \.offset) { _, m in
                        Text(m).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }
}

// MARK: - Sheet de checkpoint

struct CheckpointSheet: View {
    @EnvironmentObject var store: RunStore
    let label: String

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "pause.circle.fill").font(.largeTitle).foregroundStyle(.orange)
            Text("Checkpoint").font(.title2.bold())
            Text(label).foregroundStyle(.secondary).multilineTextAlignment(.center)
            HStack {
                Button("Parar", role: .destructive) {
                    store.control("stop"); store.checkpointLabel = nil
                }
                Button("Aprovar e continuar") {
                    store.control("approve"); store.checkpointLabel = nil
                }
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(28)
        .frame(width: 380)
    }
}

// MARK: - Settings (prompt do advisor + fluxo de git, por projeto)

struct SettingsView: View {
    @EnvironmentObject var store: RunStore
    @Environment(\.dismiss) private var dismiss
    @State private var advisorPrompt = ""
    @State private var optionsJSON = ""   // invoke_template, human_checkpoint, models, phases
    @State private var loading = true
    @State private var parseError: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Settings — \(store.selected?.name ?? "")").font(.title2.bold())
            Text("Salvo em `\(store.selected?.path ?? "")/autopilot.yaml`")
                .font(.caption).foregroundStyle(.secondary)

            if loading {
                ProgressView().frame(maxWidth: .infinity)
            } else {
                Text("Prompt do advisor").font(.headline)
                TextEditor(text: $advisorPrompt)
                    .font(.system(.callout, design: .monospaced))
                    .frame(minHeight: 150)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary))

                HStack {
                    Text("Fluxo de git + opções (JSON)").font(.headline)
                    Spacer()
                    Button("Preset seguro") { applyPreset(safe: true) }.clickableCursor()
                    Button("Preset completo") { applyPreset(safe: false) }.clickableCursor()
                }
                TextEditor(text: $optionsJSON)
                    .font(.system(.callout, design: .monospaced))
                    .frame(minHeight: 200)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary))
                if let parseError {
                    Text(parseError).font(.caption).foregroundStyle(.red)
                }
            }

            HStack {
                Spacer()
                Button("Cancelar") { dismiss() }.clickableCursor()
                Button("Salvar") { save() }
                    .keyboardShortcut(.defaultAction).clickableCursor().disabled(loading)
            }
        }
        .padding(20)
        .frame(width: 640, height: 640)
        .task { await load() }
    }

    private func load() async {
        guard let data = await store.loadConfigData(),
              var obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { loading = false; return }
        advisorPrompt = obj["advisor_prompt"] as? String ?? ""
        obj.removeValue(forKey: "advisor_prompt")
        obj.removeValue(forKey: "has_override_file")
        optionsJSON = prettyJSON(obj)
        loading = false
    }

    private func save() {
        guard var obj = (try? JSONSerialization.jsonObject(with: Data(optionsJSON.utf8)))
                as? [String: Any] else {
            parseError = "JSON inválido no editor de opções."
            return
        }
        obj["advisor_prompt"] = advisorPrompt
        guard let data = try? JSONSerialization.data(withJSONObject: obj) else { return }
        Task { if await store.saveConfigData(data) { dismiss() } }
    }

    private func applyPreset(safe: Bool) {
        var obj = (try? JSONSerialization.jsonObject(with: Data(optionsJSON.utf8)))
            as? [String: Any] ?? [:]
        obj["phases"] = safe ? Self.safePhases : Self.fullPhases
        optionsJSON = prettyJSON(obj)
    }

    private func prettyJSON(_ obj: [String: Any]) -> String {
        guard let d = try? JSONSerialization.data(
            withJSONObject: obj, options: [.prettyPrinted, .sortedKeys]) else { return "{}" }
        return String(data: d, encoding: .utf8) ?? "{}"
    }

    static let safePhases: [String: Any] = [
        "bmad-create-story": ["git": [["create_branch": "autopilot/{story_id}"], ["commit": "story: draft {story_id}"]]],
        "bmad-dev-story": ["git": [["commit": "feat: implement {story_id}"]]],
        "bmad-code-review": ["git": [["commit": "review: {story_id}"]]],
        "bmad-retrospective": ["git": [["commit": "chore: retrospective epic-{epic_id}"]]],
    ]
    static let fullPhases: [String: Any] = [
        "bmad-create-story": ["git": [["create_branch": "story/{story_id}"], ["commit": "story: draft {story_id}"]]],
        "bmad-dev-story": ["git": [["commit": "feat: implement {story_id}"]]],
        "bmad-code-review": ["git": [["commit": "review: {story_id}"], ["open_pr": ["base": "main", "title": "{story_id}"]], ["merge_pr": ["method": "squash"]]]],
        "bmad-retrospective": ["git": [["commit": "chore: retrospective epic-{epic_id}"]]],
    ]
}

// MARK: - Painel da barra de menu

struct MenuBarPanel: View {
    @EnvironmentObject var store: RunStore

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Circle().fill(store.running ? .green : .secondary).frame(width: 8, height: 8)
                Text(store.running ? "Rodando \(store.currentTarget)" : "Parado").font(.headline)
            }
            if let phase = store.phases.first(where: { $0.state == .running }) {
                Label(phase.title, systemImage: Theme.symbol(forSkill: phase.id)).font(.callout)
            }
            Divider()
            HStack {
                Button { store.control("pause") } label: { Image(systemName: "pause.fill") }
                Button { store.control("resume") } label: { Image(systemName: "play.fill") }
                Button { store.control("stop") } label: { Image(systemName: "stop.fill") }.foregroundStyle(.red)
                Spacer()
            }
            .disabled(!store.running)
            Divider()
            Text("\(store.decisions.count) decisões · \(store.gitRows.count) git")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(width: 260)
    }
}
