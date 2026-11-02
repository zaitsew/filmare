// Draws the replay label as a PNG, because this ffmpeg may have no drawtext filter.
// Usage: label <text> <video width> <output.png>
import AppKit
import Foundation

let args = CommandLine.arguments
guard args.count == 4, let width = Double(args[2]) else {
    FileHandle.standardError.write(Data("usage: label <text> <video width> <output.png>\n".utf8))
    exit(2)
}
let fontSize = (width * 0.024).rounded()
let padding = (fontSize * 0.7).rounded()
let text = NSAttributedString(string: args[1], attributes: [
    .font: NSFont.systemFont(ofSize: fontSize, weight: .medium),
    .foregroundColor: NSColor.white,
])
let size = text.size()
let pixelWidth = Int((size.width + padding * 2).rounded(.up))
let pixelHeight = Int((size.height + padding).rounded(.up))
guard let bitmap = NSBitmapImageRep(
    bitmapDataPlanes: nil, pixelsWide: pixelWidth, pixelsHigh: pixelHeight, bitsPerSample: 8, samplesPerPixel: 4,
    hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
), let context = NSGraphicsContext(bitmapImageRep: bitmap) else { exit(2) }
NSGraphicsContext.current = context
let bounds = NSRect(x: 0, y: 0, width: pixelWidth, height: pixelHeight)
NSColor(white: 0, alpha: 0.72).setFill()
NSBezierPath(roundedRect: bounds, xRadius: Double(pixelHeight) / 2, yRadius: Double(pixelHeight) / 2).fill()
text.draw(at: NSPoint(x: padding, y: padding / 2))
context.flushGraphics()
guard let png = bitmap.representation(using: .png, properties: [:]) else { exit(2) }
try png.write(to: URL(fileURLWithPath: args[3]))
