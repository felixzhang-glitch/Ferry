import XCTest
@testable import Ferry

// Entirely synthetic fixtures and mock network/reader. No Apple authorization or live server.
final class FerryTests: XCTestCase {
    private func quantity(_ index: Int = 0) -> DeviceRecord {
        DeviceRecord(id: "mock-\(index)", source: "example.mock", source_name: "Mock", metric: .heart_rate,
                     start: 1700000000, end: 1700000001, value: 70, unit: .bpm, timezone: "Asia/Shanghai")
    }
    func testCodableUsesExactWireKeysAndOmittedNullFields() throws {
        let data = try JSONEncoder().encode(quantity())
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(Set(object.keys), Set(["id", "source", "source_name", "metric", "start", "end", "value", "unit", "timezone"]))
        XCTAssertNil(object["sourceName"])
        XCTAssertNil(object["title"])
        XCTAssertEqual(try JSONDecoder().decode(DeviceRecord.self, from: data), quantity())
        let start = SyncStart(device_id: String(repeating: "a", count: 32), kind: .heart_rate,
                              window_start: 1700000000, window_end: 1700000100, timezone: "UTC", sources: [])
        try start.validate()
        let startJSON = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(start)) as? [String: Any])
        XCTAssertEqual(Set(startJSON.keys), Set(["version", "device_id", "kind", "window_start", "window_end", "timezone", "sources"]))
        XCTAssertEqual(startJSON["version"] as? Int, 1)
    }
    func testStrictRecordDecodeAndDefaultSourceName() throws {
        let valid = "{\"id\":\"mock\",\"source\":\"mock-list\",\"metric\":\"reminder\",\"title\":\"Mock\",\"completed\":false}"
        let record = try JSONDecoder().decode(DeviceRecord.self, from: Data(valid.utf8))
        XCTAssertEqual(record.source_name, "")
        let extra = String(valid.dropLast()) + ",\"notes\":\"must-not-be-accepted\"}"
        XCTAssertThrowsError(try JSONDecoder().decode(DeviceRecord.self, from: Data(extra.utf8)))
        let null = String(valid.dropLast()) + ",\"source_name\":null}"
        XCTAssertThrowsError(try JSONDecoder().decode(DeviceRecord.self, from: Data(null.utf8)))
    }
    func testRedirectDelegateNeverFollows() throws {
        let transport = URLSession(configuration: .ephemeral)
        defer { transport.invalidateAndCancel() }
        let url = URL(string: "https://mock.example.com/start")!
        let task = transport.dataTask(with: url) // Never resumed: no network request.
        let response = try XCTUnwrap(HTTPURLResponse(url: url, statusCode: 302, httpVersion: nil, headerFields: nil))
        for destination in ["https://mock.example.com/next", "https://external.example.com/", "http://mock.example.com/"] {
            var called = false
            NoRedirectDelegate().urlSession(transport, task: task, willPerformHTTPRedirection: response,
                                            newRequest: URLRequest(url: URL(string: destination)!)) { request in
                called = true; XCTAssertNil(request)
            }
            XCTAssertTrue(called)
        }
    }
    @MainActor
    func testPrivateMemoryClearsWithoutViewLifecycle() async {
        let memory = DeviceDataMemory()
        memory.page = [quantity()]
        memory.choices = [.calendar: [SourceChoice(id: "mock", title: "Mock list")]]
        memory.sources = [.calendar: ["mock"]]
        memory.selected = [.calendar, .heart_rate]
        memory.clear()
        XCTAssertTrue(memory.page.isEmpty)
        XCTAssertTrue(memory.summary.isEmpty)
        XCTAssertTrue(memory.choices.isEmpty)
        XCTAssertTrue(memory.sources.isEmpty)
        XCTAssertTrue(memory.selected.isEmpty)
    }
    func testDateOnlyAndAllDayRecurrenceFixtures() throws {
        let reminder = DeviceRecord(id: "mock-r", source: "mock-list", metric: .reminder, title: "Mock reminder",
                                    completed: false, due: DateParts(year: 2026, month: 10, day: 28))
        try reminder.validate()
        let decoded = try JSONDecoder().decode(DeviceRecord.self, from: JSONEncoder().encode(reminder))
        XCTAssertNil(decoded.due?.hour)
        XCTAssertNil(decoded.due?.timezone)
        let event = DeviceRecord(id: "mock-occurrence-1", source: "mock-calendar", metric: .event,
                                 start: 1700000000, end: 1700086400, title: "Mock all day", all_day: true,
                                 start_date: "2023-11-15", end_date: "2023-11-16", timezone: "Asia/Shanghai")
        try event.validate()
        XCTAssertEqual(try JSONDecoder().decode(DeviceRecord.self, from: JSONEncoder().encode(event)), event)
        var second = event; second.id = "mock-occurrence-2"; second.start = 1700086400; second.end = 1700172800
        XCTAssertNotEqual(event.id, second.id)
        XCTAssertThrowsError(try DateParts(year: 2026, month: 2, day: 30).validate())
    }
    func testResponseFixturesAndStrictEnums() throws {
        let summary = Data("""
        {"items":[{"device_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","kind":"oxygen","record_count":1,"last_sync":1700000000.5,"completeness":"partial","reason":"limit","window_start":1697408000.0,"window_end":1700000000.0,"retained_history":true}]}
        """.utf8)
        let response = try JSONDecoder().decode(SummaryResponse.self, from: summary)
        XCTAssertEqual(response.items[0].last_sync, 1700000000.5)
        XCTAssertTrue(response.items[0].retained_history)
        let records = try JSONDecoder().decode(RecordsResponse.self, from: Data("{\"records\":[],\"offset\":0,\"next_offset\":null}".utf8))
        XCTAssertNil(records.next_offset)
        XCTAssertThrowsError(try JSONDecoder().decode(DeviceKind.self, from: Data("\"sleep\"".utf8)))
        var oxygen = quantity(); oxygen.metric = .oxygen; oxygen.unit = .percent; oxygen.value = 98
        try oxygen.validate()
        oxygen.value = 101
        XCTAssertThrowsError(try oxygen.validate())
    }
    func testAddressRejectionAndOrigin() throws {
        for address in ["http://example.com", "https://u:p@example.com", "https://example.com/observability/",
                        "https://example.com/?x=1", "https://example.com/#x", "https://example.com/%2f",
                        "https://example.com//", "https://example.com\\evil", "https://example.com:0",
                        "https://example.com:65536", "https://example..com", "https://example.com./"] {
            XCTAssertThrowsError(try TrustedServer(address), address)
        }
        let trusted = try TrustedServer("https://EXAMPLE.com:443/")
        XCTAssertEqual(trusted.origin, "https://example.com")
        XCTAssertTrue(trusted.owns(URL(string: "https://example.com/observability/")!))
        XCTAssertFalse(trusted.owns(URL(string: "http://example.com/")!))
        XCTAssertFalse(trusted.owns(URL(string: "https://example.com:444/")!))
        XCTAssertFalse(trusted.owns(URL(string: "https://example.com.evil.test/")!))
        XCTAssertThrowsError(try trusted.url(path: "//evil.test"))
        XCTAssertThrowsError(try trusted.url(path: "/a/../b"))
        XCTAssertThrowsError(try trusted.url(path: "/%2f"))
    }
    func testTrustedCookieFiltering() throws {
        let server = try TrustedServer("https://ferry.example.com")
        func cookie(domain: String, secure: Bool, name: String = "obs_session") throws -> HTTPCookie {
            var properties: [HTTPCookiePropertyKey: Any] = [.domain: domain, .path: "/", .name: name, .value: "synthetic-cookie"]
            if secure { properties[.secure] = "TRUE" }
            return try XCTUnwrap(HTTPCookie(properties: properties))
        }
        XCTAssertTrue(server.accepts(try cookie(domain: "ferry.example.com", secure: true)))
        XCTAssertFalse(server.accepts(try cookie(domain: "evil.test", secure: true)))
        XCTAssertFalse(server.accepts(try cookie(domain: "ferry.example.com", secure: false)))
        XCTAssertFalse(server.accepts(try cookie(domain: "ferry.example.com", secure: true, name: "other")))
    }
    func testBatchCountAndEncodedByteLimits() throws {
        let result = ReadResult(records: (0..<401).map { quantity($0) })
        let plan = try UploadPlan.make(result)
        XCTAssertEqual(plan.commit.batch_count, 3)
        XCTAssertEqual(plan.commit.record_count, 401)
        XCTAssertEqual(plan.commit.completeness, .complete)
        for data in plan.batches {
            XCTAssertLessThanOrEqual(data.count, 256 * 1024)
            XCTAssertLessThanOrEqual(try JSONDecoder().decode(Batch.self, from: data).records.count, 200)
        }
        let large = (0..<200).map { index in
            DeviceRecord(id: "mock-long-\(index)", source: "mock-calendar", source_name: String(repeating: "界", count: 256),
                         metric: .event, start: 1700000000, end: 1700000100,
                         title: String(repeating: "界", count: 500), all_day: false, timezone: "UTC")
        }
        let bytesPlan = try UploadPlan.make(ReadResult(records: large))
        XCTAssertGreaterThan(bytesPlan.batches.count, 1)
        XCTAssertEqual(bytesPlan.commit.record_count, 200)
        XCTAssertTrue(bytesPlan.batches.allSatisfy { $0.count <= 256 * 1024 })
    }
    func testPartiallyReadableActivityCannotCommitAsComplete() throws {
        var step = quantity(); step.metric = .steps; step.unit = .count
        var result = ReadResult(records: [])
        result.appendHealthComponent([step])
        result.appendHealthComponent([]) // Distance: no data OR read permission denied.
        result.appendHealthComponent([]) // Active energy: equally unknown.
        let plan = try UploadPlan.make(result)
        XCTAssertEqual(plan.commit.record_count, 1)
        XCTAssertEqual(plan.commit.completeness, .partial)
        XCTAssertEqual(plan.commit.reason, .no_readable_data)
        var readable = ReadResult(records: [])
        readable.appendHealthComponent([step])
        XCTAssertEqual(try UploadPlan.make(readable).commit.completeness, .complete)
    }
    func testTransactionRecordLimit() throws {
        let plan = try UploadPlan.make(ReadResult(records: (0..<51201).map { quantity($0) }))
        XCTAssertEqual(plan.commit.batch_count, 256)
        XCTAssertEqual(plan.commit.record_count, 51200)
        XCTAssertEqual(plan.commit.completeness, .partial)
        XCTAssertEqual(plan.commit.reason, .limit)
    }
    func testEmptyAndPartialEachKind() throws {
        for _ in DeviceKind.allCases {
            let empty = try UploadPlan.make(ReadResult(records: []))
            XCTAssertTrue(empty.batches.isEmpty)
            XCTAssertEqual(empty.commit.batch_count, 0)
            XCTAssertEqual(empty.commit.record_count, 0)
            XCTAssertEqual(empty.commit.reason, .no_readable_data)
            XCTAssertEqual(empty.commit.completeness, .partial)
        }
        let denied = try UploadPlan.make(ReadResult(records: [], reason: .permission))
        XCTAssertEqual(denied.commit.reason, .permission)
        let partial = try UploadPlan.make(ReadResult(records: [quantity()], reason: .read_error))
        XCTAssertEqual(partial.commit.record_count, 1)
        XCTAssertEqual(partial.commit.completeness, .partial)
        XCTAssertThrowsError(try SyncCommit(batch_count: 0, record_count: 0, completeness: .complete, reason: .none).validate())
    }
    @MainActor
    func testMockIndependentTransactionsAndEmptyCommit() async throws {
        let transport = MockTransport()
        let client = DeviceSyncClient(console: transport)
        let now = Date(timeIntervalSince1970: 1700000100)
        for kind in [DeviceKind.heart_rate, .calendar, .reminders] {
            let commit = try await client.sync(kind: kind, start: now.addingTimeInterval(-100), end: now,
                                               sources: kind.isHealth ? [] : ["mock-list"], reader: MockReader(result: ReadResult(records: []))) { _ in }
            XCTAssertEqual(commit.reason, .no_readable_data)
        }
        XCTAssertEqual(transport.leaseCount, 3)
        XCTAssertEqual(transport.starts.map(\.kind), [.heart_rate, .calendar, .reminders])
        XCTAssertEqual(transport.commits.count, 3)
        XCTAssertFalse(transport.paths.contains { $0.contains("/batches/") })
    }
    @MainActor
    func testLostOpenResponseRetriesTheSameScope() async throws {
        let transport = MockTransport(); transport.failFirstStart = true
        let client = DeviceSyncClient(console: transport)
        _ = try await client.sync(kind: .weight, start: Date(timeIntervalSince1970: 1700000000),
                                  end: Date(timeIntervalSince1970: 1700000100), sources: [],
                                  reader: MockReader(result: ReadResult(records: []))) { _ in }
        XCTAssertEqual(transport.startAttempts, 2)
        XCTAssertEqual(transport.starts.count, 1)
        XCTAssertEqual(transport.commits.count, 1)
    }
    @MainActor
    func testMockGenerationStopsBeforeStart() async throws {
        let transport = MockTransport()
        let reader = ChangingMockReader { transport.generation += 1 }
        do {
            _ = try await DeviceSyncClient(console: transport).sync(kind: .activity, start: Date(timeIntervalSince1970: 1700000000),
                    end: Date(timeIntervalSince1970: 1700000100), sources: [], reader: reader) { _ in }
            XCTFail("A stale lease must not open a transaction")
        } catch { XCTAssertTrue(transport.starts.isEmpty) }
    }
    @MainActor
    func testMockBatchFailureAbortsOnlyItsTransaction() async throws {
        let transport = MockTransport(); transport.failBatch = true
        let client = DeviceSyncClient(console: transport)
        do {
            _ = try await client.sync(kind: .heart_rate, start: Date(timeIntervalSince1970: 1700000000),
                                      end: Date(timeIntervalSince1970: 1700000100), sources: [],
                                      reader: MockReader(result: ReadResult(records: [quantity()]))) { _ in }
            XCTFail("Mock batch failure must not commit")
        } catch {
            XCTAssertTrue(transport.commits.isEmpty)
            XCTAssertEqual(transport.deletedTransactions, 1)
        }
        transport.failBatch = false
        _ = try await client.sync(kind: .weight, start: Date(timeIntervalSince1970: 1700000000),
                                  end: Date(timeIntervalSince1970: 1700000100), sources: [],
                                  reader: MockReader(result: ReadResult(records: []))) { _ in }
        XCTAssertEqual(transport.commits.count, 1)
    }
}
private struct MockReader: DeviceReading {
    let result: ReadResult
    func read(kind: DeviceKind, start: Date, end: Date, sources: [String]) async throws -> ReadResult { result }
}
@MainActor
private struct ChangingMockReader: DeviceReading {
    let change: () -> Void
    func read(kind: DeviceKind, start: Date, end: Date, sources: [String]) async throws -> ReadResult {
        change(); return ReadResult(records: [])
    }
}
@MainActor
private final class MockTransport: ConsoleTransport {
    let deviceID = String(repeating: "a", count: 32)
    var generation: UInt64 = 0
    var leaseCount = 0
    var starts: [SyncStart] = []
    var commits: [SyncCommit] = []
    var paths: [String] = []
    var failBatch = false
    var failFirstStart = false
    var startAttempts = 0
    var deletedTransactions = 0
    func lease() async throws -> ConsoleLease {
        leaseCount += 1
        return ConsoleLease(server: try TrustedServer("https://mock.example.com"), generation: generation, cookie: "mock", csrf: "mock")
    }
    func check(_ lease: ConsoleLease) throws {
        try Task.checkCancellation()
        guard lease.generation == generation else { throw DeviceError.cancelled }
    }
    func request(_ lease: ConsoleLease, method: String, path: String, query: [URLQueryItem], body: Data?) async throws -> Data {
        try check(lease); paths.append(path)
        if path.hasSuffix("/syncs"), method == "POST" {
            startAttempts += 1
            if !(failFirstStart && startAttempts == 2) {
                starts.append(try JSONDecoder().decode(SyncStart.self, from: XCTUnwrap(body)))
            }
            if failFirstStart && startAttempts == 1 { throw URLError(.timedOut) }
            return Data("{\"sync_id\":\"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\"}".utf8)
        }
        if path.contains("/batches/"), failBatch { throw DeviceError.server(400) }
        if path.hasSuffix("/commit") { commits.append(try JSONDecoder().decode(SyncCommit.self, from: XCTUnwrap(body))) }
        if method == "DELETE" { deletedTransactions += 1 }
        return Data("{\"ok\":true}".utf8)
    }
}
