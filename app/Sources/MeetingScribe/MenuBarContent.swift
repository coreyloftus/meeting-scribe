import SwiftUI

struct MenuBarContent: View {
    @EnvironmentObject var state: AppState
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Group {
            if state.isRecording {
                Text("● Recording \(state.elapsedString)")
            } else if let job = state.status?.activeJob {
                Text("Processing \(job.meetingId)… (\(job.phase ?? job.type))")
            } else if state.daemonUp {
                Text("Idle")
            } else {
                Text("Daemon not running")
            }

            Divider()

            if state.stopping {
                Button("Stopping…") {}.disabled(true)
            } else if state.isRecording {
                Button("Stop & Process") { state.stopRecording() }
                    .keyboardShortcut("s")
                Button("Open Notes Pane") { openMain() }
            } else if state.daemonUp {
                Button("Start Recording") { state.startRecording() }
                    .keyboardShortcut("r")
            } else {
                Button("Start Background Service") { state.startDaemon() }
            }

            Divider()

            Menu("Recent Meetings") {
                let recent = Array(state.meetings.prefix(8))
                if recent.isEmpty {
                    Text("No meetings yet")
                }
                ForEach(recent) { m in
                    Button("\(statusIcon(m)) \(m.displayTitle)") {
                        state.selectedMeetingID = m.id
                        openMain()
                    }
                }
            }

            Button("Open Meeting Scribe") { openMain() }
                .keyboardShortcut("o")
            SettingsLink { Text("Settings…") }

            Divider()

            Text(state.daemonUp
                 ? "Daemon: running (v\(state.status?.daemonVersion ?? "?"))"
                 : "Daemon: stopped")
            if !state.daemonUp {
                Button("Start Daemon") { state.startDaemon() }
            }

            Divider()
            Button("Quit Meeting Scribe") { NSApp.terminate(nil) }
                .keyboardShortcut("q")
        }
        .onAppear { state.bootstrap() }
    }

    private func openMain() {
        openWindow(id: "main")
        NSApp.activate(ignoringOtherApps: true)
    }

    private func statusIcon(_ m: Meeting) -> String {
        switch m.status {
        case "done": return "✓"
        case "failed": return "✗"
        case "recording": return "●"
        default: return m.isProcessing ? "…" : "·"
        }
    }
}
