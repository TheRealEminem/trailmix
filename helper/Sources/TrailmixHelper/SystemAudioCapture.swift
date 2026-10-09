import AppKit
import AVFoundation
import CoreAudio

/// Small typed wrappers around the Core Audio property API.
enum CA {
    static func address(_ selector: AudioObjectPropertySelector, scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    }

    static func value<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, default fallback: T) -> T {
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
    /// it in step), so prefer one without a microphone (running a Bluetooth headset's input would switch
    /// it into its low-quality call mode) and with a steady rate: the built-in speakers, else the default output.
    static func clockDeviceUID() throws -> String {
        let fallback: AudioObjectID = value(system, kAudioHardwarePropertyDefaultOutputDevice, default: AudioObjectID(kAudioObjectUnknown))
        // Built-in speakers first: their rate never changes. A Bluetooth headset's drops (48 → 24 kHz) when a
        // call starts using its mic, and a clock that changes rate mid-call is asking for trouble.
        var candidates = objects(system, kAudioHardwarePropertyDevices).filter {
            value($0, kAudioDevicePropertyTransportType, default: UInt32(0)) == kAudioDeviceTransportTypeBuiltIn
        }
        candidates.append(fallback)
        for device in candidates where device != kAudioObjectUnknown && streamCount(device, kAudioObjectPropertyScopeOutput) > 0
            && streamCount(device, kAudioObjectPropertyScopeInput) == 0 {
            if let uid = string(device, kAudioDevicePropertyDeviceUID) { return uid }
        }
        guard fallback != kAudioObjectUnknown, let uid = string(fallback, kAudioDevicePropertyDeviceUID) else {
            throw CaptureError("There's no audio output device to listen to")
        }
        return uid
    }

    /// The format a device's last input stream really delivers to an IO proc, at the device's current rate.
    static func inputStreamFormat(_ device: AudioObjectID) -> AudioStreamBasicDescription? {
        var address = address(kAudioDevicePropertyStreams, scope: kAudioObjectPropertyScopeInput)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(device, &address, 0, nil, &size) == noErr, size > 0 else { return nil }
        var streams = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(device, &address, 0, nil, &size, &streams) == noErr, let last = streams.last else { return nil }
        var format = value(last, kAudioStreamPropertyVirtualFormat, default: AudioStreamBasicDescription())
        let rate = value(device, kAudioDevicePropertyNominalSampleRate, default: Float64(0))
        if rate > 0 { format.mSampleRate = rate }
        return format.mSampleRate > 0 && format.mChannelsPerFrame > 0 ? format : nil
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
    var isListening = false  // using a microphone

    static func all() -> [AudioProcess] {
        CA.objects(CA.system, kAudioHardwarePropertyProcessObjectList).map { object in
            AudioProcess(
                objectID: object,
                pid: CA.value(object, kAudioProcessPropertyPID, default: pid_t(-1)),
                bundleID: CA.string(object, kAudioProcessPropertyBundleID) ?? "",
                isPlaying: CA.value(object, kAudioProcessPropertyIsRunningOutput, default: UInt32(0)) != 0,
                isListening: CA.value(object, kAudioProcessPropertyIsRunningInput, default: UInt32(0)) != 0
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

    /// Apps people take calls in. Browsers aren't here: they play all sorts of sound.
    static let callApps: [String: String] = [
        "us.zoom.xos": "Zoom", "com.microsoft.teams2": "Microsoft Teams", "com.microsoft.teams": "Microsoft Teams",
        calls: "FaceTime", "com.apple.FaceTime": "FaceTime", "com.cisco.webexmeetingsapp": "Webex",
        "Cisco-Systems.Spark": "Webex", "com.tinyspeck.slackmacgap": "Slack", "com.hnc.Discord": "Discord",
        "net.whatsapp.WhatsApp": "WhatsApp", "com.skype.skype": "Skype", "com.google.meet": "Google Meet",
    ]

    /// A call app that has the mic or the speakers open right now (a call is starting or under way), if any.
    static func callInProgress() -> String? {
        for process in AudioProcess.all() where process.isPlaying || process.isListening {
            if let name = callApps.first(where: { process.belongs(to: $0.key) })?.value { return name }
        }
        return nil
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
    private var formatWatches: [(object: AudioObjectID, address: AudioObjectPropertyAddress, block: AudioObjectPropertyListenerBlock)] = []
    private var pendingRestart: DispatchWorkItem?
    private var refresh: DispatchSourceTimer?
    private var rateCheck: DispatchSourceTimer?
    /// Set when the audio arriving doesn't match the rate Core Audio claims (see `checkRate`).
    private var measuredRate: Double?
    private let counter = FrameCounter()

    init(target: Target) {
        self.target = target
    }

    /// TRAILMIX_AUDIO_DEBUG=1 prints the formats and buffer layout Core Audio hands us (see --capture-test).
    static let debug = ProcessInfo.processInfo.environment["TRAILMIX_AUDIO_DEBUG"] != nil

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
            pendingRestart?.cancel()
            pendingRestart = nil
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

        // Read the format the aggregate device actually delivers, not the tap's own: the tap can say 48 kHz
        // while the device runs at another rate, and believing it made the other side of a call play at
        // double speed (half the samples, padded out with silence).
        let tapASBD = CA.value(tapID, kAudioTapPropertyFormat, default: AudioStreamBasicDescription())
        var format = CA.inputStreamFormat(device) ?? tapASBD
        if let measuredRate {
            format.mSampleRate = measuredRate
        } else if Self.debug, let fake = Double(ProcessInfo.processInfo.environment["TRAILMIX_AUDIO_FAKE_RATE"] ?? "") {
            format.mSampleRate = fake  // tests: pretend Core Audio misreported the rate
        }
        guard let streamFormat = AVAudioFormat(streamDescription: &format), let converter = WireConverter(from: streamFormat) else {
            throw CaptureError("Couldn't read the format of other apps' audio")
        }
        self.converter = converter
        if Self.debug {
            debugLog("clock: \(clock)")
            debugLog("tap format: \(tapASBD.mSampleRate) Hz, \(tapASBD.mChannelsPerFrame) ch, \(tapASBD.mBytesPerFrame) bytes/frame")
            debugLog("reading: \(streamFormat.sampleRate) Hz, \(streamFormat.channelCount) ch, interleaved \(streamFormat.isInterleaved)\(measuredRate == nil ? "" : " (measured rate)")")
        }
        // If the clock device brings input streams of its own, they come first; the tap's are the last ones.
        let wanted = streamFormat.isInterleaved ? 1 : Int(streamFormat.channelCount)
        let subset = AudioBufferList.allocate(maximumBuffers: wanted)
        tapBuffers = subset
        let counter = counter
        counter.reset(rate: streamFormat.sampleRate)
        try CA.check(AudioDeviceCreateIOProcIDWithBlock(&procID, device, io) { [weak self] _, input, _, _, _ in
            let all = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
            if Self.debug, counter.callbacks < 3 {
                debugLog("callback: \(all.count) buffers: " + all.map { "\($0.mNumberChannels) ch × \($0.mDataByteSize) bytes" }.joined(separator: ", "))
            }
            var list = input
            if all.count > wanted {
                for i in 0..<wanted { subset[i] = all[all.count - wanted + i] }
                list = UnsafePointer(subset.unsafePointer)
            }
            guard let self, let buffer = AVAudioPCMBuffer(pcmFormat: streamFormat, bufferListNoCopy: list, deallocator: nil) else { return }
            counter.add(Int(buffer.frameLength))
            let samples = converter.convert(buffer)
            if !samples.isEmpty { self.onAudio?(samples) }
        }, "start listening to other apps")
        try CA.check(AudioDeviceStart(device, procID), "start listening to other apps")
        watchFormat()
        checkRate()
    }

    /// A headset switching modes, or the tap's format changing, changes what arrives: rebuild for the new format.
    private func watchFormat() {
        let watched: [(AudioObjectID, AudioObjectPropertySelector, AudioObjectPropertyScope)] = [
            (deviceID, kAudioDevicePropertyNominalSampleRate, kAudioObjectPropertyScopeGlobal),
            (deviceID, kAudioDevicePropertyStreamConfiguration, kAudioObjectPropertyScopeInput),
            (tapID, kAudioTapPropertyFormat, kAudioObjectPropertyScopeGlobal),
        ]
        for (object, selector, scope) in watched {
            var address = CA.address(selector, scope: scope)
            let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in self?.restartSoon("the audio format changed") }
            if AudioObjectAddPropertyListenerBlock(object, &address, control, block) == noErr {
                formatWatches.append((object, address, block))
            }
        }
    }

    /// Belt and braces: if the audio arriving doesn't match the rate we were told (10% off), trust the
    /// clock and restart at the measured rate. Checked a few seconds in, then every 10 s.
    private func checkRate() {
        rateCheck?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: control)
        timer.schedule(deadline: .now() + 4, repeating: 10)
        timer.setEventHandler { [weak self] in
            guard let self, let measure = self.counter.measure(), measure.seconds >= 3 else { return }
            let rate = measure.rate
            let measured = FrameCounter.standardRates.min { abs($0 - rate) < abs($1 - rate) }!
            if abs(rate / self.counter.expectedRate - 1) > 0.1, abs(measured / rate - 1) < 0.05 {
                if Self.debug { debugLog("audio arrives at \(Int(rate)) Hz, not \(Int(self.counter.expectedRate)): restarting at \(Int(measured))") }
                self.measuredRate = measured
                self.restartSoon("the audio rate was off")
            }
        }
        timer.resume()
        rateCheck = timer
    }

    private func restartSoon(_ why: String) {
        pendingRestart?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.tapID != kAudioObjectUnknown else { return }
            if Self.debug { debugLog("restarting: \(why)") }
            self.stopDevice()
            try? self.startDevice()
        }
        pendingRestart = work
        control.asyncAfter(deadline: .now() + 0.3, execute: work)  // several changes often arrive together
    }

    private func stopDevice() {
        rateCheck?.cancel()
        rateCheck = nil
        for watch in formatWatches {
            var address = watch.address
            AudioObjectRemovePropertyListenerBlock(watch.object, &address, control, watch.block)
        }
        formatWatches = []
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

func debugLog(_ message: String) {
    FileHandle.standardError.write(Data("[audio] \(message)\n".utf8))
}

/// Counts frames as they arrive, to compare the real rate with the one Core Audio reports.
final class FrameCounter {
    static let standardRates: [Double] = [8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000]
    private let lock = NSLock()
    private var frames = 0
    private var since: Double?
    private(set) var callbacks = 0
    private(set) var expectedRate = 48000.0

    func reset(rate: Double) {
        lock.lock(); defer { lock.unlock() }
        frames = 0
        since = nil
        callbacks = 0
        expectedRate = rate
    }

    func add(_ count: Int) {
        lock.lock(); defer { lock.unlock() }
        callbacks += 1
        if since == nil { since = CACurrentMediaTime() } else { frames += count }  // time from the first buffer
    }

    /// Frames per second since the first buffer, and over how long.
    func measure() -> (rate: Double, seconds: Double)? {
        lock.lock(); defer { lock.unlock() }
        guard let since else { return nil }
        let seconds = CACurrentMediaTime() - since
        return seconds > 0 ? (Double(frames) / seconds, seconds) : nil
    }
}
