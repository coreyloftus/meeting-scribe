// Dock behavior: clicking the Dock icon reopens the main window, and closing
// the window never quits the app (the menu-bar badge keeps running).
import AppKit

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    weak var state: AppState?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // The menu-bar label (which sets state and the open action) may appear a tick later.
        if let open = state?.openMainWindow { open(); return }
        DispatchQueue.main.async { [weak self] in self?.state?.openMainWindow?() }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        // `flag` counts the menu-bar item's window, so ignore it; openWindow just brings an open window forward.
        state?.openMainWindow?()
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}
