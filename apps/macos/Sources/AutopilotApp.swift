import SwiftUI
import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    // Fechar a janela encerra o app (e, abaixo, para o run).
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
    // Ao encerrar: para o run ativo e mata o backend filho (não vaza tokens).
    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated { RunStore.shared.shutdownForQuit() }
    }
}

@main
struct AutopilotApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var store = RunStore.shared

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(store)
                .frame(minWidth: 1040, minHeight: 640)
        }
        .windowStyle(.titleBar)
        .windowToolbarStyle(.unified)

        MenuBarExtra("Autopilot", systemImage: "airplane.circle") {
            MenuBarPanel()
                .environmentObject(store)
        }
        .menuBarExtraStyle(.window)
    }
}
