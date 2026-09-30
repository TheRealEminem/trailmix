// swift-tools-version:5.10
// Trailmix Helper: a menu bar app that records your mic plus other apps' audio (Zoom, Teams, FaceTime…)
// and streams both to the Trailmix backend. Build it with ../scripts/make-helper-app.sh (or ./trailmix helper).
import PackageDescription

let package = Package(
    name: "TrailmixHelper",
    platforms: [.macOS("14.2")],  // Core Audio process taps arrived in macOS 14.2
    targets: [
        .executableTarget(name: "TrailmixHelper", path: "Sources/TrailmixHelper"),
    ]
)
