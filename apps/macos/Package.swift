// swift-tools-version: 5.9
import PackageDescription

// Build e execução via SwiftPM:  swift build  (roda ./.build/debug/AutopilotApp)
// O backend é lançado a partir do repo via .venv (ver Backend.swift / README).
let package = Package(
    name: "AutopilotApp",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "AutopilotApp",
            path: "Sources"
        )
    ]
)
