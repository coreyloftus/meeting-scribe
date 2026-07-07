// Settings: integrations (enable/connect outputs) + health (doctor).
// Config edits go through PUT /v1/config so the daemon stays the source of truth.
import SwiftUI

struct SettingsView: View {
    var body: some View {
        TabView {
            IntegrationsSettings()
                .tabItem { Label("Integrations", systemImage: "square.and.arrow.up") }
            HealthSettings()
                .tabItem { Label("Health", systemImage: "stethoscope") }
        }
        .frame(width: 520, height: 420)
    }
}

struct IntegrationsSettings: View {
    @EnvironmentObject var state: AppState
    @State private var notionToken = ""
    @State private var notionDB = ""
    @State private var googleClientID = ""
    @State private var googleClientSecret = ""
    @State private var gdriveFolder = ""
    @State private var gdocsFolder = ""
    @State private var connecting = false
    @State private var message: String?

    var body: some View {
        Form {
            Section("Auto-run on stop") {
                ForEach(state.integrations?.outputs ?? []) { integ in
                    Toggle(isOn: binding(for: integ)) {
                        HStack {
                            Text(integ.label)
                            if !integ.configured {
                                Text("not configured")
                                    .font(.caption)
                                    .foregroundStyle(.orange)
                            }
                        }
                    }
                }
            }

            Section("Notion") {
                SecureField("Integration token", text: $notionToken)
                TextField("Database ID", text: $notionDB)
                Button("Save Notion Settings") {
                    save(["outputs": ["notion": ["token": notionToken,
                                                 "database_id": notionDB]]])
                }
            }

            Section("Google (Docs & Drive)") {
                if state.integrations?.google.connected == true {
                    Label("Connected", systemImage: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                } else {
                    TextField("OAuth client ID (Desktop app)", text: $googleClientID)
                    SecureField("OAuth client secret", text: $googleClientSecret)
                    Button(connecting ? "Waiting for browser consent…" : "Connect Google") {
                        connectGoogle()
                    }
                    .disabled(connecting)
                }
                TextField("Drive folder ID (optional)", text: $gdriveFolder)
                TextField("Docs folder ID (optional)", text: $gdocsFolder)
                Button("Save Google Folders") {
                    save(["outputs": ["gdrive": ["folder_id": gdriveFolder],
                                      "gdocs": ["folder_id": gdocsFolder]]])
                }
            }

            if let message {
                Text(message).font(.callout).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .task { await state.refreshIntegrations() }
    }

    private func binding(for integ: IntegrationInfo) -> Binding<Bool> {
        Binding(
            get: {
                state.integrations?.outputs.first { $0.key == integ.key }?.enabled ?? false
            },
            set: { on in
                save(["outputs": [integ.key: ["enabled": on]]])
            })
    }

    private func save(_ patch: [String: Any]) {
        Task {
            do {
                try await state.client?.putConfig(patch)
                await state.refreshIntegrations()
                message = "Saved."
            } catch {
                message = "Save failed: \(error.localizedDescription)"
            }
        }
    }

    private func connectGoogle() {
        connecting = true
        Task {
            do {
                if !googleClientID.isEmpty {
                    try await state.client?.putConfig(
                        ["google": ["client_id": googleClientID,
                                    "client_secret": googleClientSecret]])
                }
                try await state.client?.connectGoogle()
                message = "Google connected."
            } catch {
                message = "Google connect failed: \(error.localizedDescription)"
            }
            connecting = false
            await state.refreshIntegrations()
        }
    }
}

struct HealthSettings: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        List {
            HStack {
                Image(systemName: state.daemonUp ? "checkmark.circle.fill" : "xmark.circle.fill")
                    .foregroundStyle(state.daemonUp ? .green : .red)
                Text(state.daemonUp
                     ? "Daemon running (v\(state.status?.daemonVersion ?? "?"))"
                     : "Daemon not reachable")
                Spacer()
                if !state.daemonUp {
                    Button("Start") { state.startDaemon() }
                }
            }
            ForEach(state.doctorChecks) { c in
                HStack(alignment: .firstTextBaseline) {
                    Image(systemName: c.ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                        .foregroundStyle(c.ok ? .green : .orange)
                    VStack(alignment: .leading) {
                        Text(c.key)
                        Text(c.detail).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .task { await state.refreshDoctor() }
    }
}
