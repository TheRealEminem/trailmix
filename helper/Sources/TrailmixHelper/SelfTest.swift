import AppKit
import SwiftUI

/// Checks the helper without microphone or system-audio permission: audio files stand in for the
/// mic and the meeting, and everything else is the real thing (model, hotkey actions, WebSocket, server).
///
///   TrailmixHelper --self-test http://127.0.0.1:8790 me.wav them.wav
///   TrailmixHelper --list-audio
///   TrailmixHelper --snapshot /tmp/shots
@MainActor
enum SelfTest {
    static func run(_ args: [String]) {
        let rest = Array(args.drop { $0 != "--self-test" }.dropFirst())
        guard rest.count >= 2, let server = URL(string: rest[0]) else {
            print("usage: --self-test <server> <mic audio> [<meeting audio>]")
            exit(2)
        }
        let model = HelperModel()
        model.serverOverride = server
        model.needsMicPermission = false
        model.makeSources = { _ in
            var sources: [(UInt8, AudioSource)] = [(Wire.mic, try FileSource(path: rest[1]))]
            if rest.count > 2 { sources.append((Wire.meetingAudio, try FileSource(path: rest[2]))) }
            return sources
        }
        Task {
            await check(model)
            exit(failures == 0 ? 0 : 1)
        }
        RunLoop.main.run()
    }

    private static var failures = 0

    private static func expect(_ ok: Bool, _ what: String) {
        print(ok ? "  ok    \(what)" : "  FAIL  \(what)")
        if !ok { failures += 1 }
    }

    private static func waitUntil(_ seconds: Double, _ condition: () -> Bool) async -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            if condition() { return true }
            try? await Task.sleep(for: .milliseconds(100))
        }
        return condition()
    }

    private static func check(_ model: HelperModel) async {
        await model.poll()
        expect(model.connection == .online, "reaches the server (\(model.connection))")

        print("1. Start/stop hotkey starts a recording")
        model.toggle()
        expect(await waitUntil(20) { model.phase == .recording }, "recording (\(model.error ?? "no error"))")
        let first = model.meetingID
        try? await Task.sleep(for: .seconds(5))
        print("2. Mark hotkey flags a moment")
        model.markMoment()
        expect(await waitUntil(5) { !model.marks.isEmpty }, "server confirmed the mark: \(model.marks)")
        expect(await waitUntil(25) { !model.drafts.isEmpty }, "live draft arrived: \(model.drafts.first?.text.prefix(60) ?? "none")")
        print("3. Start/stop hotkey stops it")
        model.toggle()
        expect(await waitUntil(15) { model.phase == .idle }, "stopped cleanly")
        expect(model.lastMeetingID == first && model.error == nil, "meeting \(first ?? -1) saved, no error")

        print("4. A recording stopped from the Trailmix window ends here too")
        model.toggle()
        expect(await waitUntil(20) { model.phase == .recording }, "recording again")
        let second = model.meetingID ?? -1
        try? await Task.sleep(for: .seconds(3))
        var stop = URLRequest(url: model.serverOverride!.appendingPathComponent("api/meetings/\(second)/stop"))
        stop.httpMethod = "POST"
        _ = try? await URLSession.shared.data(for: stop)
        expect(await waitUntil(10) { model.phase == .idle }, "stopped from outside")
        expect(model.error == nil, "no error shown (\(model.error ?? ""))")
        print(failures == 0 ? "All checks passed." : "\(failures) check(s) failed.")
    }

    /// What the meeting-audio menu would offer right now. Needs no permission.
    static func listAudio() {
        print("Microphones:")
        for mic in Microphone.all() { print("  \(mic.name)") }
        print("Apps connected to audio:")
        for app in AudioApp.current() { print("  \(app.name) [\(app.id)]\(app.isPlaying ? "  playing" : "")") }
        print("Audio processes: \(AudioProcess.all().count); anything playing: \(AudioApp.anythingPlaying())")
        print("Meeting audio is clocked by: \((try? CA.clockDeviceUID()) ?? "no output device")")
    }
}

/// Renders the menu in several states to PNGs, so the layout can be checked without clicking around.
@MainActor
enum Snapshot {
    static func render(into folder: String) {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        try? FileManager.default.createDirectory(atPath: folder, withIntermediateDirectories: true)
        for dark in [false, true] {
            for state in HelperModel.PreviewState.allCases {
                let model = HelperModel()
                model.preview(state)
                let view = MenuView(model: model, prefs: model.prefs, meters: model.meters)
                    .background(Color(nsColor: .windowBackgroundColor))
                let host = NSHostingView(rootView: view)
                host.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
                host.frame.size = host.fittingSize
                let window = NSWindow(contentRect: host.frame, styleMask: .borderless, backing: .buffered, defer: false)
                window.appearance = host.appearance
                window.contentView = host
                host.layoutSubtreeIfNeeded()
                guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { continue }
                host.cacheDisplay(in: host.bounds, to: rep)
                let path = "\(folder)/\(state)\(dark ? "-dark" : "").png"
                try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
                print("saved \(path)")
            }
        }
        for (name, image) in [("icon", Brand.menuIcon), ("icon-offline", Brand.menuIconOffline)] {
            let size = NSSize(width: 72, height: 72)
            let big = NSImage(size: size, flipped: false) { rect in
                NSColor.white.setFill()
                rect.fill()
                image.draw(in: rect.insetBy(dx: 0, dy: 0))
                return true
            }
            if let tiff = big.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff) {
                try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: "\(folder)/\(name).png"))
            }
        }
    }
}
