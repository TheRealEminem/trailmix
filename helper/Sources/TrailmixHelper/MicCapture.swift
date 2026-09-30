import AVFoundation

struct Microphone: Identifiable, Hashable {
    let id: String  // AVCaptureDevice.uniqueID
    let name: String

    static func all() -> [Microphone] {
        AVCaptureDevice.DiscoverySession(deviceTypes: [.microphone, .external], mediaType: .audio, position: .unspecified)
            .devices.map { Microphone(id: $0.uniqueID, name: $0.localizedName) }
    }

    static var defaultName: String {
        AVCaptureDevice.default(for: .audio)?.localizedName ?? "No microphone"
    }
}

/// Your side of the conversation, from the chosen microphone (or the system default), as wire-format audio.
/// If that mic disappears mid-meeting (a headset unplugged), it carries on with the default one.
final class MicCapture: NSObject, AudioSource, AVCaptureAudioDataOutputSampleBufferDelegate {
    var onAudio: (([Int16]) -> Void)?
    /// Called (on a background queue) when the mic changed under us, with the new mic's name.
    var onSwitched: ((String) -> Void)?
    private(set) var name = ""

    private let deviceID: String
    private let queue = DispatchQueue(label: "trailmix.mic", qos: .userInitiated)
    private var session: AVCaptureSession?
    private var converter: WireConverter?
    private var converterFormat: AVAudioFormat?
    private var disconnectObserver: NSObjectProtocol?

    init(deviceID: String) {
        self.deviceID = deviceID
    }

    func start() throws {
        let chosen = deviceID.isEmpty ? nil : AVCaptureDevice(uniqueID: deviceID)
        guard let device = chosen ?? AVCaptureDevice.default(for: .audio) else { throw CaptureError("No microphone is connected") }
        try run(device)
        disconnectObserver = NotificationCenter.default.addObserver(
            forName: AVCaptureDevice.wasDisconnectedNotification, object: nil, queue: nil
        ) { [weak self] note in
            guard let self, let gone = note.object as? AVCaptureDevice, gone.localizedName == self.name,
                  let fallback = AVCaptureDevice.default(for: .audio), fallback.uniqueID != gone.uniqueID else { return }
            self.queue.async {
                self.session?.stopRunning()
                if (try? self.run(fallback)) != nil { self.onSwitched?(fallback.localizedName) }
            }
        }
    }

    private func run(_ device: AVCaptureDevice) throws {
        let session = AVCaptureSession()
        let input = try AVCaptureDeviceInput(device: device)
        let output = AVCaptureAudioDataOutput()
        // Ask for the wire format directly; anything else that arrives is converted below.
        output.audioSettings = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: Wire.sampleRate,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ]
        output.setSampleBufferDelegate(self, queue: queue)
        session.beginConfiguration()
        guard session.canAddInput(input), session.canAddOutput(output) else {
            session.commitConfiguration()
            throw CaptureError("Couldn't use \(device.localizedName)")
        }
        session.addInput(input)
        session.addOutput(output)
        session.commitConfiguration()
        session.startRunning()
        self.session = session
        name = device.localizedName
    }

    func stop() {
        if let disconnectObserver { NotificationCenter.default.removeObserver(disconnectObserver) }
        disconnectObserver = nil
        session?.stopRunning()
        session = nil
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let description = CMSampleBufferGetFormatDescription(sampleBuffer) else { return }
        let frames = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        let format = AVAudioFormat(cmAudioFormatDescription: description)
        guard frames > 0, let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames) else { return }
        buffer.frameLength = frames
        guard CMSampleBufferCopyPCMDataIntoAudioBufferList(sampleBuffer, at: 0, frameCount: Int32(frames), into: buffer.mutableAudioBufferList) == noErr else { return }

        if format.commonFormat == .pcmFormatInt16, format.sampleRate == Wire.sampleRate, format.channelCount == 1, let data = buffer.int16ChannelData {
            onAudio?(Array(UnsafeBufferPointer(start: data[0], count: Int(frames))))
            return
        }
        if converterFormat != format {
            converter = WireConverter(from: format)
            converterFormat = format
        }
        if let samples = converter?.convert(buffer), !samples.isEmpty { onAudio?(samples) }
    }
}
