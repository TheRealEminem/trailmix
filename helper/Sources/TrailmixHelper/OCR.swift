import Foundation
import Vision

/// Text recognition on this Mac, for the backend's attachments (attachments.py): a photo of a whiteboard or a scanned
/// page becomes searchable without leaving the computer.
///   TrailmixHelper --ocr photo.heic [page2.jpg …]
/// prints each picture's text, the pictures separated by a form feed; exits 1 if a picture can't be read.
enum OCR {
    static func run(_ args: [String]) {
        let files = Array(args.drop { $0 != "--ocr" }.dropFirst())
        guard !files.isEmpty else {
            FileHandle.standardError.write("usage: --ocr <picture> [<picture> …]\n".data(using: .utf8)!)
            exit(2)
        }
        var pages: [String] = []
        for file in files {
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.usesLanguageCorrection = true
            request.automaticallyDetectsLanguage = true
            do {
                try VNImageRequestHandler(url: URL(fileURLWithPath: file)).perform([request])
            } catch {
                FileHandle.standardError.write("\(file): \(error.localizedDescription)\n".data(using: .utf8)!)
                exit(1)
            }
            let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
            pages.append(lines.joined(separator: "\n"))
        }
        print(pages.joined(separator: "\u{0C}"))
        exit(0)
    }
}
