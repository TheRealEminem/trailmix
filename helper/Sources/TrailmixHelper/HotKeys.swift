import AppKit
import Carbon.HIToolbox

/// A global keyboard shortcut. Stored with Carbon key codes and modifier flags, which is what
/// RegisterEventHotKey takes; global hotkeys registered that way need no Accessibility permission.
struct Shortcut: Codable, Equatable {
    var keyCode: UInt32
    var modifiers: UInt32

    static let toggleDefault = Shortcut(keyCode: UInt32(kVK_ANSI_R), modifiers: UInt32(controlKey | optionKey | cmdKey))
    static let markDefault = Shortcut(keyCode: UInt32(kVK_ANSI_M), modifiers: UInt32(controlKey | optionKey | cmdKey))

    /// From a key press, if it makes a sensible global shortcut (a modifier, or a function key on its own).
    init?(event: NSEvent) {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        var carbon: UInt32 = 0
        if flags.contains(.command) { carbon |= UInt32(cmdKey) }
        if flags.contains(.option) { carbon |= UInt32(optionKey) }
        if flags.contains(.control) { carbon |= UInt32(controlKey) }
        if flags.contains(.shift) { carbon |= UInt32(shiftKey) }
        let code = UInt32(event.keyCode)
        let functionKey = Shortcut.functionKeys[Int(code)] != nil
        guard carbon & UInt32(cmdKey | optionKey | controlKey) != 0 || functionKey else { return nil }
        self.init(keyCode: code, modifiers: carbon)
    }

    init(keyCode: UInt32, modifiers: UInt32) {
        self.keyCode = keyCode
        self.modifiers = modifiers
    }

    var display: String {
        var s = ""
        if modifiers & UInt32(controlKey) != 0 { s += "⌃" }
        if modifiers & UInt32(optionKey) != 0 { s += "⌥" }
        if modifiers & UInt32(shiftKey) != 0 { s += "⇧" }
        if modifiers & UInt32(cmdKey) != 0 { s += "⌘" }
        return s + Shortcut.keyName(keyCode)
    }

    private static let functionKeys: [Int: String] = [
        kVK_F1: "F1", kVK_F2: "F2", kVK_F3: "F3", kVK_F4: "F4", kVK_F5: "F5", kVK_F6: "F6", kVK_F7: "F7", kVK_F8: "F8",
        kVK_F9: "F9", kVK_F10: "F10", kVK_F11: "F11", kVK_F12: "F12", kVK_F13: "F13", kVK_F14: "F14", kVK_F15: "F15",
        kVK_F16: "F16", kVK_F17: "F17", kVK_F18: "F18", kVK_F19: "F19", kVK_F20: "F20",
    ]
    private static let namedKeys: [Int: String] = [
        kVK_Space: "Space", kVK_Return: "↩", kVK_Tab: "⇥", kVK_Delete: "⌫", kVK_ForwardDelete: "⌦", kVK_Escape: "⎋",
        kVK_LeftArrow: "←", kVK_RightArrow: "→", kVK_UpArrow: "↑", kVK_DownArrow: "↓", kVK_Home: "↖", kVK_End: "↘",
        kVK_PageUp: "⇞", kVK_PageDown: "⇟",
    ]

    /// The key's label on the current keyboard layout (so it's right on AZERTY and friends too).
    static func keyName(_ code: UInt32) -> String {
        if let name = functionKeys[Int(code)] ?? namedKeys[Int(code)] { return name }
        guard let source = TISCopyCurrentKeyboardLayoutInputSource()?.takeRetainedValue(),
              let raw = TISGetInputSourceProperty(source, kTISPropertyUnicodeKeyLayoutData) else { return "?" }
        let data = Unmanaged<CFData>.fromOpaque(raw).takeUnretainedValue() as Data
        var deadKeys: UInt32 = 0
        var chars = [UniChar](repeating: 0, count: 4)
        var length = 0
        let status = data.withUnsafeBytes { layout in
            UCKeyTranslate(layout.bindMemory(to: UCKeyboardLayout.self).baseAddress, UInt16(code), UInt16(kUCKeyActionDisplay), 0,
                           UInt32(LMGetKbdType()), OptionBits(kUCKeyTranslateNoDeadKeysBit), &deadKeys, chars.count, &length, &chars)
        }
        guard status == noErr, length > 0 else { return "?" }
        return String(utf16CodeUnits: chars, count: length).uppercased()
    }
}

/// Registers global hotkeys and calls their handlers on the main thread.
final class HotKeys {
    static let shared = HotKeys()
    private var refs: [UInt32: EventHotKeyRef] = [:]
    private var handlers: [UInt32: () -> Void] = [:]
    private var installed = false

    /// Returns false if the shortcut couldn't be registered (usually another app already uses it).
    @discardableResult
    func register(_ id: UInt32, _ shortcut: Shortcut, handler: @escaping () -> Void) -> Bool {
        installHandler()
        unregister(id)
        var ref: EventHotKeyRef?
        let hotKeyID = EventHotKeyID(signature: OSType(0x544D_4958), id: id)  // 'TMIX'
        guard RegisterEventHotKey(shortcut.keyCode, shortcut.modifiers, hotKeyID, GetEventDispatcherTarget(), 0, &ref) == noErr,
              let ref else { return false }
        refs[id] = ref
        handlers[id] = handler
        return true
    }

    func unregister(_ id: UInt32) {
        if let ref = refs.removeValue(forKey: id) { UnregisterEventHotKey(ref) }
        handlers[id] = nil
    }

    private func installHandler() {
        guard !installed else { return }
        installed = true
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetEventDispatcherTarget(), { _, event, _ in
            var hotKeyID = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &hotKeyID)
            let id = hotKeyID.id
            DispatchQueue.main.async { HotKeys.shared.handlers[id]?() }
            return noErr
        }, 1, &spec, nil, nil)
    }
}
