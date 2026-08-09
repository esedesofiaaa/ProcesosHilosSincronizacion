// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "ChatClientSwift",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(
            name: "ChatClientSwift",
            targets: ["ChatClientSwift"]
        )
    ],
    targets: [
        .executableTarget(
            name: "ChatClientSwift"
        )
    ]
)
