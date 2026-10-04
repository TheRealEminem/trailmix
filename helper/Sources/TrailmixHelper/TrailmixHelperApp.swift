import AppKit
import SwiftUI

@main
enum Entry {
    @MainActor
    static func main() {
        let args = CommandLine.arguments
        if args.contains("--self-test") { return SelfTest.run(args) }
        if let i = args.firstIndex(of: "--snapshot"), i + 1 < args.count { return Snapshot.render(into: args[i + 1]) }
        if args.contains("--list-audio") { return SelfTest.listAudio() }
        if args.contains("--capture-test") { return SelfTest.captureTest(args) }
        if args.contains("--window-test") { return SelfTest.windowTest(args) }
        TrailmixHelperApp.main()
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)  // menu bar only, even when run outside the .app bundle
        // Opened by you (not by "open at login"): show the Trailmix window, since a menu bar icon alone looks like nothing happened.
        let event = NSAppleEventManager.shared().currentAppleEvent
        let atLogin = event?.paramDescriptor(forKeyword: keyAEPropData)?.enumCodeValue == keyAELaunchedAsLogInItem
        MainActor.assumeIsolated {
            let model = HelperModel.shared
            model.begin()
            guard Bundled.isBundled else { return }
            if atLogin {
                Task { await model.startServerIfNeeded() }
            } else {
                model.openTrailmix()
            }
        }
    }

    /// Double-clicking Trailmix again (or opening it from Spotlight) brings up the window.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        MainActor.assumeIsolated { if Bundled.isBundled { HelperModel.shared.openTrailmix() } }
        return true
    }

    /// Quitting mid-recording would cut the meeting short: ask first.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        MainActor.assumeIsolated {
            let model = HelperModel.shared
            guard model.phase != .idle || model.other != nil else { return .terminateNow }
            let alert = NSAlert()
            alert.messageText = "Trailmix is recording"
            alert.informativeText = "Quitting now stops the recording. What's been recorded so far is kept and processed next time."
            alert.addButton(withTitle: "Keep Recording")
            alert.addButton(withTitle: "Quit Anyway")
            return alert.runModal() == .alertSecondButtonReturn ? .terminateNow : .terminateCancel
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated { BundledServer.shared.stop() }
    }
}

struct TrailmixHelperApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = HelperModel.shared

    var body: some Scene {
        MenuBarExtra {
            MenuView(model: model, prefs: model.prefs, meters: model.meters)
        } label: {
            StatusItemLabel(model: model)
        }
        .menuBarExtraStyle(.window)
    }
}

extension HelperModel {
    static let shared = HelperModel()
}
