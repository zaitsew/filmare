// Prints the text the on-device Vision framework reads in an image.
// One file per argument: each line of output is "<argument index>\t<text>".
import AppKit
import Foundation
import Vision

for (index, path) in CommandLine.arguments.dropFirst().enumerated() {
    guard let image = NSImage(contentsOfFile: path),
          let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        FileHandle.standardError.write(Data("unreadable image\n".utf8))
        exit(2)
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["en-US", "ru-RU", "es-ES"]
    try VNImageRequestHandler(cgImage: cg).perform([request])
    for observation in request.results ?? [] {
        if let line = observation.topCandidates(1).first?.string { print("\(index)\t\(line)") }
    }
}
