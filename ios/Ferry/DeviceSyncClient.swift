import Foundation

@MainActor
protocol ConsoleTransport: AnyObject {
    var deviceID: String { get }
    func lease() async throws -> ConsoleLease
    func check(_ lease: ConsoleLease) throws
    func request(_ lease: ConsoleLease, method: String, path: String, query: [URLQueryItem], body: Data?) async throws -> Data
}
extension ConsoleSession: ConsoleTransport {}
@MainActor
final class DeviceSyncClient {
    private let console: ConsoleTransport
    private let base = "/api/observability/v1/device-data"
    init(console: ConsoleTransport) { self.console = console }
    private func send<T: Decodable>(_ lease: ConsoleLease, method: String, path: String,
                                    query: [URLQueryItem] = [], body: Data? = nil, retry: Bool = false) async throws -> T {
        var attempts = 0
        while true {
            try console.check(lease)
            do {
                let data = try await console.request(lease, method: method, path: path, query: query, body: body)
                try console.check(lease)
                return try JSONDecoder().decode(T.self, from: data)
            } catch {
                attempts += 1
                let retryable: Bool
                if case DeviceError.server(let status) = error { retryable = (500...599).contains(status) }
                else { retryable = error is URLError && (error as? URLError)?.code != .cancelled }
                guard retry, retryable, attempts < 3 else { throw error }
            }
        }
    }
    func sync(kind: DeviceKind, start: Date, end: Date, sources: [String], reader: DeviceReading,
              progress: (String) -> Void) async throws -> SyncCommit {
        // Refresh CSRF per independent kind, before collecting any private data.
        let lease = try await console.lease()
        let request = SyncStart(device_id: console.deviceID, kind: kind, window_start: start.timeIntervalSince1970,
                                window_end: end.timeIntervalSince1970, timezone: TimeZone.current.identifier, sources: sources)
        try request.validate()
        let control = try JSONEncoder().encode(request)
        guard control.count <= 4096 else { throw DeviceError.scopeLimit }
        progress("读取 \(kind.label)…")
        let result = try await reader.read(kind: kind, start: start, end: end, sources: sources)
        try console.check(lease)
        let plan = try UploadPlan.make(result)
        // An identical active scope reopens the same ID after a lost response.
        let opened: SyncStarted = try await send(lease, method: "POST", path: base + "/syncs", body: control, retry: true)
        guard opened.sync_id.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil else { throw DeviceError.invalidRecord }
        let transaction = base + "/syncs/" + opened.sync_id
        do {
            for (index, batch) in plan.batches.enumerated() {
                progress("\(kind.label)：上传批次 \(index + 1)/\(plan.batches.count)")
                let response: OKResponse = try await send(lease, method: "PUT", path: transaction + "/batches/\(index)", body: batch, retry: true)
                guard response.ok else { throw DeviceError.invalidRecord }
            }
            let committed: OKResponse = try await send(lease, method: "POST", path: transaction + "/commit", body: JSONEncoder().encode(plan.commit), retry: true)
            guard committed.ok else { throw DeviceError.invalidRecord }
            try console.check(lease)
            return plan.commit
        } catch {
            // Best effort abort on the SAME lease. A changed/expired session is never reused.
            let cleanup = Task { @MainActor in
                guard (try? self.console.check(lease)) != nil else { return }
                _ = try? await self.console.request(lease, method: "DELETE", path: transaction, query: [], body: nil)
            }
            await cleanup.value
            throw error
        }
    }
    func summary() async throws -> [SummaryItem] {
        let lease = try await console.lease()
        let response: SummaryResponse = try await send(lease, method: "GET", path: base + "/summary",
                                                      query: [URLQueryItem(name: "device_id", value: console.deviceID)])
        return response.items
    }
    func records(kind: DeviceKind, offset: Int) async throws -> RecordsResponse {
        guard offset >= 0 else { throw DeviceError.invalidRecord }
        let lease = try await console.lease()
        let page: RecordsResponse = try await send(lease, method: "GET", path: base + "/records", query: [
            URLQueryItem(name: "device_id", value: console.deviceID), URLQueryItem(name: "kind", value: kind.rawValue),
            URLQueryItem(name: "offset", value: String(offset)), URLQueryItem(name: "limit", value: "100")])
        guard page.offset == offset, page.records.count <= 100, page.next_offset.map({ $0 > offset }) ?? true else { throw DeviceError.invalidRecord }
        for record in page.records { try record.validate() }
        return page
    }
    func delete(kind: DeviceKind) async throws {
        let lease = try await console.lease()
        let response: OKResponse = try await send(lease, method: "DELETE", path: base + "/data",
                                                 body: JSONEncoder().encode(DeleteData(device_id: console.deviceID, kind: kind)))
        guard response.ok else { throw DeviceError.invalidRecord }
    }
}
