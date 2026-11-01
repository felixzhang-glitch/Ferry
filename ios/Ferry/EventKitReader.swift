import Foundation
import EventKit
import CryptoKit

extension String {
    func wirePrefix(_ count: Int) -> String { String(String.UnicodeScalarView(unicodeScalars.prefix(count))) }
}
struct SourceChoice: Identifiable {
    let id: String
    let title: String
}
@MainActor
final class EventKitReader: DeviceReading {
    private let store = EKEventStore()
    private var queries: [UUID: () -> Void] = [:]
    func authorized(_ kind: DeviceKind) -> Bool {
        // Only fullAccess is accepted. Future limited statuses do not imply full readability.
        EKEventStore.authorizationStatus(for: kind == .calendar ? .event : .reminder) == .fullAccess
    }
    func authorize(_ kind: DeviceKind) async throws {
        let granted: Bool
        if kind == .calendar { granted = try await store.requestFullAccessToEvents() }
        else { granted = try await store.requestFullAccessToReminders() }
        try Task.checkCancellation()
        guard granted, authorized(kind) else { throw DeviceError.permission }
    }
    func sources(_ kind: DeviceKind) -> [SourceChoice] {
        guard authorized(kind) else { return [] }
        return store.calendars(for: kind == .calendar ? .event : .reminder)
            .filter { (1...256).contains($0.calendarIdentifier.unicodeScalars.count) }
            .map { SourceChoice(id: $0.calendarIdentifier, title: $0.title.wirePrefix(256)) }
            .sorted { $0.title.localizedStandardCompare($1.title) == .orderedAscending }
    }
    func cancel() { queries.values.forEach { $0() }; queries.removeAll() }
    private func stable(_ value: String) -> String { SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined() }
    private func day(_ date: Date, zone: TimeZone) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", parts.year!, parts.month!, parts.day!)
    }
    func read(kind: DeviceKind, start: Date, end: Date, sources: [String]) async throws -> ReadResult {
        guard authorized(kind) else { return ReadResult(records: [], reason: .permission) }
        guard !sources.isEmpty, sources.count <= 100 else { throw DeviceError.invalidRecord }
        let calendars = store.calendars(for: kind == .calendar ? .event : .reminder).filter { sources.contains($0.calendarIdentifier) }
        var reason: PartialReason = Set(calendars.map(\.calendarIdentifier)) == Set(sources) ? .none : .permission
        guard !calendars.isEmpty else { return ReadResult(records: [], reason: .permission) }
        var records: [DeviceRecord] = [], seen = Set<String>()
        if kind == .calendar {
            try Task.checkCancellation()
            let events = store.events(matching: store.predicateForEvents(withStart: start, end: end, calendars: calendars))
            for event in events {
                try Task.checkCancellation()
                let zone = event.timeZone ?? .current
                // Detached/rescheduled recurrence uses the original occurrence date, not
                // its edited start. Non-recurring IDs remain stable when their times change.
                let occurrence = event.hasRecurrenceRules || event.isDetached
                    ? String((event.occurrenceDate ?? event.startDate).timeIntervalSince1970) : "single"
                let id = stable(event.calendar.calendarIdentifier + ":" + event.calendarItemIdentifier + ":" + occurrence)
                guard seen.insert(id).inserted else { continue }
                if records.count == UploadPlan.maxRecords { reason = .limit; break }
                let record = DeviceRecord(id: id, source: event.calendar.calendarIdentifier,
                                          source_name: event.calendar.title.wirePrefix(256), metric: .event,
                                          start: event.startDate.timeIntervalSince1970, end: event.endDate.timeIntervalSince1970,
                                          title: (event.title ?? "").wirePrefix(500), all_day: event.isAllDay,
                                          start_date: event.isAllDay ? day(event.startDate, zone: zone) : nil,
                                          end_date: event.isAllDay ? day(event.endDate, zone: zone) : nil,
                                          timezone: event.timeZone?.identifier)
                do { try record.validate(); records.append(record) } catch { if reason != .limit { reason = .read_error } }
            }
        } else {
            // EventKit offers no bounded fetch limit. Each system fetch is in memory only;
            // serialization is capped at 51,200 unique records, never notes/location/URL.
            let predicates = [store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: calendars),
                              store.predicateForCompletedReminders(withCompletionDateStarting: start, ending: end, calendars: calendars)]
            for predicate in predicates {
                let reminders: [EKReminder]
                do { reminders = try await fetch(predicate) }
                catch is CancellationError { throw CancellationError() }
                catch { reason = .read_error; continue }
                for reminder in reminders {
                    try Task.checkCancellation()
                    let id = stable(reminder.calendar.calendarIdentifier + ":" + reminder.calendarItemIdentifier)
                    guard seen.insert(id).inserted else { continue }
                    if records.count == UploadPlan.maxRecords { reason = .limit; break }
                    do {
                        var due: DateParts?
                        if let parts = reminder.dueDateComponents {
                            guard let year = parts.year, let month = parts.month, let day = parts.day else { throw DeviceError.invalidRecord }
                            due = DateParts(year: year, month: month, day: day, hour: parts.hour,
                                            minute: parts.hour == nil ? nil : (parts.minute ?? 0), timezone: parts.timeZone?.identifier)
                        }
                        let record = DeviceRecord(id: id, source: reminder.calendar.calendarIdentifier,
                                                  source_name: reminder.calendar.title.wirePrefix(256), metric: .reminder,
                                                  title: (reminder.title ?? "").wirePrefix(500), completed: reminder.isCompleted,
                                                  completed_at: reminder.isCompleted ? reminder.completionDate?.timeIntervalSince1970 : nil,
                                                  due: due, timezone: reminder.dueDateComponents?.timeZone?.identifier)
                        try record.validate()
                        records.append(record)
                    } catch { if reason != .limit { reason = .read_error } }
                }
                if reason == .limit { break }
            }
        }
        try Task.checkCancellation()
        return ReadResult(records: records, reason: reason)
    }
    private func fetch(_ predicate: NSPredicate) async throws -> [EKReminder] {
        let gate = QueryGate<[EKReminder]>(), key = UUID()
        queries[key] = { gate.cancel() }
        defer { queries.removeValue(forKey: key) }
        return try await withTaskCancellationHandler(operation: {
            try await withCheckedThrowingContinuation { continuation in
                // Cancellation is installed before fetching; token holder handles cancellation races.
                let token = EventFetchToken(store: store)
                if gate.install(continuation, stop: { token.cancel() }) {
                    token.set(store.fetchReminders(matching: predicate) { items in
                        if let items { gate.finish(.success(items)) }
                        else { gate.finish(.failure(DeviceError.invalidRecord)) }
                    })
                }
            }
        }, onCancel: { gate.cancel() })
    }
}
private final class EventFetchToken {
    private let lock = NSLock()
    private let store: EKEventStore
    private var token: Any?
    private var cancelled = false
    init(store: EKEventStore) { self.store = store }
    func set(_ token: Any) {
        lock.lock(); self.token = token; let cancelNow = cancelled; lock.unlock()
        if cancelNow { store.cancelFetchRequest(token) }
    }
    func cancel() {
        lock.lock(); cancelled = true; let value = token; lock.unlock()
        if let value { store.cancelFetchRequest(value) }
    }
}
