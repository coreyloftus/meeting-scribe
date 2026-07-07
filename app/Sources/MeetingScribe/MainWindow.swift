// Main window: meetings list sidebar + detail pane, Granola-style.
// While recording, the detail pane becomes a live notes editor.
import SwiftUI

struct MainWindow: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        NavigationSplitView {
            MeetingListView()
                .navigationSplitViewColumnWidth(min: 240, ideal: 300)
        } detail: {
            if state.isRecording && (state.selectedMeetingID == nil
                                     || state.selectedMeetingID == state.recordingMeetingID) {
                LiveRecordingView()
            } else if state.detail != nil {
                MeetingDetailView()
            } else {
                ContentUnavailableView("Select a meeting",
                                       systemImage: "text.bubble",
                                       description: Text("Pick a meeting from the list, or start recording."))
            }
        }
        .toolbar { TopBar() }
        .alert("Error", isPresented: .init(
            get: { state.lastError != nil },
            set: { if !$0 { state.lastError = nil } })
        ) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(state.lastError ?? "")
        }
        .onChange(of: state.selectedMeetingID) {
            Task { await state.refreshDetail() }
        }
        .task { await state.refreshMeetings() }
    }
}

struct TopBar: ToolbarContent {
    @EnvironmentObject var state: AppState

    var body: some ToolbarContent {
        ToolbarItem(placement: .primaryAction) {
            if state.stopping {
                Button {} label: {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("Stopping…")
                    }
                }
                .disabled(true)
                .help("Finalizing the audio files")
            } else if state.isRecording {
                Button {
                    state.stopRecording()
                } label: {
                    Label("Stop \(state.elapsedString)", systemImage: "stop.circle.fill")
                        .foregroundStyle(.red)
                        .monospacedDigit()
                }
                .help("Stop recording and process in the background")
            } else {
                Button {
                    state.selectedMeetingID = nil
                    state.startRecording()
                } label: {
                    Label("Record", systemImage: "record.circle")
                }
                .disabled(!state.daemonUp)
                .help(state.daemonUp ? "Start recording system audio + mic"
                                     : "Daemon not running")
            }
        }
        ToolbarItem(placement: .automatic) {
            SettingsLink {
                Image(systemName: "gearshape")
            }
            .help("Settings")
        }
    }
}

// MARK: - Sidebar

struct MeetingListView: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        List(selection: $state.selectedMeetingID) {
            if !state.daemonUp {
                VStack(alignment: .leading, spacing: 6) {
                    Label("Background service not running", systemImage: "exclamationmark.triangle")
                        .font(.callout)
                    Button("Start Service") { state.startDaemon() }
                }
                .padding(.vertical, 4)
            }
            ForEach(state.meetings) { m in
                MeetingRow(meeting: m).tag(m.id)
            }
        }
        .searchable(text: $state.searchQuery, placement: .sidebar, prompt: "Search meetings")
        .onChange(of: state.searchQuery) {
            Task { await state.refreshMeetings() }
        }
    }
}

struct MeetingRow: View {
    let meeting: Meeting

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                StatusChip(status: meeting.status)
                Text(meeting.displayTitle)
                    .lineLimit(1)
                    .font(.body)
            }
            HStack(spacing: 6) {
                Text(meeting.id.prefix(10))
                if let d = meeting.durationSec {
                    Text("· \(d / 60)m")
                }
                ForEach(uniqueTargets(), id: \.0) { target, ok in
                    Text("\(target)\(ok ? " ✓" : " ✗")")
                        .padding(.horizontal, 4)
                        .background(ok ? Color.green.opacity(0.15) : Color.red.opacity(0.15))
                        .clipShape(Capsule())
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
    }

    private func uniqueTargets() -> [(String, Bool)] {
        var seen: [String: Bool] = [:]
        for o in (meeting.outputs ?? []).reversed() {   // latest attempt wins
            seen[o.target] = (o.status == "ok")
        }
        return seen.sorted { $0.key < $1.key }.map { ($0.key, $0.value) }
    }
}

struct StatusChip: View {
    let status: String

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 8, height: 8)
            .help(status)
    }

    private var color: Color {
        switch status {
        case "recording": return .red
        case "done": return .green
        case "failed": return .orange
        case "recorded": return .gray
        default: return .blue // processing phases
        }
    }
}

// MARK: - Live recording / notes pane

struct LiveRecordingView: View {
    @EnvironmentObject var state: AppState
    @State private var notes: String = ""
    @State private var saveTask: Task<Void, Never>?
    @State private var loadedFor: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Image(systemName: "record.circle.fill").foregroundStyle(.red)
                Text("Recording — \(state.elapsedString)")
                    .font(.title2).monospacedDigit()
                Spacer()
            }
            Text("Type rough notes while you talk — they'll be woven into the AI summary when you stop.")
                .font(.callout)
                .foregroundStyle(.secondary)
            TextEditor(text: $notes)
                .font(.body)
                .scrollContentBackground(.hidden)
                .padding(8)
                .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
                .onChange(of: notes) { _, newValue in
                    debounceSave(newValue)
                }
        }
        .padding()
        .task(id: state.recordingMeetingID) { await loadExisting() }
    }

    private func loadExisting() async {
        guard let mid = state.recordingMeetingID, loadedFor != mid else { return }
        loadedFor = mid
        if let m = try? await state.client?.meeting(mid), let existing = m.userNotes {
            notes = existing
        } else {
            notes = ""
        }
    }

    private func debounceSave(_ text: String) {
        saveTask?.cancel()
        saveTask = Task {
            try? await Task.sleep(for: .milliseconds(800))
            guard !Task.isCancelled else { return }
            try? await state.client?.saveSessionNotes(text)
        }
    }
}

// MARK: - Detail

struct MeetingDetailView: View {
    @EnvironmentObject var state: AppState
    @State private var transcriptExpanded = false
    @State private var notesDraft: String = ""
    @State private var notesEditing = false
    @State private var confirmDelete = false

    var body: some View {
        if let m = state.detail {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    header(m)
                    OutputsBar(meeting: m)
                    if m.status == "failed", let err = m.error {
                        errorBox(m, err)
                    }
                    if let summary = m.summaryMd {
                        MarkdownBlock(text: summary)
                    } else if m.isProcessing {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small)
                            Text("Processing — \(m.status)…").foregroundStyle(.secondary)
                        }
                    } else if m.status == "recorded" {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Not processed yet.").foregroundStyle(.secondary)
                            Button("Transcribe & Summarize") { state.reprocess(m.id) }
                        }
                    }
                    notesSection(m)
                    transcriptSection(m)
                }
                .padding()
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .confirmationDialog("Delete this meeting?", isPresented: $confirmDelete) {
                Button("Remove from list only") { state.deleteMeeting(m.id, deleteFiles: false) }
                Button("Delete audio & files too", role: .destructive) {
                    state.deleteMeeting(m.id, deleteFiles: true)
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Remote pages (Notion/Google) are never touched.")
            }
        }
    }

    @ViewBuilder
    private func header(_ m: Meeting) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(m.displayTitle).font(.title.bold())
            HStack(spacing: 10) {
                StatusChip(status: m.status)
                Text(m.startedAt?.replacingOccurrences(of: "T", with: "  ") ?? m.id)
                if let d = m.durationSec {
                    Text("· \(d / 60)m \(d % 60)s")
                }
                Spacer()
                Menu {
                    Button("Reprocess (re-transcribe & summarize)") { state.reprocess(m.id) }
                    Button("Reveal in Finder") { state.revealInFinder(m) }
                    Divider()
                    Button("Delete…", role: .destructive) { confirmDelete = true }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
                .menuStyle(.borderlessButton)
                .frame(width: 40)
            }
            .font(.callout)
            .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func errorBox(_ m: Meeting, _ err: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("Processing failed", systemImage: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
            Text(err.components(separatedBy: "\n").first ?? err)
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
            Button("Retry") { state.reprocess(m.id) }
        }
        .padding(10)
        .background(.orange.opacity(0.1), in: RoundedRectangle(cornerRadius: 8))
    }

    @ViewBuilder
    private func notesSection(_ m: Meeting) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("My Notes").font(.headline)
                Spacer()
                if notesEditing {
                    Button("Save") {
                        state.saveNotes(meetingID: m.id, text: notesDraft)
                        notesEditing = false
                    }
                    Button("Cancel") { notesEditing = false }
                } else {
                    Button(m.userNotes == nil ? "Add Notes" : "Edit") {
                        notesDraft = m.userNotes ?? ""
                        notesEditing = true
                    }
                }
            }
            if notesEditing {
                TextEditor(text: $notesDraft)
                    .font(.body)
                    .frame(minHeight: 100)
                    .padding(6)
                    .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
                Text("Tip: Reprocess after editing notes to fold them into the summary.")
                    .font(.caption).foregroundStyle(.secondary)
            } else if let notes = m.userNotes, !notes.isEmpty {
                MarkdownBlock(text: notes)
            }
        }
    }

    @ViewBuilder
    private func transcriptSection(_ m: Meeting) -> some View {
        if let t = m.transcript, !t.isEmpty {
            DisclosureGroup("Full Transcript", isExpanded: $transcriptExpanded) {
                Text(t)
                    .font(.callout)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, 6)
            }
            .font(.headline)
        }
    }
}

// MARK: - Outputs bar

struct OutputsBar: View {
    @EnvironmentObject var state: AppState
    let meeting: Meeting

    var body: some View {
        HStack(spacing: 8) {
            ForEach(state.integrations?.outputs ?? []) { integ in
                outputButton(integ)
            }
            Spacer()
        }
    }

    @ViewBuilder
    private func outputButton(_ integ: IntegrationInfo) -> some View {
        let latest = (meeting.outputs ?? []).first { $0.target == integ.key }
        HStack(spacing: 4) {
            Button {
                state.push(meeting.id, target: integ.key)
            } label: {
                HStack(spacing: 4) {
                    if let latest {
                        Image(systemName: latest.status == "ok"
                              ? "checkmark.circle.fill" : "xmark.circle.fill")
                            .foregroundStyle(latest.status == "ok" ? .green : .red)
                    }
                    Text(pushLabel(integ, pushed: latest != nil))
                }
            }
            .disabled(!integ.configured || meeting.summaryMd == nil)
            .help(integ.configured ? "Send this note to \(integ.label)"
                                   : "\(integ.label) is not configured (see Settings)")

            if let url = latest?.url, latest?.status == "ok", url.hasPrefix("http") {
                Link(destination: URL(string: url)!) {
                    Image(systemName: "arrow.up.right.square")
                }
                .help("Open in \(integ.label)")
            }
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
    }

    private func pushLabel(_ integ: IntegrationInfo, pushed: Bool) -> String {
        switch integ.key {
        case "markdown": return pushed ? "Markdown" : "Write Markdown"
        case "notion": return pushed ? "Notion" : "Push to Notion"
        case "gdocs": return pushed ? "Google Doc" : "Create Google Doc"
        case "gdrive": return pushed ? "Drive" : "Save to Drive"
        default: return integ.label
        }
    }
}

// MARK: - Minimal markdown rendering (headings/bullets/checkboxes/paragraphs)

struct MarkdownBlock: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(text.components(separatedBy: "\n").enumerated()), id: \.offset) { _, raw in
                line(raw)
            }
        }
        .textSelection(.enabled)
    }

    @ViewBuilder
    private func line(_ raw: String) -> some View {
        let s = raw.trimmingCharacters(in: .whitespaces)
        if s.isEmpty {
            Spacer().frame(height: 2)
        } else if s == "---" {
            Divider()
        } else if s.hasPrefix("### ") {
            Text(inline(String(s.dropFirst(4)))).font(.headline)
        } else if s.hasPrefix("## ") {
            Text(inline(String(s.dropFirst(3)))).font(.title3.bold()).padding(.top, 6)
        } else if s.hasPrefix("# ") {
            Text(inline(String(s.dropFirst(2)))).font(.title2.bold()).padding(.top, 6)
        } else if s.hasPrefix("- [ ] ") || s.hasPrefix("- [x] ") || s.hasPrefix("- [X] ") {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: s.hasPrefix("- [ ]") ? "square" : "checkmark.square")
                    .foregroundStyle(.secondary)
                Text(inline(String(s.dropFirst(6))))
            }
        } else if s.hasPrefix("- ") || s.hasPrefix("* ") {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("•").foregroundStyle(.secondary)
                Text(inline(String(s.dropFirst(2))))
            }
        } else {
            Text(inline(s))
        }
    }

    private func inline(_ s: String) -> AttributedString {
        (try? AttributedString(markdown: s)) ?? AttributedString(s)
    }
}
