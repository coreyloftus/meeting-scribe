// Central observable state: daemon connection, live status, meetings list.
// Subscribes to /v1/events (SSE) and falls back to 2s polling if the stream drops.
import CoreGraphics
import Foundation
import SwiftUI

@MainActor
final class AppState: ObservableObject {
    @Published var client: DaemonClient?
    @Published var daemonUp = false
    @Published var status: DaemonStatus?
    @Published var meetings: [Meeting] = []
    @Published var selectedMeetingID: String?
    @Published var detail: Meeting?                 // full detail for selection
    @Published var integrations: IntegrationsResponse?
    @Published var doctorChecks: [DoctorCheck] = []
    @Published var elapsedSec: Int = 0
    @Published var lastError: String?
    @Published var searchQuery: String = ""
    @Published var stopping = false

    private var eventTask: Task<Void, Never>?
    private var pollTask: Task<Void, Never>?
    private var bootstrapped = false

    init() {
        // MenuBarExtra content views only appear when clicked, so kick the
        // connection off at construction — the badge must be live immediately.
        bootstrap()
    }

    var isRecording: Bool { status?.recording ?? false }
    var isProcessing: Bool { status?.activeJob != nil }
    var recordingMeetingID: String? { status?.session?.meetingId }

    var elapsedString: String {
        let m = elapsedSec / 60, s = elapsedSec % 60
        return m >= 60 ? String(format: "%d:%02d:%02d", m / 60, m % 60, s)
                       : String(format: "%d:%02d", m, s)
    }

    // MARK: bootstrap

    func bootstrap() {
        if bootstrapped { return }
        bootstrapped = true
        connect()
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                guard let self else { return }
                if !self.daemonUp { self.connect() }
                else if self.eventTask == nil { await self.refreshStatus() }
            }
        }
    }

    func connect() {
        guard let c = DaemonClient.discover() else {
            daemonUp = false
            return
        }
        client = c
        Task {
            do {
                status = try await c.status()
                daemonUp = true
                lastError = nil
                await refreshMeetings()
                await refreshIntegrations()
                startEventStream()
            } catch {
                daemonUp = false
            }
        }
    }

    /// Try to get scribed running: kickstart the LaunchAgent if installed,
    /// otherwise spawn `scribed serve` directly (unbundled personal build).
    func startDaemon() {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let script = """
        launchctl kickstart -k gui/$(id -u)/com.meetingscribe.scribed 2>/dev/null || \
        PATH="/opt/homebrew/bin:/usr/local/bin:$PATH" \
        nohup "\(home)/.local/bin/scribed" serve >> "\(home)/.local/state/meeting-scribe/scribed.log" 2>&1 &
        """
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = ["-c", script]
        try? p.run()
        Task {
            try? await Task.sleep(for: .seconds(2))
            connect()
        }
    }

    // MARK: event stream

    private func startEventStream() {
        eventTask?.cancel()
        guard let c = client else { return }
        eventTask = Task { [weak self] in
            do {
                for try await event in c.events() {
                    guard let self else { return }
                    self.handle(event)
                }
            } catch {}
            // Stream ended/failed: drop to polling; bootstrap loop reconnects.
            self?.eventTask = nil
        }
    }

    private func handle(_ e: SSEEvent) {
        switch e.type {
        case "tick":
            elapsedSec = e.data["elapsed_sec"] as? Int ?? elapsedSec
        case "recording_started", "recording_stopped":
            elapsedSec = 0
            Task { await refreshStatus(); await refreshMeetings() }
        case "job_progress":
            Task { await refreshStatus() }
            if let mid = e.data["meeting_id"] as? String, let phase = e.data["phase"] as? String,
               let idx = meetings.firstIndex(where: { $0.id == mid }) {
                meetings[idx].status = phase
            }
        case "meeting_updated", "output_pushed":
            Task {
                await refreshMeetings()
                if let mid = e.data["meeting_id"] as? String, mid == selectedMeetingID {
                    await refreshDetail()
                }
            }
        case "hello":
            Task { await refreshStatus() }
        default:
            break
        }
    }

    // MARK: refreshers

    func refreshStatus() async {
        guard let c = client else { return }
        do {
            status = try await c.status()
            daemonUp = true
            if let s = status?.session?.elapsedSec { elapsedSec = s }
        } catch {
            daemonUp = false
        }
    }

    func refreshMeetings() async {
        guard let c = client else { return }
        if let m = try? await c.meetings(query: searchQuery.isEmpty ? nil : searchQuery) {
            meetings = m
        }
    }

    func refreshDetail() async {
        guard let c = client, let id = selectedMeetingID else { detail = nil; return }
        detail = try? await c.meeting(id)
    }

    func refreshIntegrations() async {
        guard let c = client else { return }
        integrations = try? await c.integrations()
    }

    func refreshDoctor() async {
        guard let c = client else { return }
        doctorChecks = (try? await c.doctor()) ?? []
    }

    // MARK: actions

    /// `hint` is appended only if the operation fails — guidance we suspect is
    /// relevant but can't confirm, so it never fires on a working setup.
    private func run(_ label: String, hint: String? = nil,
                     _ op: @escaping () async throws -> Void) {
        Task {
            do {
                try await op()
                lastError = nil
            } catch {
                var msg = "\(label): \(error.localizedDescription)"
                if let hint { msg += "\n\n" + hint }
                lastError = msg
            }
            await refreshStatus()
            await refreshMeetings()
        }
    }

    static let screenRecordingHint = """
        Meeting Scribe itself does not have Screen Recording permission, which \
        may be the cause. Approve the system prompt, or grant it under System \
        Settings > Privacy & Security > Screen Recording. macOS only applies \
        the answer to processes started afterwards, so quit and relaunch \
        Meeting Scribe and restart the background service. If scribed runs \
        under launchd it holds its own grant — grant the permission to it \
        instead of to this app.
        """

    /// Screen Recording is granted to whichever process TCC holds *responsible*
    /// for `syscap`. That is this app only when the app spawned scribed itself
    /// (`startDaemon()`'s fallback branch); with the LaunchAgent installed the
    /// daemon is a child of launchd and carries its own grant, which we cannot
    /// see from here. So this is a nudge, never a gate: raise the system prompt
    /// if *we* lack the grant, then let /v1/start be the judge. Blocking on our
    /// own preflight would lock out every working launchd setup.
    ///
    /// Returns whether this app holds the grant — used only to decide if the
    /// permission hint is worth attaching to a failure.
    private func nudgeScreenRecordingAccess() -> Bool {
        if CGPreflightScreenCaptureAccess() { return true }
        // Shows the system prompt; the answer lands too late for this attempt.
        return CGRequestScreenCaptureAccess()
    }

    func startRecording() {
        let granted = nudgeScreenRecordingAccess()
        run("start", hint: granted ? nil : Self.screenRecordingHint) { [self] in
            try await client?.start()
        }
    }

    func stopRecording() {
        stopping = true
        run("stop") { [self] in
            defer { stopping = false }
            try await client?.stop()
        }
    }
    func push(_ id: String, target: String) { run("push \(target)") { [self] in try await client?.push(id, target: target) } }
    func reprocess(_ id: String) { run("reprocess") { [self] in try await client?.reprocess(id) } }
    func deleteMeeting(_ id: String, deleteFiles: Bool) {
        run("delete") { [self] in
            try await client?.delete(id, deleteFiles: deleteFiles)
            if selectedMeetingID == id { selectedMeetingID = nil; detail = nil }
        }
    }

    func saveNotes(meetingID: String, text: String) {
        Task { try? await client?.saveNotes(meetingID, notes: text) }
    }

    func revealInFinder(_ meeting: Meeting) {
        guard let base = meeting.basePath else { return }
        let dir = (base as NSString).deletingLastPathComponent
        NSWorkspace.shared.selectFile(base + ".system.wav", inFileViewerRootedAtPath: dir)
    }
}
