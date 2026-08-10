// miccap — capture a macOS input device to a WAV file using AVAudioEngine.
//
// This replaces `ffmpeg -f avfoundation` for microphone capture. ffmpeg's
// avfoundation input silently drops roughly one sample in nine on this
// hardware: it reports 48 kHz and advances its timestamps at speed=1x, but
// writes only ~42.5k samples per second. The result is a mic.wav that is
// real-time audio with ~11% of it missing, packed into a timeline ~11% too
// short — which then looks like the *system* channel being stretched when the
// two are lined up. Raising -thread_queue_size does not help (the loss is
// inside the indev, not the muxer queue) and -af aresample=async only papers
// over the timeline while leaving the audio gone. AVAudioEngine loses nothing.
//
// Usage:   miccap <output.wav> [--device <name>] [--channels 1]
//          miccap --list-devices
// Stops cleanly on SIGINT/SIGTERM and finalizes the WAV header.
//
// Requires macOS 13+. Build:  swiftc -O miccap.swift -o miccap

import AVFoundation
import CoreAudio
import Darwin

// ---- Argument parsing -------------------------------------------------------

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write(("miccap: " + msg + "\n").data(using: .utf8)!)
    exit(1)
}

func note(_ msg: String) {
    FileHandle.standardError.write(("miccap: " + msg + "\n").data(using: .utf8)!)
}

let rawArgs = Array(CommandLine.arguments.dropFirst())

func stringFlag(_ name: String) -> String? {
    if let i = rawArgs.firstIndex(of: name), i + 1 < rawArgs.count {
        return rawArgs[i + 1]
    }
    return nil
}

func intFlag(_ name: String, _ fallback: Int) -> Int {
    if let v = stringFlag(name), let i = Int(v) { return i }
    return fallback
}

// ---- CoreAudio device lookup ------------------------------------------------

/// All device IDs known to the HAL.
func allDeviceIDs() -> [AudioDeviceID] {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
                                          mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject),
                                         &addr, 0, nil, &size) == noErr, size > 0 else {
        return []
    }
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                     &addr, 0, nil, &size, &ids) == noErr else {
        return []
    }
    return ids
}

func deviceName(_ id: AudioDeviceID) -> String {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioObjectPropertyName,
                                          mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    // The property yields a +1 CFStringRef, so take it as unmanaged and release
    // it via takeRetainedValue — binding it straight to a CFString variable
    // would both warn and leak.
    var name: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &name) == noErr,
          let name else { return "" }
    return name.takeRetainedValue() as String
}

/// True when the device exposes at least one input channel.
func hasInputChannels(_ id: AudioDeviceID) -> Bool {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreamConfiguration,
                                          mScope: kAudioObjectPropertyScopeInput,
                                          mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size) == noErr, size > 0 else {
        return false
    }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size),
                                               alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, raw) == noErr else { return false }
    let lists = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
    return lists.contains { $0.mNumberChannels > 0 }
}

func inputDevices() -> [(id: AudioDeviceID, name: String)] {
    allDeviceIDs().filter(hasInputChannels).map { ($0, deviceName($0)) }
}

if rawArgs.contains("--list-devices") {
    for d in inputDevices() { print(d.name) }
    exit(0)
}

guard let outputPath = rawArgs.first(where: { !$0.hasPrefix("--") }) else {
    fail("usage: miccap <output.wav> [--device <name>] [--channels N]")
}
let wantedDevice = stringFlag("--device")
let wantedChannels = intFlag("--channels", 1)

// ---- Capture engine ---------------------------------------------------------

final class MicRecorder {
    private let engine = AVAudioEngine()
    private let outputURL: URL
    private let channels: Int
    private var audioFile: AVAudioFile?
    private var monoFormat: AVAudioFormat?
    private let lock = NSLock()
    private var frameCount: Int64 = 0

    init(outputURL: URL, channels: Int) {
        self.outputURL = outputURL
        self.channels = channels
    }

    func start(deviceNamed wanted: String?) throws {
        let input = engine.inputNode

        // Pick the device BEFORE reading the format: the node caches the HAL
        // unit's format, so switching devices afterwards would leave us writing
        // a file whose header describes the old device.
        if let wanted, !wanted.isEmpty {
            if let match = inputDevices().first(where: { $0.name == wanted }) {
                var devID = match.id
                let err = AudioUnitSetProperty(input.audioUnit!,
                                               kAudioOutputUnitProperty_CurrentDevice,
                                               kAudioUnitScope_Global, 0,
                                               &devID, UInt32(MemoryLayout<AudioDeviceID>.size))
                if err != noErr {
                    note("warning — could not select \"\(wanted)\" (OSStatus \(err)); using the default input")
                }
            } else {
                note("warning — no input device named \"\(wanted)\"; using the default input")
            }
        }

        let fmt = input.outputFormat(forBus: 0)
        guard fmt.sampleRate > 0, fmt.channelCount > 0 else {
            throw NSError(domain: "miccap", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "input device reported an empty format "
                    + "(\(fmt.sampleRate) Hz, \(fmt.channelCount) ch) — is the mic in use or disconnected?"
            ])
        }

        let outChannels = min(max(channels, 1), Int(fmt.channelCount))
        guard let outFormat = AVAudioFormat(standardFormatWithSampleRate: fmt.sampleRate,
                                            channels: AVAudioChannelCount(outChannels)) else {
            throw NSError(domain: "miccap", code: 2, userInfo: [
                NSLocalizedDescriptionKey: "could not build a \(outChannels)-channel output format"
            ])
        }
        monoFormat = outFormat

        // 16-bit on disk like the old ffmpeg command; float32 in memory so the
        // tap buffers need no conversion on the audio thread.
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: fmt.sampleRate,
            AVNumberOfChannelsKey: outChannels,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ]
        audioFile = try AVAudioFile(forWriting: outputURL, settings: settings,
                                    commonFormat: .pcmFormatFloat32, interleaved: false)

        input.installTap(onBus: 0, bufferSize: 4096, format: fmt) { [weak self] buffer, _ in
            self?.write(buffer)
        }

        engine.prepare()
        try engine.start()
        note("capturing \(Int(fmt.sampleRate)) Hz, \(fmt.channelCount) ch → \(outChannels) ch…")
    }

    /// Downmix to the requested channel count and append. Runs on the audio
    /// thread, so it allocates only the one output buffer and never blocks on
    /// anything but the file write.
    private func write(_ buffer: AVAudioPCMBuffer) {
        guard let outFormat = monoFormat,
              let src = buffer.floatChannelData,
              buffer.frameLength > 0,
              let out = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: buffer.frameLength),
              let dst = out.floatChannelData else { return }
        out.frameLength = buffer.frameLength

        let frames = Int(buffer.frameLength)
        let inCh = Int(buffer.format.channelCount)
        let outCh = Int(outFormat.channelCount)

        if outCh == inCh {
            for ch in 0..<outCh {
                memcpy(dst[ch], src[ch], frames * MemoryLayout<Float>.size)
            }
        } else if outCh == 1 {
            // Average every input channel — same as ffmpeg's `-ac 1`. Taking
            // channel 0 instead would silently halve the level on devices whose
            // first channel is unused.
            let scale = 1.0 / Float(inCh)
            for f in 0..<frames {
                var sum: Float = 0
                for ch in 0..<inCh { sum += src[ch][f] }
                dst[0][f] = sum * scale
            }
        } else {
            for ch in 0..<outCh {
                memcpy(dst[ch], src[min(ch, inCh - 1)], frames * MemoryLayout<Float>.size)
            }
        }

        lock.lock()
        defer { lock.unlock() }
        guard let file = audioFile else { return }
        do {
            try file.write(from: out)
            frameCount += Int64(buffer.frameLength)
        } catch {
            note("write error: \(error)")
        }
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        lock.lock()
        let frames = frameCount
        let rate = monoFormat?.sampleRate ?? 0
        audioFile = nil // flush + finalize the WAV header
        lock.unlock()
        if frames == 0 {
            note("warning — no audio was captured (is the microphone permitted and unmuted?)")
        } else if rate > 0 {
            note(String(format: "wrote %lld frames (%.1f s)", frames, Double(frames) / rate))
        }
    }
}

// ---- Signal handling: stop cleanly so the WAV is valid ----------------------

let recorder = MicRecorder(outputURL: URL(fileURLWithPath: outputPath), channels: wantedChannels)

let stopSem = DispatchSemaphore(value: 0)
var signalSources: [DispatchSourceSignal] = []
for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { stopSem.signal() }
    src.resume()
    signalSources.append(src) // hold a reference so the source stays alive
}

do {
    try recorder.start(deviceNamed: wantedDevice)
} catch {
    fail("failed to start capture: \(error.localizedDescription)")
}

// Wait on a background thread for the stop signal, then tear down on main.
DispatchQueue.global().async {
    stopSem.wait()
    recorder.stop()
    exit(0)
}

RunLoop.main.run()
