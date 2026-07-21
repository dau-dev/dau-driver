import Foundation
import SystemExtensions

// Minimal host tool: request activation (or with "deactivate", removal) of the
// com.dau-dev.xdma DriverKit extension embedded in this app bundle.
final class RequestDelegate: NSObject, OSSystemExtensionRequestDelegate {
    func request(_ request: OSSystemExtensionRequest, actionForReplacingExtension existing: OSSystemExtensionProperties, withExtension ext: OSSystemExtensionProperties) -> OSSystemExtensionRequest.ReplacementAction {
        print("replacing \(existing.bundleShortVersion) with \(ext.bundleShortVersion)")
        return .replace
    }

    func requestNeedsUserApproval(_ request: OSSystemExtensionRequest) {
        print("needs approval: System Settings > Privacy & Security > allow the extension")
    }

    func request(_ request: OSSystemExtensionRequest, didFinishWithResult result: OSSystemExtensionRequest.Result) {
        print("finished: \(result == .completed ? "completed" : "will complete after reboot")")
        exit(0)
    }

    func request(_ request: OSSystemExtensionRequest, didFailWithError error: Error) {
        print("failed: \(error)")
        exit(1)
    }
}

let identifier = "com.dau-dev.xdma"
let deactivate = CommandLine.arguments.contains("deactivate")
let request: OSSystemExtensionRequest = deactivate
    ? .deactivationRequest(forExtensionWithIdentifier: identifier, queue: .main)
    : .activationRequest(forExtensionWithIdentifier: identifier, queue: .main)
let delegate = RequestDelegate()
request.delegate = delegate
OSSystemExtensionManager.shared.submitRequest(request)
print("\(deactivate ? "deactivation" : "activation") requested for \(identifier); waiting...")
RunLoop.main.run()
