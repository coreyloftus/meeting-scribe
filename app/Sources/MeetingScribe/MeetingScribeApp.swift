// Menu-bar-first app (LSUIElement in Info.plist — no Dock icon).
// The menu bar item is the always-on surface; the window opens on demand.
import SwiftUI

@main
struct MeetingScribeApp: App {
    @StateObject private var state = AppState()

    var body: some Scene {
        MenuBarExtra {
            MenuBarContent()
                .environmentObject(state)
        } label: {
            MenuBarLabel()
                .environmentObject(state)
        }

        Window("Meeting Scribe", id: "main") {
            MainWindow()
                .environmentObject(state)
                .onAppear { state.bootstrap() }
                .frame(minWidth: 780, minHeight: 480)
        }
        .defaultSize(width: 980, height: 640)

        Settings {
            SettingsView()
                .environmentObject(state)
        }
    }
}

struct MenuBarLabel: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        // Rendered by AppKit in the status bar; keep it tiny.
        if state.isRecording {
            HStack(spacing: 3) {
                Image(systemName: "record.circle.fill")
                Text(state.elapsedString).monospacedDigit()
            }
        } else if state.isProcessing {
            Image(systemName: "waveform.circle")
        } else {
            Image(systemName: "mic")
        }
    }
}
