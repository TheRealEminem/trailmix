// Renders an SVG to a PNG at an exact pixel size: swift scripts/render-svg.swift in.svg out.png 1320 840
import AppKit

let args = CommandLine.arguments
guard args.count == 5, let w = Int(args[3]), let h = Int(args[4]), let image = NSImage(contentsOfFile: args[1]) else {
    print("usage: render-svg.swift in.svg out.png width height")
    exit(2)
}
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h, bitsPerSample: 8, samplesPerPixel: 4,
                           hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
image.draw(in: NSRect(x: 0, y: 0, width: w, height: h))
NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: args[2]))
print("saved \(args[2]) (\(w)x\(h))")
