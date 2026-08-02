package com.example.syncclipboard

import android.content.Context
import android.os.Build
import android.provider.Settings
import java.net.URI

data class AppConfig(
    val serverUrl: String,
    val secret: String,
    val localPort: Int,
    val deviceName: String,
    val downloadTreeUri: String,
    val autoStart: Boolean,
    val autoClipboard: Boolean,
    val autoDownload: Boolean,
    val keepAwake: Boolean,
    val allowServerHostWithoutKey: Boolean,
    val debugMode: Boolean,
    val notificationTimeoutSeconds: Int,
    val screenOffPowerSave: Boolean,
) {
    fun endpoint(path: String): String = serverUrl.trimEnd('/') + "/" + path.trimStart('/')

    fun serverHost(): String? = runCatching { URI(serverUrl).host }.getOrNull()

    companion object {
        private const val PREFS = "syncclipboard_config"

        fun defaultDeviceName(context: Context): String =
            runCatching { Settings.Global.getString(context.contentResolver, Settings.Global.DEVICE_NAME) }
                .getOrNull()
                ?.trim()
                ?.takeIf { it.isNotEmpty() }
                ?: Build.MODEL.trim().ifBlank { "Android" }

        fun load(context: Context): AppConfig {
            val p = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            return AppConfig(
                serverUrl = p.getString("serverUrl", "http://192.168.1.10:8000") ?: "http://192.168.1.10:8000",
                secret = p.getString("secret", "123456") ?: "123456",
                localPort = p.getInt("localPort", 8080).coerceIn(1024, 65535),
                deviceName = p.getString("deviceName", null)?.trim().orEmpty().ifBlank { defaultDeviceName(context) },
                downloadTreeUri = p.getString("downloadTreeUri", "") ?: "",
                autoStart = p.getBoolean("autoStart", true),
                autoClipboard = p.getBoolean("autoClipboard", true),
                autoDownload = p.getBoolean("autoDownload", false),
                keepAwake = p.getBoolean("keepAwake", false),
                allowServerHostWithoutKey = p.getBoolean("allowServerHostWithoutKey", true),
                debugMode = p.getBoolean("debugMode", false),
                notificationTimeoutSeconds = p.getInt("notificationTimeoutSeconds", 60).coerceIn(0, 86400),
                screenOffPowerSave = p.getBoolean("screenOffPowerSave", false),
            )
        }

        fun save(context: Context, config: AppConfig) {
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putString("serverUrl", config.serverUrl.trimEnd('/'))
                .putString("secret", config.secret)
                .putInt("localPort", config.localPort)
                .putString("deviceName", config.deviceName)
                .putString("downloadTreeUri", config.downloadTreeUri)
                .remove("uploadPath")
                .putBoolean("autoStart", config.autoStart)
                .putBoolean("autoClipboard", config.autoClipboard)
                .putBoolean("autoDownload", config.autoDownload)
                .putBoolean("keepAwake", config.keepAwake)
                .putBoolean("allowServerHostWithoutKey", config.allowServerHostWithoutKey)
                .putBoolean("debugMode", config.debugMode)
                .putInt("notificationTimeoutSeconds", config.notificationTimeoutSeconds.coerceIn(0, 86400))
                .putBoolean("screenOffPowerSave", config.screenOffPowerSave)
                .apply()
        }
    }
}
