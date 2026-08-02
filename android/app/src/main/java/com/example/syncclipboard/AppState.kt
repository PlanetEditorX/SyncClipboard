package com.example.syncclipboard

import android.content.Context
import android.content.Intent
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

object AppState {
    const val ACTION_STATE_CHANGED = "com.example.syncclipboard.STATE_CHANGED"
    private const val PREFS = "syncclipboard_state"
    private const val MAX_LOG_LENGTH = 12000

    var isAppInForeground: Boolean = false
    
    // 全局防回显指纹
    @Volatile
    var lastGlobalText: String = ""
    @Volatile
    var lastGlobalAt: Long = 0

    fun setRunning(context: Context, running: Boolean, detail: String = "") {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putBoolean("running", running)
            .putString("detail", detail)
            .apply()
        notifyChanged(context)
    }

    fun isRunning(context: Context): Boolean =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean("running", false)

    fun detail(context: Context): String =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString("detail", "") ?: ""

    @Synchronized
    fun log(context: Context, message: String, isDebug: Boolean = false) {
        if (isDebug && !AppConfig.load(context).debugMode) return
        
        val time = SimpleDateFormat("HH:mm:ss.SSS", Locale.getDefault()).format(Date())
        val formattedMessage = "[$time] $message"
        android.util.Log.d("SyncClipboard", formattedMessage)
        
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val old = prefs.getString("log", "") ?: ""
        val next = (formattedMessage + "\n" + old).take(MAX_LOG_LENGTH)
        prefs.edit().putString("log", next).apply()
        notifyChanged(context)
    }

    fun logs(context: Context): String =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString("log", "") ?: ""

    fun clearLogs(context: Context) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().remove("log").apply()
        notifyChanged(context)
    }

    private fun notifyChanged(context: Context) {
        context.sendBroadcast(Intent(ACTION_STATE_CHANGED).setPackage(context.packageName))
    }
}
