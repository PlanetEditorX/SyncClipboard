package com.example.syncclipboard

import android.app.NotificationManager
import android.app.Service
import android.content.BroadcastReceiver
import android.content.ClipData
import android.content.ClipDescription
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.net.Uri
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PersistableBundle
import android.os.PowerManager
import android.os.SystemClock
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject
import java.nio.charset.StandardCharsets
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit

class SyncService : Service(), MiniHttpServer.Listener {
    private val worker = Executors.newSingleThreadExecutor()
    private var scheduler: ScheduledExecutorService? = null
    private var server: MiniHttpServer? = null
    private var config: AppConfig? = null
    private var initialized = false
    private var clipboardManager: ClipboardManager? = null
    private var lastClipboardAttempt = 0L
    private var lastOverlayAt = 0L
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null
    @Volatile private var screenOffIdle = false
    private var screenReceiverRegistered = false
    private var clipboardListenerRegistered = false

    private val clipboardListener = ClipboardManager.OnPrimaryClipChangedListener {
        val current = config ?: return@OnPrimaryClipChangedListener
        if (!current.autoClipboard || screenOffIdle) return@OnPrimaryClipChangedListener
        worker.execute { syncClipboardChange() }
    }

    private val screenStateReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            if (config?.screenOffPowerSave != true) return
            when (intent?.action) {
                Intent.ACTION_SCREEN_OFF -> worker.execute { enterScreenOffIdle() }
                Intent.ACTION_SCREEN_ON, Intent.ACTION_USER_PRESENT -> worker.execute { exitScreenOffIdle() }
            }
        }
    }

    override fun onCreate() {
        super.onCreate()
        NotificationHelper.createChannels(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action ?: ACTION_START) {
            ACTION_STOP -> {
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_RESTART -> {
                shutdownRuntime()
                initializeRuntime()
            }
            else -> {
                initializeRuntime()
                if (intent?.action == null || intent.action == ACTION_START) {
                    worker.execute { registerDevice() }
                }
            }
        }

        when (intent?.action) {
            ACTION_REGISTER -> worker.execute { registerDevice() }
            ACTION_SEND_TEXT -> {
                val text = intent.getStringExtra(EXTRA_TEXT).orEmpty()
                val reason = intent.getStringExtra(EXTRA_REASON).orEmpty().ifBlank { "手动发送" }
                if (text.isNotEmpty()) worker.execute { sendText(text, reason) }
            }
            ACTION_UPLOAD_URIS -> {
                val uris = intent.getStringArrayListExtra(EXTRA_URIS).orEmpty().map(Uri::parse)
                val uploadUrl = intent.getStringExtra(EXTRA_UPLOAD_URL).orEmpty()
                val uploadClient = intent.getStringExtra(EXTRA_UPLOAD_CLIENT).orEmpty()
                if (uris.isNotEmpty()) worker.execute { uploadUris(uris, uploadUrl, uploadClient) }
            }
            ACTION_UPLOAD_FILES -> {
                val files = intent.getStringArrayListExtra(EXTRA_FILES).orEmpty()
                val uploadUrl = intent.getStringExtra(EXTRA_UPLOAD_URL).orEmpty()
                val uploadClient = intent.getStringExtra(EXTRA_UPLOAD_CLIENT).orEmpty()
                if (files.isNotEmpty()) worker.execute { uploadFiles(files, uploadUrl, uploadClient) }
            }
            ACTION_DOWNLOAD_PENDING -> worker.execute { downloadPendingFiles() }
            ACTION_SYNC_CLIPBOARD -> {
                if (Build.VERSION.SDK_INT >= 29 && Settings.canDrawOverlays(this)) {
                    trySyncWithOverlay()
                } else {
                    worker.execute { syncClipboardChange() }
                }
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        shutdownRuntime()
        worker.shutdownNow()
        AppState.setRunning(this, false, "已停止")
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun initializeRuntime() {
        if (initialized) return
        initialized = true
        val loaded = AppConfig.load(this)
        config = loaded
        startAsForeground("监听端口 ${loaded.localPort}")
        AppState.setRunning(this, true, "监听端口 ${loaded.localPort}")
        AppState.log(this, "服务启动；服务器 ${loaded.serverUrl}")

        registerScreenStateReceiver()

        if (loaded.screenOffPowerSave && !isDeviceInteractive()) {
            screenOffIdle = false
            enterScreenOffIdle()
        } else {
            screenOffIdle = false
            startActiveRuntime()
            startBackgroundTriggers()
        }
    }

    private fun startActiveRuntime() {
        if (!initialized || screenOffIdle) return
        val loaded = config ?: return
        if (clipboardManager == null) {
            clipboardManager = getSystemService(ClipboardManager::class.java)
        }
        if (!clipboardListenerRegistered) {
            clipboardManager?.addPrimaryClipChangedListener(clipboardListener)
            clipboardListenerRegistered = true
        }
        startLocalServer(loaded.localPort)
        if (loaded.keepAwake) acquireLocks()
    }

    private fun stopActiveRuntime() {
        if (clipboardListenerRegistered) {
            runCatching { clipboardManager?.removePrimaryClipChangedListener(clipboardListener) }
            clipboardListenerRegistered = false
        }
        clipboardManager = null
        server?.stop()
        server = null
        releaseLocks()
    }

    private fun startLocalServer(port: Int) {
        if (server != null || screenOffIdle || !initialized) return
        val localServer = MiniHttpServer({ config ?: AppConfig.load(this) }, this)
        server = localServer
        worker.execute {
            var attempts = 0
            var success = false
            while (attempts < 3 && !success && initialized && !screenOffIdle && server === localServer) {
                runCatching { localServer.start(port) }
                    .onSuccess { success = true }
                    .onFailure {
                        attempts++
                        if (it.message?.contains("EADDRINUSE", true) == true && attempts < 3) {
                            AppState.log(this, "端口 $port 被占用，1秒后重试 ($attempts/3)...")
                            Thread.sleep(1000)
                        } else {
                            AppState.log(this, "监听端口失败：${it.message}")
                            AppState.setRunning(this, true, "端口 $port 监听失败")
                            if (server === localServer) server = null
                            success = true
                        }
                    }
            }
        }
    }

    private fun enterScreenOffIdle() {
        if (!initialized || screenOffIdle || config?.screenOffPowerSave != true) return
        screenOffIdle = true
        stopActiveRuntime()
        stopBackgroundTriggers()
        updateServiceNotification("省电待机 · 息屏暂停同步")
        AppState.log(this, "已进入息屏省电模式：服务保活，暂停 HTTP 接收、剪贴板同步和文件传输")
    }

    private fun exitScreenOffIdle() {
        if (!initialized || !screenOffIdle) return
        screenOffIdle = false
        startActiveRuntime()
        startBackgroundTriggers()
        updateServiceNotification("恢复同步 · 端口 ${config?.localPort}")
        AppState.log(this, "屏幕已点亮，已恢复接收与同步")
    }

    private fun shutdownRuntime() {
        if (!initialized) return
        initialized = false
        stopActiveRuntime()
        stopBackgroundTriggers()
        unregisterScreenStateReceiver()
        screenOffIdle = false
        stopForeground(STOP_FOREGROUND_REMOVE)
    }

    private fun startAsForeground(detail: String) {
        val notification = NotificationHelper.serviceNotification(this, detail)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NotificationHelper.SERVICE_NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE,
            )
        } else {
            startForeground(NotificationHelper.SERVICE_NOTIFICATION_ID, notification)
        }
    }

    private fun updateServiceNotification(detail: String) {
        getSystemService(NotificationManager::class.java).notify(
            NotificationHelper.SERVICE_NOTIFICATION_ID,
            NotificationHelper.serviceNotification(this, detail),
        )
        AppState.setRunning(this, true, detail)
    }

    private fun registerDevice() {
        if (screenOffIdle) return
        val currentConfig = config ?: return
        val client = client() ?: return
        AppState.log(this, "[调试] 正在向服务器注册: ${currentConfig.serverUrl}", isDebug = true)
        runCatching { client.register() }
            .onSuccess {
                AppState.log(this, "已向服务器注册，HTTP ${it.code}")
                updateServiceNotification("已连接 · 端口 ${config?.localPort}")
            }
            .onFailure {
                AppState.log(this, "注册失败：${it.message}")
                updateServiceNotification("等待服务器 · 端口 ${config?.localPort}")
            }
    }

    private fun sendText(text: String, reason: String) {
        if (screenOffIdle) {
            AppState.log(this, "$reason 已跳过：当前处于息屏省电模式")
            return
        }
        val now = SystemClock.elapsedRealtime()
        
        if (text == AppState.lastGlobalText && (now - AppState.lastGlobalAt < 10000)) {
            AppState.log(this, "[忽略] $reason: 与全局最新记录匹配，判定为回波", isDebug = true)
            return
        }
        
        val client = client() ?: return
        runCatching { client.sendText(text) }
            .onSuccess {
                AppState.lastGlobalText = text
                AppState.lastGlobalAt = SystemClock.elapsedRealtime()
                AppState.log(this, "$reason：文字已同步（${text.length} 字符）")
            }
            .onFailure { AppState.log(this, "${reason}失败：${it.message}") }
    }

    private fun syncClipboardChange() {
        if (screenOffIdle) return
        val now = SystemClock.elapsedRealtime()
        if (now - lastClipboardAttempt < 350) return
        lastClipboardAttempt = now
        val manager = clipboardManager ?: return
        val description = runCatching { manager.primaryClipDescription }.getOrNull()
        
        if (isRemoteClip(description)) {
            AppState.log(this, "[调试] 剪贴板带有远程标记，跳过同步", isDebug = true)
            return
        }

        val sensitive = if (Build.VERSION.SDK_INT >= 33) {
            description?.extras?.getBoolean(ClipDescription.EXTRA_IS_SENSITIVE, false) == true
        } else {
            description?.extras?.getBoolean("android.content.extra.IS_SENSITIVE", false) == true
        }
        if (sensitive) {
            AppState.log(this, "已跳过标记为敏感的剪贴板内容")
            return
        }
        val clip = runCatching { manager.primaryClip }.getOrNull()
        if (clip == null) {
            if (Build.VERSION.SDK_INT >= 29 && Settings.canDrawOverlays(this)) {
                trySyncWithOverlay()
            } else if (!AppState.isAppInForeground) {
                AppState.log(this, "后台读取受限，请授予悬浮窗权限或点击通知")
            }
            return
        }
        val text = runCatching { clip.getItemAt(0)?.coerceToText(this)?.toString() }
            .getOrNull()
            .orEmpty()
        if (text.isBlank()) return
        sendText(text, "剪贴板变化")
    }

    private fun trySyncWithOverlay() {
        if (screenOffIdle) return
        val now = SystemClock.elapsedRealtime()
        if (now - lastOverlayAt < 2000) return // 2秒频率限制，防止系统通知闪烁
        lastOverlayAt = now

        val wm = getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val params = WindowManager.LayoutParams().apply {
            width = 1
            height = 1
            type = WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
            flags = WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or 
                    WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN
            format = android.graphics.PixelFormat.TRANSLUCENT
            gravity = Gravity.TOP or Gravity.START
            alpha = 0.01f 
        }
        
        val view = View(this).apply {
            isFocusable = true
            isFocusableInTouchMode = true
        }
        
        val handler = Handler(Looper.getMainLooper())
        handler.post {
            try {
                wm.addView(view, params)
                view.requestFocus()
                handler.postDelayed({
                    val manager = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                    val clip = manager.primaryClip
                    if (clip != null && clip.itemCount > 0) {
                        if (!isRemoteClip(clip.description)) {
                            val text = clip.getItemAt(0).coerceToText(this).toString()
                            if (text.isNotEmpty()) {
                                AppState.log(this, "[调试] 悬浮窗获取到剪贴板", isDebug = true)
                                worker.execute { sendText(text, "无感同步") }
                            }
                        } else {
                            AppState.log(this, "[调试] 悬浮窗检测到远程标记，忽略", isDebug = true)
                        }
                    }
                    runCatching { wm.removeView(view) }
                }, 200)
            } catch (e: Exception) {
                runCatching { wm.removeView(view) }
            }
        }
    }

    private fun uploadUris(uris: List<Uri>, uploadUrl: String, uploadClient: String) {
        if (screenOffIdle) {
            AppState.log(this, "文件发送已跳过：当前处于息屏省电模式")
            return
        }
        if (uploadUrl.isBlank()) {
            AppState.log(this, "文件上传已取消：没有选择目标客户端")
            return
        }
        val client = client() ?: return
        val target = uploadClient.ifBlank { uploadUrl }
        var successCount = 0
        uris.forEachIndexed { index, uri ->
            runCatching { client.upload(uri, uploadUrl, target) }
                .onSuccess {
                    successCount += 1
                    AppState.log(this, "文件 ${index + 1}/${uris.size} 已发送到 $target")
                }
                .onFailure { AppState.log(this, "文件 ${index + 1}/${uris.size} 上传失败：${it.message}") }
        }
        NotificationHelper.showUploadResult(this, target, successCount, uris.size)
    }

    private fun uploadFiles(paths: List<String>, uploadUrl: String, uploadClient: String) {
        if (screenOffIdle) {
            AppState.log(this, "分享文件已跳过：当前处于息屏省电模式")
            paths.forEach { runCatching { java.io.File(it).delete() } }
            return
        }
        if (uploadUrl.isBlank()) {
            AppState.log(this, "分享文件上传已取消：没有选择目标客户端")
            paths.forEach { runCatching { java.io.File(it).delete() } }
            return
        }
        val client = client() ?: run {
            paths.forEach { runCatching { java.io.File(it).delete() } }
            return
        }
        val target = uploadClient.ifBlank { uploadUrl }
        var successCount = 0
        paths.forEachIndexed { index, path ->
            val file = java.io.File(path)
            runCatching { client.upload(file, uploadUrl, target) }
                .onSuccess {
                    successCount += 1
                    AppState.log(this, "分享文件 ${index + 1}/${paths.size} 已发送到 $target")
                }
                .onFailure { AppState.log(this, "分享文件 ${index + 1}/${paths.size} 上传失败：${it.message}") }
            runCatching { file.delete() }
        }
        NotificationHelper.showUploadResult(this, target, successCount, paths.size)
    }

    private fun downloadPendingFiles() {
        if (screenOffIdle) {
            AppState.log(this, "文件下载已跳过：当前处于息屏省电模式")
            return
        }
        NotificationHelper.cancelFile(this)
        val client = client() ?: return
        val directFiles = PendingDownloads.load(this)
        val files = if (directFiles.isNotEmpty()) {
            directFiles
        } else {
            runCatching { client.requestFiles() }
                .onFailure { AppState.log(this, "获取文件列表失败：${it.message}") }
                .getOrDefault(emptyList())
        }
        if (files.isEmpty()) {
            AppState.log(this, "服务器没有待下载文件")
            return
        }
        var successCount = 0
        files.forEach { file ->
            runCatching { client.download(file) }
                .onSuccess { uri ->
                    successCount += 1
                    AppState.log(this, "已下载：${file.filename}")
                    NotificationHelper.showDownloadComplete(this, file.filename, uri)
                }
                .onFailure { AppState.log(this, "下载 ${file.filename} 失败：${it.message}") }
        }
        if (successCount == files.size) PendingDownloads.clear(this)
    }

    override fun onTextUpdate(payload: JSONObject) {
        worker.execute {
            if (screenOffIdle) return@execute
            val source = payload.optString("source")
            val id = payload.optString("id")
            val statePrefs = getSharedPreferences("syncclipboard_remote", Context.MODE_PRIVATE)
            val lastId = statePrefs.getString("lastRemoteId", "").orEmpty()
            if (source == config?.deviceName || (id.isNotBlank() && id == lastId)) return@execute
            var text = payload.optString("content")
            if (payload.optString("encode").equals("base64", ignoreCase = true)) {
                text = runCatching {
                    String(android.util.Base64.decode(text, android.util.Base64.DEFAULT), StandardCharsets.UTF_8)
                }.getOrDefault(text)
            }
            if (text.isEmpty()) return@execute
            
            AppState.lastGlobalText = text
            AppState.lastGlobalAt = SystemClock.elapsedRealtime()
            
            val clip = ClipData.newPlainText("SyncClipboard", text)
            val extras = PersistableBundle().apply {
                if (Build.VERSION.SDK_INT >= 34) putBoolean(ClipDescription.EXTRA_IS_REMOTE_DEVICE, true)
                else putBoolean("android.content.extra.IS_REMOTE_DEVICE", true)
            }
            clip.description.extras = extras
            clipboardManager?.setPrimaryClip(clip)
            if (id.isNotBlank()) statePrefs.edit().putString("lastRemoteId", id).apply()
            AppState.log(this, "已接收远程文字（${text.length} 字符）")
        }
    }

    override fun onFileUpdate(payload: Any) {
        worker.execute {
            if (screenOffIdle) return@execute
            val filenames = extractFileNames(payload)
            val logText = if (filenames.isEmpty()) "未知文件" else filenames.joinToString("、")
            AppState.log(this, "收到服务器文件通知: $logText")
            if (config?.autoDownload == true) downloadPendingFiles()
            else NotificationHelper.showFileAvailable(this, filenames)
        }
    }

    override fun onDirectFile(file: ProtocolClient.RemoteFile) {
        worker.execute {
            if (screenOffIdle) return@execute
            PendingDownloads.append(this, file)
            AppState.log(this, "收到中转文件：${file.filename}")
            if (config?.autoDownload == true) downloadPendingFiles()
            else {
                val pendingNames = PendingDownloads.load(this).map { it.filename }
                NotificationHelper.showFileAvailable(this, pendingNames)
            }
        }
    }

    private fun extractFileNames(payload: Any?): List<String> {
        val names = linkedSetOf<String>()

        fun addString(value: String) {
            val clean = value.trim()
            if (clean.isBlank() || clean.contains("://") || clean.length > 255) return
            names += clean
        }

        fun visit(value: Any?) {
            when (value) {
                null, JSONObject.NULL -> Unit
                is JSONArray -> {
                    for (index in 0 until value.length()) visit(value.opt(index))
                }
                is JSONObject -> {
                    val direct = value.optString("filename").ifBlank {
                        value.optString("file_name").ifBlank { value.optString("name") }
                    }
                    if (direct.isNotBlank()) addString(direct)
                    listOf("files", "items", "latest_global").forEach { key ->
                        if (value.has(key)) visit(value.opt(key))
                    }
                    val content = value.opt("content")
                    if (direct.isBlank() || content is JSONArray || content is JSONObject) visit(content)
                }
                is Iterable<*> -> value.forEach { visit(it) }
                is Array<*> -> value.forEach { visit(it) }
                is String -> {
                    val text = value.trim()
                    when {
                        text.startsWith("[") -> runCatching { visit(JSONArray(text)) }.onFailure { addString(text) }
                        text.startsWith("{") -> runCatching { visit(JSONObject(text)) }.onFailure { addString(text) }
                        else -> addString(text)
                    }
                }
            }
        }

        visit(payload)
        return names.toList()
    }

    override fun onServerLog(message: String, isDebug: Boolean) {
        AppState.log(this, message, isDebug)
    }

    private fun isRemoteClip(desc: ClipDescription?): Boolean {
        if (desc == null) return false
        return if (Build.VERSION.SDK_INT >= 34) {
            desc.extras?.getBoolean(ClipDescription.EXTRA_IS_REMOTE_DEVICE, false) == true
        } else {
            desc.extras?.getBoolean("android.content.extra.IS_REMOTE_DEVICE", false) == true
        }
    }

    private fun client(): ProtocolClient? = config?.let { ProtocolClient(this, it) }

    private fun startBackgroundTriggers() {
        if (!initialized || screenOffIdle) return
        registerNetworkCallback()
        if (scheduler?.isShutdown != false) {
            scheduler = Executors.newSingleThreadScheduledExecutor().also { timer ->
                timer.schedule({ registerDevice() }, 1, TimeUnit.SECONDS)
                timer.scheduleAtFixedRate({ registerDevice() }, 12, 12, TimeUnit.HOURS)
            }
        }
    }

    private fun stopBackgroundTriggers() {
        scheduler?.shutdownNow()
        scheduler = null
        unregisterNetworkCallback()
    }

    private fun registerScreenStateReceiver() {
        if (screenReceiverRegistered) return
        val filter = IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_OFF)
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_USER_PRESENT)
        }
        ContextCompat.registerReceiver(
            this,
            screenStateReceiver,
            filter,
            ContextCompat.RECEIVER_NOT_EXPORTED,
        )
        screenReceiverRegistered = true
    }

    private fun unregisterScreenStateReceiver() {
        if (!screenReceiverRegistered) return
        runCatching { unregisterReceiver(screenStateReceiver) }
        screenReceiverRegistered = false
    }

    private fun isDeviceInteractive(): Boolean =
        getSystemService(PowerManager::class.java).isInteractive

    private fun registerNetworkCallback() {
        if (networkCallback != null) return
        val manager = getSystemService(ConnectivityManager::class.java)
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                if (!screenOffIdle) scheduler?.schedule({ registerDevice() }, 2, TimeUnit.SECONDS)
            }
        }
        runCatching { manager.registerDefaultNetworkCallback(callback) }
            .onSuccess { networkCallback = callback }
            .onFailure { AppState.log(this, "网络监听注册失败：${it.message}") }
    }

    private fun unregisterNetworkCallback() {
        val callback = networkCallback ?: return
        runCatching { getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(callback) }
        networkCallback = null
    }

    @Suppress("DEPRECATION")
    private fun acquireLocks() {
        if (wakeLock?.isHeld == true || wifiLock?.isHeld == true) return
        val power = getSystemService(PowerManager::class.java)
        wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "SyncClipboard:Background").apply {
            setReferenceCounted(false)
            acquire()
        }
        val wifi = applicationContext.getSystemService(WifiManager::class.java)
        wifiLock = wifi.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "SyncClipboard:Wifi").apply {
            setReferenceCounted(false)
            acquire()
        }
        AppState.log(this, "已启用保持唤醒（耗电会增加）")
    }

    private fun releaseLocks() {
        runCatching { if (wakeLock?.isHeld == true) wakeLock?.release() }
        runCatching { if (wifiLock?.isHeld == true) wifiLock?.release() }
        wakeLock = null
        wifiLock = null
    }

    companion object {
        const val ACTION_START = "com.example.syncclipboard.START"
        const val ACTION_STOP = "com.example.syncclipboard.STOP"
        const val ACTION_RESTART = "com.example.syncclipboard.RESTART"
        const val ACTION_REGISTER = "com.example.syncclipboard.REGISTER"
        const val ACTION_SEND_TEXT = "com.example.syncclipboard.SEND_TEXT"
        const val ACTION_UPLOAD_URIS = "com.example.syncclipboard.UPLOAD_URIS"
        const val ACTION_UPLOAD_FILES = "com.example.syncclipboard.UPLOAD_FILES"
        const val ACTION_DOWNLOAD_PENDING = "com.example.syncclipboard.DOWNLOAD_PENDING"
        const val ACTION_SYNC_CLIPBOARD = "com.example.syncclipboard.SYNC_CLIPBOARD"
        const val EXTRA_TEXT = "text"
        const val EXTRA_REASON = "reason"
        const val EXTRA_URIS = "uris"
        const val EXTRA_FILES = "files"
        const val EXTRA_UPLOAD_URL = "upload_url"
        const val EXTRA_UPLOAD_CLIENT = "upload_client"
        private const val REMOTE_ECHO_WINDOW_MS = 3_000L
        private const val LOCAL_DUPLICATE_WINDOW_MS = 500L

        fun startAction(context: Context, action: String, configure: (Intent.() -> Unit)? = null) {
            val intent = Intent(context, SyncService::class.java).setAction(action)
            configure?.invoke(intent)
            if (action == ACTION_STOP) {
                context.startService(intent)
            } else {
                ContextCompat.startForegroundService(context, intent)
            }
        }
    }
}
