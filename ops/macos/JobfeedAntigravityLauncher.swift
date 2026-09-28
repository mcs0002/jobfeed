import AppKit
import ApplicationServices
import Foundation

let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let python = root.appendingPathComponent(".venv/bin/python").path
let backend = root.appendingPathComponent("bin/application-launcher").path
let antigravityBundle = "com.google.antigravity"
let support = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/Jobfeed")
let healthURL = support.appendingPathComponent("application-launcher-health.json")
let dumpURL = support.appendingPathComponent("application-launcher-ax-dump.json")
let dumpRequestURL = support.appendingPathComponent("ax-dump.request")

let newConversationLabels: Set<String> = ["new conversation", "start a new chat", "start a new conversation"]
let sendLabels: Set<String> = ["send message"]

enum LaunchFailure: String, Error {
    case accessibility_denied, antigravity_missing, antigravity_not_frontmost
    case new_conversation_missing, chat_input_missing, send_button_missing
    case paste_verification_failed, second_message_timeout, invalid_payload, helper_error
}

func writeHealth(_ code: String = "") {
    let payload: [String: Any] = [
        "accessibility_trusted": AXIsProcessTrusted(),
        "checked_at": ISO8601DateFormatter().string(from: Date()),
        "error_code": code,
    ]
    guard let data = try? JSONSerialization.data(withJSONObject: payload) else { return }
    try? data.write(to: healthURL, options: .atomic)
    try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: healthURL.path)
}

func runPython(_ arguments: [String]) -> (Int32, Data) {
    let task = Process()
    let pipe = Pipe()
    task.executableURL = URL(fileURLWithPath: python)
    task.arguments = [backend] + arguments
    task.currentDirectoryURL = root
    task.standardOutput = pipe
    task.standardError = FileHandle.nullDevice
    do { try task.run() } catch { return (2, Data()) }
    task.waitUntilExit()
    return (task.terminationStatus, pipe.fileHandleForReading.readDataToEndOfFile())
}

func attr(_ element: AXUIElement, _ name: String) -> AnyObject? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}

func textAttr(_ element: AXUIElement, _ name: String) -> String {
    return attr(element, name) as? String ?? ""
}

func actionNames(_ element: AXUIElement) -> [String] {
    var names: CFArray?
    guard AXUIElementCopyActionNames(element, &names) == .success else { return [] }
    return (names as? [String]) ?? []
}

func canPress(_ element: AXUIElement) -> Bool {
    return actionNames(element).contains(kAXPressAction as String)
}

/// One visited Accessibility element plus its position in the walked tree.
struct AXNode {
    let element: AXUIElement
    let parent: Int
    let depth: Int
}

private struct AXKey: Hashable {
    let element: AXUIElement
    static func == (lhs: AXKey, rhs: AXKey) -> Bool { CFEqual(lhs.element, rhs.element) }
    func hash(into hasher: inout Hasher) { hasher.combine(CFHash(element)) }
}

/// Application element plus its menu bar; Electron hangs the chat UI off windows
/// and the menu bar is a separate attribute rather than a child.
func appRoots(_ axApp: AXUIElement) -> [AXUIElement] {
    var roots = [axApp]
    if let menuBar = attr(axApp, kAXMenuBarAttribute) as! AXUIElement? { roots.append(menuBar) }
    return roots
}

var lastWalkHitCap = false

/// Antigravity's chat controls sit ~20-40 levels inside the web area, so a
/// shallow cap reports every one of them missing. The caps only bound a
/// runaway tree; hitting either is recorded and shows up in the diagnostic.
func walk(_ roots: [AXUIElement], maxNodes: Int = 8000, maxDepth: Int = 60) -> [AXNode] {
    var nodes: [AXNode] = []
    var seen = Set<AXKey>()
    var queue: [(AXUIElement, Int, Int)] = roots.map { ($0, -1, 0) }
    var head = 0
    while head < queue.count && nodes.count < maxNodes {
        let (element, parent, depth) = queue[head]
        head += 1
        guard seen.insert(AXKey(element: element)).inserted else { continue }
        let index = nodes.count
        nodes.append(AXNode(element: element, parent: parent, depth: depth))
        guard let children = attr(element, kAXChildrenAttribute) as? [AXUIElement], !children.isEmpty else { continue }
        guard depth < maxDepth else { lastWalkHitCap = true; continue }
        for child in children { queue.append((child, index, depth + 1)) }
    }
    if nodes.count >= maxNodes { lastWalkHitCap = true }
    return nodes
}

/// Lowercase, drop parenthesised shortcut hints and keyboard glyphs, collapse space.
func normalize(_ raw: String) -> String {
    var value = raw.lowercased()
    value = value.replacingOccurrences(of: "\\([^)]*\\)", with: " ", options: .regularExpression)
    value = value.replacingOccurrences(of: "\\[[^\\]]*\\]", with: " ", options: .regularExpression)
    let drop = CharacterSet(charactersIn: "⌘⌥⌃⇧↩⏎⌫…:;.,·|/\\-–—")
    value = String(String.UnicodeScalarView(value.unicodeScalars.filter { !drop.contains($0) }))
    value = value.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
    return value.trimmingCharacters(in: .whitespacesAndNewlines)
}

func labelValues(_ element: AXUIElement) -> [String] {
    return [kAXTitleAttribute, kAXDescriptionAttribute, kAXHelpAttribute].map { textAttr(element, $0) }
}

/// The element the label belongs to, or its single pressable near ancestor.
func pressTarget(_ index: Int, _ nodes: [AXNode]) -> AXUIElement? {
    if canPress(nodes[index].element) { return nodes[index].element }
    var ancestors: [AXUIElement] = []
    var cursor = nodes[index].parent
    while cursor >= 0 && ancestors.count < 3 {
        ancestors.append(nodes[cursor].element)
        cursor = nodes[cursor].parent
    }
    let pressable = ancestors.filter(canPress)
    return pressable.count == 1 ? pressable[0] : nil
}

/// Exactly one labelled, pressable control or nothing. Exact labels win outright;
/// a contains match is only consulted when nothing matched exactly.
func findPressTarget(_ nodes: [AXNode], labels: Set<String>) -> AXUIElement? {
    var exact: [Int] = []
    var partial: [Int] = []
    for (index, node) in nodes.enumerated() {
        let values = labelValues(node.element).map(normalize).filter { !$0.isEmpty }
        if values.isEmpty { continue }
        if values.contains(where: { labels.contains($0) }) {
            exact.append(index)
        } else if values.contains(where: { value in labels.contains(where: { value.contains($0) }) }) {
            partial.append(index)
        }
    }
    let group = exact.isEmpty ? partial : exact
    var targets: [(element: AXUIElement, depth: Int)] = []
    for index in group {
        guard let target = pressTarget(index, nodes) else { continue }
        if let seen = targets.firstIndex(where: { CFEqual($0.element, target) }) {
            targets[seen].depth = min(targets[seen].depth, nodes[index].depth)
        } else {
            targets.append((target, nodes[index].depth))
        }
    }
    if targets.count == 1 { return targets[0].element }
    // Antigravity labels both the sidebar's primary nav item and a per-section
    // button "New Conversation". The primary chrome sits nearer the root, so the
    // strictly shallowest match is the intended control. A tie is genuinely
    // ambiguous and still fails closed.
    guard let shallowest = targets.min(by: { $0.depth < $1.depth }),
          targets.filter({ $0.depth == shallowest.depth }).count == 1 else { return nil }
    return shallowest.element
}

func intendedInput(_ nodes: [AXNode]) -> AXUIElement? {
    let candidates = nodes.map { $0.element }.filter {
        let role = textAttr($0, kAXRoleAttribute)
        guard role == (kAXTextAreaRole as String) || role == (kAXTextFieldRole as String) else { return false }
        return textAttr($0, kAXSubroleAttribute) != (kAXSearchFieldSubrole as String)
    }
    let named = candidates.filter {
        let value = labelValues($0).joined(separator: " ").lowercased()
        return ["message", "prompt", "ask", "chat"].contains(where: value.contains)
    }
    if named.count == 1 { return named[0] }
    let focused = candidates.filter { (attr($0, kAXFocusedAttribute) as? Bool) == true }
    if focused.count == 1 { return focused[0] }
    let areas = candidates.filter { textAttr($0, kAXRoleAttribute) == (kAXTextAreaRole as String) }
    return areas.count == 1 ? areas[0] : nil
}

// MARK: - Bounded diagnostic

func clip(_ value: String) -> String {
    let flat = value.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
    return flat.count <= 80 ? flat : String(flat.prefix(80))
}

/// Structure only: role, position and labels. Never AXValue, never page or
/// conversation text, so the file is safe to read over SSH.
func summarize(_ nodes: [AXNode], cap: Int = 4000) -> [[String: Any]] {
    var rows: [[String: Any]] = []
    for (index, node) in nodes.enumerated() {
        if rows.count >= cap { break }
        var row: [String: Any] = [
            "i": index, "p": node.parent, "d": node.depth,
            "role": clip(textAttr(node.element, kAXRoleAttribute)),
        ]
        for (key, name) in [("subrole", kAXSubroleAttribute), ("roledesc", kAXRoleDescriptionAttribute),
                            ("id", kAXIdentifierAttribute), ("title", kAXTitleAttribute),
                            ("desc", kAXDescriptionAttribute), ("help", kAXHelpAttribute)] {
            let text = clip(textAttr(node.element, name))
            if !text.isEmpty { row[key] = text }
        }
        let actions = actionNames(node.element)
        if !actions.isEmpty { row["actions"] = actions }
        rows.append(row)
    }
    return rows
}

func writeDump(reason: String, nodes: [AXNode], notes: [String: Any]) {
    var payload: [String: Any] = notes
    payload["version"] = 1
    payload["reason"] = reason
    payload["written_at"] = ISO8601DateFormatter().string(from: Date())
    payload["node_count"] = nodes.count
    payload["walk_hit_cap"] = lastWalkHitCap
    payload["nodes"] = summarize(nodes)
    guard let data = try? JSONSerialization.data(withJSONObject: payload) else { return }
    try? data.write(to: dumpURL, options: .atomic)
    try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: dumpURL.path)
}

// MARK: - Antigravity

func antigravity() throws -> NSRunningApplication {
    if let app = NSRunningApplication.runningApplications(withBundleIdentifier: antigravityBundle).first { return app }
    guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: antigravityBundle) else {
        throw LaunchFailure.antigravity_missing
    }
    let semaphore = DispatchSemaphore(value: 0)
    var opened: NSRunningApplication?
    NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration()) { app, _ in
        opened = app; semaphore.signal()
    }
    _ = semaphore.wait(timeout: .now() + 15)
    guard let app = opened else { throw LaunchFailure.antigravity_missing }
    return app
}

func isFocused(_ app: NSRunningApplication) -> Bool {
    if NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier { return true }
    let system = AXUIElementCreateSystemWide()
    guard let focused = attr(system, kAXFocusedApplicationAttribute) as! AXUIElement? else { return false }
    var pid: pid_t = 0
    return AXUIElementGetPid(focused, &pid) == .success && pid == app.processIdentifier
}

func activate(_ app: NSRunningApplication) throws {
    app.unhide()
    _ = app.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
    let axApp = AXUIElementCreateApplication(app.processIdentifier)
    let system = AXUIElementCreateSystemWide()
    _ = AXUIElementSetAttributeValue(system, kAXFocusedApplicationAttribute as CFString, axApp)
    if let windows = attr(axApp, kAXWindowsAttribute) as? [AXUIElement] {
        for window in windows { _ = AXUIElementPerformAction(window, kAXRaiseAction as CFString) }
    }
    let deadline = Date().addingTimeInterval(5)
    while Date() < deadline {
        if isFocused(app) { return }
        usleep(100_000)
    }
    throw LaunchFailure.antigravity_not_frontmost
}

/// Electron may serve a window-level stub to an assistive client that has not
/// asked for the web-content tree. This live Antigravity exposes it unprompted,
/// so the call is only cold-start insurance; AXEnhancedUserInterface is
/// deliberately not set, since it was proven unnecessary here and is known to
/// disturb window management.
func enableRemoteAccessibility(_ axApp: AXUIElement) {
    _ = AXUIElementSetAttributeValue(axApp, "AXManualAccessibility" as CFString, kCFBooleanTrue)
}

func awaitPressTarget(_ axApp: AXUIElement, labels: Set<String>,
                      seconds: Double) -> (AXUIElement?, [AXNode]) {
    let deadline = Date().addingTimeInterval(seconds)
    var nodes: [AXNode] = []
    repeat {
        nodes = walk(appRoots(axApp))
        if let target = findPressTarget(nodes, labels: labels) { return (target, nodes) }
        usleep(300_000)
    } while Date() < deadline
    return (nil, nodes)
}

func paste(_ message: String, into input: AXUIElement, app: NSRunningApplication) throws {
    try activate(app)
    var pid: pid_t = 0
    guard AXUIElementGetPid(input, &pid) == .success, pid == app.processIdentifier else {
        throw LaunchFailure.chat_input_missing
    }
    guard AXUIElementSetAttributeValue(input, kAXFocusedAttribute as CFString, kCFBooleanTrue) == .success else {
        throw LaunchFailure.chat_input_missing
    }
    let board = NSPasteboard.general
    board.clearContents(); board.setString(message, forType: .string)
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: 9, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: 9, keyDown: false) else {
        throw LaunchFailure.helper_error
    }
    down.flags = .maskCommand; up.flags = .maskCommand
    down.postToPid(app.processIdentifier); up.postToPid(app.processIdentifier)
    let deadline = Date().addingTimeInterval(3)
    while Date() < deadline {
        if textAttr(input, kAXValueAttribute) == message { return }
        usleep(150_000)
    }
    throw LaunchFailure.paste_verification_failed
}

func snapshotPasteboard() -> [[String: Data]] {
    return (NSPasteboard.general.pasteboardItems ?? []).map { item in
        Dictionary(uniqueKeysWithValues: item.types.compactMap { type in
            item.data(forType: type).map { (type.rawValue, $0) }
        })
    }
}

func restorePasteboard(_ snapshot: [[String: Data]]) {
    let board = NSPasteboard.general; board.clearContents()
    let items = snapshot.map { values -> NSPasteboardItem in
        let item = NSPasteboardItem()
        for (type, data) in values { item.setData(data, forType: NSPasteboard.PasteboardType(type)) }
        return item
    }
    if !items.isEmpty { board.writeObjects(items) }
}

func launch(messages: [String]) throws {
    let trusted = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
    guard trusted else { throw LaunchFailure.accessibility_denied }
    let app = try antigravity(); try activate(app)
    let axApp = AXUIElementCreateApplication(app.processIdentifier)
    enableRemoteAccessibility(axApp)
    let (target, nodes) = awaitPressTarget(axApp, labels: newConversationLabels, seconds: 12)
    guard let conversation = target, AXUIElementPerformAction(conversation, kAXPressAction as CFString) == .success else {
        writeDump(reason: LaunchFailure.new_conversation_missing.rawValue, nodes: nodes,
                  notes: ["target_found": target != nil])
        throw LaunchFailure.new_conversation_missing
    }
    usleep(500_000)
    let clipboard = snapshotPasteboard(); defer { restorePasteboard(clipboard) }
    for index in 0..<2 {
        if index == 1 {
            let deadline = Date().addingTimeInterval(15)
            var ready = false
            while Date() < deadline {
                try activate(app)
                if let candidate = intendedInput(walk(appRoots(axApp))),
                   textAttr(candidate, kAXValueAttribute).isEmpty {
                    ready = true; break
                }
                usleep(250_000)
            }
            if !ready {
                writeDump(reason: LaunchFailure.second_message_timeout.rawValue,
                          nodes: walk(appRoots(axApp)), notes: ["message_index": index])
                throw LaunchFailure.second_message_timeout
            }
        }
        try activate(app)
        let nodes = walk(appRoots(axApp))
        guard let input = intendedInput(nodes) else {
            writeDump(reason: LaunchFailure.chat_input_missing.rawValue, nodes: nodes,
                      notes: ["message_index": index])
            throw index == 1 ? LaunchFailure.second_message_timeout : LaunchFailure.chat_input_missing
        }
        do {
            try paste(messages[index], into: input, app: app)
        } catch {
            let current = walk(appRoots(axApp))
            writeDump(reason: LaunchFailure.paste_verification_failed.rawValue, nodes: current,
                      notes: ["message_index": index,
                              "input_value_length": textAttr(input, kAXValueAttribute).count,
                              "expected_length": messages[index].count])
            throw error
        }
        let afterPaste = walk(appRoots(axApp))
        guard let send = findPressTarget(afterPaste, labels: sendLabels),
              AXUIElementPerformAction(send, kAXPressAction as CFString) == .success else {
            writeDump(reason: LaunchFailure.send_button_missing.rawValue, nodes: afterPaste,
                      notes: ["message_index": index])
            throw LaunchFailure.send_button_missing
        }
    }
}

/// Operator-triggered structural snapshot. Presence of the request file is the
/// whole protocol: it carries no data and never changes launch state.
func handleDumpRequest() {
    let manager = FileManager.default
    guard let info = try? manager.attributesOfItem(atPath: dumpRequestURL.path),
          (info[.type] as? FileAttributeType) == .typeRegular else { return }
    try? manager.removeItem(at: dumpRequestURL)
    guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: antigravityBundle).first else {
        writeDump(reason: "on_demand", nodes: [], notes: ["antigravity_running": false])
        return
    }
    let axApp = AXUIElementCreateApplication(app.processIdentifier)
    var notes: [String: Any] = ["antigravity_running": true, "trusted": AXIsProcessTrusted()]
    notes["focused"] = (try? activate(app)) != nil
    enableRemoteAccessibility(axApp)
    usleep(1_500_000)
    let started = Date()
    let nodes = walk(appRoots(axApp))
    notes["walk_ms"] = Int(Date().timeIntervalSince(started) * 1000)
    notes["new_conversation_found"] = findPressTarget(nodes, labels: newConversationLabels) != nil
    notes["send_found"] = findPressTarget(nodes, labels: sendLabels) != nil
    notes["input_found"] = intendedInput(nodes) != nil
    writeDump(reason: "on_demand", nodes: nodes, notes: notes)
}

struct Payload: Decodable { let version: Int; let workflow_id: String; let launch_request_id: String; let messages: [String] }

_ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
writeHealth()
var healthTick = 0
while true {
    autoreleasepool {
        handleDumpRequest()
        let (status, data) = runPython(["--claim-next"])
        if status == 0, let payload = try? JSONDecoder().decode(Payload.self, from: data) {
            var result = "succeeded"; var code = ""
            let valid = payload.version == 1 && payload.messages.count == 2
                && (payload.messages[0].hasPrefix("/browser http://") || payload.messages[0].hasPrefix("/browser https://"))
                && payload.messages[1].hasPrefix("Use /assisted-apply for workflow \(payload.workflow_id).")
            do { if !valid { throw LaunchFailure.invalid_payload }; try launch(messages: payload.messages) }
            catch let error as LaunchFailure { result = "failed"; code = error.rawValue }
            catch { result = "failed"; code = LaunchFailure.helper_error.rawValue }
            _ = runPython(["--finish", payload.launch_request_id, result, code])
            writeHealth(code)
        }
    }
    healthTick += 1
    if healthTick >= 5 { writeHealth(); healthTick = 0 }
    sleep(2)
}
