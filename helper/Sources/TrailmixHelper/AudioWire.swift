import AVFoundation
import Foundation

/// What the Trailmix backend expects on the audio WebSocket: 16 kHz mono little-endian int16 PCM,
/// each message prefixed with one channel byte (0 = your mic, 1 = meeting audio).
enum Wire {
    static let sampleRate = 16_000.0
    static let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: sampleRate, channels: 1, interleaved: true)!
    static let frameSamples = 4096  // ~256 ms per message, like the web app
    static let mic: UInt8 = 0
    static let meetingAudio: UInt8 = 1
}

struct CaptureError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

/// Something that produces wire-format audio: the mic, other apps, or a file (for tests).
protocol AudioSource: AnyObject {
    var onAudio: (([Int16]) -> Void)? { get set }
    var name: String { get }
    func start() throws
    func stop()
}

/// Converts audio in any PCM format to the wire format, keeping resampler state between buffers.
final class WireConverter {
    private let converter: AVAudioConverter
    private let ratio: Double

    init?(from format: AVAudioFormat) {
        guard let converter = AVAudioConverter(from: format, to: Wire.format) else { return nil }
        converter.downmix = true  // mix stereo down to mono instead of dropping a channel
        self.converter = converter
        ratio = Wire.sampleRate / format.sampleRate
    }

    func convert(_ buffer: AVAudioPCMBuffer) -> [Int16] {
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
        guard buffer.frameLength > 0, let out = AVAudioPCMBuffer(pcmFormat: Wire.format, frameCapacity: capacity) else { return [] }
        var consumed = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if consumed {
                status.pointee = .noDataNow
                return nil
            }
            consumed = true
            status.pointee = .haveData
            return buffer
        }
        guard error == nil, let data = out.int16ChannelData else { return [] }
        return Array(UnsafeBufferPointer(start: data[0], count: Int(out.frameLength)))
    }
}

/// Loudness 0…1 on the same scale as the web app's meters (-58 dB is silence, -12 dB is loud).
func meterLevel(_ samples: [Int16]) -> Float {
    guard !samples.isEmpty else { return 0 }
    var sum: Float = 0
    for s in samples {
        let f = Float(s) / 32768
        sum += f * f
    }
    let db = 20 * log10(sqrt(sum / Float(samples.count)) + 1e-8)
    return min(1, max(0, (db + 58) / 46))
}

/// Packs both tracks into WebSocket messages on one serial queue, and keeps them in step:
/// each track's first sample lines up with the start of the recording, and if a track stalls
/// (say the output device changed mid-call) the gap is filled with silence so timestamps stay true.
/// Messages wait here until `attach` gives it somewhere to send them.
final class Streamer {
    private struct Track {
        var pending: [Int16] = []
        var sent = 0
        var total: Int { sent + pending.count }
    }

    private let queue = DispatchQueue(label: "trailmix.wire")
    private let started = DispatchTime.now()
    private var tracks: [UInt8: Track] = [:]
    private var sender: ((Data) -> Void)?
    private var unsent: [Data] = []
    private var timer: DispatchSourceTimer?

    init() {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 1, repeating: 1)
        timer.setEventHandler { [weak self] in self?.fillGaps() }
        timer.resume()
        self.timer = timer
    }

    /// Starts sending, beginning with everything captured so far, in order.
    func attach(_ send: @escaping (Data) -> Void) {
        queue.async { [self] in
            sender = send
            unsent.forEach(send)
            unsent = []
        }
    }

    private var elapsedSamples: Int {
        Int(Double(DispatchTime.now().uptimeNanoseconds - started.uptimeNanoseconds) / 1e9 * Wire.sampleRate)
    }

    func push(_ samples: [Int16], channel: UInt8) {
        queue.async { [self] in
            if tracks[channel] == nil {
                var track = Track()
                let lead = elapsedSamples - samples.count  // this buffer began `lead` samples after the start
                if lead > Int(Wire.sampleRate / 20) { track.pending = [Int16](repeating: 0, count: lead) }
                tracks[channel] = track
            }
            tracks[channel]!.pending += samples
            drain(channel, all: false)
        }
    }

    private func fillGaps() {
        let expected = elapsedSamples
        for channel in tracks.keys {
            let gap = expected - tracks[channel]!.total
            // Only real stalls: under 1.5 s is ordinary buffering; over 10 s means the Mac slept, so leave it.
            guard gap > Int(1.5 * Wire.sampleRate), gap < Int(10 * Wire.sampleRate) else { continue }
            tracks[channel]!.pending += [Int16](repeating: 0, count: gap - Int(0.3 * Wire.sampleRate))
            drain(channel, all: false)
        }
    }

    private func drain(_ channel: UInt8, all: Bool) {
        while let count = tracks[channel]?.pending.count, count >= Wire.frameSamples || (all && count > 0) {
            let n = min(Wire.frameSamples, count)
            var data = Data(capacity: 1 + n * 2)
            data.append(channel)
            tracks[channel]!.pending[..<n].withUnsafeBytes { data.append(contentsOf: $0) }  // little-endian on every Mac
            tracks[channel]!.pending.removeFirst(n)
            tracks[channel]!.sent += n
            if let sender { sender(data) } else { unsent.append(data) }
        }
    }

    /// Sends whatever is left, then calls `done`.
    func finish(_ done: @escaping () -> Void) {
        queue.async { [self] in
            timer?.cancel()
            timer = nil
            for channel in tracks.keys { drain(channel, all: true) }
            done()
        }
    }
}

/// Plays a raw 16 kHz int16 file (or a WAV) as if it were a live source. Used by the self-test.
final class FileSource: AudioSource {
    var onAudio: (([Int16]) -> Void)?
    let name: String
    private let samples: [Int16]
    private var timer: DispatchSourceTimer?
    private var position = 0

    init(path: String) throws {
        name = (path as NSString).lastPathComponent
        var data = try Data(contentsOf: URL(fileURLWithPath: path))
        if data.starts(with: Array("RIFF".utf8)), let header = data.range(of: Data("data".utf8)) {
            data = data.subdata(in: (header.upperBound + 4)..<data.count)  // skip the WAV header
        }
        samples = data.withUnsafeBytes { Array($0.bindMemory(to: Int16.self)) }
    }

    func start() throws {
        let chunk = 1600  // 100 ms
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "trailmix.file"))
        timer.schedule(deadline: .now(), repeating: 0.1)
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            let end = min(self.samples.count, self.position + chunk)
            let slice = end > self.position ? Array(self.samples[self.position..<end]) : [Int16](repeating: 0, count: chunk)
            self.position = end
            self.onAudio?(slice)
        }
        timer.resume()
        self.timer = timer
    }

    func stop() {
        timer?.cancel()
        timer = nil
    }
}
