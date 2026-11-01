import SwiftUI

@main
@MainActor
struct FerryApp: App {
    @StateObject private var console = ConsoleSession()
    @StateObject private var memory = DeviceDataMemory()
    private let health = HealthReader()
    private let eventKit = EventKitReader()
    @Environment(\.scenePhase) private var scenePhase
    var body: some Scene {
        WindowGroup {
            FerryRootView(console: console, health: health, eventKit: eventKit, memory: memory)
                .overlay {
                    if scenePhase != .active { Color(uiColor: .systemBackground).ignoresSafeArea().accessibilityHidden(true) }
                }
                .onAppear { console.cancelReaders = { [health, eventKit, memory] in health.cancel(); eventKit.cancel(); memory.clear() } }
                .onChange(of: scenePhase) { _, phase in
                    if phase == .background { console.setBackground(true) }
                    else if phase == .active { console.setBackground(false) }
                }
        }
    }
}
@MainActor
private struct FerryRootView: View {
    @ObservedObject var console: ConsoleSession
    let health: HealthReader
    let eventKit: EventKitReader
    let memory: DeviceDataMemory
    @State private var settings = false
    @State private var address = ""
    @State private var logoutConfirmation = false
    @State private var logoutTask: Task<Void, Never>?
    var body: some View {
        TabView {
            VStack(spacing: 0) {
                HStack {
                    Text("Ferry").font(.headline)
                    Spacer()
                    Button("服务器") { address = console.server?.origin ?? ""; settings = true }
                    Button("退出") { logoutConfirmation = true }.disabled(console.server == nil)
                }.padding(.horizontal).padding(.vertical, 8)
                if console.server != nil {
                    // Never reuse a live document across cookie/station generations.
                    // Its old content rules stay attached until that view is destroyed.
                    ConsoleWebView(console: console).id(console.generation)
                } else {
                    ContentUnavailableView("连接自己的 Ferry", systemImage: "network", description: Text("设置受信任的 HTTPS 根地址，在网页控制台登录。"))
                }
            }
            .tabItem { Label("控制台", systemImage: "bubble.left.and.bubble.right") }
            DeviceDataView(console: console, health: health, eventKit: eventKit, memory: memory)
                .tabItem { Label("设备数据", systemImage: "heart.text.square") }
        }
        .sheet(isPresented: $settings) {
            NavigationStack {
                Form {
                    Section("可信 HTTPS 服务器根地址") {
                        TextField("https://ferry.example.com", text: $address)
                            .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        Text("不含 /observability 路径、用户名、密码、查询或片段。切站会取消所有读取/上传并清空原生内存；已有网页 Cookie 留在各自站点。")
                            .font(.footnote)
                        Button("信任此站点并连接") {
                            do { try console.setServer(address); settings = false }
                            catch { console.message = DeviceError.invalidAddress.localizedDescription }
                        }
                        Text(console.message).font(.footnote)
                    }
                }
                .navigationTitle("服务器")
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("取消") { settings = false } } }
            }
        }
        .confirmationDialog("退出当前站点？", isPresented: $logoutConfirmation, titleVisibility: .visible) {
            Button("退出并清空原生数据", role: .destructive) {
                logoutTask?.cancel()
                logoutTask = Task { await console.logout(); logoutTask = nil }
            }
        } message: { Text("停止同步，向当前服务器发送退出请求并移除本机登录 Cookie。") }
        .onChange(of: console.server) { _, _ in logoutTask?.cancel() }
    }
}
