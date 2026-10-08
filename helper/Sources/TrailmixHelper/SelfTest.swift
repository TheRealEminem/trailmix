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
        _ = NSApplication.shared  // the window step needs a real app (WebKit)
        NSApp.setActivationPolicy(.accessory)
        Task {
            await check(model)
            if !args.contains("--no-window") { await windowRecord(model) }
            print(failures == 0 ? "All checks passed." : "\(failures) check(s) failed.")
            exit(failures == 0 ? 0 : 1)
        }
        NSApp.run()
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

        print("5. The Record button in the Trailmix window starts a recording here")
        await model.checkIn()
        let base = model.serverOverride!
        let status = try? JSONSerialization.jsonObject(with: (try? await URLSession.shared.data(from: base.appendingPathComponent("api/recorder")))?.0 ?? Data()) as? [String: Any]
        expect(status?["available"] as? Bool == true, "the window sees the recorder (\(status?["mic"] as? String ?? "?"), \(status?["source"] as? String ?? "?"))")
        var press = URLRequest(url: base.appendingPathComponent("api/recorder/start"))
        press.httpMethod = "POST"
        let answer = try? await URLSession.shared.data(for: press)
        expect((answer?.1 as? HTTPURLResponse)?.statusCode == 202, "Record pressed in the window")
        await model.checkIn()
        expect(await waitUntil(20) { model.phase == .recording }, "recording started from the window")
        try? await Task.sleep(for: .seconds(2))
        model.toggle()
        expect(await waitUntil(15) { model.phase == .idle }, "stopped cleanly")
    }

    private static func get(_ url: URL) async -> Data? {
        guard let (data, response) = try? await URLSession.shared.data(from: url),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return data
    }

    private static func waitUntilAsync(_ seconds: Double, _ condition: () async -> Bool) async -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            if await condition() { return true }
            try? await Task.sleep(for: .milliseconds(300))
        }
        return await condition()
    }

    /// The real Trailmix window and page, the way you use them: Record clicked in the window right after a
    /// (re)start, before the menu bar recorder has checked in; the recording must still go through the
    /// recorder, never the page's own capture. Then the transcript (made while recording) must arrive.
    private static func windowRecord(_ model: HelperModel) async {
        let base = model.serverOverride!
        print("6. Record in the real window, clicked before the recorder has checked in")
        var put = URLRequest(url: base.appendingPathComponent("api/settings"))
        put.httpMethod = "PUT"
        put.setValue("application/json", forHTTPHeaderField: "Content-Type")
        put.httpBody = Data(#"{"live_final": true}"#.utf8)
        _ = try? await URLSession.shared.data(for: put)

        MainWindow.shared.offscreen = true
        MainWindow.shared.show(base)
        let loaded = await waitUntilAsync(40) { await MainWindow.shared.evaluate("!!document.querySelector('[data-testid=record]')") == "true" }
        expect(loaded, "the window shows the Record button")

        // The page the window runs must be the one the server has now (not a cached older version).
        let html = String(data: await get(base) ?? Data(), encoding: .utf8) ?? ""
        let script = await MainWindow.shared.evaluate("document.querySelector('script[src*=\"assets/\"]')?.getAttribute('src')")
        let src = (try? JSONSerialization.jsonObject(with: Data(script.utf8), options: .fragmentsAllowed)) as? String ?? "?"
        expect(src != "?" && html.contains(src), "the window runs the current web app (\(src))")

        try? await Task.sleep(for: .seconds(6))  // the server now considers the recorder gone, as after a restart
        _ = await MainWindow.shared.evaluate("document.querySelector('[data-testid=record]').click()")
        let checkIns = Task { @MainActor in
            try? await Task.sleep(for: .seconds(3))  // the recorder comes back a few seconds later
            while !Task.isCancelled {
                await model.checkIn()
                try? await Task.sleep(for: .seconds(1))
            }
        }
        expect(await waitUntil(30) { model.phase == .recording }, "the recorder started (\(model.error ?? "no error"))")
        let id = model.meetingID ?? -1
        let pageError = await MainWindow.shared.evaluate("document.body.innerText.includes(\"Couldn't start recording\")")
        expect(pageError == "false", "the window shows no recording error")
        try? await Task.sleep(for: .seconds(8))
        let stopShown = await waitUntilAsync(10) { await MainWindow.shared.evaluate("!!document.querySelector('[data-testid=stop]')") == "true" }
        expect(stopShown, "the window shows the recording")
        _ = await MainWindow.shared.evaluate("document.querySelector('[data-testid=stop]').click()")
        expect(await waitUntil(20) { model.phase == .idle }, "Stop in the window stopped the recorder")
        checkIns.cancel()

        var transcript = ""
        let done = await waitUntilAsync(180) {
            guard let data = await get(base.appendingPathComponent("api/meetings/\(id)")),
                  let m = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
            if m["status"] as? String == "error" { transcript = "error: \(m["error"] ?? "")"; return true }
            transcript = m["transcript"] as? String ?? ""
            return !transcript.isEmpty
        }
        expect(done && !transcript.hasPrefix("error") && !transcript.isEmpty,
               "the transcript arrived: \(transcript.prefix(80).replacingOccurrences(of: "\n", with: " "))")
    }

    /// Records other apps' audio for a few seconds and reports what arrived: the formats and buffer layout
    /// (TRAILMIX_AUDIO_DEBUG is turned on), and how many wire samples per second came out, which must be
    /// 16000. Without System Audio Recording permission the audio is silent, but the layout is still real.
    ///   TrailmixHelper --capture-test 6 /tmp/them.wav
    static func captureTest(_ args: [String]) {
        let rest = Array(args.drop { $0 != "--capture-test" }.dropFirst())
        let seconds = Double(rest.first ?? "") ?? 6
        setenv("TRAILMIX_AUDIO_DEBUG", "1", 1)
        let capture = SystemAudioCapture(target: .allApps)
        let lock = NSLock()
        var samples: [Int16] = []
        capture.onAudio = { chunk in lock.lock(); samples += chunk; lock.unlock() }
        do { try capture.start() } catch {
            print("couldn't start: \(error.localizedDescription)")
            exit(1)
        }
        Thread.sleep(forTimeInterval: seconds)
        capture.stop()
        lock.lock()
        let got = samples
        lock.unlock()
        let peak = got.map { abs(Int($0)) }.max() ?? 0
        print(String(format: "%d samples in %.1f s = %.0f per second (should be 16000); peak %d", got.count, seconds, Double(got.count) / seconds, peak))
        if rest.count > 1 {
            var wav = Data("RIFF".utf8)
            func le<T: FixedWidthInteger>(_ v: T) { withUnsafeBytes(of: v.littleEndian) { wav.append(contentsOf: $0) } }
            le(UInt32(36 + got.count * 2)); wav += Data("WAVEfmt ".utf8)
            le(UInt32(16)); le(UInt16(1)); le(UInt16(1)); le(UInt32(16000)); le(UInt32(32000)); le(UInt16(2)); le(UInt16(16))
            wav += Data("data".utf8); le(UInt32(got.count * 2))
            got.withUnsafeBytes { wav.append(contentsOf: $0) }
            try? wav.write(to: URL(fileURLWithPath: rest[1]))
            print("saved \(rest[1])")
        }
        exit(0)
    }

    /// Opens the Trailmix window off screen at a URL and saves what it shows: the in-app window, checked
    /// without anyone clicking.  TrailmixHelper --window-test http://127.0.0.1:8765/#meeting-2 /tmp/window.png
    static func windowTest(_ args: [String]) {
        let rest = Array(args.drop { $0 != "--window-test" }.dropFirst())
        guard rest.count >= 2, let url = URL(string: rest[0]) else {
            print("usage: --window-test <url> <out.png> [seconds]")
            exit(2)
        }
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)
        MainWindow.shared.offscreen = true
        MainWindow.shared.show(url)
        Task {
            let ok = await MainWindow.shared.snapshot(to: rest[1], after: Double(rest.count > 2 ? rest[2] : "") ?? 4)
            print(ok ? "saved \(rest[1])" : "couldn't take a snapshot")
            exit(ok ? 0 : 1)
        }
        NSApp.run()
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
