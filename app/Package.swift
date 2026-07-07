// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "MeetingScribe",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "MeetingScribe",
            path: "Sources/MeetingScribe"
        )
    ]
)
