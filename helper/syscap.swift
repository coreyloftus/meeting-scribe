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
    private var stream: SCStream?
    private var audioFile: AVAudioFile?
    private let outputURL: URL
    private let sampleRate: Int
    private let channels: Int
    private var wroteAnything = false

    init(outputURL: URL, sampleRate: Int, channels: Int) {
        self.outputURL = outputURL
        self.sampleRate = sampleRate
        self.channels = channels
    }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false,
                                                                           onScreenWindowsOnly: false)
        guard let display = content.displays.first else {
            fail("no display available to attach the audio capture to")
        }

        // We must attach to a display to capture audio, but we don't care about
        // the video. Exclude our own process so we never record ourselves.
        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.sampleRate = sampleRate
        config.channelCount = channels
        config.excludesCurrentProcessAudio = true
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
            wroteAnything = true
        } catch {
            FileHandle.standardError.write("syscap: write error: \(error)\n".data(using: .utf8)!)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write("syscap: stream stopped: \(error.localizedDescription)\n".data(using: .utf8)!)
    }

    func stop() async {
        try? await stream?.stopCapture()
        audioFile = nil // flush + finalize the WAV header
        if !wroteAnything {
            FileHandle.standardError.write("syscap: warning — no audio was captured (was anything playing?)\n".data(using: .utf8)!)
        }
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
        await recorder.stop()
        exit(0)
    }
}

RunLoop.main.run()
