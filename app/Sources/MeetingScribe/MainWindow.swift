// Main window: meetings list sidebar + detail pane, Granola-style.
// While recording, the detail pane becomes a live notes editor plus a trailing transcript.
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
    @AppStorage("liveShowNotes") private var showNotes = true
    @AppStorage("liveShowTranscript") private var showTranscript = true

    var body: some ToolbarContent {
        // Shown in every state so the recording layout can be set before a call starts.
        ToolbarItemGroup(placement: .automatic) {
            // Each toggle is disabled while it is the only visible panel, so one always stays.
            Toggle(isOn: $showNotes) { Label("Notes", systemImage: "note.text") }
                .disabled(showNotes && !showTranscript)
                .help(showNotes ? "Hide notes while recording" : "Show notes while recording")
            Toggle(isOn: $showTranscript) { Label("Transcript", systemImage: "text.bubble") }
                .disabled(showTranscript && !showNotes)
                .help(showTranscript ? "Hide live transcript while recording"
                                     : "Show live transcript while recording")
        }
        ToolbarItem(placement: .automatic) {
            SettingsLink {
                Image(systemName: "gearshape")
            }
            .help("Settings")
        }
        // Last, so it sits far right; its own slot on macOS 26 keeps the shared glass from clipping it.
        if #available(macOS 26.0, *) {
            ToolbarSpacer(.fixed)
            ToolbarItem(placement: .automatic) { recordButton }
                .sharedBackgroundVisibility(.hidden)
        } else {
            ToolbarItem(placement: .automatic) { recordButton }
        }
    }

    @ViewBuilder
    private var recordButton: some View {
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
                // Not a Label: toolbars draw labels as icon only, which hid the timer.
                HStack(spacing: 6) {
                    Image(systemName: "stop.fill")
                    Text(state.elapsedString).monospacedDigit()
                }
            }
            .buttonStyle(RecordButtonStyle())
            .help("Stop recording and process in the background (⌘R)")
        } else {
            Button {
                state.selectedMeetingID = nil
                state.startRecording()
            } label: {
                Image(systemName: "record.circle")
            }
            .buttonStyle(RecordButtonStyle())
            .accessibilityLabel("Record")
            .disabled(!state.daemonUp)
            .help(state.daemonUp ? "Start recording system audio + mic (⌘R)"
                                 : "Daemon not running")
        }
    }
}

/// Red capsule that brightens and grows slightly on hover, and darkens while pressed.
struct RecordButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    @State private var hovering = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.body.weight(.semibold))
            .foregroundStyle(.white)
            .padding(.horizontal, 11)
            .padding(.vertical, 4)
            .background(Capsule().fill(fill(pressed: configuration.isPressed)))
            .scaleEffect(hovering && isEnabled && !configuration.isPressed ? 1.06 : 1)
            .opacity(isEnabled ? 1 : 0.4)
            .animation(.easeOut(duration: 0.12), value: hovering)
            .animation(.easeOut(duration: 0.08), value: configuration.isPressed)
            .onHover { hovering = $0 }
    }

    private func fill(pressed: Bool) -> Color {
        if pressed { return Color(red: 0.70, green: 0.10, blue: 0.10) }
        return hovering && isEnabled ? Color(red: 1.0, green: 0.30, blue: 0.27) : .red
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
            ForEach(sections, id: \.title) { section in
                Section(section.title) {
                    ForEach(section.meetings) { m in
                        MeetingRow(meeting: m).tag(m.id)
                    }
                }
            }
        }
        .searchable(text: $state.searchQuery, placement: .sidebar, prompt: "Search meetings")
        .onChange(of: state.searchQuery) {
            Task { await state.refreshMeetings() }
        }
    }

    /// Meetings grouped by day: Today, Yesterday, This Week, then by month.
    private var sections: [(title: String, meetings: [Meeting])] {
        let cal = Calendar.current
        var out: [(title: String, meetings: [Meeting])] = []
        for m in state.meetings {
            var title = "Undated"
            if let d = meetingDate(m) {
                let days = cal.dateComponents([.day], from: cal.startOfDay(for: d),
                                              to: cal.startOfDay(for: .now)).day ?? 0
                title = days == 0 ? "Today" : days == 1 ? "Yesterday" : days < 7 ? "This Week"
                    : d.formatted(.dateTime.month(.wide).year())
            }
            if out.last?.title == title { out[out.count - 1].meetings.append(m) }
            else { out.append((title, [m])) }
        }
        return out
    }
}

struct MeetingRow: View {
    let meeting: Meeting

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Text(rowTitle(meeting))
                    .lineLimit(1)
                    .font(.body.weight(.medium))
                Spacer(minLength: 4)
                StatusGlyph(status: meeting.status)
            }
            HStack(spacing: 8) {
                if let d = meetingDate(meeting) { Text(d.formatted(date: .omitted, time: .shortened)) }
                if let sec = meeting.durationSec { Text(durationText(sec)) }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .padding(.vertical, 4)
    }
}

/// The section header already shows the day, so drop a leading "2026-10-23 " from titles.
func rowTitle(_ m: Meeting) -> String {
    guard var t = m.title, !t.isEmpty else { return m.status == "recording" ? "Recording now" : "Untitled meeting" }
    if t.count > 11, t.prefix(10).allSatisfy({ $0.isNumber || $0 == "-" }), t.dropFirst(10).first == " " {
        t = String(t.dropFirst(11))
    }
    return t.prefix(1).uppercased() + t.dropFirst()
}

/// Only states that need attention get a glyph; finished meetings stay quiet.
struct StatusGlyph: View {
    let status: String

    var body: some View {
        switch status {
        case "recording":
            RecordingDot(size: 8).help("Recording")
        case "failed":
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                .font(.caption).help("Processing failed")
        case "recorded":
            Image(systemName: "circle.dashed").foregroundStyle(.secondary)
                .font(.caption).help("Not processed yet")
        case "done":
            EmptyView()
        default:
            ProgressView().controlSize(.mini).help(status.capitalized)
        }
    }
}

// MARK: - Live recording / notes pane

struct LiveRecordingView: View {
    @EnvironmentObject var state: AppState
    @State private var notes: String = ""
    @State private var saveTask: Task<Void, Never>?
    @State private var loadedFor: String?
    @AppStorage("liveShowNotes") private var showNotes = true
    @AppStorage("liveShowTranscript") private var showTranscript = true

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .center, spacing: 14) {
                RecordingDot(size: 14)
                Text(state.elapsedString)
                    .font(.system(size: 44, weight: .semibold, design: .rounded))
                    .monospacedDigit()
                    .contentTransition(.numericText())
                Text("Recording")
                    .font(.title3.weight(.medium))
                    .foregroundStyle(.secondary)
                Spacer()
            }
            VSplitView {
                if showNotes {
                    notesSheet
                        .frame(minHeight: 100)
                        .padding(.bottom, showTranscript ? 8 : 0)
                }
                if showTranscript {
                    LiveTranscriptPane()
                        .frame(minHeight: 180, idealHeight: 420)
                        .padding(.top, showNotes ? 8 : 0)
                }
            }
        }
        .padding(24)
        // The red wash marks the window as live; it is the only color the window gets.
        .background(alignment: .top) {
            LinearGradient(colors: [.red.opacity(0.16), .clear], startPoint: .top, endPoint: .center)
                .ignoresSafeArea()
        }
        .task(id: state.recordingMeetingID) { await loadExisting() }
    }

    private var notesSheet: some View {
        ZStack(alignment: .topLeading) {
            TextEditor(text: $notes)
                .font(.body)
                .scrollContentBackground(.hidden)
                .padding(14)
                .onChange(of: notes) { _, newValue in
                    debounceSave(newValue)
                }
            if notes.isEmpty {
                Text("Notes for the summary. What you type here is added to it when you stop.")
                    .foregroundStyle(.tertiary)
                    .padding(.horizontal, 19)
                    .padding(.vertical, 14)
                    .allowsHitTesting(false)
            }
        }
        .glassSheet()
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
        state.pendingSessionNotes = text
        saveTask = Task {
            try? await Task.sleep(for: .milliseconds(800))
            guard !Task.isCancelled else { return }
            await state.flushSessionNotes()
        }
    }
}

struct LiveTranscriptPane: View {
    @EnvironmentObject var state: AppState
    /// True while the newest line is on screen; scrolling up to read back turns it off.
    @State private var following = true

    private var live: LiveTranscript? { state.liveTranscript }
    private var lines: [LiveLine] { live?.lines ?? [] }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text("Live transcript").font(.headline)
                Text("Preview. The full transcript is made after you stop.")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
            }
            .padding(.horizontal, 18)
            .padding(.top, 14)
            .padding(.bottom, 6)
            ScrollViewReader { proxy in
                ZStack(alignment: .bottom) {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 10) {
                            if lines.isEmpty {
                                Text(emptyText).foregroundStyle(.secondary)
                            }
                            ForEach(Array(lines.enumerated()), id: \.element.id) { i, line in
                                TranscriptBubble(speaker: line.speaker, text: line.text,
                                                 timestamp: line.timestamp,
                                                 showsHeader: i == 0 || lines[i - 1].speaker != line.speaker)
                            }
                            Color.clear.frame(height: 1).id("bottom")
                                .onAppear { following = true }
                                .onDisappear { following = false }
                        }
                        .padding(.horizontal, 18)
                        .padding(.vertical, 10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if !following {
                        Button {
                            withAnimation { proxy.scrollTo("bottom", anchor: .bottom) }
                        } label: {
                            Label("Latest", systemImage: "arrow.down")
                                .padding(.horizontal, 6)
                        }
                        .buttonStyle(.borderless)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .glassSheet(cornerRadius: 16)
                        .padding(12)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                    }
                }
                .animation(.easeOut(duration: 0.2), value: following)
                .onChange(of: lines) {
                    if following { proxy.scrollTo("bottom", anchor: .bottom) }
                }
                .onAppear { proxy.scrollTo("bottom", anchor: .bottom) }
            }
        }
        .glassSheet()
    }

    private var emptyText: String {
        switch live?.state {
        case "loading": return "Loading the speech model…"
        case "running": return "Listening. Lines show up about 10 seconds after someone speaks."
        case "failed": return "Live transcript stopped: \(live?.error ?? "unknown error"). The recording is not affected."
        case "off": return "Live transcript needs the Parakeet engine (Settings)."
        default: return "Connecting…"
        }
    }
}

// MARK: - Detail

struct MeetingDetailView: View {
    @EnvironmentObject var state: AppState
    @State private var transcriptExpanded = false
    @State private var notesDraft: String = ""
    @State private var notesEditing = false
    @State private var titleDraft: String = ""
    @State private var titleEditing = false
    @State private var summaryDraft: String = ""
    @State private var summaryEditing = false
    @State private var confirmDelete = false
    @State private var confirmReprocess = false
    @State private var reprocessReusesTranscript = false

    var body: some View {
        if let m = state.detail {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    header(m)
                    OutputsBar(meeting: m)
                    if m.status == "failed", let err = m.error {
                        errorBox(m, err)
                    }
                    if summaryEditing {
                        summaryEditor(m)
                    } else if let summary = m.summaryMd {
                        MarkdownBlock(text: summary)
                            .padding(22)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .glassSheet()
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
                .padding(28)
                .frame(maxWidth: 860, alignment: .leading)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onChange(of: m.id) {
                titleEditing = false
                summaryEditing = false
                notesEditing = false
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
            .confirmationDialog("Replace your edited summary?", isPresented: $confirmReprocess) {
                Button("Replace Summary", role: .destructive) {
                    state.reprocess(m.id, reuseTranscript: reprocessReusesTranscript)
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("This writes a new summary over the one you edited.")
            }
        }
    }

    @ViewBuilder
    private func header(_ m: Meeting) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            if titleEditing {
                HStack {
                    TextField("Title", text: $titleDraft)
                        .font(.title.bold())
                        .textFieldStyle(.plain)
                        .onSubmit { saveTitle(m) }
                        .onExitCommand { titleEditing = false }
                    Button("Save") { saveTitle(m) }
                    Button("Cancel") { titleEditing = false }
                }
            } else {
                Text(rowTitle(m))
                    .font(.system(size: 30, weight: .bold, design: .rounded))
                    .textSelection(.enabled)
            }
            HStack(spacing: 8) {
                if let d = meetingDate(m) {
                    Text(d.formatted(.dateTime.weekday(.wide).month(.wide).day().hour().minute())).chip()
                }
                if let sec = m.durationSec { Text(durationText(sec)).chip() }
                if m.status != "done" { Text(statusText(m.status)).chip() }
                Spacer()
                Menu {
                    Button("Rename…") {
                        titleDraft = m.displayTitle
                        titleEditing = true
                    }
                    Button("Edit Summary") {
                        summaryDraft = m.summaryMd ?? ""
                        summaryEditing = true
                    }
                    .disabled(m.summaryMd == nil)
                    Divider()
                    Button("Re-summarize") { reprocess(m, reuseTranscript: true) }
                        .disabled(m.isProcessing)
                    Button("Re-transcribe & Summarize") { reprocess(m, reuseTranscript: false) }
                        .disabled(m.isProcessing)
                    Button("Reveal in Finder") { state.revealInFinder(m) }
                    Button("Copy Transcript Path") { state.copyTranscriptPath(m) }
                        .disabled(m.basePath == nil)
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
        .padding(.bottom, 4)
    }

    private func statusText(_ status: String) -> String {
        switch status {
        case "recording": return "Recording"
        case "recorded": return "Not processed"
        case "failed": return "Failed"
        case "writing_outputs": return "Saving…"
        default: return status.capitalized + "…"
        }
    }

    private func reprocess(_ m: Meeting, reuseTranscript: Bool) {
        if m.summaryEdited == 1 {
            reprocessReusesTranscript = reuseTranscript
            confirmReprocess = true
        } else {
            state.reprocess(m.id, reuseTranscript: reuseTranscript)
        }
    }

    private func saveTitle(_ m: Meeting) {
        state.renameMeeting(m.id, title: titleDraft)
        titleEditing = false
    }

    @ViewBuilder
    private func summaryEditor(_ m: Meeting) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Edit Summary").font(.headline)
                Spacer()
                Button("Save") {
                    state.saveSummary(m.id, text: summaryDraft)
                    summaryEditing = false
                }
                Button("Cancel") { summaryEditing = false }
            }
            TextEditor(text: $summaryDraft)
                .font(.body.monospaced())
                .frame(minHeight: 400)
                .padding(6)
                .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
            Text("Push to Notion or Markdown again to send the edited summary.")
                .font(.caption).foregroundStyle(.secondary)
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
            Button("Retry") { state.reprocess(m.id, reuseTranscript: true) }
        }
        .padding(10)
        .background(.orange.opacity(0.1), in: RoundedRectangle(cornerRadius: 8))
    }

    @ViewBuilder
    private func notesSection(_ m: Meeting) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("My notes").font(.headline)
                Spacer()
                if notesEditing {
                    Button("Save") {
                        state.saveNotes(meetingID: m.id, text: notesDraft)
                        notesEditing = false
                    }
                    Button("Cancel") { notesEditing = false }
                } else {
                    Button(m.userNotes == nil ? "Add notes" : "Edit") {
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
                Text("Tip: Re-summarize after editing notes to fold them into the summary.")
                    .font(.caption).foregroundStyle(.secondary)
            } else if let notes = m.userNotes, !notes.isEmpty {
                MarkdownBlock(text: notes)
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassSheet()
    }

    @ViewBuilder
    private func transcriptSection(_ m: Meeting) -> some View {
        if let t = m.transcript, !t.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                Button {
                    withAnimation(.easeOut(duration: 0.2)) { transcriptExpanded.toggle() }
                } label: {
                    HStack {
                        Text("Transcript").font(.headline)
                        Spacer()
                        Image(systemName: "chevron.down")
                            .rotationEffect(.degrees(transcriptExpanded ? 0 : -90))
                            .foregroundStyle(.secondary)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                if transcriptExpanded {
                    let lines = transcriptLines(t)
                    LazyVStack(alignment: .leading, spacing: 10) {
                        ForEach(Array(lines.enumerated()), id: \.offset) { i, line in
                            TranscriptBubble(speaker: line.speaker, text: line.text,
                                             showsHeader: !line.speaker.isEmpty
                                                && (i == 0 || lines[i - 1].speaker != line.speaker))
                        }
                    }
                }
            }
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .leading)
            .glassSheet()
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

            if let url = latest?.url, latest?.status == "ok", url.hasPrefix("http"),
               let destination = URL(string: url) {
                Link(destination: destination) {
                    Image(systemName: "arrow.up.right")
                }
                .help("Open in \(integ.label)")
            }
        }
        .buttonStyle(.bordered)
        .buttonBorderShape(.capsule)
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
        Text(rendered).textSelection(.enabled)
    }

    // One Text so selection spans every line, not just one.
    private var rendered: AttributedString {
        var out = AttributedString()
        for (i, raw) in text.components(separatedBy: "\n").enumerated() {
            if i > 0 { out += AttributedString("\n") }
            out += line(raw)
        }
        return out
    }

    private func line(_ raw: String) -> AttributedString {
        let s = raw.trimmingCharacters(in: .whitespaces)
        if s.isEmpty { return AttributedString("") }
        if s == "---" { return AttributedString("────────") }
        if s.hasPrefix("### ") { return styled(String(s.dropFirst(4)), .headline) }
        if s.hasPrefix("## ") { return styled(String(s.dropFirst(3)), .title3.bold()) }
        if s.hasPrefix("# ") { return styled(String(s.dropFirst(2)), .title2.bold()) }
        if s.hasPrefix("- [ ] ") { return AttributedString("☐ ") + inline(String(s.dropFirst(6))) }
        if s.hasPrefix("- [x] ") || s.hasPrefix("- [X] ") {
            return AttributedString("☑ ") + inline(String(s.dropFirst(6)))
        }
        if s.hasPrefix("- ") || s.hasPrefix("* ") { return AttributedString("• ") + inline(String(s.dropFirst(2))) }
        return inline(s)
    }

    private func styled(_ s: String, _ font: Font) -> AttributedString {
        var a = inline(s)
        a.font = font
        return a
    }

    private func inline(_ s: String) -> AttributedString {
        (try? AttributedString(markdown: s)) ?? AttributedString(s)
    }
}
