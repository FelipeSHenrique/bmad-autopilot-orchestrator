import SwiftUI
import AppKit

/// Mostra a mãozinha (pointing hand) ao passar o mouse — em botões/linhas clicáveis.
struct ClickableCursor: ViewModifier {
    func body(content: Content) -> some View {
        content.onHover { inside in
            if inside { NSCursor.pointingHand.push() } else { NSCursor.pop() }
        }
    }
}

extension View {
    func clickableCursor() -> some View { modifier(ClickableCursor()) }
}

enum Theme {
    static func color(for status: String) -> Color {
        switch status {
        case "done": return .green
        case "review": return .purple
        case "in-progress": return .blue
        case "ready-for-dev": return .teal
        case "backlog": return .secondary
        case "optional": return .orange
        default: return .secondary
        }
    }

    static func symbol(forSkill skill: String) -> String {
        switch skill {
        case "bmad-create-story": return "doc.badge.plus"
        case "bmad-dev-story": return "hammer"
        case "bmad-code-review": return "checkmark.shield"
        case "bmad-retrospective": return "person.3.sequence"
        default: return "circle"
        }
    }

    static func color(for state: PhaseState) -> Color {
        switch state {
        case .pending: return .secondary
        case .running: return .blue
        case .done: return .green
        }
    }

    // papéis na conversa
    static func roleColor(_ role: String) -> Color { role == "advisor" ? .purple : .blue }
    static func roleIcon(_ role: String) -> String {
        role == "advisor" ? "brain.head.profile" : "hammer.fill"
    }
    static func roleLabel(_ role: String) -> String { role == "advisor" ? "Advisor" : "Worker" }
}

struct StatusBadge: View {
    let status: String
    var body: some View {
        Text(status)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(Theme.color(for: status).opacity(0.18), in: Capsule())
            .foregroundStyle(Theme.color(for: status))
    }
}
