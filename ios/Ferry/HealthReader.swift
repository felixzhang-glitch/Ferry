import Foundation
import HealthKit

// One-shot callback gate: stopping an Apple query must also resume its waiter.
final class QueryGate<Value> {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Value, Error>?
    private var stop: (() -> Void)?
    private var finished = false
    private var cancelled = false
    func install(_ continuation: CheckedContinuation<Value, Error>, stop: @escaping () -> Void, start: (() -> Void)? = nil) -> Bool {
        lock.lock()
        if cancelled {
            lock.unlock()
            continuation.resume(throwing: CancellationError())
            return false
        }
        self.continuation = continuation
        self.stop = stop
        // HK execute callbacks are asynchronous. Serialize execute versus cancellation
        // so a stopped query can never be accidentally executed afterward.
        start?()
        lock.unlock()
        return true
    }
    func finish(_ result: Result<Value, Error>) {
        lock.lock()
        guard !finished, !cancelled else { lock.unlock(); return }
        finished = true
        let waiter = continuation, action = stop
        continuation = nil
        stop = nil
        lock.unlock()
        action?() // Also release a statistics collection query after its initial result.
        waiter?.resume(with: result)
    }
    func cancel() {
        lock.lock()
        guard !finished, !cancelled else { lock.unlock(); return }
        cancelled = true
        let waiter = continuation, action = stop
        continuation = nil
        stop = nil
        lock.unlock()
        action?()
        waiter?.resume(throwing: CancellationError())
    }
}
@MainActor
final class HealthReader: DeviceReading {
    private let store = HKHealthStore()
    private var queries: [UUID: () -> Void] = [:]
    private func types(_ kind: DeviceKind) throws -> [(HKQuantityType, Metric, HKUnit, QuantityUnit)] {
        let specs: [(HKQuantityTypeIdentifier, Metric, HKUnit, QuantityUnit)]
        switch kind {
        case .activity: specs = [(.stepCount, .steps, .count(), .count),
                                (.distanceWalkingRunning, .distance, .meter(), .m),
                                (.activeEnergyBurned, .active_energy, .kilocalorie(), .kcal)]
        case .heart_rate: specs = [(.heartRate, .heart_rate, HKUnit.count().unitDivided(by: .minute()), .bpm)]
        case .weight: specs = [(.bodyMass, .weight, .gramUnit(with: .kilo), .kg)]
        case .oxygen: specs = [(.oxygenSaturation, .oxygen, .percent(), .percent)]
        default: throw DeviceError.invalidRecord
        }
        return try specs.map {
            guard let type = HKObjectType.quantityType(forIdentifier: $0.0) else { throw DeviceError.unavailable }
            return (type, $0.1, $0.2, $0.3)
        }
    }
    func authorize(_ kind: DeviceKind) async throws {
        guard HKHealthStore.isHealthDataAvailable() else { throw DeviceError.unavailable }
        let read = Set(try types(kind).map { $0.0 as HKObjectType })
        // Never request write types. Success is NOT evidence of readable authorization.
        try await store.requestAuthorization(toShare: [], read: read)
        try Task.checkCancellation()
    }
    func cancel() { queries.values.forEach { $0() }; queries.removeAll() }
    func read(kind: DeviceKind, start: Date, end: Date, sources: [String]) async throws -> ReadResult {
        guard HKHealthStore.isHealthDataAvailable() else { throw DeviceError.unavailable }
        var result = ReadResult(records: [])
        for (type, metric, hkUnit, wireUnit) in try types(kind) {
            try Task.checkCancellation()
            do {
                if kind == .activity {
                    let component = try await daily(type: type, metric: metric, unit: hkUnit, wireUnit: wireUnit, start: start, end: end)
                    result.appendHealthComponent(component)
                } else {
                    let samples = try await samples(type: type, start: start, end: end)
                    if samples.count > UploadPlan.maxRecords { result.reason = .limit }
                    let component = samples.prefix(UploadPlan.maxRecords).map { sample in
                        DeviceRecord(id: sample.uuid.uuidString.lowercased(), source: sample.sourceRevision.source.bundleIdentifier,
                                     source_name: sample.sourceRevision.source.name.wirePrefix(256), metric: metric,
                                     start: sample.startDate.timeIntervalSince1970, end: sample.endDate.timeIntervalSince1970,
                                     value: sample.quantity.doubleValue(for: hkUnit) * (metric == .oxygen ? 100 : 1),
                                     unit: wireUnit, timezone: (sample.metadata?[HKMetadataKeyTimeZone] as? String) ?? TimeZone.current.identifier)
                    }
                    result.appendHealthComponent(component)
                }
            } catch is CancellationError { throw CancellationError() }
            catch { result.reason = .read_error }
        }
        try Task.checkCancellation()
        return result
    }
    private func samples(type: HKQuantityType, start: Date, end: Date) async throws -> [HKQuantitySample] {
        let gate = QueryGate<[HKQuantitySample]>(), key = UUID()
        let healthStore = store
        queries[key] = { gate.cancel() }
        defer { queries.removeValue(forKey: key) }
        return try await withTaskCancellationHandler(operation: {
            try await withCheckedThrowingContinuation { continuation in
                let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: .strictStartDate)
                let query = HKSampleQuery(sampleType: type, predicate: predicate, limit: UploadPlan.maxRecords + 1,
                                          sortDescriptors: [NSSortDescriptor(key: HKSampleSortIdentifierStartDate, ascending: true)]) { _, samples, error in
                    if let error { gate.finish(.failure(error)) }
                    else { gate.finish(.success(samples as? [HKQuantitySample] ?? [])) }
                }
                _ = gate.install(continuation, stop: { healthStore.stop(query) }, start: { healthStore.execute(query) })
            }
        }, onCancel: { gate.cancel() })
    }
    private func daily(type: HKQuantityType, metric: Metric, unit: HKUnit, wireUnit: QuantityUnit, start: Date, end: Date) async throws -> [DeviceRecord] {
        let gate = QueryGate<[DeviceRecord]>(), key = UUID()
        let healthStore = store
        queries[key] = { gate.cancel() }
        defer { queries.removeValue(forKey: key) }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = .current
        let zone = calendar.timeZone.identifier
        let anchor = calendar.startOfDay(for: start)
        return try await withTaskCancellationHandler(operation: {
            try await withCheckedThrowingContinuation { continuation in
                let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: .strictStartDate)
                // No separateBySource and no raw sample summation: use HealthKit's daily aggregate.
                let query = HKStatisticsCollectionQuery(quantityType: type, quantitySamplePredicate: predicate,
                                                       options: .cumulativeSum, anchorDate: anchor,
                                                       intervalComponents: DateComponents(day: 1))
                query.initialResultsHandler = { _, collection, error in
                    if let error { gate.finish(.failure(error)); return }
                    var output: [DeviceRecord] = []
                    collection?.enumerateStatistics(from: start, to: end) { statistic, _ in
                        guard statistic.startDate < end, let quantity = statistic.sumQuantity() else { return }
                        let begin = max(statistic.startDate, start), finish = min(statistic.endDate, end)
                        output.append(DeviceRecord(id: "\(metric.rawValue):\(Int(statistic.startDate.timeIntervalSince1970)):\(zone)",
                                                   source: "HealthKit.daily", source_name: "HealthKit daily statistics", metric: metric,
                                                   start: begin.timeIntervalSince1970, end: finish.timeIntervalSince1970,
                                                   value: quantity.doubleValue(for: unit), unit: wireUnit, timezone: zone))
                    }
                    gate.finish(.success(output))
                }
                _ = gate.install(continuation, stop: { healthStore.stop(query) }, start: { healthStore.execute(query) })
            }
        }, onCancel: { gate.cancel() })
    }
}
