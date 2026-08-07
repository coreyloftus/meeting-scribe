// syscap — capture macOS system audio to a WAV file using ScreenCaptureKit.
//
// No virtual audio device (BlackHole/Background Music) and no menu-bar app are
// required: ScreenCaptureKit taps the system audio mix directly. The only cost
// is a one-time Screen Recording permission grant for the terminal that runs it.
//
// Usage:   syscap <output.wav> [--sample-rate 48000] [--channels 2]
// Stops cleanly on SIGINT/SIGTERM and finalizes the WAV header.
//
// Requires macOS 13+. Build:  swiftc -O syscap.swift -o syscap

import AVFoundation
import ScreenCaptureKit
import Darwin

// ---- Argument parsing -------------------------------------------------------

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write(("syscap: " + msg + "\n").data(using: .utf8)!)
    exit(1)
}

let rawArgs = Array(CommandLine.arguments.dropFirst())
guard let outputPath = rawArgs.first(where: { !$0.hasPrefix("--") }) else {
    fail("usage: syscap <output.wav> [--sample-rate N] [--channels N]")
}

func intFlag(_ name: String, _ fallback: Int) -> Int {
    if let i = rawArgs.firstIndex(of: name), i + 1 < rawArgs.count, let v = Int(rawArgs[i + 1]) {
        return v
    }
    return fallback
}

let sampleRate = intFlag("--sample-rate", 48000)
let channels = intFlag("--channels", 2)

// ---- Capture engine ---------------------------------------------------------

final class SystemAudioRecorder: NSObject, SCStreamOutput, SCStreamDelegate {
    /// Invoked when the stream dies on its own, so the owner can tear the whole
    /// process down instead of letting a dead stream look like a live recording.
    var onStreamFailure: (() -> Void)?

    private var stream: SCStream?
    private var audioFile: AVAudioFile?
    private let outputURL: URL
    private let sampleRate: Int
    private let channels: Int

    // Written from the SCStream callback queues, read during teardown — guard
    // both so the report at stop() can't race the delegate.
    private let stateLock = NSLock()
    private var wroteAnything = false
    private var streamError: Error?

    init(outputURL: URL, sampleRate: Int, channels: Int) {
        self.outputURL = outputURL
        self.sampleRate = sampleRate
        self.channels = channels
    }

    // Kept synchronous on purpose: NSLock may not be taken across an await.
    private func markWrote() {
        stateLock.lock()
        defer { stateLock.unlock() }
        wroteAnything = true
    }

    private func recordFailure(_ error: Error) {
        stateLock.lock()
        defer { stateLock.unlock() }
        streamError = error
    }

    private func snapshot() -> (wrote: Bool, error: Error?) {
        stateLock.lock()
        defer { stateLock.unlock() }
        return (wroteAnything, streamError)
    }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false,
                                                                           onScreenWindowsOnly: false)
        guard let display = content.displays.first else {
            fail("no display available to attach the audio capture to")
        }

        // Attach to a display to capture its audio; the video path is ignored.
        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.sampleRate = sampleRate
        config.channelCount = channels
        // `true` yields pure silence (or drops the stream outright) on macOS 15+/26
        // via a broken per-process tap. Leaving it off is safe here because nothing
        // in the capture pipeline plays audio — syscap only writes a file — so
        // there is no output of our own to feed back into the recording.
        config.excludesCurrentProcessAudio = false
        // Keep the (ignored) video path as cheap as possible.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        config.queueDepth = 6

        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        let queue = DispatchQueue(label: "syscap.audio")
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try await stream.startCapture()
        self.stream = stream
        FileHandle.standardError.write("syscap: capturing system audio…\n".data(using: .utf8)!)
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid else { return }
        guard let pcm = sampleBuffer.toPCMBuffer() else { return }

        if audioFile == nil {
            // Lazily create the file using the real incoming format so we never
            // mismatch sample rate / channel count (that mismatch is exactly the
            // "underwater / slow-motion" bug this whole tool was rebuilt to kill).
            let settings: [String: Any] = [
                AVFormatIDKey: kAudioFormatLinearPCM,
                AVSampleRateKey: pcm.format.sampleRate,
                AVNumberOfChannelsKey: pcm.format.channelCount,
                AVLinearPCMBitDepthKey: 16,
                AVLinearPCMIsFloatKey: false,
                AVLinearPCMIsBigEndianKey: false,
                AVLinearPCMIsNonInterleaved: false,
            ]
            do {
                audioFile = try AVAudioFile(forWriting: outputURL, settings: settings,
                                            commonFormat: .pcmFormatFloat32, interleaved: false)
            } catch {
                fail("could not open output file: \(error.localizedDescription)")
            }
        }

        do {
            try audioFile?.write(from: pcm)
            markWrote()
        } catch {
            FileHandle.standardError.write("syscap: write error: \(error)\n".data(using: .utf8)!)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        recordFailure(error)
        FileHandle.standardError.write("syscap: stream stopped: \(error.localizedDescription)\n".data(using: .utf8)!)
        // Don't linger on a dead stream. The caller only checks that our pid is
        // alive (recorder.py, one second after start), so staying up would hide
        // the failure until the meeting ended — with a silent or truncated WAV.
        onStreamFailure?()
    }

    /// Finalize the WAV and report what actually happened. Returns the process
    /// exit code: non-zero whenever the stream failed, so the failure surfaces
    /// through the caller's liveness check instead of only in the log.
    func stop() async -> Int32 {
        try? await stream?.stopCapture()
        audioFile = nil // flush + finalize the WAV header

        let (wrote, err) = snapshot()

        // Report a stream failure even when some audio landed: a truncated
        // system track silently misaligns against the full-length mic track.
        if let err {
            let what = wrote
                ? "capture ended early — the WAV is truncated and will not line up with the mic track"
                : "capture failed, no audio written"
            let msg = "syscap: error — \(what). The ScreenCaptureKit stream stopped early "
                + "(\(err.localizedDescription)). Most often that means a missing or "
                + "invalidated Screen Recording permission — grant it under System Settings > "
                + "Privacy & Security > Screen Recording and retry. It can also mean the "
                + "captured display was disconnected or slept.\n"
            FileHandle.standardError.write(msg.data(using: .utf8)!)
            return 1
        }
        if !wrote {
            FileHandle.standardError.write("syscap: warning — stream ran but no audio arrived (was anything actually playing through the selected output?).\n".data(using: .utf8)!)
        }
        return 0
    }
}

extension CMSampleBuffer {
    /// Convert a ScreenCaptureKit audio sample buffer into an owned PCM buffer.
    ///
    /// We build a *standard* float32 format via `standardFormatWithSampleRate`
    /// (guaranteed `isPCMFormat == true`) rather than from the raw stream
    /// description — the latter isn't recognised as PCM and crashes
    /// `AVAudioPCMBuffer(pcmFormat:)`. The no-copy buffer is only valid inside
    /// `withAudioBufferList`, so we copy the samples into an owned buffer.
    func toPCMBuffer() -> AVAudioPCMBuffer? {
        guard let fmtDesc = CMSampleBufferGetFormatDescription(self),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fmtDesc)?.pointee,
              let format = AVAudioFormat(standardFormatWithSampleRate: asbd.mSampleRate,
                                         channels: asbd.mChannelsPerFrame) else {
            return nil
        }
        let result = try? withAudioBufferList { abl, _ -> AVAudioPCMBuffer? in
            guard let noCopy = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: abl.unsafePointer),
                  noCopy.frameLength > 0,
                  let owned = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: noCopy.frameLength),
                  let src = noCopy.floatChannelData, let dst = owned.floatChannelData else {
                return nil
            }
            owned.frameLength = noCopy.frameLength
            let bytes = Int(noCopy.frameLength) * MemoryLayout<Float>.size
            for ch in 0..<Int(format.channelCount) {
                memcpy(dst[ch], src[ch], bytes)
            }
            return owned
        }
        return result ?? nil
    }
}

// ---- Signal handling: stop cleanly so the WAV is valid ----------------------

let recorder = SystemAudioRecorder(outputURL: URL(fileURLWithPath: outputPath),
                                   sampleRate: sampleRate, channels: channels)

let stopSem = DispatchSemaphore(value: 0)

// A stream that dies on its own tears the process down the same way a signal
// does, so the WAV header is still finalized before we exit non-zero.
recorder.onStreamFailure = { stopSem.signal() }

var signalSources: [DispatchSourceSignal] = []
for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { stopSem.signal() }
    src.resume()
    signalSources.append(src) // hold a reference so the source stays alive
}

Task {
    do {
        try await recorder.start()
    } catch {
        fail("failed to start capture: \(error.localizedDescription)")
    }
}

// Wait on a background thread for the stop signal, then tear down on main.
DispatchQueue.global().async {
    stopSem.wait()
    Task {
        exit(await recorder.stop())
    }
}

RunLoop.main.run()
