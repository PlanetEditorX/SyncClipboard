package com.example.syncclipboard

import android.Manifest
import android.app.AlertDialog
import android.content.BroadcastReceiver
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.Toast
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.compose.foundation.background
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.Button
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ElevatedCard
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import java.util.concurrent.Executors

class MainActivity : AppCompatActivity() {
    private var configState by mutableStateOf<AppConfig?>(null)
    private var selectedTreeUri by mutableStateOf("")
    private var running by mutableStateOf(false)
    private var runningDetail by mutableStateOf("")
    private var logText by mutableStateOf("")
    private val networkExecutor = Executors.newSingleThreadExecutor()

    private val stateReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) = refreshState()
    }

    private val folderPicker =
        registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
            if (uri != null) {
                val flags = Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                runCatching { contentResolver.takePersistableUriPermission(uri, flags) }
                selectedTreeUri = uri.toString()
            }
        }

    private val filePicker =
        registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
            if (uris.isNotEmpty()) {
                uris.forEach { uri ->
                    runCatching {
                        contentResolver.takePersistableUriPermission(
                            uri,
                            Intent.FLAG_GRANT_READ_URI_PERMISSION,
                        )
                    }
                }
                chooseUploadTarget(uris)
            }
        }

    private val notificationPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (!granted) toast("未授予通知权限，后台状态可能不完整")
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val loadedConfig = AppConfig.load(this)
        configState = loadedConfig
        selectedTreeUri = loadedConfig.downloadTreeUri
        refreshState()

        setContent {
            val config = configState
            if (config != null) {
                SyncClipboardTheme {
                    SyncClipboardScreen(
                        initialConfig = config,
                        downloadFolder = selectedTreeUri,
                        isRunning = running,
                        runningDetail = runningDetail,
                        logs = logText,
                        onSaveAndStart = { candidate ->
                            if (saveConfig(candidate)) {
                                SyncService.startAction(this, SyncService.ACTION_RESTART)
                            }
                        },
                        onStop = {
                            SyncService.startAction(this, SyncService.ACTION_STOP)
                        },
                        onSendText = { text, candidate ->
                            if (text.isBlank()) {
                                toast("请输入要发送的文字")
                            } else if (saveConfig(candidate)) {
                                SyncService.startAction(this, SyncService.ACTION_SEND_TEXT) {
                                    putExtra(SyncService.EXTRA_TEXT, text)
                                }
                            }
                        },
                        onSendClipboard = { candidate ->
                            if (saveConfig(candidate)) sendCurrentClipboard()
                        },
                        onPickFiles = { candidate ->
                            if (saveConfig(candidate)) filePicker.launch(arrayOf("*/*"))
                        },
                        onPickFolder = { folderPicker.launch(null) },
                        onOpenAccessibility = {
                            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
                        },
                        onOpenOverlay = {
                            startActivity(
                                Intent(
                                    Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                                    Uri.parse("package:" + packageName),
                                ),
                            )
                        },
                        onOpenBatterySettings = {
                            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
                        },
                        onDebugModeChanged = ::setDebugMode,
                        onClearLogs = { AppState.clearLogs(this) },
                    )
                }
            }
        }

        requestNotificationPermissionIfNeeded()
        if (loadedConfig.autoStart) {
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

    private fun saveConfig(candidate: AppConfig): Boolean {
        val url = candidate.serverUrl.trim().trimEnd('/')
        if (!(url.startsWith("http://") || url.startsWith("https://"))) {
            toast("主机地址必须以 http:// 或 https:// 开头")
            return false
        }
        if (candidate.localPort !in 1024..65535) {
            toast("本机监听端口必须在 1024 到 65535 之间")
            return false
        }
        if (candidate.notificationTimeoutSeconds !in 0..86400) {
            toast("通知保留时间必须在 0 到 86400 秒之间")
            return false
        }

        val normalized = candidate.copy(
            serverUrl = url,
            deviceName = candidate.deviceName.trim().ifBlank { AppConfig.defaultDeviceName(this) },
        )
        AppConfig.save(this, normalized)
        configState = normalized
        selectedTreeUri = normalized.downloadTreeUri
        return true
    }

    private fun setDebugMode(enabled: Boolean) {
        val updated = AppConfig.load(this).copy(debugMode = enabled)
        AppConfig.save(this, updated)
        configState = updated
        AppState.log(this, if (enabled) "已开启调试日志" else "已关闭调试日志")
    }

    private fun chooseUploadTarget(uris: List<Uri>) {
        toast("正在获取可发送的在线客户端")
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
                    val names = clients.map { it.displayName + " · " + it.os }.toTypedArray()
                    AlertDialog.Builder(this)
                        .setTitle("发送 " + uris.size + " 个文件到")
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
                            toast("正在发送到 " + target.displayName)
                        }
                        .setNegativeButton("取消", null)
                        .show()
                }.onFailure {
                    AppState.log(this, "获取在线客户端失败：" + it.message)
                    toast("获取在线客户端失败：" + it.message)
                }
            }
        }
    }

    private fun sendCurrentClipboard() {
        val manager = getSystemService(ClipboardManager::class.java)
        val text = manager.primaryClip
            ?.getItemAt(0)
            ?.coerceToText(this)
            ?.toString()
            .orEmpty()
        if (text.isBlank()) {
            toast("剪贴板为空")
        } else {
            SyncService.startAction(this, SyncService.ACTION_SEND_TEXT) {
                putExtra(SyncService.EXTRA_TEXT, text)
                putExtra(SyncService.EXTRA_FORCE_SEND, true)
            }
        }
    }

    private fun refreshState() {
        running = AppState.isRunning(this)
        runningDetail = AppState.detail(this)
        logText = AppState.logs(this)
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (
            Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
                PackageManager.PERMISSION_GRANTED
        ) {
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    private fun toast(message: String) =
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
}

@Composable
private fun SyncClipboardTheme(content: @Composable () -> Unit) {
    val context = LocalContext.current
    val useDarkTheme = isSystemInDarkTheme()
    val colors = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && useDarkTheme -> dynamicDarkColorScheme(context)
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S -> dynamicLightColorScheme(context)
        useDarkTheme -> darkColorScheme(
            primary = Color(0xFF9BCBFF),
            secondary = Color(0xFFB8C7DC),
        )
        else -> lightColorScheme(
            primary = Color(0xFF0B57D0),
            secondary = Color(0xFF465D80),
            tertiary = Color(0xFF006A60),
        )
    }
    MaterialTheme(colorScheme = colors, content = content)
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun SyncClipboardScreen(
    initialConfig: AppConfig,
    downloadFolder: String,
    isRunning: Boolean,
    runningDetail: String,
    logs: String,
    onSaveAndStart: (AppConfig) -> Unit,
    onStop: () -> Unit,
    onSendText: (String, AppConfig) -> Unit,
    onSendClipboard: (AppConfig) -> Unit,
    onPickFiles: (AppConfig) -> Unit,
    onPickFolder: () -> Unit,
    onOpenAccessibility: () -> Unit,
    onOpenOverlay: () -> Unit,
    onOpenBatterySettings: () -> Unit,
    onDebugModeChanged: (Boolean) -> Unit,
    onClearLogs: () -> Unit,
) {
    var serverUrl by rememberSaveable { mutableStateOf(initialConfig.serverUrl) }
    var secret by rememberSaveable { mutableStateOf(initialConfig.secret) }
    var localPort by rememberSaveable { mutableStateOf(initialConfig.localPort.toString()) }
    var deviceName by rememberSaveable { mutableStateOf(initialConfig.deviceName) }
    var autoStart by rememberSaveable { mutableStateOf(initialConfig.autoStart) }
    var autoClipboard by rememberSaveable { mutableStateOf(initialConfig.autoClipboard) }
    var autoDownload by rememberSaveable { mutableStateOf(initialConfig.autoDownload) }
    var notificationTimeout by rememberSaveable {
        mutableStateOf(initialConfig.notificationTimeoutSeconds.toString())
    }
    var screenOffPowerSave by rememberSaveable { mutableStateOf(initialConfig.screenOffPowerSave) }
    var keepAwake by rememberSaveable { mutableStateOf(initialConfig.keepAwake) }
    var allowServerHost by rememberSaveable {
        mutableStateOf(initialConfig.allowServerHostWithoutKey)
    }
    var debugMode by rememberSaveable { mutableStateOf(initialConfig.debugMode) }
    var directText by rememberSaveable { mutableStateOf("") }
    var showSecret by rememberSaveable { mutableStateOf(false) }

    fun draftConfig() = AppConfig(
        serverUrl = serverUrl,
        secret = secret,
        localPort = localPort.toIntOrNull() ?: -1,
        deviceName = deviceName,
        downloadTreeUri = downloadFolder,
        autoStart = autoStart,
        autoClipboard = autoClipboard,
        autoDownload = autoDownload,
        keepAwake = keepAwake,
        allowServerHostWithoutKey = allowServerHost,
        debugMode = debugMode,
        notificationTimeoutSeconds = notificationTimeout.toIntOrNull() ?: -1,
        screenOffPowerSave = screenOffPowerSave,
    )

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 16.dp, vertical = 20.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text(
                text = "SyncClipboard",
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.Bold,
            )
            Text(
                text = "跨设备剪贴板与文件同步",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        ServiceStatusCard(
            isRunning = isRunning,
            detail = runningDetail,
            port = localPort,
        )

        SectionCard(
            title = "连接设置",
            subtitle = "服务器与本机服务的连接信息",
        ) {
            SettingsTextField(
                value = serverUrl,
                onValueChange = { serverUrl = it },
                label = "服务器地址",
                supportingText = "例如 http://192.168.1.10:8000",
            )
            OutlinedTextField(
                value = secret,
                onValueChange = { secret = it },
                modifier = Modifier.fillMaxWidth(),
                label = { Text("密钥") },
                singleLine = true,
                visualTransformation = if (showSecret) VisualTransformation.None else PasswordVisualTransformation(),
                trailingIcon = {
                    TextButton(onClick = { showSecret = !showSecret }) {
                        Text(if (showSecret) "隐藏" else "显示")
                    }
                },
            )
            SettingsTextField(
                value = localPort,
                onValueChange = { localPort = it },
                label = "本机监听端口",
                supportingText = "用于接收局域网请求，范围 1024–65535",
                keyboardType = KeyboardType.Number,
            )
            SettingsTextField(
                value = deviceName,
                onValueChange = { deviceName = it },
                label = "设备名称",
                supportingText = "默认采用手机系统设备名称",
            )
        }

        SectionCard(
            title = "同步与下载",
            subtitle = "控制后台运行、下载和通知行为",
        ) {
            FolderSummary(
                folder = downloadFolder,
                onPickFolder = onPickFolder,
            )
            SettingSwitch(
                title = "开机启动服务",
                description = "设备重启后自动恢复同步服务",
                checked = autoStart,
                onCheckedChange = { autoStart = it },
            )
            SettingSwitch(
                title = "自动同步剪贴板",
                description = "检测本机复制内容并自动发送",
                checked = autoClipboard,
                onCheckedChange = { autoClipboard = it },
            )
            SettingSwitch(
                title = "自动下载通知文件",
                description = "收到文件通知后自动下载到所选目录",
                checked = autoDownload,
                onCheckedChange = { autoDownload = it },
            )
            SettingsTextField(
                value = notificationTimeout,
                onValueChange = { notificationTimeout = it },
                label = "临时通知保留时间（秒）",
                supportingText = "填 0 可关闭自动清理",
                keyboardType = KeyboardType.Number,
            )
            SettingSwitch(
                title = "息屏省电模式",
                description = "息屏后仅保活，暂停 HTTP 接收、剪贴板同步和文件传输",
                checked = screenOffPowerSave,
                onCheckedChange = { screenOffPowerSave = it },
            )
            SettingSwitch(
                title = "保持唤醒锁",
                description = "避免服务运行时 CPU 与 Wi‑Fi 休眠，提高后台同步稳定性，但会增加耗电",
                checked = keepAwake,
                onCheckedChange = { keepAwake = it },
            )
            SettingSwitch(
                title = "允许服务器免密上传",
                description = "仅适用于可信局域网中的兼容服务器",
                checked = allowServerHost,
                onCheckedChange = { allowServerHost = it },
            )
        }

        SectionCard(
            title = "服务控制",
            subtitle = "保存设置后立即重启后台同步服务",
        ) {
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(10.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                Button(onClick = { onSaveAndStart(draftConfig()) }) {
                    Text("保存并启动")
                }
                OutlinedButton(onClick = onStop) {
                    Text("停止服务")
                }
            }
        }

        SectionCard(
            title = "手动发送",
            subtitle = "可随时发送文字、剪贴板或文件",
        ) {
            OutlinedTextField(
                value = directText,
                onValueChange = { directText = it },
                modifier = Modifier.fillMaxWidth(),
                label = { Text("要发送的文字") },
                minLines = 3,
            )
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(10.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                Button(onClick = { onSendText(directText, draftConfig()) }) {
                    Text("发送文字")
                }
                OutlinedButton(onClick = { onSendClipboard(draftConfig()) }) {
                    Text("发送剪贴板")
                }
            }
            OutlinedButton(
                onClick = { onPickFiles(draftConfig()) },
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text("选择并发送文件")
            }
        }

        SectionCard(
            title = "权限与系统设置",
            subtitle = "为后台同步和剪贴板功能授予必要权限",
        ) {
            OutlinedButton(
                onClick = onOpenAccessibility,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text("授予无障碍权限")
            }
            OutlinedButton(
                onClick = onOpenOverlay,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text("授予悬浮窗权限")
            }
            OutlinedButton(
                onClick = onOpenBatterySettings,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text("加入电池优化白名单")
            }
        }

        ElevatedCard(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.elevatedCardColors(
                containerColor = MaterialTheme.colorScheme.surfaceContainerLow,
            ),
        ) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(modifier = Modifier.weight(1f)) {
                        Text(
                            text = "运行日志",
                            style = MaterialTheme.typography.titleLarge,
                            fontWeight = FontWeight.SemiBold,
                        )
                        Text(
                            text = "默认显示简短运行记录；需要排查问题时再开启调试",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    Switch(
                        checked = debugMode,
                        onCheckedChange = {
                            debugMode = it
                            onDebugModeChanged(it)
                        },
                    )
                }
                SelectionContainer {
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .heightIn(min = 112.dp, max = 320.dp)
                            .background(
                                MaterialTheme.colorScheme.surfaceVariant,
                                RoundedCornerShape(14.dp),
                            )
                            .padding(12.dp)
                            .verticalScroll(rememberScrollState()),
                    ) {
                        Text(
                            text = logs.ifBlank { "暂无运行日志" },
                            fontFamily = FontFamily.Monospace,
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
                TextButton(
                    onClick = onClearLogs,
                    modifier = Modifier.align(Alignment.End),
                ) {
                    Text("清空日志")
                }
            }
        }

        Spacer(modifier = Modifier.height(12.dp))
    }
}

@Composable
private fun ServiceStatusCard(
    isRunning: Boolean,
    detail: String,
    port: String,
) {
    val container = if (isRunning) {
        MaterialTheme.colorScheme.primaryContainer
    } else {
        MaterialTheme.colorScheme.surfaceVariant
    }
    val content = if (isRunning) {
        MaterialTheme.colorScheme.onPrimaryContainer
    } else {
        MaterialTheme.colorScheme.onSurfaceVariant
    }
    ElevatedCard(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.elevatedCardColors(containerColor = container),
    ) {
        Row(
            modifier = Modifier.padding(18.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                modifier = Modifier
                    .width(12.dp)
                    .height(12.dp)
                    .background(
                        if (isRunning) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.outline,
                        RoundedCornerShape(50),
                    ),
            )
            Spacer(modifier = Modifier.width(12.dp))
            Column {
                Text(
                    text = if (isRunning) "同步服务正在运行" else "同步服务已停止",
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold,
                    color = content,
                )
                Text(
                    text = detail.ifBlank {
                        if (isRunning) "正在监听端口 " + port else "保存设置后可启动服务"
                    },
                    style = MaterialTheme.typography.bodyMedium,
                    color = content,
                )
            }
        }
    }
}

@Composable
private fun SectionCard(
    title: String,
    subtitle: String,
    content: @Composable ColumnScope.() -> Unit,
) {
    ElevatedCard(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.elevatedCardColors(
            containerColor = MaterialTheme.colorScheme.surfaceContainerLow,
        ),
    ) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
            content = {
                Text(
                    text = title,
                    style = MaterialTheme.typography.titleLarge,
                    fontWeight = FontWeight.SemiBold,
                )
                Text(
                    text = subtitle,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                content()
            },
        )
    }
}

@Composable
private fun SettingsTextField(
    value: String,
    onValueChange: (String) -> Unit,
    label: String,
    supportingText: String,
    keyboardType: KeyboardType = KeyboardType.Text,
) {
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        modifier = Modifier.fillMaxWidth(),
        label = { Text(label) },
        supportingText = { Text(supportingText) },
        singleLine = true,
        keyboardOptions = KeyboardOptions(keyboardType = keyboardType),
    )
}

@Composable
private fun SettingSwitch(
    title: String,
    description: String,
    checked: Boolean,
    onCheckedChange: (Boolean) -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(text = title, style = MaterialTheme.typography.bodyLarge)
            Text(
                text = description,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Spacer(modifier = Modifier.width(12.dp))
        Switch(checked = checked, onCheckedChange = onCheckedChange)
    }
}

@Composable
private fun FolderSummary(
    folder: String,
    onPickFolder: () -> Unit,
) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(
            text = "下载文件夹",
            style = MaterialTheme.typography.bodyLarge,
        )
        Text(
            text = folder.ifBlank { "默认保存到 Download/SyncClipboard" },
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            maxLines = 2,
        )
        OutlinedButton(onClick = onPickFolder) {
            Text("选择下载文件夹")
        }
    }
}