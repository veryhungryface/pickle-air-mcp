import AppKit
import Foundation
import ApplicationServices
if CommandLine.arguments.contains("--check") {
    print(AXIsProcessTrusted() ? "ACCESSIBILITY_READY" : "ACCESSIBILITY_NOT_GRANTED")
    exit(AXIsProcessTrusted() ? 0 : 3)
}
guard AXIsProcessTrusted() else { fputs("Existing macOS Accessibility permission is required; no settings changed\n", stderr); exit(3) }
guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: "com.now.gg.BlueStacks").first else { fputs("BlueStacks Air is not running\n", stderr); exit(3) }
// Original clipboard formats remain only in memory. Never restore over a newer copy.
let pb = NSPasteboard.general
let originals: [[(NSPasteboard.PasteboardType, Data)]] = (pb.pasteboardItems ?? []).map { item in
    item.types.compactMap { type in item.data(forType: type).map { (type, $0) } }
}
guard let line = readLine(), let bytes = Data(base64Encoded: line), let text = String(data: bytes, encoding: .utf8) else {
    fputs("Invalid clipboard input\n", stderr); exit(2)
}
pb.clearContents()
pb.setString(text, forType: .string)
let ownedChange = pb.changeCount
// Local fix: another app is usually in front (the MCP client). Bring BlueStacks forward for real,
// pumping the run loop so the frontmost state is current.
let previous = NSWorkspace.shared.frontmostApplication
func pump(_ seconds: Double) { RunLoop.current.run(until: Date(timeIntervalSinceNow: seconds)) }
func isFront() -> Bool { NSWorkspace.shared.frontmostApplication?.bundleIdentifier == "com.now.gg.BlueStacks" || app.isActive }
var front = isFront()
var attempt = 0
while !front && attempt < 20 {
    if attempt == 6 {
        let opener = Process(); opener.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        opener.arguments = ["-b", "com.now.gg.BlueStacks"]; try? opener.run()
    } else {
        app.activate(options: [.activateIgnoringOtherApps])
    }
    pump(0.25); front = isFront(); attempt += 1
}
fputs("diag: previous=\(previous?.bundleIdentifier ?? "nil") attempts=\(attempt) front=\(front) active=\(app.isActive) frontmost=\(NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "nil")\n", stderr)
guard front else { fputs("BlueStacks could not be brought to the front; nothing pasted\n", stderr); exit(4) }
// The emulator copies the Mac clipboard only when its window becomes active. That is all we need:
// give it a moment, hand focus straight back, and let the CLI paste inside Android (keyevent 279).
// No keystroke is posted from here, so nothing can be typed into the wrong app.
pump(1.6)
if let previous = previous, previous.bundleIdentifier != "com.now.gg.BlueStacks" { previous.activate(options: [.activateIgnoringOtherApps]); pump(0.2) }
print("READY")
fflush(stdout)
_ = readLine()
if pb.changeCount == ownedChange {
    pb.clearContents()
    let items = originals.map { entries -> NSPasteboardItem in
        let item = NSPasteboardItem()
        for (type, data) in entries { item.setData(data, forType: type) }
        return item
    }
    if !items.isEmpty { pb.writeObjects(items) }
    print("RESTORED")
} else {
    print("CHANGED_BY_USER_NOT_RESTORED")
}
