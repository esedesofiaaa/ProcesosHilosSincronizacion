import SwiftUI

@main
struct ChatClientSwiftApp: App {
    @StateObject private var client = ChatConnection()

    var body: some Scene {
        WindowGroup("Cliente TCP Chat") {
            ContentView(client: client)
        }
        .defaultSize(width: 1_180, height: 760)
        .windowResizability(.contentMinSize)
    }
}
