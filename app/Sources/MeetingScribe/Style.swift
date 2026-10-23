// Shared visual pieces: glass sheets, transcript bubbles, the live record dot.
import SwiftUI

extension View {
    /// A floating glass sheet; falls back to a material on macOS before 26.
    @ViewBuilder
    func glassSheet(cornerRadius: CGFloat = 22) -> some View {
        let shape = RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
        if #available(macOS 26.0, *) {
            self.glassEffect(.regular, in: shape)
        } else {
            self.background(.regularMaterial, in: shape)
                .overlay(shape.strokeBorder(.white.opacity(0.08)))
        }
    }

    /// A small rounded capsule for metadata like the date and length.
    func chip() -> some View {
        self.font(.callout)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 10)
            .padding(.vertical, 4)
            .background(.quaternary.opacity(0.6), in: Capsule())
    }
}

/// Red dot that breathes while recording; still when Reduce Motion is on.
struct RecordingDot: View {
    var size: CGFloat = 12
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var dim = false

    var body: some View {
        Circle()
            .fill(.red)
            .frame(width: size, height: size)
            .shadow(color: .red.opacity(0.6), radius: dim ? 2 : 6)
            .opacity(dim ? 0.55 : 1)
            .onAppear {
                guard !reduceMotion else { return }
                withAnimation(.easeInOut(duration: 1.2).repeatForever(autoreverses: true)) { dim = true }
            }
    }
}

/// One transcript line as a chat bubble: your lines on the right, everyone else on the left.
struct TranscriptBubble: View {
    let speaker: String
    let text: String
    var timestamp: String?
    var showsHeader = true

    private var isMe: Bool { speaker == "Me" }

    var body: some View {
        VStack(alignment: isMe ? .trailing : .leading, spacing: 3) {
            if showsHeader {
                HStack(spacing: 6) {
                    Text(speaker).fontWeight(.semibold)
                    if let timestamp { Text(timestamp).monospacedDigit() }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 12)
            }
            Text(text)
                .textSelection(.enabled)
                .padding(.horizontal, 14)
                .padding(.vertical, 9)
                .background(bubbleFill, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        }
        .frame(maxWidth: 560, alignment: isMe ? .trailing : .leading)
        .frame(maxWidth: .infinity, alignment: isMe ? .trailing : .leading)
    }

    private var bubbleFill: AnyShapeStyle {
        isMe ? AnyShapeStyle(Color.accentColor.opacity(0.22)) : AnyShapeStyle(.quaternary.opacity(0.7))
    }
}

/// Splits a stored "Speaker: text" transcript into (speaker, text) lines.
func transcriptLines(_ transcript: String) -> [(speaker: String, text: String)] {
    transcript.split(separator: "\n").compactMap { raw in
        let line = raw.trimmingCharacters(in: .whitespaces)
        guard !line.isEmpty else { return nil }
        if let colon = line.firstIndex(of: ":"), line.distance(from: line.startIndex, to: colon) <= 24 {
            return (String(line[..<colon]), line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces))
        }
        return ("", line)
    }
}

/// Parses the daemon's ISO start time ("2026-10-23T09:59:42").
func meetingDate(_ m: Meeting) -> Date? {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
    return m.startedAt.flatMap { f.date(from: $0) }
}

func durationText(_ sec: Int) -> String {
    if sec < 60 { return "\(sec) s" }
    let m = sec / 60
    return m >= 60 ? "\(m / 60) h \(m % 60) min" : "\(m) min"
}
