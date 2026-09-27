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
    @State private var notionConnecting = false
    @State private var notionDatabases: [NotionDatabase] = []
    @State private var loadingDatabases = false
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
                if state.integrations?.notion.oauthAvailable == true {
                    notionOAuth
                    DisclosureGroup("Advanced: use an internal integration token") {
                        notionManual
                    }
                } else {
                    notionManual
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
        .task(id: "\(state.integrations?.notion.connected == true)-\(state.integrationsChanges)") {
            if state.integrations?.notion.connected == true {
                notionConnecting = false
                await loadNotionDatabases()
            } else {
                notionDatabases = []
            }
        }
    }

    // MARK: Notion

    @ViewBuilder private var notionOAuth: some View {
        if let notion = state.integrations?.notion, notion.connected {
            HStack {
                Label("Connected to \(notion.workspaceName.isEmpty ? "Notion" : notion.workspaceName)",
                      systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                Spacer()
                Button("Disconnect") { disconnectNotion() }
            }
            HStack {
                Picker("Database", selection: notionDatabaseBinding) {
                    Text("Choose a database…").tag("")
                    ForEach(notionDatabases) { db in
                        Text(db.title).tag(db.id)
                    }
                }
                Button {
                    Task { await loadNotionDatabases() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless)
                .disabled(loadingDatabases)
                .help("Reload databases")
            }
            if !loadingDatabases && notionDatabases.isEmpty {
                Text("No databases shared yet. Click Connect Notion again and select a database on the Notion page.")
                    .font(.caption).foregroundStyle(.secondary)
                Button("Connect Notion") { connectNotion() }
            } else if let db = selectedNotionDatabase, db.dateProperty.isEmpty {
                Text("No date column — notes will be filed without a date.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        } else if notionConnecting {
            HStack {
                ProgressView().controlSize(.small)
                Text("Waiting for Notion in your browser…")
                Spacer()
                Button("Cancel") { notionConnecting = false }
            }
        } else {
            Button("Connect Notion") { connectNotion() }
        }
    }

    @ViewBuilder private var notionManual: some View {
        SecureField("Integration token", text: $notionToken)
        TextField("Database ID", text: $notionDB)
        Button("Save Notion Settings") {
            save(["outputs": ["notion": ["token": notionToken,
                                         "database_id": notionDB]]])
        }
    }

    /// Notion IDs come with or without dashes depending on where they were copied from.
    private static func sameNotionID(_ a: String, _ b: String) -> Bool {
        a.replacingOccurrences(of: "-", with: "") == b.replacingOccurrences(of: "-", with: "")
    }

    private var selectedNotionDatabase: NotionDatabase? {
        guard let current = state.integrations?.notion.databaseId, !current.isEmpty else { return nil }
        return notionDatabases.first { Self.sameNotionID($0.id, current) }
    }

    private var notionDatabaseBinding: Binding<String> {
        Binding(
            get: { selectedNotionDatabase?.id ?? "" },
            set: { id in
                guard let db = notionDatabases.first(where: { $0.id == id }) else { return }
                Task {
                    do {
                        try await state.client?.setNotionDatabase(db)
                        await state.refreshIntegrations()
                        message = "Notes will go to \(db.title)."
                    } catch {
                        message = "Save failed: \(error.localizedDescription)"
                    }
                }
            })
    }

    private func loadNotionDatabases() async {
        loadingDatabases = true
        defer { loadingDatabases = false }
        do {
            notionDatabases = try await state.client?.notionDatabases() ?? []
        } catch {
            message = "Could not load Notion databases: \(error.localizedDescription)"
        }
    }

    private func connectNotion() {
        notionConnecting = true
        Task {
            do {
                try await state.client?.connectNotion()
            } catch {
                notionConnecting = false
                message = "Notion connect failed: \(error.localizedDescription)"
            }
        }
    }

    private func disconnectNotion() {
        Task {
            do {
                try await state.client?.disconnectNotion()
                await state.refreshIntegrations()
                message = "Notion disconnected."
            } catch {
                message = "Disconnect failed: \(error.localizedDescription)"
            }
        }
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
