import AppKit
import SwiftUI

/// Trailmix's palette (the same tokens as the web app), adapting to light and dark menus.
enum Brand {
    /// "Trailmix", or "Trailmix Helper" for the thin build that talks to a server elsewhere.
    static let appName = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "Trailmix"

    static let forest = Color(light: 0x236347, dark: 0x4FA27A)
    static let trail = Color(light: 0xE86636, dark: 0xEE7445)
    static let sun = Color(light: 0xF5B83A, dark: 0xF2BE4E)
    static let sunInk = Color(light: 0x7D5205, dark: 0xF2D08A)
    static let sky = Color(light: 0x4F86A8, dark: 0x7FB2D3)
    static let berry = Color(light: 0x9B5575, dark: 0xCB86A6)
    static let danger = Color(light: 0xA63F17, dark: 0xF5A886)

    /// The menu bar icon: the Trailmix mark (a low sun, a ridge, a trail winding up to it) as line art.
    static let menuIcon = icon(dimmed: false)
    static let menuIconOffline = icon(dimmed: true)

    private static func icon(dimmed: Bool) -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
            NSColor.black.setStroke()
            NSColor.black.setFill()
            NSBezierPath(ovalIn: NSRect(x: 11.2, y: 10.6, width: 4.6, height: 4.6)).fill()  // sun

            let ridge = NSBezierPath()
            ridge.move(to: NSPoint(x: 1.2, y: 7.2))
            ridge.curve(to: NSPoint(x: 16.8, y: 7.6), controlPoint1: NSPoint(x: 5.5, y: 11.2), controlPoint2: NSPoint(x: 11, y: 10.6))
            ridge.lineWidth = 1.3
            ridge.lineCapStyle = .round
            ridge.stroke()

            let trail = NSBezierPath()
            trail.move(to: NSPoint(x: 5.2, y: 1.2))
            trail.curve(to: NSPoint(x: 8.6, y: 4.6), controlPoint1: NSPoint(x: 6.2, y: 3.4), controlPoint2: NSPoint(x: 9.8, y: 3.2))
            trail.curve(to: NSPoint(x: 10.8, y: 7.6), controlPoint1: NSPoint(x: 7.4, y: 6), controlPoint2: NSPoint(x: 9.2, y: 7.2))
            trail.lineWidth = 1.9
            trail.lineCapStyle = .round
            trail.lineJoinStyle = .round
            trail.stroke()
            return true
        }
        image.isTemplate = true
        if dimmed {
            let faded = NSImage(size: image.size, flipped: false) { rect in
                image.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 0.45)
                return true
            }
            faded.isTemplate = true
            return faded
        }
        return image
    }

    /// Shown next to the timer while recording: Trailmix's recording orange, not a template.
    static let recordingIcon: NSImage = {
        let image = NSImage(size: NSSize(width: 10, height: 18), flipped: false) { _ in
            NSColor(srgbRed: 0.91, green: 0.40, blue: 0.21, alpha: 1).setFill()
            NSBezierPath(ovalIn: NSRect(x: 1, y: 5, width: 8, height: 8)).fill()
            return true
        }
        image.isTemplate = false
        return image
    }()

    /// Flashed briefly when a moment is marked.
    static let flagIcon: NSImage = {
        let image = NSImage(systemSymbolName: "flag.fill", accessibilityDescription: "Moment marked")!
            .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 13, weight: .semibold))!
        image.isTemplate = true
        return image
    }()
}

extension Color {
    init(light: UInt32, dark: UInt32) {
        self.init(nsColor: NSColor(name: nil) { appearance in
            let hex = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
            return NSColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                           blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
        })
    }
}

/// 0:42, 12:03, 1:02:03
func clock(_ seconds: TimeInterval) -> String {
    let s = max(0, Int(seconds.rounded(.down)))
    let (h, m, r) = (s / 3600, (s % 3600) / 60, s % 60)
    return h > 0 ? String(format: "%d:%02d:%02d", h, m, r) : String(format: "%d:%02d", m, r)
}
