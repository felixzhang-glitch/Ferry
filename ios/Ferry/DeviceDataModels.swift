import Foundation

// Field names intentionally match core/device_data_contract.py verbatim.
enum DeviceKind: String, Codable, CaseIterable, Identifiable {
    case activity, heart_rate, weight, oxygen, calendar, reminders
    var id: String { rawValue }
    var label: String {
        switch self {
        case .activity: return "活动（步数、距离、活动能量）"
        case .heart_rate: return "心率"
        case .weight: return "体重"
        case .oxygen: return "血氧"
        case .calendar: return "Apple 日历"
        case .reminders: return "Apple 提醒事项"
        }
    }
    var isHealth: Bool { self != .calendar && self != .reminders }
}
enum Metric: String, Codable { case steps, distance, active_energy, heart_rate, weight, oxygen, event, reminder }
enum QuantityUnit: String, Codable { case count, m, kcal, bpm, kg, percent }
enum Completeness: String, Codable { case complete, partial }
enum PartialReason: String, Codable { case none, no_readable_data, limit, cancelled, read_error, permission }
enum DeviceError: LocalizedError {
    case invalidAddress, invalidRecord, limit, scopeLimit, cancelled, authentication, server(Int), unavailable, permission
    var errorDescription: String? {
        switch self {
        case .invalidAddress: return "仅接受不含凭证、路径、查询或片段的 HTTPS 服务器根地址。"
        case .invalidRecord: return "读取结果不符合服务端契约，未上传。"
        case .limit: return "达到同步上限；仅上传有界部分结果。"
        case .scopeLimit: return "同步范围超过4 KiB控制请求上限，请减少所选日历/提醒列表。"
        case .cancelled: return "操作已取消，请重新登录或重试。"
        case .authentication: return "请先在控制台登录；会话已失效或没有控制台权限。"
        case .server(let code): return "服务端请求失败（HTTP \(code)），未显示响应正文。"
        case .unavailable: return "此设备不支持健康数据读取。"
        case .permission: return "需要完整日历/提醒访问权限才能读取；App 不会写入。"
        }
    }
}
struct DateParts: Codable, Equatable {
    var year: Int
    var month: Int
    var day: Int
    var hour: Int? = nil
    var minute: Int? = nil
    var timezone: String? = nil
    func validate() throws {
        guard (1970...2100).contains(year), (1...12).contains(month), (1...31).contains(day),
              (hour == nil) == (minute == nil), hour.map({ (0...23).contains($0) }) ?? true,
              minute.map({ (0...59).contains($0) }) ?? true, (timezone?.unicodeScalars.count ?? 0) <= 96 else { throw DeviceError.invalidRecord }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let parts = DateComponents(year: year, month: month, day: day)
        guard let date = calendar.date(from: parts) else { throw DeviceError.invalidRecord }
        let normalized = calendar.dateComponents([.year, .month, .day], from: date)
        guard normalized.year == year, normalized.month == month, normalized.day == day else { throw DeviceError.invalidRecord }
    }
}
struct DeviceRecord: Codable, Equatable {
    enum CodingKeys: String, CodingKey, CaseIterable {
        case id, source, source_name, metric, start, end, value, unit, title, all_day,
             start_date, end_date, completed, completed_at, due, timezone
    }
    var id: String
    var source: String
    var source_name: String = ""
    var metric: Metric
    var start: Double? = nil
    var end: Double? = nil
    var value: Double? = nil
    var unit: QuantityUnit? = nil
    var title: String? = nil
    var all_day: Bool? = nil
    var start_date: String? = nil
    var end_date: String? = nil
    var completed: Bool? = nil
    var completed_at: Double? = nil
    var due: DateParts? = nil
    var timezone: String? = nil

    func validate() throws {
        guard (1...256).contains(id.unicodeScalars.count), (1...256).contains(source.unicodeScalars.count), source_name.unicodeScalars.count <= 256,
              (title?.unicodeScalars.count ?? 0) <= 500, (timezone?.unicodeScalars.count ?? 0) <= 96 else { throw DeviceError.invalidRecord }
        for timestamp in [start, end, completed_at].compactMap({ $0 }) {
            guard timestamp.isFinite, (0...4102444800).contains(timestamp) else { throw DeviceError.invalidRecord }
        }
        if let end { guard let start, end >= start else { throw DeviceError.invalidRecord } }
        let numeric: (QuantityUnit, Double)?
        switch metric {
        case .steps: numeric = (.count, 1e7)
        case .distance: numeric = (.m, 1e7)
        case .active_energy: numeric = (.kcal, 1e6)
        case .heart_rate: numeric = (.bpm, 1000)
        case .weight: numeric = (.kg, 2000)
        case .oxygen: numeric = (.percent, 100)
        default: numeric = nil
        }
        if let numeric {
            guard start != nil, end != nil, unit == numeric.0, let value, value.isFinite,
                  (0...numeric.1).contains(value), title == nil, all_day == nil, start_date == nil,
                  end_date == nil, completed == nil, completed_at == nil, due == nil else { throw DeviceError.invalidRecord }
        } else {
            guard value == nil, unit == nil, title != nil else { throw DeviceError.invalidRecord }
            if metric == .event {
                guard start != nil, end != nil, all_day != nil, completed == nil, completed_at == nil, due == nil else { throw DeviceError.invalidRecord }
                if all_day == true {
                    guard let first = start_date, let last = end_date, Self.validDay(first), Self.validDay(last), first < last else { throw DeviceError.invalidRecord }
                } else if start_date != nil || end_date != nil { throw DeviceError.invalidRecord }
            } else {
                guard let completed, completed ? completed_at != nil : completed_at == nil,
                      start == nil, end == nil, all_day == nil, start_date == nil, end_date == nil else { throw DeviceError.invalidRecord }
                try due?.validate()
            }
        }
    }
    private static func validDay(_ value: String) -> Bool {
        let parts = value.split(separator: "-", omittingEmptySubsequences: false)
        guard value.count == 10, parts.count == 3, parts[0].count == 4, parts[1].count == 2, parts[2].count == 2,
              let year = Int(parts[0]), let month = Int(parts[1]), let day = Int(parts[2]) else { return false }
        return (try? DateParts(year: year, month: month, day: day).validate()) != nil
    }
}
struct SyncStart: Codable {
    var version: Int = 1
    var device_id: String
    var kind: DeviceKind
    var window_start: Double
    var window_end: Double
    var timezone: String
    var sources: [String]
    func validate() throws {
        guard version == 1, device_id.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil,
              window_start.isFinite, window_end.isFinite, window_start >= 0, window_end <= 4102444800,
              window_end > window_start, window_end - window_start <= 62 * 86400,
              (1...96).contains(timezone.unicodeScalars.count), sources.count <= 100,
              Set(sources).count == sources.count, sources.allSatisfy({ (1...256).contains($0.unicodeScalars.count) }) else { throw DeviceError.invalidRecord }
    }
}
struct Batch: Codable { var records: [DeviceRecord] }
struct SyncCommit: Codable {
    var batch_count: Int
    var record_count: Int
    var completeness: Completeness
    var reason: PartialReason
    func validate() throws {
        guard (0...256).contains(batch_count), (0...51200).contains(record_count),
              completeness == .complete ? reason == .none : reason != .none,
              record_count != 0 || (completeness == .partial && batch_count == 0) else { throw DeviceError.invalidRecord }
    }
}
struct DeleteData: Codable { var device_id: String; var kind: DeviceKind }
struct SyncStarted: Codable { var sync_id: String }
struct OKResponse: Codable { var ok: Bool }
struct SummaryItem: Codable, Identifiable {
    var device_id: String
    var kind: DeviceKind
    var record_count: Int
    var last_sync: Double
    var completeness: Completeness
    var reason: PartialReason
    var window_start: Double
    var window_end: Double
    var retained_history: Bool
    var id: String { device_id + ":" + kind.rawValue }
}
struct SummaryResponse: Codable { var items: [SummaryItem] }
struct RecordsResponse: Codable { var records: [DeviceRecord]; var offset: Int; var next_offset: Int? }
struct ReadResult {
    var records: [DeviceRecord]
    var reason: PartialReason = .none
    var completeness: Completeness { reason == .none && !records.isEmpty ? .complete : .partial }
    var effectiveReason: PartialReason { records.isEmpty && reason == .none ? .no_readable_data : reason }
    mutating func appendHealthComponent(_ component: [DeviceRecord]) {
        records.append(contentsOf: component)
        // An empty selected metric is unknown (no data OR no read permission),
        // even when other metrics yielded records. Never fill it with zeroes.
        if component.isEmpty && reason == .none { reason = .no_readable_data }
    }
}
struct UploadPlan {
    static let maxRecords = 51200
    // Wire cap is 256KiB. Leave headroom for server canonicalization (e.g.
    // whole-valued Double fields gain a .0 suffix in Python JSON).
    static let maxBytes = 240 * 1024
    var batches: [Data]
    var commit: SyncCommit
    static func make(_ result: ReadResult) throws -> UploadPlan {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        let prefix = Data("{\"records\":[".utf8), suffix = Data("]}".utf8)
        var batches: [Data] = [], current = prefix
        var currentCount = 0, count = 0
        var limited = result.records.count > maxRecords || result.reason == .limit
        for record in result.records.prefix(maxRecords) {
            try record.validate()
            let encoded = try encoder.encode(record)
            guard prefix.count + encoded.count + suffix.count <= maxBytes else { throw DeviceError.invalidRecord }
            if currentCount == 200 || current.count + (currentCount == 0 ? 0 : 1) + encoded.count + suffix.count > maxBytes {
                current.append(suffix)
                batches.append(current)
                current = prefix
                currentCount = 0
                if batches.count == 256 { limited = true; break }
            }
            if currentCount > 0 { current.append(0x2c) }
            current.append(encoded)
            currentCount += 1
            count += 1
        }
        if currentCount > 0 { current.append(suffix); batches.append(current) }
        let reason: PartialReason = limited ? .limit : result.effectiveReason
        let commit = SyncCommit(batch_count: batches.count, record_count: count,
                                completeness: reason == .none ? .complete : .partial, reason: reason)
        try commit.validate()
        return UploadPlan(batches: batches, commit: commit)
    }
}
protocol DeviceReading {
    func read(kind: DeviceKind, start: Date, end: Date, sources: [String]) async throws -> ReadResult
}
private struct WireKey: CodingKey {
    let stringValue: String
    let intValue: Int? = nil
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}
extension DeviceRecord {
    init(from decoder: Decoder) throws {
        let raw = try decoder.container(keyedBy: WireKey.self)
        let allowed = Set(CodingKeys.allCases.map(\.rawValue))
        guard raw.allKeys.allSatisfy({ allowed.contains($0.stringValue) }) else { throw DeviceError.invalidRecord }
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        source = try values.decode(String.self, forKey: .source)
        source_name = values.contains(.source_name) ? try values.decode(String.self, forKey: .source_name) : ""
        metric = try values.decode(Metric.self, forKey: .metric)
        start = try values.decodeIfPresent(Double.self, forKey: .start)
        end = try values.decodeIfPresent(Double.self, forKey: .end)
        value = try values.decodeIfPresent(Double.self, forKey: .value)
        unit = try values.decodeIfPresent(QuantityUnit.self, forKey: .unit)
        title = try values.decodeIfPresent(String.self, forKey: .title)
        all_day = try values.decodeIfPresent(Bool.self, forKey: .all_day)
        start_date = try values.decodeIfPresent(String.self, forKey: .start_date)
        end_date = try values.decodeIfPresent(String.self, forKey: .end_date)
        completed = try values.decodeIfPresent(Bool.self, forKey: .completed)
        completed_at = try values.decodeIfPresent(Double.self, forKey: .completed_at)
        due = try values.decodeIfPresent(DateParts.self, forKey: .due)
        timezone = try values.decodeIfPresent(String.self, forKey: .timezone)
        try validate()
    }
}
