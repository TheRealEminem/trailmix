import AppKit
import AVFoundation
import CoreAudio

/// Small typed wrappers around the Core Audio property API.
enum CA {
    static func address(_ selector: AudioObjectPropertySelector, scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    }

    static func value<T: BitwiseCopyable>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, default fallback: T) -> T {
        var address = address(selector)
        var result = fallback
        var size = UInt32(MemoryLayout<T>.size)
        return AudioObjectGetPropertyData(object, &address, 0, nil, &size, &result) == noErr ? result : fallback
    }

    static func string(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
        var address = address(selector)
        var result: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(object, &address, 0, nil, &size, &result) == noErr, let result else { return nil }
        return result.takeRetainedValue() as String
    }

    static func objects(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> [AudioObjectID] {
        var address = address(selector)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(object, &address, 0, nil, &size) == noErr, size > 0 else { return [] }
        var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(object, &address, 0, nil, &size, &ids) == noErr else { return [] }
        return ids
    }

    static let system = AudioObjectID(kAudioObjectSystemObject)

    static func processObject(pid: pid_t) -> AudioObjectID? {
        var address = address(kAudioHardwarePropertyTranslatePIDToProcessObject)
        var pid = pid
        var object = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        let status = AudioObjectGetPropertyData(system, &address, UInt32(MemoryLayout<pid_t>.size), &pid, &size, &object)
        return status == noErr && object != kAudioObjectUnknown ? object : nil
    }

    static func streamCount(_ device: AudioObjectID, _ scope: AudioObjectPropertyScope) -> Int {
        var address = address(kAudioDevicePropertyStreams, scope: scope)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(device, &address, 0, nil, &size) == noErr else { return 0 }
        return Int(size) / MemoryLayout<AudioObjectID>.size
    }

    /// The device whose clock drives the capture. The tap works with any clock (drift compensation keeps
    /// it in step), so prefer one without a microphone: running a Bluetooth headset's input would switch
    /// it into its low-quality call mode. That's the default output unless it's such a headset, in which
    /// case the built-in speakers.
    static func clockDeviceUID() throws -> String {
        let fallback: AudioObjectID = value(system, kAudioHardwarePropertyDefaultOutputDevice, default: AudioObjectID(kAudioObjectUnknown))
        var candidates = [fallback]
        candidates += objects(system, kAudioHardwarePropertyDevices).filter {
            value($0, kAudioDevicePropertyTransportType, default: UInt32(0)) == kAudioDeviceTransportTypeBuiltIn
        }
        for device in candidates where device != kAudioObjectUnknown && streamCount(device, kAudioObjectPropertyScopeOutput) > 0
            && streamCount(device, kAudioObjectPropertyScopeInput) == 0 {
            if let uid = string(device, kAudioDevicePropertyDeviceUID) { return uid }
        }
        guard fallback != kAudioObjectUnknown, let uid = string(fallback, kAudioDevicePropertyDeviceUID) else {
            throw CaptureError("There's no audio output device to listen to")
        }
        return uid
    }

    static func check(_ status: OSStatus, _ what: String) throws {
        guard status == noErr else { throw CaptureError("Couldn't \(what) (Core Audio error \(status))") }
    }
}

/// A process that is connected to the Mac's audio system.
struct AudioProcess {
    let objectID: AudioObjectID
    let pid: pid_t
    let bundleID: String
    let isPlaying: Bool

    static func all() -> [AudioProcess] {
        CA.objects(CA.system, kAudioHardwarePropertyProcessObjectList).map { object in
            AudioProcess(
                objectID: object,
                pid: CA.value(object, kAudioProcessPropertyPID, default: pid_t(-1)),
                bundleID: CA.string(object, kAudioProcessPropertyBundleID) ?? "",
                isPlaying: CA.value(object, kAudioProcessPropertyIsRunningOutput, default: UInt32(0)) != 0
            )
        }
    }

    /// True if this process belongs to the app with this bundle ID (helpers like com.google.Chrome.helper count).
    func belongs(to appID: String) -> Bool {
        bundleID == appID || bundleID.hasPrefix(appID + ".")
    }
}

/// An app you can pick as the meeting-audio source.
struct AudioApp: Identifiable, Hashable {
    let id: String  // bundle ID
    let name: String
    let isPlaying: Bool

    /// FaceTime and iPhone calls on the Mac play through a system service rather than an app.
    static let calls = "com.apple.avconferenced"

    /// Apps that are running and connected to audio, the ones playing sound right now first.
    static func current() -> [AudioApp] {
        let processes = AudioProcess.all()
        let running = NSWorkspace.shared.runningApplications.filter { $0.activationPolicy == .regular && $0.bundleIdentifier != nil }
        var apps: [String: AudioApp] = [:]
        for process in processes where process.pid != getpid() {
            if process.belongs(to: calls) {
                apps[calls] = AudioApp(id: calls, name: "FaceTime & iPhone calls", isPlaying: process.isPlaying || apps[calls]?.isPlaying == true)
                continue
            }
            guard let app = running.first(where: { process.belongs(to: $0.bundleIdentifier!) }), let id = app.bundleIdentifier else { continue }
            let playing = process.isPlaying || apps[id]?.isPlaying == true
            apps[id] = AudioApp(id: id, name: app.localizedName ?? id, isPlaying: playing)
        }
        return apps.values.sorted { ($0.isPlaying ? 0 : 1, $0.name.lowercased()) < ($1.isPlaying ? 0 : 1, $1.name.lowercased()) }
    }

    /// Is anything other than us making sound? Used to tell "silence" from "not allowed to listen".
    static func anythingPlaying() -> Bool {
        AudioProcess.all().contains { $0.isPlaying && $0.pid != getpid() }
    }
}

/// Records other apps' audio with a Core Audio process tap (macOS 14.2+): either everything the Mac plays,
/// or just the apps you picked. The tap feeds a private aggregate device whose input we read.
/// The first time, macOS asks for "System Audio Recording" permission; without it the tap delivers silence.
final class SystemAudioCapture: AudioSource {
    enum Target: Equatable {
        case allApps
        case apps([String])  // bundle IDs
    }

    var onAudio: (([Int16]) -> Void)?
    var name: String {
        switch target {
        case .allApps: return "All apps"
        case .apps(let ids): return ids.joined(separator: ", ")
        }
    }

    private let target: Target
    private let io = DispatchQueue(label: "trailmix.meeting-audio.io", qos: .userInitiated)
    private let control = DispatchQueue(label: "trailmix.meeting-audio.control")
    private let tapUUID = UUID()
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var deviceID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private var converter: WireConverter?
    private var tapped: [AudioObjectID] = []
    private var tapBuffers: UnsafeMutableAudioBufferListPointer?
    private var outputListener: AudioObjectPropertyListenerBlock?
    private var refresh: DispatchSourceTimer?

    init(target: Target) {
        self.target = target
    }

    func start() throws {
        try control.sync {
            var tap = AudioObjectID(kAudioObjectUnknown)
            try CA.check(AudioHardwareCreateProcessTap(makeDescription(), &tap), "listen to other apps")
            tapID = tap
            try startDevice()
            watchOutputDevice()
            if case .apps = target { watchApps() }
        }
    }

    func stop() {
        control.sync {
            refresh?.cancel()
            refresh = nil
            if let listener = outputListener {
                var address = CA.address(kAudioHardwarePropertyDefaultOutputDevice)
                AudioObjectRemovePropertyListenerBlock(CA.system, &address, control, listener)
                outputListener = nil
            }
            stopDevice()
            if tapID != kAudioObjectUnknown {
                AudioHardwareDestroyProcessTap(tapID)
                tapID = AudioObjectID(kAudioObjectUnknown)
            }
        }
    }

    private func makeDescription() -> CATapDescription {
        let description: CATapDescription
        switch target {
        case .allApps:
            let me = CA.processObject(pid: getpid())
            description = CATapDescription(stereoGlobalTapButExcludeProcesses: me.map { [$0] } ?? [])
        case .apps(let ids):
            tapped = AudioProcess.all().filter { process in ids.contains { process.belongs(to: $0) } }.map(\.objectID)
            description = CATapDescription(stereoMixdownOfProcesses: tapped)
        }
        description.uuid = tapUUID
        description.name = "Trailmix meeting audio"
        description.isPrivate = true
        description.muteBehavior = .unmuted  // you still hear the call
        return description
    }

    private func startDevice() throws {
        let clock = try CA.clockDeviceUID()
        let settings: [String: Any] = [
            kAudioAggregateDeviceNameKey: "Trailmix meeting audio",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: clock,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            // Don't wait for a tapped app to make a sound before starting: silence is part of the recording.
            kAudioAggregateDeviceTapAutoStartKey: false,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: clock]],
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true, kAudioSubTapUIDKey: tapUUID.uuidString]],
        ]
        var device = AudioObjectID(kAudioObjectUnknown)
        try CA.check(AudioHardwareCreateAggregateDevice(settings as CFDictionary, &device), "set up the meeting-audio device")
        deviceID = device

        var format = CA.value(tapID, kAudioTapPropertyFormat, default: AudioStreamBasicDescription())
        guard let tapFormat = AVAudioFormat(streamDescription: &format), let converter = WireConverter(from: tapFormat) else {
            throw CaptureError("Couldn't read the format of other apps' audio")
        }
        self.converter = converter
        // If the clock device brings input streams of its own, they come first; the tap's are the last ones.
        let wanted = tapFormat.isInterleaved ? 1 : Int(tapFormat.channelCount)
        let subset = AudioBufferList.allocate(maximumBuffers: wanted)
        tapBuffers = subset
        try CA.check(AudioDeviceCreateIOProcIDWithBlock(&procID, device, io) { [weak self] _, input, _, _, _ in
            let all = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
            var list = input
            if all.count > wanted {
                for i in 0..<wanted { subset[i] = all[all.count - wanted + i] }
                list = UnsafePointer(subset.unsafePointer)
            }
            guard let self, let buffer = AVAudioPCMBuffer(pcmFormat: tapFormat, bufferListNoCopy: list, deallocator: nil) else { return }
            let samples = converter.convert(buffer)
            if !samples.isEmpty { self.onAudio?(samples) }
        }, "start listening to other apps")
        try CA.check(AudioDeviceStart(device, procID), "start listening to other apps")
    }

    private func stopDevice() {
        if deviceID != kAudioObjectUnknown {
            if let procID {
                AudioDeviceStop(deviceID, procID)
                AudioDeviceDestroyIOProcID(deviceID, procID)
            }
            AudioHardwareDestroyAggregateDevice(deviceID)
        }
        procID = nil
        deviceID = AudioObjectID(kAudioObjectUnknown)
        if let tapBuffers { free(tapBuffers.unsafeMutablePointer) }  // AudioBufferList.allocate uses malloc
        tapBuffers = nil
    }

    /// Plugging in headphones (or AirPods connecting) changes the output device and possibly the tap's
    /// format; rebuild the capture device so both are right.
    private func watchOutputDevice() {
        var address = CA.address(kAudioHardwarePropertyDefaultOutputDevice)
        let listener: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            guard let self, self.tapID != kAudioObjectUnknown else { return }
            self.stopDevice()
            try? self.startDevice()
        }
        if AudioObjectAddPropertyListenerBlock(CA.system, &address, control, listener) == noErr {
            outputListener = listener
        }
    }

    /// Apps start extra audio processes mid-call (and some restart them); keep the tap's list current.
    private func watchApps() {
        let timer = DispatchSource.makeTimerSource(queue: control)
        timer.schedule(deadline: .now() + 3, repeating: 3)
        timer.setEventHandler { [weak self] in
            guard let self, self.tapID != kAudioObjectUnknown else { return }
            let before = self.tapped
            let description = self.makeDescription()
            guard self.tapped != before else { return }
            var address = CA.address(kAudioTapPropertyDescription)
            var reference = Unmanaged.passUnretained(description)
            AudioObjectSetPropertyData(self.tapID, &address, 0, nil, UInt32(MemoryLayout<Unmanaged<CATapDescription>>.size), &reference)
        }
        timer.resume()
        refresh = timer
    }
}
