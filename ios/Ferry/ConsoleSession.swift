import Foundation
import WebKit
import Combine

struct TrustedServer: Equatable {
    let root: URL
    let origin: String
    init(_ address: String) throws {
        guard address == address.trimmingCharacters(in: .whitespacesAndNewlines),
              !address.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              let components = URLComponents(string: address), components.scheme == "https",
              let host = components.host, !host.isEmpty, host.range(of: "^[A-Za-z0-9.-]+$", options: .regularExpression) != nil,
              !host.hasPrefix("."), !host.hasSuffix("."), !host.contains(".."),
              components.user == nil, components.password == nil,
              components.path.isEmpty || components.path == "/",
              components.query == nil, components.fragment == nil,
              components.port.map({ (1...65535).contains($0) }) ?? true else { throw DeviceError.invalidAddress }
        var clean = URLComponents()
        clean.scheme = "https"
        clean.host = host.lowercased()
        clean.port = components.port == 443 ? nil : components.port
        guard let url = clean.url else { throw DeviceError.invalidAddress }
        root = url
        origin = url.absoluteString
    }
    func owns(_ url: URL) -> Bool {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.user == nil, parts.password == nil else { return false }
        return parts.scheme == "https" && parts.host?.lowercased() == root.host?.lowercased()
            && (parts.port ?? 443) == (root.port ?? 443)
    }
    func url(path: String, query: [URLQueryItem] = []) throws -> URL {
        guard path.hasPrefix("/"), !path.hasPrefix("//"), !path.contains(".."), !path.contains("%"), !path.contains("\\") else { throw DeviceError.invalidAddress }
        var parts = URLComponents(url: root, resolvingAgainstBaseURL: false)!
        parts.path = path
        parts.queryItems = query.isEmpty ? nil : query
        guard let url = parts.url, owns(url) else { throw DeviceError.invalidAddress }
        return url
    }
    func accepts(_ cookie: HTTPCookie) -> Bool {
        let domain = cookie.domain.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "."))
        let host = root.host!.lowercased()
        return cookie.name == "obs_session" && cookie.isSecure
            && host == domain && cookie.path == "/"
            && (cookie.expiresDate.map { $0 > Date() } ?? true)
    }
}
final class NoRedirectDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil) // Reject even same-origin redirects for native API requests.
    }
}
struct ConsoleLease {
    let server: TrustedServer
    let generation: UInt64
    let cookie: String
    let csrf: String
}
@MainActor
final class ConsoleSession: NSObject, ObservableObject, WKHTTPCookieStoreObserver {
    @Published private(set) var server: TrustedServer?
    @Published private(set) var generation: UInt64 = 0
    @Published private(set) var busy = false
    @Published var message = ""
    @Published private(set) var suspended = false
    let websiteStore = WKWebsiteDataStore.default()
    let deviceID: String
    private let redirectDelegate = NoRedirectDelegate()
    private var transport: URLSession!
    private var cookieValue: String?
    private var observedCookie: String?
    private var removingCookies = false
    private var csrf: String?
    private var work: Task<Void, Never>?
    var cancelReaders: (() -> Void)?

    override init() {
        if let saved = UserDefaults.standard.string(forKey: "ferry.deviceID"),
           saved.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil { deviceID = saved }
        else {
            deviceID = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
            UserDefaults.standard.set(deviceID, forKey: "ferry.deviceID")
        }
        if let address = UserDefaults.standard.string(forKey: "ferry.server") { server = try? TrustedServer(address) }
        super.init()
        renewTransport()
        websiteStore.httpCookieStore.add(self)
        cookiesDidChange(in: websiteStore.httpCookieStore)
    }
    private func renewTransport() {
        let config = URLSessionConfiguration.ephemeral
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 120
        transport = URLSession(configuration: config, delegate: redirectDelegate, delegateQueue: nil)
    }
    func invalidate() {
        generation &+= 1
        work?.cancel()
        work = nil
        busy = false
        cancelReaders?()
        cookieValue = nil
        csrf = nil
        transport.invalidateAndCancel()
        renewTransport()
    }
    func setServer(_ address: String) throws {
        let trusted = try TrustedServer(address)
        invalidate()
        server = trusted
        observedCookie = nil
        UserDefaults.standard.set(trusted.origin, forKey: "ferry.server")
        cookiesDidChange(in: websiteStore.httpCookieStore)
        message = "请在控制台登录，然后手动同步。"
    }
    func setBackground(_ background: Bool) {
        suspended = background
        if background { invalidate(); message = "进入后台已取消操作，数据未缓存在手机。" }
    }
    func perform(_ operation: @escaping () async throws -> Void) {
        guard !busy, !suspended else { return }
        busy = true
        let expected = generation
        work = Task { [weak self] in
            do { try await operation() }
            catch {
                if self?.generation == expected { self?.message = (error as? DeviceError)?.localizedDescription ?? "读取或请求失败；未记录敏感错误正文。" }
            }
            guard let self, self.generation == expected else { return }
            self.busy = false
            self.work = nil
        }
    }
    private func cookies() async -> [HTTPCookie] {
        await withCheckedContinuation { continuation in
            websiteStore.httpCookieStore.getAllCookies { continuation.resume(returning: $0) }
        }
    }
    nonisolated func cookiesDidChange(in cookieStore: WKHTTPCookieStore) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            let expected = self.generation
            let all = await self.cookies()
            guard expected == self.generation, !self.removingCookies, let server = self.server else { return }
            let matched = all.filter { server.accepts($0) }
            let value = matched.count == 1 ? matched[0].value : nil
            if value != self.observedCookie || (self.cookieValue != nil && value != self.cookieValue) {
                self.observedCookie = value
                self.invalidate()
                self.message = "登录 Cookie 已变化，操作已取消；请重新手动操作。"
            }
        }
    }
    func check(_ lease: ConsoleLease) throws {
        try Task.checkCancellation()
        guard !suspended, generation == lease.generation, server == lease.server,
              cookieValue == lease.cookie else { throw DeviceError.cancelled }
    }
    func lease() async throws -> ConsoleLease {
        guard !suspended, let target = server else { throw DeviceError.invalidAddress }
        let expected = generation
        let all = await cookies()
        guard generation == expected, server == target else { throw DeviceError.cancelled }
        let matched = all.filter { target.accepts($0) }
        guard matched.count == 1 else { invalidate(); throw DeviceError.authentication }
        let cookie = matched[0].value
        if let previous = cookieValue, previous != cookie { invalidate(); throw DeviceError.cancelled }
        cookieValue = cookie
        let provisional = ConsoleLease(server: target, generation: expected, cookie: cookie, csrf: "")
        let response = try await request(provisional, method: "GET", path: "/api/observability/v1/session")
        struct SessionResponse: Decodable { let console: Bool; let console_csrf_token: String? }
        let state = try JSONDecoder().decode(SessionResponse.self, from: response)
        guard state.console, let token = state.console_csrf_token, !token.isEmpty else { invalidate(); throw DeviceError.authentication }
        try check(provisional)
        csrf = token
        return ConsoleLease(server: target, generation: expected, cookie: cookie, csrf: token)
    }
    func request(_ lease: ConsoleLease, method: String, path: String, query: [URLQueryItem] = [], body: Data? = nil) async throws -> Data {
        try check(lease)
        // Recheck expiry and logout before every request, not only observer callbacks.
        let current = await cookies().filter { lease.server.accepts($0) }
        try check(lease)
        guard current.count == 1, current[0].value == lease.cookie else { invalidate(); throw DeviceError.authentication }
        var request = URLRequest(url: try lease.server.url(path: path, query: query))
        request.httpMethod = method
        request.httpShouldHandleCookies = false
        request.setValue("obs_session=" + lease.cookie, forHTTPHeaderField: "Cookie")
        request.setValue(lease.server.origin, forHTTPHeaderField: "Origin")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if method != "GET" {
            request.setValue(lease.csrf, forHTTPHeaderField: "X-Ferry-CSRF")
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let isBatch = path.hasPrefix("/api/observability/v1/device-data/syncs/") && path.contains("/batches/")
        guard (body?.count ?? 0) <= (isBatch ? UploadPlan.maxBytes : 4096) else { throw DeviceError.limit }
        request.httpBody = body
        request.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
        let (bytes, response) = try await transport.bytes(for: request)
        try check(lease)
        guard let response = response as? HTTPURLResponse, let url = response.url, lease.server.owns(url) else { throw DeviceError.invalidAddress }
        if response.statusCode == 401 {
            if path != "/api/observability/v1/logout" { invalidate() }
            throw DeviceError.authentication
        }
        guard (200...299).contains(response.statusCode) else { throw DeviceError.server(response.statusCode) }
        guard response.value(forHTTPHeaderField: "Content-Encoding").map({ $0.lowercased() == "identity" }) ?? true else { throw DeviceError.invalidRecord }
        var data = Data()
        for try await byte in bytes {
            guard data.count < 2 * 1024 * 1024 else { throw DeviceError.limit }
            data.append(byte)
        }
        try check(lease)
        return data
    }
    func logout() async {
        guard let target = server else { return }
        // Logout needs the cookie + correct Origin, not console permission or a CSRF refresh.
        invalidate()
        let expected = generation
        let initial = await cookies()
        guard expected == generation, target == server, !Task.isCancelled else { return }
        let matched = initial.filter { target.accepts($0) }
        if matched.count == 1 {
            cookieValue = matched[0].value
            let lease = ConsoleLease(server: target, generation: expected, cookie: matched[0].value, csrf: "")
            _ = try? await request(lease, method: "POST", path: "/api/observability/v1/logout", body: Data("{}".utf8))
        }
        // Never disturb a new station or a changed account, even on the same host.
        guard expected == generation, target == server, !Task.isCancelled else { return }
        invalidate()
        let removalGeneration = generation
        removingCookies = true
        defer { removingCookies = false }
        let all = await cookies()
        guard removalGeneration == generation, target == server else { return }
        for cookie in all where cookie.name == "obs_session" && cookie.domain.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: ".")) == target.root.host {
            guard removalGeneration == generation, target == server else { return }
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                websiteStore.httpCookieStore.delete(cookie) { continuation.resume() }
            }
        }
        guard target == server else { return }
        observedCookie = nil
        invalidate() // Reload the WebView only with the now-removed local cookie.
        message = "本机已退出；若网络失败，服务端会话可能需在控制台另行撤销。"
    }
}
