import Foundation
import AppKit
import Vision

struct OCRObservation: Encodable {
    let text: String
    let candidates: [String]
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

struct OCRResult: Encodable {
    let path: String
    let observations: [OCRObservation]
}

func encode(_ result: OCRResult) throws {
    let encoder = JSONEncoder()
    let data = try encoder.encode(result)
    if let text = String(data: data, encoding: .utf8) {
        print(text)
    }
}

func ocrImage(at path: String) throws -> OCRResult {
    guard let image = NSImage(contentsOfFile: path) else {
        throw NSError(domain: "vision_ocr", code: 2, userInfo: [NSLocalizedDescriptionKey: "failed to load image: \(path)"])
    }
    var rect = NSRect(origin: .zero, size: image.size)
    guard let cgImage = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else {
        throw NSError(domain: "vision_ocr", code: 3, userInfo: [NSLocalizedDescriptionKey: "failed to build cgImage: \(path)"])
    }

    let request = VNRecognizeTextRequest()
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = false
    request.recognitionLevel = .accurate

    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    try handler.perform([request])

    let observations = (request.results ?? []).sorted {
        let a = $0.boundingBox
        let b = $1.boundingBox
        if abs(a.minY - b.minY) > 0.02 {
            return a.minY > b.minY
        }
        return a.minX < b.minX
    }.map { item in
        OCRObservation(
            text: item.topCandidates(1).first?.string ?? "",
            candidates: item.topCandidates(5).map(\.string),
            x: item.boundingBox.minX,
            y: item.boundingBox.minY,
            width: item.boundingBox.width,
            height: item.boundingBox.height
        )
    }

    return OCRResult(path: path, observations: observations)
}

let paths = Array(CommandLine.arguments.dropFirst())
if paths.isEmpty {
    fputs("usage: vision_ocr.swift <image> [<image> ...]\n", stderr)
    exit(1)
}

for path in paths {
    do {
        try encode(ocrImage(at: path))
    } catch {
        fputs("\(error.localizedDescription)\n", stderr)
        exit(1)
    }
}
