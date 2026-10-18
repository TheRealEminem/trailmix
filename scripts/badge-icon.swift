// Draws a label badge ("DEV") across the bottom of an app icon, so a build from source can't be mistaken for the
// installed app: swift scripts/badge-icon.swift in.png out.png DEV
import AppKit

let args = CommandLine.arguments
guard args.count == 4, let image = NSImage(contentsOfFile: args[1]),
      let source = image.representations.first else {
    print("usage: badge-icon.swift in.png out.png LABEL")
    exit(2)
}
let w = source.pixelsWide, h = source.pixelsHigh
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h, bitsPerSample: 8, samplesPerPixel: 4,
                           hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
image.draw(in: NSRect(x: 0, y: 0, width: w, height: h))

// A pill on the lower part of the artwork (icons sit inside a ~10% margin), in the app's sky blue.
let s = CGFloat(w) / 1024
let pill = NSRect(x: 212 * s, y: 130 * s, width: 600 * s, height: 220 * s)
NSColor(calibratedRed: 0.20, green: 0.47, blue: 0.86, alpha: 1).setFill()
NSBezierPath(roundedRect: pill, xRadius: 110 * s, yRadius: 110 * s).fill()
NSColor.white.withAlphaComponent(0.9).setStroke()
let ring = NSBezierPath(roundedRect: pill.insetBy(dx: 6 * s, dy: 6 * s), xRadius: 104 * s, yRadius: 104 * s)
ring.lineWidth = 12 * s
ring.stroke()
let text = NSAttributedString(string: args[3], attributes: [
    .font: NSFont.systemFont(ofSize: 150 * s, weight: .heavy),
    .foregroundColor: NSColor.white,
    .kern: 8 * s,
])
let size = text.size()
text.draw(at: NSPoint(x: pill.midX - size.width / 2, y: pill.midY - size.height / 2))
NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: args[2]))
print("saved \(args[2])")
