// Dock app with a menu-bar badge. The Dock icon opens the main window; the
// menu-bar item shows recording status and quick actions.
import SwiftUI

@main
struct MeetingScribeApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var state = AppState()

    var body: some Scene {
        MenuBarExtra {
            MenuBarContent()
                .environmentObject(state)
        } label: {
            MenuBarLabel()
                .environmentObject(state)
                .onAppear { appDelegate.state = state }
        }

        Window("Meeting Scribe", id: "main") {
            MainWindow()
                .environmentObject(state)
                .onAppear { state.bootstrap() }
                .frame(minWidth: 780, minHeight: 480)
        }
        .defaultSize(width: 980, height: 640)
        .commands { RecordingCommands(state: state) }

        Settings {
            SettingsView()
                .environmentObject(state)
        }
    }
}

/// ⌘R starts or stops a recording whenever the app is frontmost, like the toolbar button.
struct RecordingCommands: Commands {
    @ObservedObject var state: AppState

    var body: some Commands {
        CommandMenu("Recording") {
            if state.isRecording {
                Button("Stop Recording") { state.stopRecording() }
                    .keyboardShortcut("r")
                    .disabled(state.stopping)
            } else {
                Button("Start Recording") {
                    state.selectedMeetingID = nil
                    state.startRecording()
                }
                .keyboardShortcut("r")
                .disabled(!state.daemonUp)
            }
        }
    }
}

struct MenuBarLabel: View {
    @EnvironmentObject var state: AppState
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        // Rendered by AppKit in the status bar; keep it tiny.
        Group {
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
        // The label lives for the whole app, so it owns the shared open-window action.
        .onAppear {
            state.openMainWindow = {
                openWindow(id: "main")
                NSApp.activate(ignoringOtherApps: true)
            }
        }
    }
}
