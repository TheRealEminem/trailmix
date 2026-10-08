import AppKit
import Carbon.HIToolbox
import SwiftUI

/// A value owned by a view. SwiftUI's @State is a compiler macro that the Command Line Tools can't expand
/// (the plugin ships with Xcode), so views here keep their state in one of these, via @StateObject.
final class ViewState<Value>: ObservableObject {
    @Published var value: Value
    init(_ value: Value) { self.value = value }
}

/// What the menu bar icon shows: the Trailmix mark, or a recording dot and timer.
struct StatusItemLabel: View {
    @ObservedObject var model: HelperModel

    var body: some View {
        if let elapsed = model.elapsed {
            HStack(spacing: 3) {
                Image(nsImage: model.flash ? Brand.flagIcon : Brand.recordingIcon)
                Text(clock(elapsed)).monospacedDigit()
            }
        } else {
            Image(nsImage: model.flash ? Brand.flagIcon : model.connection == .offline ? Brand.menuIconOffline : Brand.menuIcon)
        }
    }
}

/// The panel that opens from the menu bar icon.
struct MenuView: View {
    @ObservedObject var model: HelperModel
    @ObservedObject var prefs: Prefs
    let meters: Meters
    @StateObject private var apps = ViewState<[AudioApp]>([])
    @StateObject private var mics = ViewState<[Microphone]>([])
    @StateObject private var showSettings = ViewState(false)

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            VStack(alignment: .leading, spacing: 10) {
                main
                messages
            }
            .padding(14)
            Divider()
            sources
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
            Divider()
            footer
                .padding(6)
        }
        .frame(width: 320)
        .tint(Brand.forest)
        .onAppear {
            model.menuOpen = true
            apps.value = AudioApp.current()
            mics.value = Microphone.all()
            Task { await model.poll() }
        }
        .onDisappear { model.menuOpen = false }
    }

    // MARK: Header

    private var header: some View {
        HStack(spacing: 10) {
            Image(nsImage: NSApp.applicationIconImage)
                .resizable()
                .frame(width: 28, height: 28)
            VStack(alignment: .leading, spacing: 1) {
                Text("Trailmix").font(.system(size: 13, weight: .semibold))
                Text(statusLine).font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Spacer()
            Circle()
                .fill(statusColor)
                .frame(width: 7, height: 7)
                .help(statusLine)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    private var statusLine: String {
        if model.startingServer { return "Starting Trailmix…" }
        switch model.phase {
        case .starting: return "Starting…"
        case .stopping: return "Saving…"
        case .recording: return "Recording"
        case .idle: break
        }
        if model.other != nil { return "Recording in the Trailmix window" }
        switch model.connection {
        case .online: return "Ready · \(prefs.toggleShortcut.display) to record"
        case .offline: return prefs.canStartServer ? "Not running · starts when you record" : "Can't reach Trailmix"
        case .needsToken: return "Needs an access token"
        case .outdated: return "Restart Trailmix to finish updating"
        case .unknown: return "Connecting…"
        }
    }

    private var statusColor: Color {
        if model.phase == .recording || model.other != nil { return Brand.trail }
        switch model.connection {
        case .online: return Brand.forest
        case .offline, .needsToken, .outdated: return Brand.sun
        case .unknown: return .secondary
        }
    }

    // MARK: Recording controls

    @ViewBuilder private var main: some View {
        if model.phase == .recording || model.phase == .stopping {
            RecordingPanel(model: model, prefs: prefs, meters: meters, sourceName: sourceName)
        } else if let other = model.other {
            OtherRecordingPanel(model: model, prefs: prefs, other: other)
        } else {
            Button(action: model.start) {
                HStack(spacing: 8) {
                    if model.phase == .starting {
                        ProgressView().controlSize(.small).tint(.white)
                    } else {
                        Circle().fill(.white).frame(width: 8, height: 8)
                            .overlay(Circle().stroke(.white.opacity(0.35), lineWidth: 3).padding(-3))
                    }
                    Text(model.startingServer ? "Starting Trailmix…" : model.phase == .starting ? "Starting…" : "Start recording")
                    Spacer()
                    Text(prefs.toggleShortcut.display).font(.system(size: 11, weight: .medium)).opacity(0.8)
                }
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(.white)
                .padding(.horizontal, 12)
                .frame(height: 34)
                .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Brand.trail.gradient))
                .shadow(color: Brand.trail.opacity(0.35), radius: 4, y: 2)
                .contentShape(Rectangle())
            }
            .buttonStyle(PressableStyle())
            .disabled(model.phase != .idle || model.connection == .needsToken)
            if model.connection == .offline, !prefs.canStartServer {
                Text("Can't reach \(prefs.server). Check the address in Settings.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            if model.connection == .outdated {
                Text("Recording works, but the Trailmix window can't follow it or stop it until Trailmix's server restarts.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var sourceName: String {
        switch prefs.source {
        case .allApps: return "all apps"
        case .none: return ""
        case .app(let id): return apps.value.first { $0.id == id }?.name ?? appName(id)
        }
    }

    @ViewBuilder private var messages: some View {
        if let error = model.error {
            MessageRow(text: error, tone: .error) { model.error = nil }
        }
        if let notice = model.notice {
            MessageRow(text: notice, tone: .warning) { model.notice = nil }
        }
        if let problem = model.shortcutProblem {
            MessageRow(text: problem, tone: .warning, dismiss: nil)
        }
    }

    // MARK: Sources

    private var sources: some View {
        VStack(spacing: 8) {
            SettingRow("Microphone") {
                Picker("Microphone", selection: $prefs.micID) {
                    Text("System default").tag("")
                    if !prefs.micID.isEmpty, !mics.value.contains(where: { $0.id == prefs.micID }) {
                        Text("Unplugged mic").tag(prefs.micID)
                    }
                    ForEach(mics.value) { Text($0.name).tag($0.id) }
                }
            }
            SettingRow("Meeting audio") {
                Picker("Meeting audio", selection: Binding(get: { prefs.source.raw }, set: { prefs.source = MeetingSource($0) })) {
                    Text("All apps").tag("all")
                    Text("None (in person)").tag("none")
                    Divider()
                    ForEach(apps.value) { app in
                        Text(app.isPlaying ? "\(app.name) · playing" : app.name).tag(app.id)
                    }
                    if case .app(let id) = prefs.source, !apps.value.contains(where: { $0.id == id }) {
                        Text("\(appName(id)) (not open)").tag(id)
                    }
                }
            }
            SettingRow("Live draft") {
                Toggle("Live draft", isOn: $prefs.liveDraft)
                    .toggleStyle(.switch)
                    .controlSize(.mini)
            }
            if model.phase == .recording {
                Text("Changes apply to your next recording.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .labelsHidden()
        .disabled(model.phase != .idle)
    }

    private func appName(_ bundleID: String) -> String {
        if bundleID == AudioApp.calls { return "FaceTime & iPhone calls" }
        if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) {
            return FileManager.default.displayName(atPath: url.path).replacingOccurrences(of: ".app", with: "")
        }
        return bundleID
    }

    // MARK: Footer

    private var footer: some View {
        VStack(spacing: 0) {
            UpdateRow(updater: Updater.shared)
            MenuRow(title: "Open Trailmix", icon: "macwindow") { model.openTrailmix() }
            if let id = model.lastMeetingID {
                MenuRow(title: "Open last recording", icon: "doc.text") { model.openTrailmix(meeting: id) }
            }
            MenuRow(title: "Settings", icon: "gearshape", trailing: showSettings.value ? "▾" : "▸") {
                withAnimation(.easeOut(duration: 0.18)) { showSettings.value.toggle() }
            }
            if showSettings.value {
                SettingsPanel(model: model, prefs: prefs)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 8)
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
            MenuRow(title: "Quit \(Brand.appName)", icon: "power") { NSApp.terminate(nil) }
                .disabled(model.phase != .idle)
                .help(model.phase != .idle ? "Stop the recording first" : "")
        }
    }
}

// MARK: - Recording

private struct RecordingPanel: View {
    @ObservedObject var model: HelperModel
    @ObservedObject var prefs: Prefs
    @ObservedObject var meters: Meters
    let sourceName: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                PulsingDot()
                Text(model.phase == .stopping ? "Saving…" : "Recording").font(.system(size: 13, weight: .semibold))
                Spacer()
                Text(clock(model.elapsed ?? 0))
                    .font(.system(size: 20, weight: .semibold, design: .rounded))
                    .monospacedDigit()
            }
            MeterRow(label: "You", detail: model.micName, level: meters.mic, color: Brand.sky)
            if model.capturingMeetingAudio {
                MeterRow(label: "Them", detail: sourceName, level: meters.meeting, color: Brand.berry)
            }
            DraftPreview(lines: model.drafts, enabled: prefs.liveDraft)
            HStack(spacing: 8) {
                Button(action: model.markMoment) {
                    Label { Text("Mark") + Text("  \(prefs.markShortcut.display)").foregroundStyle(.secondary).font(.system(size: 11)) } icon: {
                        Image(systemName: "flag").foregroundStyle(Brand.sunInk)
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(SoftButtonStyle())
                Button(action: model.stop) {
                    Label(model.phase == .stopping ? "Saving…" : "Stop", systemImage: "stop.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(RecordButtonStyle())
                .disabled(model.phase == .stopping)
            }
            if !model.marks.isEmpty {
                Text("\(model.marks.count) moment\(model.marks.count == 1 ? "" : "s") flagged")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
        }
    }
}

/// A recording the Trailmix window is capturing: the hotkeys and these buttons work on it too.
private struct OtherRecordingPanel: View {
    @ObservedObject var model: HelperModel
    @ObservedObject var prefs: Prefs
    let other: LiveRecording

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                PulsingDot()
                Text(other.client == "web" ? "Recording in Trailmix" : "Recording elsewhere").font(.system(size: 13, weight: .semibold))
                Spacer()
                Text(clock(model.elapsed ?? other.duration))
                    .font(.system(size: 20, weight: .semibold, design: .rounded))
                    .monospacedDigit()
            }
            DraftPreview(lines: other.drafts, enabled: other.draft)
            HStack(spacing: 8) {
                Button(action: model.markMoment) {
                    Label("Mark", systemImage: "flag").frame(maxWidth: .infinity)
                }
                .buttonStyle(SoftButtonStyle())
                Button { model.stopOther(other) } label: {
                    Label("Stop", systemImage: "stop.fill").frame(maxWidth: .infinity)
                }
                .buttonStyle(RecordButtonStyle())
            }
            Text("\(prefs.markShortcut.display) marks and \(prefs.toggleShortcut.display) stops this recording too.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
        }
    }
}

private struct DraftPreview: View {
    let lines: [DraftLine]
    let enabled: Bool

    var body: some View {
        if let last = lines.last {
            HStack(alignment: .top, spacing: 6) {
                if let speaker = last.speaker {
                    Text(speaker)
                        .font(.system(size: 10, weight: .semibold))
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(Capsule().fill((speaker == "You" ? Brand.sky : Brand.berry).opacity(0.18)))
                        .foregroundStyle(speaker == "You" ? Brand.sky : Brand.berry)
                }
                Text(last.text)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .id(lines.count)
            .transition(.opacity)
        } else if enabled {
            Text("The live draft appears here after a few seconds of speech.")
                .font(.system(size: 11)).foregroundStyle(.tertiary)
        }
    }
}

private struct MeterRow: View {
    let label: String
    let detail: String
    let level: Float
    let color: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(label).font(.system(size: 11, weight: .semibold))
                Text(detail).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1).truncationMode(.tail)
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.primary.opacity(0.08))
                    Capsule().fill(color).frame(width: max(4, geo.size.width * CGFloat(level)))
                        .animation(.linear(duration: 0.08), value: level)
                }
            }
            .frame(height: 5)
        }
    }
}

private struct PulsingDot: View {
    var body: some View {
        Image(systemName: "circle.fill")
            .font(.system(size: 8))
            .foregroundStyle(Brand.trail)
            .symbolEffect(.pulse, options: .repeating)
            .frame(width: 14, height: 14)
    }
}

// MARK: - Messages

private struct MessageRow: View {
    enum Tone { case error, warning }
    let text: String
    let tone: Tone
    let dismiss: (() -> Void)?

    private var settingsURL: URL? {
        if text.contains("Screen & System Audio Recording") {
            return URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")
        }
        if text.contains("Privacy & Security → Microphone") {
            return URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")
        }
        return nil
    }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: tone == .error ? "exclamationmark.triangle.fill" : "info.circle.fill")
                .foregroundStyle(tone == .error ? Brand.danger : Brand.sunInk)
                .font(.system(size: 12))
            VStack(alignment: .leading, spacing: 6) {
                Text(text).font(.system(size: 11.5)).fixedSize(horizontal: false, vertical: true)
                if let settingsURL {
                    Button("Open System Settings") { NSWorkspace.shared.open(settingsURL) }
                        .buttonStyle(.link)
                        .font(.system(size: 11.5, weight: .medium))
                }
            }
            Spacer(minLength: 0)
            if let dismiss {
                Button(action: dismiss) { Image(systemName: "xmark").font(.system(size: 9, weight: .bold)) }
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
                    .help("Dismiss")
            }
        }
        .padding(9)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill((tone == .error ? Brand.danger : Brand.sun).opacity(0.12)))
    }
}

// MARK: - Settings

private struct SettingsPanel: View {
    @ObservedObject var model: HelperModel
    @ObservedObject var prefs: Prefs
    @StateObject private var server = ViewState("")
    @StateObject private var token = ViewState("")

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Trailmix address").font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                TextField("http://127.0.0.1:8765", text: $server.value)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(save)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text("Access token").font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                SecureField("Only for a hosted Trailmix", text: $token.value)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(save)
            }
            if server.value != prefs.server || token.value != prefs.token {
                Button("Save", action: save).controlSize(.small)
            }
            Divider()
            SettingRow("Start / stop") { ShortcutField(shortcut: $prefs.toggleShortcut, model: model) }
            SettingRow("Mark moment") { ShortcutField(shortcut: $prefs.markShortcut, model: model) }
            SettingRow("Sounds") { Toggle("Sounds", isOn: $prefs.sounds).toggleStyle(.switch).controlSize(.mini) }
            SettingRow("Open at login") {
                Toggle("Open at login", isOn: Binding(get: { prefs.openAtLogin }, set: { prefs.openAtLogin = $0 }))
                    .toggleStyle(.switch)
                    .controlSize(.mini)
            }
        }
        .labelsHidden()
        .font(.system(size: 12))
        .onAppear {
            server.value = prefs.server
            token.value = prefs.token
        }
    }

    private func save() {
        prefs.server = server.value.trimmingCharacters(in: .whitespaces)
        prefs.token = token.value.trimmingCharacters(in: .whitespaces)
        Task { await model.poll() }
    }
}

/// Click, then press the new shortcut. Escape cancels.
private struct ShortcutField: View {
    @Binding var shortcut: Shortcut
    let model: HelperModel
    @StateObject private var listener = ShortcutListener()

    var body: some View {
        Button(listener.listening ? "Type shortcut…" : shortcut.display) {
            if listener.listening {
                listener.finish()
            } else {
                listener.listen(model: model) { shortcut = $0 }
            }
        }
        .controlSize(.small)
        .frame(minWidth: 96)
        .onDisappear { listener.finish() }
    }
}

private final class ShortcutListener: ObservableObject {
    @Published var listening = false
    private var monitor: Any?
    private weak var model: HelperModel?

    @MainActor
    func listen(model: HelperModel, onShortcut: @escaping (Shortcut) -> Void) {
        self.model = model
        listening = true
        model.pauseHotKeys()  // so pressing the current shortcut doesn't start a recording
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            MainActor.assumeIsolated {
                if event.keyCode == UInt16(kVK_Escape) {
                    self?.finish()
                } else if let new = Shortcut(event: event) {
                    onShortcut(new)
                    self?.finish()
                }
            }
            return nil
        }
    }

    @MainActor
    func finish() {
        guard listening else { return }
        listening = false
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
        model?.registerHotKeys()
    }
}

// MARK: - Building blocks

private struct SettingRow<Control: View>: View {
    let title: String
    @ViewBuilder let control: Control

    init(_ title: String, @ViewBuilder control: () -> Control) {
        self.title = title
        self.control = control()
    }

    var body: some View {
        HStack {
            Text(title).font(.system(size: 12))
            Spacer()
            control.frame(maxWidth: 170, alignment: .trailing)
        }
    }
}

private struct MenuRow: View {
    let title: String
    let icon: String
    var trailing: String?
    let action: () -> Void
    @StateObject private var hover = ViewState(false)
    @Environment(\.isEnabled) private var enabled

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Image(systemName: icon).frame(width: 16).foregroundStyle(.secondary)
                Text(title)
                Spacer()
                if let trailing { Text(trailing).foregroundStyle(.secondary) }
            }
            .font(.system(size: 13))
            .padding(.horizontal, 8)
            .frame(height: 26)
            .background(RoundedRectangle(cornerRadius: 5, style: .continuous).fill(hover.value && enabled ? Color.primary.opacity(0.08) : .clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .opacity(enabled ? 1 : 0.45)
        .onHover { hover.value = $0 }
    }
}

private struct PressableStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .brightness(configuration.isPressed ? -0.03 : 0)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

private struct SoftButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12.5, weight: .medium))
            .frame(height: 30)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.primary.opacity(configuration.isPressed ? 0.12 : 0.07)))
            .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).stroke(Color.primary.opacity(0.08)))
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

private struct RecordButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12.5, weight: .semibold))
            .foregroundStyle(.white)
            .frame(height: 30)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Brand.trail.gradient))
            .brightness(configuration.isPressed ? -0.04 : 0)
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

/// "Update to 0.4.0" in the menu when a new version is out (and what's happening while it installs).
struct UpdateRow: View {
    @ObservedObject var updater: Updater

    var body: some View {
        switch updater.state {
        case .idle, .checking, .upToDate, .offline:
            EmptyView()
        case .available(let version):
            MenuRow(title: "Update to Trailmix \(version)", icon: "arrow.down.circle") { updater.install() }
        case .downloading(let version, let progress):
            MenuRow(title: "Downloading \(version)… \(progress.map { "\(Int($0 * 100))%" } ?? "")", icon: "arrow.down.circle") {}
                .disabled(true)
        case .installing(let version):
            MenuRow(title: "Installing \(version)…", icon: "arrow.down.circle") {}
                .disabled(true)
        case .failed(let why):
            MenuRow(title: "Update failed: \(why)", icon: "exclamationmark.triangle") { updater.install() }
                .help("Click to try again")
        }
    }
}
