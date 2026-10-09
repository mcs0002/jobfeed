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
/// Labels only an opened conversation carries, read from the live app on
/// 2026-10-02: a fresh New Conversation view had none of them, a running one
/// had all. "user message" is counted separately to prove where a send landed.
let transcriptLabels: Set<String> = ["user message", "agent response", "conversation log",
                                     "stop execution", "undo to this point"]
let quotaLabels: Set<String> = ["enable overages", "see plans"]

enum LaunchFailure: String, Error {
    case accessibility_denied, antigravity_missing, antigravity_not_frontmost
    case new_conversation_missing, chat_input_missing, send_button_missing
    case paste_verification_failed, invalid_payload, helper_error
    case input_not_empty, send_not_accepted
    case conversation_not_fresh, conversation_not_created, landed_in_existing_conversation
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

/// The chat input's value with surrounding whitespace removed. An emptied
/// Antigravity input can keep a bare newline or zero-width space that it paints
/// as the placeholder; read raw, that failed every launch as input_not_empty on
/// 2026-09-28 while the box looked empty.
let invisible = CharacterSet.whitespacesAndNewlines.union(
    CharacterSet(charactersIn: "\u{200B}\u{200C}\u{200D}\u{2060}\u{FEFF}"))
func inputText(_ input: AXUIElement) -> String {
    return textAttr(input, kAXValueAttribute).trimmingCharacters(in: invisible)
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

/// What the visible conversation pane holds, from labels alone.
struct PaneState {
    var transcript = 0     // any marker of an opened conversation
    var userMessages = 0   // "User message" articles
    var quotaBanner = false
    var fresh: Bool { transcript == 0 }
}

func paneState(_ nodes: [AXNode]) -> PaneState {
    var state = PaneState()
    for node in nodes {
        for value in labelValues(node.element).map(normalize) where !value.isEmpty {
            if transcriptLabels.contains(value) || value.hasPrefix("no more older messages") {
                state.transcript += 1
            }
            if value == "user message" { state.userMessages += 1 }
            if quotaLabels.contains(value) { state.quotaBanner = true }
        }
    }
    return state
}

func paneNotes(_ state: PaneState) -> [String: Any] {
    return ["transcript_markers": state.transcript, "user_messages": state.userMessages,
            "quota_banner": state.quotaBanner]
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

/// The running app, and whether this call had to start it. A cold start matters:
/// on 2026-09-28 the UI came up before the chat backend, so the first Send was
/// ignored and the unsent text poisoned every retry.
func antigravity() throws -> (NSRunningApplication, Bool) {
    if let app = NSRunningApplication.runningApplications(withBundleIdentifier: antigravityBundle).first { return (app, false) }
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
    return (app, true)
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
        if inputText(input) == message.trimmingCharacters(in: invisible) { return }
        usleep(150_000)
    }
    throw LaunchFailure.paste_verification_failed
}

func postKey(_ key: CGKeyCode, command: Bool, to app: NSRunningApplication) {
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: false) else { return }
    if command { down.flags = .maskCommand; up.flags = .maskCommand }
    down.postToPid(app.processIdentifier); up.postToPid(app.processIdentifier)
}

/// Leave the chat input empty or fail closed. Antigravity keeps an unsent draft
/// across New Conversation, so a failed send leaves text that the next paste
/// would land on top of (2,056 characters where 1,276 were pasted, 2026-09-28).
func ensureEmpty(_ input: AXUIElement, app: NSRunningApplication) throws {
    if inputText(input).isEmpty { return }
    try activate(app)
    _ = AXUIElementSetAttributeValue(input, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    postKey(0, command: true, to: app)     // select all
    postKey(51, command: false, to: app)   // delete
    let deadline = Date().addingTimeInterval(2)
    while Date() < deadline {
        if inputText(input).isEmpty { return }
        usleep(150_000)
    }
    throw LaunchFailure.input_not_empty
}

/// Press Send until the message has left the input. A retry happens only while
/// the input still holds exactly the unsent message, so a message that was
/// accepted is never sent twice. A backend that is not ready yet ignores the
/// press; that was the cold-start failure of 2026-09-28.
func sendAndConfirm(_ message: String, input: AXUIElement, axApp: AXUIElement,
                    app: NSRunningApplication) throws {
    for _ in 0..<3 {
        let nodes = walk(appRoots(axApp))
        guard let send = findPressTarget(nodes, labels: sendLabels),
              AXUIElementPerformAction(send, kAXPressAction as CFString) == .success else {
            writeDump(reason: LaunchFailure.send_button_missing.rawValue, nodes: nodes, notes: [:])
            throw LaunchFailure.send_button_missing
        }
        let deadline = Date().addingTimeInterval(10)
        while Date() < deadline {
            if inputText(input) != message.trimmingCharacters(in: invisible) { return }
            usleep(250_000)
        }
    }
    writeDump(reason: LaunchFailure.send_not_accepted.rawValue, nodes: walk(appRoots(axApp)), notes: [:])
    try? ensureEmpty(input, app: app)
    throw LaunchFailure.send_not_accepted
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

/// Wait until the pane shows no conversation at all. New Conversation is a
/// client-side navigation, so the previous transcript can linger a moment.
func awaitFresh(_ axApp: AXUIElement, seconds: Double) -> (Bool, [AXNode]) {
    let deadline = Date().addingTimeInterval(seconds)
    var nodes: [AXNode] = []
    repeat {
        nodes = walk(appRoots(axApp))
        if paneState(nodes).fresh { return (true, nodes) }
        usleep(250_000)
    } while Date() < deadline
    return (false, nodes)
}

/// The one opening message goes into a verified-empty New Conversation or
/// nowhere. Until 2026-10-02 a second "echo" message followed once the input
/// was empty again, wherever that input was: Antigravity rejected one run's
/// opening message while its quota was exhausted, the window was back on
/// another, stalled conversation, and the echo woke that parent, whose browser
/// agent resumed on Chrome while the board showed the first run running. So there is no
/// second message, the pane must hold no transcript immediately before the
/// paste, and after Send exactly one user message must be on screen.
func launch(message: String) throws {
    let trusted = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
    guard trusted else { throw LaunchFailure.accessibility_denied }
    let (app, coldStart) = try antigravity(); try activate(app)
    let axApp = AXUIElementCreateApplication(app.processIdentifier)
    enableRemoteAccessibility(axApp)
    let (target, nodes) = awaitPressTarget(axApp, labels: newConversationLabels,
                                           seconds: coldStart ? 45 : 12)
    guard let conversation = target, AXUIElementPerformAction(conversation, kAXPressAction as CFString) == .success else {
        writeDump(reason: LaunchFailure.new_conversation_missing.rawValue, nodes: nodes,
                  notes: ["target_found": target != nil])
        throw LaunchFailure.new_conversation_missing
    }
    usleep(500_000)
    let (fresh, freshNodes) = awaitFresh(axApp, seconds: 6)
    guard fresh else {
        writeDump(reason: LaunchFailure.conversation_not_fresh.rawValue, nodes: freshNodes,
                  notes: paneNotes(paneState(freshNodes)))
        throw LaunchFailure.conversation_not_fresh
    }
    let clipboard = snapshotPasteboard(); defer { restorePasteboard(clipboard) }
    try activate(app)
    let current = walk(appRoots(axApp))
    guard let input = intendedInput(current) else {
        writeDump(reason: LaunchFailure.chat_input_missing.rawValue, nodes: current, notes: [:])
        throw LaunchFailure.chat_input_missing
    }
    do {
        try ensureEmpty(input, app: app)
        // Re-checked after the draft is cleared and right before the paste:
        // nothing may have navigated the pane to a conversation meanwhile.
        let check = walk(appRoots(axApp))
        guard paneState(check).fresh else {
            writeDump(reason: LaunchFailure.conversation_not_fresh.rawValue, nodes: check,
                      notes: paneNotes(paneState(check)))
            throw LaunchFailure.conversation_not_fresh
        }
        try paste(message, into: input, app: app)
    } catch LaunchFailure.input_not_empty {
        let raw = textAttr(input, kAXValueAttribute)
        writeDump(reason: LaunchFailure.input_not_empty.rawValue, nodes: walk(appRoots(axApp)),
                  notes: ["input_value_length": raw.count,
                          "input_visible_length": inputText(input).count])
        throw LaunchFailure.input_not_empty
    } catch LaunchFailure.conversation_not_fresh {
        throw LaunchFailure.conversation_not_fresh
    } catch {
        writeDump(reason: LaunchFailure.paste_verification_failed.rawValue,
                  nodes: walk(appRoots(axApp)),
                  notes: ["input_value_length": textAttr(input, kAXValueAttribute).count,
                          "expected_length": message.count])
        throw error
    }
    try sendAndConfirm(message, input: input, axApp: axApp, app: app)
    try confirmLanded(axApp)
}

/// After Send, the pane must show our message as the only user message. More
/// than one means it went into an existing conversation; none means
/// Antigravity dropped it. Either is reported as a failed launch, which pauses
/// the queue, because the board would otherwise show a run that is not there.
func confirmLanded(_ axApp: AXUIElement) throws {
    let deadline = Date().addingTimeInterval(20)
    var nodes: [AXNode] = []
    var state = PaneState()
    repeat {
        nodes = walk(appRoots(axApp))
        state = paneState(nodes)
        if state.userMessages > 1 { break }
        if state.userMessages == 1 { return }
        usleep(400_000)
    } while Date() < deadline
    let failure: LaunchFailure = state.userMessages > 1
        ? .landed_in_existing_conversation : .conversation_not_created
    writeDump(reason: failure.rawValue, nodes: nodes, notes: paneNotes(state))
    throw failure
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
    notes.merge(paneNotes(paneState(nodes))) { $1 }
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
            let valid = payload.version == 1 && payload.messages.count == 1
                && (payload.messages[0].hasPrefix("/browser http://") || payload.messages[0].hasPrefix("/browser https://"))
                && payload.messages[0].contains("\n\nUse /assisted-apply for workflow \(payload.workflow_id).")
            do { if !valid { throw LaunchFailure.invalid_payload }; try launch(message: payload.messages[0]) }
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
