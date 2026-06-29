// swift-tools-version: 5.9
import PackageDescription

// Build/dev via SwiftPM:  swift build / swift run
// (o .app "de verdade" é gerado pelo project.yml via XcodeGen — ver README)
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
