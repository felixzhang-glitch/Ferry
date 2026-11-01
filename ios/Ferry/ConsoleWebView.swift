import SwiftUI
import WebKit

@MainActor
struct ConsoleWebView: UIViewRepresentable {
    @ObservedObject var console: ConsoleSession
    func makeCoordinator() -> Coordinator { Coordinator(console: console) }
    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = console.websiteStore
        // No script-message handler or native data bridge.
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = context.coordinator
        view.uiDelegate = context.coordinator
        view.scrollView.contentInsetAdjustmentBehavior = .never // Web viewport owns safe-area padding.
        view.allowsBackForwardNavigationGestures = true
        context.coordinator.load(view)
        return view
    }
    func updateUIView(_ view: WKWebView, context: Context) {
        if context.coordinator.generation != console.generation || context.coordinator.target != console.server || context.coordinator.suspended != console.suspended {
            view.stopLoading()
            context.coordinator.load(view)
        }
    }
    static func dismantleUIView(_ view: WKWebView, coordinator: Coordinator) {
        view.stopLoading(); view.navigationDelegate = nil; view.uiDelegate = nil
    }
    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        let console: ConsoleSession
        var generation: UInt64 = 0
        var target: TrustedServer?
        var suspended = false
        init(console: ConsoleSession) { self.console = console }
        func load(_ view: WKWebView) {
            generation = console.generation
            target = console.server
            suspended = console.suspended
            guard !console.suspended, let target, let url = try? target.url(path: "/observability/") else {
                view.loadHTMLString("<html><body></body></html>", baseURL: nil); return
            }
            let expected = generation
            // Block cross-origin subresources as well as navigations, including redirect
            // destinations, before WebKit can attach any cookie to an external request.
            let host = NSRegularExpression.escapedPattern(for: target.root.host!)
            let port = target.root.port.map { ":\($0)" } ?? "(:443)?"
            let allow = "^https://" + host + port + "/"
            let rules: [[String: Any]] = [
                ["trigger": ["url-filter": "^https?://"], "action": ["type": "block"]],
                ["trigger": ["url-filter": allow], "action": ["type": "ignore-previous-rules"]]
            ]
            guard let json = try? JSONSerialization.data(withJSONObject: rules), let text = String(data: json, encoding: .utf8) else { return }
            // Keep existing protections during compilation and on failure. The
            // parent recreates this view for every session generation, so a new
            // station never needs to strip rules from a still-running document.
            WKContentRuleListStore.default().compileContentRuleList(forIdentifier: "FerryTrustedOrigin", encodedContentRuleList: text) { [weak self, weak view] list, _ in
                Task { @MainActor in
                    guard let self, let view, expected == self.console.generation,
                          target == self.console.server, !self.console.suspended else { return }
                    guard let list else { self.console.message = "无法建立网页同源边界，未加载控制台。"; return }
                    view.configuration.userContentController.add(list)
                    view.load(URLRequest(url: url))
                }
            }
        }
        private func openExternal(_ url: URL) {
            guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
                  ["https", "http"].contains(parts.scheme ?? ""), parts.user == nil, parts.password == nil,
                  parts.host != nil, console.server?.owns(url) != true else { return }
            // Safari uses its own cookie store; no copied native headers or credentials.
            UIApplication.shared.open(url)
        }
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if action.request.url?.absoluteString == "about:blank" { decisionHandler(.allow); return }
            guard !console.suspended, target == console.server, generation == console.generation,
                  let url = action.request.url, target?.owns(url) == true else {
                if action.navigationType == .linkActivated, let url = action.request.url { openExternal(url) }
                decisionHandler(.cancel); return
            }
            decisionHandler(.allow)
        }
        func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
            if response.response.url?.absoluteString == "about:blank" { decisionHandler(.allow); return }
            guard !console.suspended, target == console.server, generation == console.generation,
                  let url = response.response.url, target?.owns(url) == true else { decisionHandler(.cancel); return }
            decisionHandler(.allow)
        }
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            guard let url = navigationAction.request.url else { return nil }
            if target?.owns(url) == true { webView.load(URLRequest(url: url)) }
            else if navigationAction.navigationType == .linkActivated { openExternal(url) }
            return nil
        }
        func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                     completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
            // No certificate exceptions or stored HTTP credentials.
            if challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust {
                completionHandler(.performDefaultHandling, nil)
            } else { completionHandler(.cancelAuthenticationChallenge, nil) }
        }
    }
}
