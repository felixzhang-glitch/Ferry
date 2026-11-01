import SwiftUI
import Combine

@MainActor
final class DeviceDataMemory: ObservableObject {
    @Published var selected = Set<DeviceKind>()
    @Published var sources: [DeviceKind: Set<String>] = [:]
    @Published var choices: [DeviceKind: [SourceChoice]] = [:]
    @Published var summary: [SummaryItem] = []
    @Published var page: [DeviceRecord] = []
    @Published fileprivate var pendingScope: SyncScope?
    func clear() {
        selected.removeAll(); sources.removeAll(); choices.removeAll(); summary.removeAll(); page.removeAll(); pendingScope = nil
    }
}
fileprivate struct SyncScope {
    let server: TrustedServer
    let generation: UInt64
    let kinds: [DeviceKind]
    let sources: [DeviceKind: [String]]
    let sourceNames: [DeviceKind: [String]]
    let now: Date
    var start: Date { now.addingTimeInterval(-30 * 86400) }
    var calendarEnd: Date { now.addingTimeInterval(30 * 86400) }
}
@MainActor
struct DeviceDataView: View {
    @ObservedObject var console: ConsoleSession
    let health: HealthReader
    let eventKit: EventKitReader
    @ObservedObject var memory: DeviceDataMemory
    private var selected: Set<DeviceKind> { get { memory.selected } nonmutating set { memory.selected = newValue } }
    private var sources: [DeviceKind: Set<String>] { get { memory.sources } nonmutating set { memory.sources = newValue } }
    private var choices: [DeviceKind: [SourceChoice]] { get { memory.choices } nonmutating set { memory.choices = newValue } }
    private var summary: [SummaryItem] { get { memory.summary } nonmutating set { memory.summary = newValue } }
    private var page: [DeviceRecord] { get { memory.page } nonmutating set { memory.page = newValue } }
    @State private var recordKind: DeviceKind = .activity
    @State private var nextOffset: Int?
    @State private var showingRecords = false
    private var pendingScope: SyncScope? { get { memory.pendingScope } nonmutating set { memory.pendingScope = newValue } }
    @State private var confirmSync = false
    @State private var pendingDelete: DeviceKind?
    @State private var deleteGeneration: UInt64 = 0
    @State private var firstDelete = false
    @State private var secondDelete = false
    private var client: DeviceSyncClient { DeviceSyncClient(console: console) }

    var body: some View {
        NavigationStack {
            Form {
                Section("隐私与目标") {
                    Text(console.server?.origin ?? "先在控制台设置 HTTPS 服务器")
                        .font(.footnote).textSelection(.enabled)
                    Text("仅手动同步到自己的服务端。健康：最近30天；日历：前后30天；提醒：所选列表全部未完成及最近30天完成项。只读取，不改动设备数据，不自动生成 Ferry 任务。")
                    Text("仅上传数值、单位、时间/时区、来源，或必要标题、列表、日期与完成状态。无备注、位置、URL、参与者。手机只在内存处理，后台会取消并清空。")
                        .font(.footnote)
                    Text("健康无可读取数据可能为空或未授权，无法区分。血氧需健康库已有数据，并非手机测量。空/部分结果保留服务端旧副本；完整同步也不隐式删除历史。")
                        .font(.footnote)
                }
                Section("同步范围（不是系统权限开关）") {
                    ForEach(DeviceKind.allCases) { kind in
                        VStack(alignment: .leading, spacing: 8) {
                            Toggle(kind.label, isOn: Binding(get: { selected.contains(kind) }, set: { value in
                                if value { selected.insert(kind) } else { selected.remove(kind) }
                            }))
                            Button(kind.isHealth ? "申请此项只读健康授权" : "申请完整读取权限并刷新列表") { authorize(kind) }
                                .font(.footnote)
                            if !kind.isHealth, let lists = choices[kind] {
                                Text("选择要同步的日历/提醒列表（最多100个）").font(.caption)
                                ForEach(lists) { list in
                                    Toggle(list.title, isOn: Binding(get: { sources[kind, default: []].contains(list.id) }, set: { enabled in
                                        var current = sources[kind, default: []]
                                        if enabled, current.count < 100 { current.insert(list.id) } else { current.remove(list.id) }
                                        sources[kind] = current
                                    })).font(.footnote)
                                }
                                if lists.isEmpty { Text("没有可读取列表。请核对系统完整访问权限。").font(.caption) }
                            }
                            if kind == .activity { Text("使用 HealthKit 每日合并统计，不按来源重复累加。").font(.caption) }
                            if kind.isHealth && kind != .activity { Text("读取此指标的所有健康来源，保留来源标识。").font(.caption) }
                        }
                    }
                }.disabled(console.busy)
                Section {
                    Button("确认范围并手动同步") {
                        guard let server = console.server else { return }
                        let kinds = DeviceKind.allCases.filter { selected.contains($0) }
                        guard kinds.allSatisfy({ $0.isHealth || !(sources[$0] ?? []).isEmpty }) else {
                            console.message = "请为日历/提醒选择至少一个列表。"; return
                        }
                        let names = Dictionary(uniqueKeysWithValues: choices.map { kind, lists in
                            (kind, lists.filter { sources[kind, default: []].contains($0.id) }.map(\.title))
                        })
                        pendingScope = SyncScope(server: server, generation: console.generation, kinds: kinds,
                                                 sources: sources.mapValues { $0.sorted() }, sourceNames: names, now: Date())
                        confirmSync = true
                    }.disabled(console.busy || selected.isEmpty || console.server == nil || console.suspended)
                    if console.busy { ProgressView("处理中…") }
                    Text(console.message).font(.footnote)
                    if console.busy { Button("取消操作") { console.invalidate() } }
                }
                Section("本设备服务端副本（含保留历史）") {
                    Button("刷新摘要") { refreshSummary() }.disabled(console.busy || console.server == nil)
                    ForEach(summary) { item in
                        VStack(alignment: .leading, spacing: 6) {
                            Text("\(item.kind.label)：\(item.record_count) 条")
                            Text("最后同步 \(Date(timeIntervalSince1970: item.last_sync).formatted())；\(item.completeness.rawValue) / \(item.reason.rawValue)").font(.caption)
                            Text("窗口 \(Date(timeIntervalSince1970: item.window_start).formatted()) — \(Date(timeIntervalSince1970: item.window_end).formatted())；保留历史：\(item.retained_history ? "是" : "否")").font(.caption)
                            HStack {
                                Button("查看分页记录") { recordKind = item.kind; loadPage(offset: 0) }
                                Spacer()
                                Button("清除副本", role: .destructive) {
                                    pendingDelete = item.kind; deleteGeneration = console.generation; firstDelete = true
                                }
                            }.font(.footnote).disabled(console.busy)
                        }
                    }
                }
                if showingRecords {
                    Section("\(recordKind.label) · 当前页（最多100条）") {
                        Text("历史记录不代表设备当前状态；仅当前页存于内存。").font(.caption)
                        ForEach(Array(page.enumerated()), id: \.offset) { _, record in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(record.title ?? record.metric.rawValue)
                                Text(record.source_name).font(.caption)
                                if let value = record.value { Text("\(value.formatted()) \(record.unit?.rawValue ?? "")") }
                                if let start = record.start { Text(Date(timeIntervalSince1970: start).formatted()).font(.caption) }
                                if let end = record.end { Text("至 \(Date(timeIntervalSince1970: end).formatted())").font(.caption) }
                                if let day = record.start_date { Text("全天：\(day) — \(record.end_date ?? "")（结束日期不含当天）").font(.caption) }
                                if let due = record.due {
                                    Text("到期：\(due.year)-\(due.month)-\(due.day)" + (due.hour.map { " \($0):\(due.minute ?? 0)" } ?? "（仅日期）")).font(.caption)
                                }
                                if let done = record.completed { Text(done ? "已完成" : "未完成").font(.caption) }
                                if let completedAt = record.completed_at { Text("完成于 \(Date(timeIntervalSince1970: completedAt).formatted())").font(.caption) }
                                Text("时区：\(record.timezone ?? record.due?.timezone ?? "浮动/未提供")").font(.caption)
                            }.textSelection(.enabled)
                        }
                        if let nextOffset { Button("下一页") { loadPage(offset: nextOffset) }.disabled(console.busy) }
                        Button("关闭并清空记录") { page.removeAll(); showingRecords = false; nextOffset = nil }
                    }
                }
            }
            .navigationTitle("设备数据")
            .sheet(isPresented: $confirmSync, onDismiss: { pendingScope = nil }) {
                NavigationStack {
                    List {
                        if let scope = pendingScope {
                            Section("目标服务器") { Text(scope.server.origin).textSelection(.enabled) }
                            ForEach(scope.kinds) { kind in
                                Section(kind.label) {
                                    if kind == .reminders {
                                        Text("所选列表全部未完成项；已完成项：\(scope.start.formatted()) — \(scope.now.formatted())")
                                    } else {
                                        Text("\(scope.start.formatted()) — \((kind == .calendar ? scope.calendarEnd : scope.now).formatted())")
                                    }
                                    if kind.isHealth { Text(kind == .activity ? "HealthKit 每日合并统计" : "此指标全部健康来源") }
                                    else {
                                        ForEach(Array(scope.sourceNames[kind, default: []].enumerated()), id: \.offset) { _, name in Text(name) }
                                    }
                                }
                            }
                            Section("字段与完整性") {
                                Text("健康：数值、单位、时间、时区与来源。日历/提醒：标题（最多500字符）、列表、日期/时区、全天或完成状态。不上传备注、位置、URL、参与者。")
                                Text("每分项独立事务；空结果、读取错误及上限51200条/256批次标为部分。缺失记录不会删除服务端旧值。进入后台会取消。")
                            }
                            Button("确认并手动上传") {
                                confirmSync = false
                                synchronize(scope)
                                pendingScope = nil
                            }
                        }
                    }
                    .navigationTitle("确认上传范围")
                    .toolbar { ToolbarItem(placement: .cancellationAction) { Button("取消") { confirmSync = false; pendingScope = nil } } }
                }
            }
            .confirmationDialog("清除服务端副本？", isPresented: $firstDelete, titleVisibility: .visible) {
                Button("继续", role: .destructive) { secondDelete = true }
                Button("取消", role: .cancel) { pendingDelete = nil }
            } message: { Text("不会删除健康、Apple 日历或提醒事项中的原始数据。") }
            .alert("最终确认：不可撤销", isPresented: $secondDelete) {
                Button("清除所选分项全部副本", role: .destructive) { deleteCopy() }
                Button("取消", role: .cancel) { pendingDelete = nil }
            } message: { Text("服务器：\(console.server?.origin ?? "")\n分项：\(pendingDelete?.label ?? "")\n仅本设备的此分项；以后手动同步可再次上传。") }
            .onChange(of: console.generation) { _, _ in clearPrivateUI() }
        }
    }
    private func clearPrivateUI() {
        summary.removeAll(); page.removeAll(); choices.removeAll(); sources.removeAll(); selected.removeAll()
        pendingScope = nil; pendingDelete = nil; nextOffset = nil; showingRecords = false
        confirmSync = false; firstDelete = false; secondDelete = false
    }
    private func authorize(_ kind: DeviceKind) {
        let expected = console.generation
        console.perform {
            if kind.isHealth {
                try await health.authorize(kind)
                guard expected == console.generation else { return }
                console.message = "已完成系统授权流程；成功不代表有可读取健康数据。"
            } else {
                try await eventKit.authorize(kind)
                guard expected == console.generation else { return }
                choices[kind] = eventKit.sources(kind)
                let available = Set(choices[kind, default: []].map(\.id))
                sources[kind] = sources[kind, default: []].intersection(available)
                console.message = "请明确选择同步列表；完整系统权限不代表会上传所有列表。"
            }
        }
    }
    private func synchronize(_ scope: SyncScope) {
        guard scope.generation == console.generation, scope.server == console.server else { return }
        console.perform {
            var statuses: [String] = []
            for kind in scope.kinds {
                try Task.checkCancellation()
                guard scope.generation == console.generation, scope.server == console.server else { throw DeviceError.cancelled }
                do {
                    let reader: DeviceReading = kind.isHealth ? health : eventKit
                    let result = try await client.sync(kind: kind, start: scope.start, end: kind == .calendar ? scope.calendarEnd : scope.now,
                                                       sources: scope.sources[kind] ?? [], reader: reader) { console.message = $0 }
                    statuses.append("\(kind.label)：\(result.record_count) 条，\(result.completeness.rawValue)/\(result.reason.rawValue)")
                } catch {
                    try Task.checkCancellation()
                    guard scope.generation == console.generation else { throw DeviceError.cancelled }
                    statuses.append("\(kind.label)：失败，其他分项独立处理")
                }
            }
            guard scope.generation == console.generation else { return }
            console.message = statuses.joined(separator: "\n")
            summary = try await client.summary()
        }
    }
    private func refreshSummary() {
        let expected = console.generation
        console.perform { let items = try await client.summary(); guard expected == console.generation else { return }; summary = items }
    }
    private func loadPage(offset: Int) {
        let expected = console.generation, kind = recordKind
        page.removeAll(); nextOffset = nil
        console.perform {
            let result = try await client.records(kind: kind, offset: offset)
            guard expected == console.generation else { return }
            page = result.records; nextOffset = result.next_offset; showingRecords = true
        }
    }
    private func deleteCopy() {
        guard let kind = pendingDelete, deleteGeneration == console.generation else { return }
        let expected = console.generation
        pendingDelete = nil
        console.perform {
            try await client.delete(kind: kind)
            guard expected == console.generation else { return }
            page.removeAll(); showingRecords = false; nextOffset = nil
            summary = try await client.summary()
            console.message = "已清除服务端此分项副本，设备原始数据未改动。"
        }
    }
}
