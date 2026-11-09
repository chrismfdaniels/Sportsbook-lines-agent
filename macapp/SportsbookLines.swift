// Sportsbook Lines for Mac: a Dock app and menu bar item around the local lines
// server (python -m lines_agent.web, kept running by the launch agent).
//
// The window shows the same page as the browser and phone, so every control lives
// in one place. The menu bar item shows the last and next update, and runs a scan
// with the saved Scan controls without opening the window.
//
// Build: macapp/build.sh
import Cocoa
import ServiceManagement
import WebKit

let serverURL = URL(string: "http://127.0.0.1:8765/")!
let agentLabel = "com.sportsbook-lines-agent"
let agentPlist = NSHomeDirectory() + "/Library/LaunchAgents/\(agentLabel).plist"

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow?
    var web: WKWebView?
    var statusItem: NSStatusItem!
    let lastItem = NSMenuItem(title: "Last update: –", action: nil, keyEquivalent: "")
    let nextItem = NSMenuItem(title: "Next scan: –", action: nil, keyEquivalent: "")
    let scanItem = NSMenuItem(title: "Scan Now (current Scan controls)", action: #selector(scanNow), keyEquivalent: "")
    let loginItem = NSMenuItem(title: "Open at Login", action: #selector(toggleLogin), keyEquivalent: "")
    var retries = 0

    // Built in willFinishLaunching: a Dock click can deliver the "reopen" event
    // before didFinishLaunching, and it needs the window to exist.
    func applicationWillFinishLaunching(_ note: Notification) {
        buildMainMenu()
        buildWindow()
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        buildStatusItem()
        loadPage()
    }

    // Closing the window keeps the menu bar item; the Dock icon brings the window back.
    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool {
        showWindow()
        return true
    }

    // MARK: Window

    func buildWindow() {
        let config = WKWebViewConfiguration()
        // Lets the page know it's in the app (it hides the phone-only push controls).
        config.applicationNameForUserAgent = "SportsbookLinesMac/1.0"
        let web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = false
        self.web = web

        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 920),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Sportsbook Lines"
        window.minSize = NSSize(width: 420, height: 500)
        window.contentView = web
        window.isReleasedWhenClosed = false
        window.center()
        window.setFrameAutosaveName("MainWindow")
        window.makeKeyAndOrderFront(nil)
        self.window = window
    }

    @objc func showWindow() {
        guard let window, let web else { return }
        if web.url == nil { loadPage() }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func reloadPage() { loadPage() }

    func loadPage() {
        web?.load(URLRequest(url: serverURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 10))
    }

    // The server isn't answering: start the launch agent, show a wait page, retry.
    func webView(_ webView: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError error: Error) {
        retries += 1
        if retries == 1 { startServer() }
        let msg = retries > 30
            ? "The lines server didn't start. Check ~/Library/Logs/sportsbook-lines-agent.log, then choose View → Reload."
            : "Starting the lines server…"
        webView.loadHTMLString("""
            <html><body style="font:15px -apple-system;display:flex;height:90vh;align-items:center;justify-content:center;
            color:#888;background:Canvas">\(msg)</body></html>
            """, baseURL: nil)
        if retries <= 30 {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.loadPage() }
        }
    }

    func webView(_ webView: WKWebView, didFinish nav: WKNavigation!) {
        if webView.url?.host == serverURL.host { retries = 0 }
    }

    // Links that leave the server (none today) open in the default browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, url.host != serverURL.host, url.scheme?.hasPrefix("http") == true {
            NSWorkspace.shared.open(url)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.runModal()
        completionHandler()
    }

    func startServer() {
        let uid = String(getuid())
        // kickstart works when the agent is loaded; bootstrap loads it if it isn't.
        for args in [["kickstart", "gui/\(uid)/\(agentLabel)"], ["bootstrap", "gui/\(uid)", agentPlist]] {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            p.arguments = args
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
            try? p.run()
            p.waitUntilExit()
            if p.terminationStatus == 0 { return }
        }
    }

    // MARK: Menu bar item

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "chart.line.uptrend.xyaxis", accessibilityDescription: "Sportsbook Lines")
            button.image?.isTemplate = true
        }
        let menu = NSMenu()
        menu.delegate = self
        lastItem.isEnabled = false
        nextItem.isEnabled = false
        menu.addItem(lastItem)
        menu.addItem(nextItem)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Open Sportsbook Lines", action: #selector(showWindow), keyEquivalent: "")
        scanItem.target = self
        menu.addItem(scanItem)
        menu.addItem(.separator())
        loginItem.target = self
        menu.addItem(loginItem)
        menu.addItem(withTitle: "Quit Sportsbook Lines", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        for item in menu.items where item.action == #selector(showWindow) { item.target = self }
        statusItem.menu = menu
    }

    func menuWillOpen(_ menu: NSMenu) {
        loginItem.state = SMAppService.mainApp.status == .enabled ? .on : .off
        fetchJSON("api/status") { [weak self] s in
            guard let self else { return }
            guard let s else {
                self.lastItem.title = "Server not responding"
                self.nextItem.title = "Next scan: –"
                return
            }
            let running = s["running"] as? Bool ?? false
            self.scanItem.isEnabled = !running
            if running {
                self.lastItem.title = "\(s["running_label"] as? String ?? "Scan") running now…"
            } else if let last = s["last_run"] as? String {
                let found = (s["last_result"] as? [String: Any])?["arbs"] as? Int
                self.lastItem.title = "Last scan: \(Self.mtTime(last))" + (found.map { " · \($0) arbitrage" } ?? "")
            } else {
                self.lastItem.title = "Last scan: none since the server started"
            }
            self.nextItem.title = (s["next_run"] as? String).map { "Next scan: \(Self.mtTime($0))" } ?? "Auto-scans off"
        }
    }

    @objc func scanNow() {
        var req = URLRequest(url: serverURL.appendingPathComponent("api/scan"))
        req.httpMethod = "POST"
        req.httpBody = Data("{}".utf8)
        URLSession.shared.dataTask(with: req).resume()
        // The page notices the finished run and reloads; the Mac shows a banner if it finds arbitrage.
    }

    @objc func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
        } catch {
            let alert = NSAlert(error: error)
            alert.runModal()
        }
    }

    func fetchJSON(_ path: String, done: @escaping ([String: Any]?) -> Void) {
        URLSession.shared.dataTask(with: serverURL.appendingPathComponent(path)) { data, _, _ in
            let obj = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async { done(obj) }
        }.resume()
    }

    static func mtTime(_ iso: String) -> String {
        let parse = ISO8601DateFormatter()
        guard let date = parse.date(from: iso) else { return iso }
        let mountain = TimeZone(identifier: "America/Denver")!
        var cal = Calendar.current
        cal.timeZone = mountain
        let f = DateFormatter()
        f.timeZone = mountain
        f.dateFormat = cal.isDateInToday(date) ? "h:mm a 'MT'" : "EEE h:mm a 'MT'"
        return f.string(from: date)
    }

    // MARK: Main menu (Edit is what makes copy/paste work in the page's text boxes)

    func buildMainMenu() {
        let main = NSMenu()
        func sub(_ title: String, _ items: [NSMenuItem]) {
            let holder = NSMenuItem()
            let m = NSMenu(title: title)
            items.forEach(m.addItem)
            holder.submenu = m
            main.addItem(holder)
        }
        func item(_ t: String, _ a: Selector?, _ k: String, _ mods: NSEvent.ModifierFlags = .command, target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: t, action: a, keyEquivalent: k)
            i.keyEquivalentModifierMask = mods
            i.target = target
            return i
        }
        sub("Sportsbook Lines", [
            item("About Sportsbook Lines", #selector(NSApplication.orderFrontStandardAboutPanel(_:)), ""),
            .separator(),
            item("Hide Sportsbook Lines", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            .separator(),
            item("Quit Sportsbook Lines", #selector(NSApplication.terminate(_:)), "q"),
        ])
        sub("Edit", [
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        sub("View", [
            item("Reload", #selector(reloadPage), "r", target: self),
            item("Scan Now", #selector(scanNow), "s", [.command, .shift], target: self),
        ])
        sub("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Show Main Window", #selector(showWindow), "0", target: self),
        ])
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
