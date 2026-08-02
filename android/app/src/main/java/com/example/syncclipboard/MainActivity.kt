package com.example.syncclipboard

import android.Manifest
import android.app.AlertDialog
import android.content.BroadcastReceiver
import android.content.ClipDescription
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import java.util.concurrent.Executors

class MainActivity : AppCompatActivity() {
    private lateinit var serverUrl: EditText
    private lateinit var secret: EditText
    private lateinit var localPort: EditText
    private lateinit var deviceName: EditText
    private lateinit var downloadFolder: TextView
    private lateinit var autoStart: CheckBox
    private lateinit var autoClipboard: CheckBox
    private lateinit var autoDownload: CheckBox
    private lateinit var keepAwake: CheckBox
    private lateinit var allowServerHost: CheckBox
    private lateinit var debugMode: CheckBox
    private lateinit var notificationTimeoutSeconds: EditText
    private lateinit var screenOffPowerSave: CheckBox
    private lateinit var directText: EditText
    private lateinit var status: TextView
    private lateinit var logs: TextView
    private var selectedTreeUri: String = ""
    private val networkExecutor = Executors.newSingleThreadExecutor()

    private val stateReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) = refreshState()
    }

    private val folderPicker = registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
        if (uri != null) {
            val flags = Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
            runCatching { contentResolver.takePersistableUriPermission(uri, flags) }
            selectedTreeUri = uri.toString()
            downloadFolder.text = selectedTreeUri
        }
    }

    private val filePicker = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        if (uris.isNotEmpty()) {
            uris.forEach { uri ->
                runCatching {
                    contentResolver.takePersistableUriPermission(
                        uri,
                        Intent.FLAG_GRANT_READ_URI_PERMISSION,
                    )
                }
            }
            if (saveConfig()) chooseUploadTarget(uris)
        }
    }

    private val notificationPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (!granted) toast("未授权通知，权限不足")
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(buildUi())
        loadConfig()
        bindDebugMode()
        requestNotificationPermissionIfNeeded()
        if (AppConfig.load(this).autoStart) {
            SyncService.startAction(this, SyncService.ACTION_START)
        }
        ContextCompat.registerReceiver(
            this,
            stateReceiver,
            IntentFilter(AppState.ACTION_STATE_CHANGED),
            ContextCompat.RECEIVER_NOT_EXPORTED,
        )
    }

    override fun onResume() {
        super.onResume()
        AppState.isAppInForeground = true
        refreshState()
        if (AppConfig.load(this).autoClipboard) {
            SyncService.startAction(this, SyncService.ACTION_SYNC_CLIPBOARD)
        }
    }

    override fun onPause() {
        AppState.isAppInForeground = false
        super.onPause()
    }

    override fun onDestroy() {
        runCatching { unregisterReceiver(stateReceiver) }
        networkExecutor.shutdownNow()
        super.onDestroy()
    }

    private fun buildUi(): View {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(20), dp(20), dp(36))
        }
        val scroll = ScrollView(this).apply { addView(root) }

        root.addView(TextView(this).apply {
            text = "SyncClipboard"
            textSize = 28f
            setTypeface(typeface, Typeface.BOLD)
        })
        
        status = TextView(this).apply {
            textSize = 15f
            setPadding(dp(12), dp(10), dp(12), dp(10))
            setBackgroundColor(0x12888888)
        }
        root.addView(status, matchWrap())

        section(root, "连接设置")
        serverUrl = edit(root, "主机", "http://192.168.1.10:8000")
        secret = edit(root, "密钥", "123456").apply { inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD }
        localPort = edit(root, "本机监听端口", "8080").apply { inputType = InputType.TYPE_CLASS_NUMBER }
        deviceName = edit(root, "设备名称（默认使用手机设备名称）", AppConfig.defaultDeviceName(this))

        section(root, "下载设置")
        downloadFolder = TextView(this).apply {
            setPadding(dp(12), dp(10), dp(12), dp(10))
            setBackgroundColor(0x12888888)
        }
        root.addView(downloadFolder, matchWrap())
        root.addView(Button(this).apply {
            text = "选择下载文件夹"
            setOnClickListener { folderPicker.launch(null) }
        }, matchWrap())

        autoStart = checkbox(root, "开机启动服务", true)
        autoClipboard = checkbox(root, "自动同步剪贴板", true)
        autoDownload = checkbox(root, "自动下载通知文件", false)
        notificationTimeoutSeconds = edit(root, "临时通知保留时间（秒，0=不自动清理）", "60").apply { inputType = InputType.TYPE_CLASS_NUMBER }
        screenOffPowerSave = checkbox(root, "息屏省电模式（息屏后仅保活，暂停接收与同步）", false)
        keepAwake = checkbox(root, "保持唤醒锁（服务运行时避免 CPU 和 Wi‑Fi 休眠，提升后台同步稳定性，但会增加耗电）", false)
        allowServerHost = checkbox(root, "允许服务器免密上传", true)

        row(root,
            button("保存并启动") {
                if (saveConfig()) SyncService.startAction(this, SyncService.ACTION_RESTART)
            },
            button("停止") { SyncService.startAction(this, SyncService.ACTION_STOP) },
        )

        section(root, "手动发送")
        directText = edit(root, "发送文字", "")
        row(root,
            button("发送文字") {
                val text = directText.text.toString()
                if (text.isBlank()) toast("请输入")
                else if (saveConfig()) SyncService.startAction(this, SyncService.ACTION_SEND_TEXT) {
                    putExtra(SyncService.EXTRA_TEXT, text)
                }
            },
            button("发送剪贴板") { sendCurrentClipboard() },
        )
        root.addView(Button(this).apply {
            text = "发送一个或多个文件"
            setOnClickListener { filePicker.launch(arrayOf("*/*")) }
        }, matchWrap())

        section(root, "权限授权")
        root.addView(Button(this).apply {
            text = "授予无障碍权限"
            setOnClickListener { startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) }
        }, matchWrap())
        root.addView(Button(this).apply {
            text = "授予悬浮窗权限"
            setOnClickListener { startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName"))) }
        }, matchWrap())
        root.addView(Button(this).apply {
            text = "电池优化白名单"
            setOnClickListener { startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) }
        }, matchWrap())

        section(root, "运行日志")
        debugMode = checkbox(root, "显示调试日志（开启后记录详细请求日志；关闭时仅记录简短日志）", false)
        logs = TextView(this).apply {
            typeface = Typeface.MONOSPACE
            textSize = 12f
            setTextIsSelectable(true)
            setPadding(dp(10), dp(10), dp(10), dp(10))
            setBackgroundColor(0x12888888)
        }
        root.addView(logs, LinearLayout.LayoutParams(-1, dp(300)))
        root.addView(Button(this).apply {
            text = "清空日志"
            setOnClickListener { AppState.clearLogs(this@MainActivity) }
        }, matchWrap())
        
        return scroll
    }

    private fun loadConfig() {
        val c = AppConfig.load(this)
        serverUrl.setText(c.serverUrl)
        secret.setText(c.secret)
        localPort.setText(c.localPort.toString())
        deviceName.setText(c.deviceName)
        selectedTreeUri = c.downloadTreeUri
        downloadFolder.text = c.downloadTreeUri.ifBlank { "默认: Download/SyncClipboard" }
        autoStart.isChecked = c.autoStart
        autoClipboard.isChecked = c.autoClipboard
        autoDownload.isChecked = c.autoDownload
        notificationTimeoutSeconds.setText(c.notificationTimeoutSeconds.toString())
        screenOffPowerSave.isChecked = c.screenOffPowerSave
        keepAwake.isChecked = c.keepAwake
        allowServerHost.isChecked = c.allowServerHostWithoutKey
        debugMode.isChecked = c.debugMode
    }

    private fun bindDebugMode() {
        debugMode.setOnCheckedChangeListener { _, enabled ->
            val config = AppConfig.load(this)
            if (config.debugMode != enabled) {
                AppConfig.save(this, config.copy(debugMode = enabled))
                AppState.log(this, if (enabled) "已开启调试日志" else "已关闭调试日志")
            }
        }
    }

    private fun saveConfig(): Boolean {
        val url = serverUrl.text.toString().trim().trimEnd('/')
        val port = localPort.text.toString().toIntOrNull()
        val notificationSeconds = notificationTimeoutSeconds.text.toString().toIntOrNull()
        if (!(url.startsWith("http://") || url.startsWith("https://"))) {
            toast("URL 错误")
            return false
        }
        if (port == null || port !in 1024..65535) {
            toast("端口错误")
            return false
        }
        if (notificationSeconds == null || notificationSeconds !in 0..86400) {
            toast("通知保留时间请输入 0–86400 秒")
            return false
        }
        AppConfig.save(
            this,
            AppConfig(
                serverUrl = url,
                secret = secret.text.toString(),
                localPort = port,
                deviceName = deviceName.text.toString().trim().ifBlank { AppConfig.defaultDeviceName(this) },
                downloadTreeUri = selectedTreeUri,
                autoStart = autoStart.isChecked,
                autoClipboard = autoClipboard.isChecked,
                autoDownload = autoDownload.isChecked,
                keepAwake = keepAwake.isChecked,
                allowServerHostWithoutKey = allowServerHost.isChecked,
                debugMode = debugMode.isChecked,
                notificationTimeoutSeconds = notificationSeconds,
                screenOffPowerSave = screenOffPowerSave.isChecked,
            ),
        )
        return true
    }

    private fun chooseUploadTarget(uris: List<Uri>) {
        toast("正在获取可发送的在线客户端…")
        networkExecutor.execute {
            val result = runCatching {
                ProtocolClient(this, AppConfig.load(this)).getOnlineClients()
            }
            runOnUiThread {
                if (isFinishing || isDestroyed) return@runOnUiThread
                result.onSuccess { clients ->
                    if (clients.isEmpty()) {
                        toast("没有可发送的在线客户端（已排除本机和 iOS）")
                        return@onSuccess
                    }
                    val names = clients.map { "${it.displayName} · ${it.os}" }.toTypedArray()
                    AlertDialog.Builder(this)
                        .setTitle("发送 ${uris.size} 个文件到")
                        .setItems(names) { _, index ->
                            val target = clients[index]
                            SyncService.startAction(this, SyncService.ACTION_UPLOAD_URIS) {
                                putStringArrayListExtra(
                                    SyncService.EXTRA_URIS,
                                    ArrayList(uris.map(Uri::toString)),
                                )
                                putExtra(SyncService.EXTRA_UPLOAD_URL, target.uploadUrl)
                                putExtra(SyncService.EXTRA_UPLOAD_CLIENT, target.displayName)
                            }
                            toast("正在发送到 ${target.displayName}")
                        }
                        .setNegativeButton("取消", null)
                        .show()
                }.onFailure {
                    AppState.log(this, "获取在线客户端失败：${it.message}")
                    toast("获取在线客户端失败：${it.message}")
                }
            }
        }
    }

    private fun sendCurrentClipboard() {
        val manager = getSystemService(ClipboardManager::class.java)
        val text = manager.primaryClip?.getItemAt(0)?.coerceToText(this)?.toString().orEmpty()
        if (text.isBlank()) toast("剪贴板空")
        else if (saveConfig()) SyncService.startAction(this, SyncService.ACTION_SEND_TEXT) {
            putExtra(SyncService.EXTRA_TEXT, text)
        }
    }

    private fun refreshState() {
        val running = AppState.isRunning(this)
        status.text = if (running) "● 运行中 · ${AppState.detail(this)}" else "○ 已停止"
        logs.text = AppState.logs(this)
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    private fun section(root: LinearLayout, title: String) {
        root.addView(TextView(this).apply {
            text = title
            textSize = 18f
            setTypeface(typeface, Typeface.BOLD)
            setPadding(0, dp(20), 0, dp(4))
        })
    }

    private fun label(root: LinearLayout, text: String) {
        root.addView(TextView(this).apply {
            this.text = text
            textSize = 13f
            setPadding(0, dp(8), 0, dp(2))
        })
    }

    private fun edit(root: LinearLayout, hint: String, initial: String): EditText {
        label(root, hint)
        return EditText(this).apply {
            setText(initial)
            setSingleLine(true)
            root.addView(this, matchWrap())
        }
    }

    private fun checkbox(root: LinearLayout, text: String, checked: Boolean): CheckBox =
        CheckBox(this).apply {
            this.text = text
            isChecked = checked
            root.addView(this, matchWrap())
        }

    private fun button(text: String, action: () -> Unit): Button = Button(this).apply {
        this.text = text
        setOnClickListener { action() }
    }

    private fun row(root: LinearLayout, vararg views: View) {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
        }
        views.forEach { view -> row.addView(view, LinearLayout.LayoutParams(0, -2, 1f)) }
        root.addView(row, matchWrap())
    }

    private fun matchWrap() = LinearLayout.LayoutParams(-1, -2)
    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()
    private fun toast(message: String) = Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
}
