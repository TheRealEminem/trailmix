import AVFoundation
import Foundation

/// A three-second check that both sides of a call will be recorded: listens to the mic, then plays a short
/// chime and listens for it through other apps' audio. Run from the setup checklist in the Trailmix window,
/// so the macOS permission prompts appear at a calm moment rather than at the start of a real meeting.
enum SoundCheck {
    /// {"mic": ok|quiet|denied|error, "system": ok|silent|error, "detail": text, "at": unix time}
    static func run(micID: String) async -> [String: Any] {
        var result: [String: Any] = ["at": Date().timeIntervalSince1970]
        var details: [String] = []

        // Your side: ask for the mic if macOS hasn't yet, then listen for 2 seconds.
        var micAllowed = AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
        if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
            micAllowed = await AVCaptureDevice.requestAccess(for: .audio)
        }
        if !micAllowed {
            result["mic"] = "denied"
        } else {
            do {
                let peak = try await listen(MicCapture(deviceID: micID), seconds: 2, play: false)
                result["mic"] = peak > 400 ? "ok" : "quiet"
                result["mic_level"] = peak
            } catch {
                result["mic"] = "error"
                details.append(error.localizedDescription)
            }
        }

        // The other side: play a chime (from a separate process, which the capture doesn't exclude) and listen.
        do {
            let peak = try await listen(SystemAudioCapture(target: .allApps), seconds: 3, play: true)
            result["system"] = peak > 200 ? "ok" : "silent"
            result["system_level"] = peak
        } catch {
            result["system"] = "error"
            details.append(error.localizedDescription)
        }
        result["detail"] = details.joined(separator: " ")
        return result
    }

    /// Records from a source for a few seconds (optionally playing a sound meanwhile); returns the peak level.
    private static func listen(_ source: AudioSource, seconds: Double, play: Bool) async throws -> Int {
        let peak = PeakMeter()
        source.onAudio = { samples in peak.add(samples) }
        try source.start()
        defer { source.stop() }
        if play {
            try? await Task.sleep(for: .milliseconds(400))
            for _ in 0..<2 {
                let chime = Process()
                chime.executableURL = URL(fileURLWithPath: "/usr/bin/afplay")
                chime.arguments = ["/System/Library/Sounds/Glass.aiff"]
                try? chime.run()
                try? await Task.sleep(for: .seconds(1.1))
            }
            try? await Task.sleep(for: .seconds(max(0, seconds - 2.6)))
        } else {
            try? await Task.sleep(for: .seconds(seconds))
        }
        return peak.value
    }
}

private final class PeakMeter: @unchecked Sendable {
    private let lock = NSLock()
    private var peak = 0

    func add(_ samples: [Int16]) {
        let top = samples.reduce(0) { max($0, abs(Int($1))) }
        lock.lock()
        peak = max(peak, top)
        lock.unlock()
    }

    var value: Int {
        lock.lock()
        defer { lock.unlock() }
        return peak
    }
}
