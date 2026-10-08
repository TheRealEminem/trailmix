import AppKit
import WebKit

/// Trailmix's own window: the web app in a WebKit view, so no browser is involved. While it's open the app
/// has a Dock icon and a menu bar (Edit for copy and paste, View → Reload); closing it leaves just the menu
/// bar recorder. Links to other sites open in your browser.
@MainActor
final class MainWindow: NSObject, NSWindowDelegate, WKUIDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    static let shared = MainWindow()

    private var window: NSWindow?
    private var webView: WKWebView?
    /// Tests put the window off screen so it doesn't flash up while they take a snapshot.
    var offscreen = false

    var isOpen: Bool { window?.isVisible ?? false }

    /// Shows the window at `url`. If it already shows that server, only the part after # changes (a meeting),
    /// so the page doesn't reload.
    func show(_ url: URL) {
        let web = webView ?? build()
        if let current = web.url, current.host == url.host, current.port == url.port, current.scheme == url.scheme {
            if let fragment = url.fragment {
                web.evaluateJavaScript("location.hash = \(Self.jsString("#" + fragment))")
            }
        } else {
            web.load(URLRequest(url: url, cachePolicy: .reloadRevalidatingCacheData))  // never a stale page
        }
        guard let window else { return }
        if offscreen {
            window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
            window.orderBack(nil)
            return
        }
        if NSApp.activationPolicy() != .regular {
            NSApp.setActivationPolicy(.regular)
            NSApp.mainMenu = Self.menu()
        }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    private func build() -> WKWebView {
        let config = WKWebViewConfiguration()
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "dev"
        // A new version: forget pages cached by the old one (versions before 0.6.2 let the page be cached, so
        // after an update the window could keep running the previous version's web app).
        if UserDefaults.standard.string(forKey: "windowCacheVersion") != version {
            WKWebsiteDataStore.default().removeData(ofTypes: [WKWebsiteDataTypeDiskCache, WKWebsiteDataTypeMemoryCache],
                                                    modifiedSince: .distantPast) {}
            UserDefaults.standard.set(version, forKey: "windowCacheVersion")
        }
        config.applicationNameForUserAgent = "TrailmixApp/\(version)"  // the web app adapts (e.g. Quit quits the app)
        config.mediaTypesRequiringUserActionForPlayback = []
        config.userContentController.add(self, name: "trailmix")

        let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 1280, height: 860), configuration: config)
        web.uiDelegate = self
        web.navigationDelegate = self
        web.allowsBackForwardNavigationGestures = false
        web.setValue(false, forKey: "drawsBackground")  // no white flash before the page paints

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false
        )
        window.title = "Trailmix"
        window.minSize = NSSize(width: 420, height: 560)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("TrailmixMainWindow")
        window.tabbingMode = .disallowed
        self.window = window
        webView = web
        return web
    }

    func windowWillClose(_ notification: Notification) {
        guard !offscreen else { return }
        NSApp.setActivationPolicy(.accessory)  // back to living in the menu bar
    }

    @objc func reload() {
        webView?.reload()
    }

    /// Help → Check for Updates…: looks now and shows the result in Settings → Updates, where it installs.
    @objc func checkForUpdates() {
        Task { await Updater.shared.check(manual: true) }
        openUpdates()
    }

    @objc func openUpdates() { HelperModel.shared.openTrailmix(page: "updates") }
    @objc func openSettings() { HelperModel.shared.openTrailmix(page: "settings") }
    @objc func openWebsite() { NSWorkspace.shared.open(URL(string: "https://therealeminem.github.io/trailmix/")!) }
    @objc func reportProblem() { NSWorkspace.shared.open(URL(string: "https://github.com/TheRealEminem/trailmix/issues/new")!) }

    // MARK: Messages from the page

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? String else { return }
        if body == "quit" { NSApp.terminate(nil) }
        if body.hasPrefix("reveal:") {  // "Show in Finder" for an exported meeting
            let url = URL(fileURLWithPath: String(body.dropFirst("reveal:".count)))
            if FileManager.default.fileExists(atPath: url.path) { NSWorkspace.shared.activateFileViewerSelecting([url]) }
        }
    }

    /// File and folder pickers on the page (importing transcripts, or a Trailmix export folder).
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping @MainActor ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.canChooseFiles = !parameters.allowsDirectories
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.prompt = parameters.allowsDirectories ? "Import Folder" : "Import"
        if let window { panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) } }
        else { panel.begin { completionHandler($0 == .OK ? panel.urls : nil) } }
    }

    // MARK: Links, pop-ups, permissions

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, action.navigationType == .linkActivated, !isOurs(url) {
            NSWorkspace.shared.open(url)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    /// The page may use the mic itself (recording in the window when the menu bar recorder isn't available).
    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin, initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType, decisionHandler: @escaping @MainActor (WKPermissionDecision) -> Void) {
        decisionHandler(isOurs(webView.url) ? .grant : .prompt)
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.runModal()
        completionHandler()
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }

    private func isOurs(_ url: URL?) -> Bool {
        guard let url, let current = webView?.url ?? Prefs.shared.serverURL else { return false }
        return url.host == current.host && url.port == current.port
    }

    private static func jsString(_ text: String) -> String {
        let data = try? JSONSerialization.data(withJSONObject: [text])
        return String(data: data ?? Data("[\"\"]".utf8), encoding: .utf8).map { String($0.dropFirst().dropLast()) } ?? "\"\""
    }

    // MARK: Snapshots (tests)

    /// Waits for the page to settle, then saves what the window shows as a PNG.
    func snapshot(to path: String, after seconds: Double) async -> Bool {
        try? await Task.sleep(for: .seconds(seconds))
        guard let web = webView else { return false }
        let image: NSImage? = await withCheckedContinuation { done in
            web.takeSnapshot(with: nil) { image, _ in done.resume(returning: image) }
        }
        guard let tiff = image?.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else { return false }
        return (try? png.write(to: URL(fileURLWithPath: path))) != nil
    }

    // MARK: Menu

    private static func menu() -> NSMenu {
        let main = NSMenu()
        func submenu(_ title: String, _ items: [NSMenuItem]) {
            let holder = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            let menu = NSMenu(title: title)
            items.forEach(menu.addItem)
            holder.submenu = menu
            main.addItem(holder)
        }
        func item(_ title: String, _ action: Selector?, _ key: String, _ modifiers: NSEvent.ModifierFlags = .command, target: AnyObject? = nil) -> NSMenuItem {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
            item.keyEquivalentModifierMask = modifiers
            item.target = target
            return item
        }
        let name = Brand.appName
        submenu(name, [
            item("About \(name)", #selector(NSApplication.orderFrontStandardAboutPanel(_:)), ""),
            item("Check for Updates…", #selector(MainWindow.checkForUpdates), "", target: MainWindow.shared),
            .separator(),
            item("Settings…", #selector(MainWindow.openSettings), ",", target: MainWindow.shared),
            .separator(),
            item("Hide \(name)", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            .separator(),
            item("Quit \(name)", #selector(NSApplication.terminate(_:)), "q"),
        ])
        submenu("Edit", [
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        submenu("View", [
            item("Reload", #selector(MainWindow.reload), "r", target: MainWindow.shared),
        ])
        submenu("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Close", #selector(NSWindow.performClose(_:)), "w"),
        ])
        submenu("Help", [
            item("Check for Updates…", #selector(MainWindow.checkForUpdates), "", target: MainWindow.shared),
            item("Update Settings", #selector(MainWindow.openUpdates), "", target: MainWindow.shared),
            .separator(),
            item("\(name) Website", #selector(MainWindow.openWebsite), "", target: MainWindow.shared),
            item("Report a Problem…", #selector(MainWindow.reportProblem), "", target: MainWindow.shared),
        ])
        if let help = main.items.last?.submenu { NSApp.helpMenu = help }  // macOS adds its search field
        return main
    }
}
