import AppKit

/// "Zoom call started. Record it?": a small card at the top right of the screen when a call app starts using
/// the microphone and nothing is recording. And during a recording, once the call app has let go of the mic and
/// the speakers for a while, "The call seems to have ended. Stop recording?". The card never takes focus from
/// the call, shows over full-screen apps, and goes away by itself. Off with "Remind me to record calls"
/// (Settings, or the menu); "Don't ask for Zoom" stops it for one app.
@MainActor
final class CallReminder {
    static let shared = CallReminder()

    private enum Kind { case start, stop }
    private var timer: Timer?
    private var panel: NSPanel?
    private var shown: Kind?
    private var hideTask: Task<Void, Never>?
    private var askedAbout: String?       // the call we already asked about, until it ends
    private var recordingCall: String?    // the call app in use during this recording
    private var quietSince: Date?         // when that app last let go of the mic and speakers
    private var askedToStop = false

    static let checkEvery: TimeInterval = 3
    static let endedAfter: TimeInterval = 60

    /// Browsers people take calls in (Google Meet and the like): only counted while they use the microphone.
    static let browsers: [String: String] = [
        "com.google.Chrome": "Chrome", "company.thebrowser.Browser": "Arc", "com.microsoft.edgemac": "Edge",
        "org.mozilla.firefox": "Firefox", "com.brave.Browser": "Brave", "com.apple.WebKit.GPU": "Safari",
    ]

    func begin() {
        guard timer == nil else { return }
        timer = Timer.scheduledTimer(withTimeInterval: Self.checkEvery, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
    }

    /// The call app (or browser) using the microphone right now, if any. "Using the mic" is what tells a call
    /// from an app that merely plays a sound.
    static func callUsingMic() -> String? {
        for p in AudioProcess.all() where p.isListening && p.pid != getpid() {
            if let name = AudioApp.callApps.first(where: { p.belongs(to: $0.key) })?.value { return name }
            if let name = browsers.first(where: { p.belongs(to: $0.key) })?.value { return "a call in \(name)" }
        }
        return nil
    }

    /// Whether the app still has the mic or the speakers (muted calls often release the mic but keep playing).
    static func stillInCall(_ name: String) -> Bool {
        AudioProcess.all().contains { p in
            (p.isListening || p.isPlaying) && p.pid != getpid()
                && (AudioApp.callApps.contains { p.belongs(to: $0.key) && $0.value == name }
                    || browsers.contains { p.belongs(to: $0.key) && "a call in \($0.value)" == name })
        }
    }

    private func tick() {
        let model = HelperModel.shared
        let prefs = Prefs.shared
        guard prefs.callReminders, model.connection == .online else { hide(); return }
        switch model.phase {
        case .idle where model.other == nil:
            recordingCall = nil
            quietSince = nil
            askedToStop = false
            if shown == .stop { hide() }
            guard let call = Self.callUsingMic() else {
                askedAbout = nil  // the call ended: the next one gets asked about again
                if shown == .start { hide() }
                return
            }
            guard askedAbout != call, !prefs.mutedCallApps.contains(call) else { return }
            askedAbout = call
            show(.start, call)
        case .recording:
            if shown == .start { hide() }
            if let call = Self.callUsingMic() { recordingCall = call }
            guard let call = recordingCall, !askedToStop else { return }
            if Self.stillInCall(call) {
                quietSince = nil
                if shown == .stop { hide() }
                return
            }
            quietSince = quietSince ?? Date()
            if Date().timeIntervalSince(quietSince!) >= Self.endedAfter {
                askedToStop = true
                show(.stop, call)
            }
        default:
            if shown == .start { hide() }
        }
    }

    // MARK: The card

    private func show(_ kind: Kind, _ call: String) {
        hide()
        let isCall = !call.hasPrefix("a call in")
        let title = kind == .start ? (isCall ? "\(call) call started" : "Looks like \(call)") : "The call seems to have ended"
        let detail = kind == .start ? "Record it with Trailmix? You'll get the notes when it ends."
                                    : "\(isCall ? call : "The call") hasn't used the mic or speakers for a minute."
        let (primary, secondary) = kind == .start ? ("Record", "Not now") : ("Stop recording", "Keep recording")

        let primaryButton = Self.button(primary, primary: true) { [weak self] in
            self?.hide()
            let model = HelperModel.shared
            if kind == .start { model.start() } else { model.stop() }
        }
        let secondaryButton = Self.button(secondary, primary: false) { [weak self] in self?.hide() }
        var rows: [NSView] = [Self.label(title, bold: true), Self.label(detail, bold: false),
                              Self.row([primaryButton, secondaryButton])]
        if kind == .start {
            let name = call
            let mute = Self.button(isCall ? "Don't ask for \(call)" : "Don't ask for calls in this browser", primary: false, small: true) { [weak self] in
                Prefs.shared.mutedCallApps.append(name)
                self?.hide()
            }
            mute.contentTintColor = .secondaryLabelColor
            rows.append(mute)
        }
        let stack = NSStackView(views: rows)
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 6
        stack.setCustomSpacing(10, after: rows[1])
        stack.edgeInsets = NSEdgeInsets(top: 14, left: 16, bottom: 12, right: 16)
        stack.translatesAutoresizingMaskIntoConstraints = false

        let background = NSVisualEffectView()
        background.material = .popover
        background.state = .active
        background.wantsLayer = true
        background.layer?.cornerRadius = 14
        background.layer?.masksToBounds = true
        background.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: background.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: background.trailingAnchor),
            stack.topAnchor.constraint(equalTo: background.topAnchor),
            stack.bottomAnchor.constraint(equalTo: background.bottomAnchor),
            stack.widthAnchor.constraint(equalToConstant: 320),
        ])

        let panel = NSPanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isFloatingPanel = true
        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.hidesOnDeactivate = false
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.contentView = background
        panel.setContentSize(background.fittingSize)
        if let screen = NSScreen.main {
            let area = screen.visibleFrame
            panel.setFrameTopLeftPoint(NSPoint(x: area.maxX - panel.frame.width - 14, y: area.maxY - 10))
        }
        panel.alphaValue = 0
        panel.orderFrontRegardless()
        NSAnimationContext.runAnimationGroup { $0.duration = 0.2; panel.animator().alphaValue = 1 }
        if Prefs.shared.sounds { NSSound(named: "Tink")?.play() }
        self.panel = panel
        shown = kind
        hideTask = Task { [weak self] in  // gone by itself if nobody answers
            try? await Task.sleep(for: .seconds(kind == .start ? 45 : 90))
            self?.hide()
        }
    }

    /// Draws the card to a PNG and quits (--reminder-preview out.png [--stop]), so it can be checked by eye.
    static func preview(into path: String, stop: Bool) {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)
        shared.show(stop ? .stop : .start, "Zoom")
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(600))
            guard let view = shared.panel?.contentView, let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { exit(1) }
            view.cacheDisplay(in: view.bounds, to: rep)
            try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
            print("saved \(path)")
            exit(0)
        }
        NSApp.run()
    }

    func hide() {
        hideTask?.cancel()
        hideTask = nil
        guard let panel else { return }
        self.panel = nil
        shown = nil
        NSAnimationContext.runAnimationGroup({ $0.duration = 0.15; panel.animator().alphaValue = 0 }) { panel.orderOut(nil) }
    }

    private static func label(_ text: String, bold: Bool) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = bold ? .systemFont(ofSize: 13, weight: .semibold) : .systemFont(ofSize: 12)
        label.textColor = bold ? .labelColor : .secondaryLabelColor
        label.preferredMaxLayoutWidth = 288
        return label
    }

    private static func row(_ views: [NSView]) -> NSStackView {
        let row = NSStackView(views: views)
        row.orientation = .horizontal
        row.spacing = 8
        return row
    }

    private static func button(_ title: String, primary: Bool, small: Bool = false, action: @escaping () -> Void) -> NSButton {
        let button = ActionButton(title: title, action: action)
        button.bezelStyle = small ? .inline : .rounded
        button.isBordered = !small
        button.controlSize = small ? .small : .regular
        if primary { button.keyEquivalent = "\r" }  // the blue, default look
        return button
    }
}

/// An NSButton that runs a closure.
private final class ActionButton: NSButton {
    private let run: () -> Void

    init(title: String, action: @escaping () -> Void) {
        run = action
        super.init(frame: .zero)
        self.title = title
        target = self
        self.action = #selector(fire)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    @objc private func fire() { run() }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }  // one click works without focusing the card
}
