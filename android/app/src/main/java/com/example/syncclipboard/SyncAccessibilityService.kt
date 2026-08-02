package com.example.syncclipboard

import android.accessibilityservice.AccessibilityService
import android.os.SystemClock
import android.view.accessibility.AccessibilityEvent

class SyncAccessibilityService : AccessibilityService() {
    
    private var selectedText: String = ""
    private var lastSelectedAt: Long = 0
    private var lastProcessedText: String = ""

    override fun onAccessibilityEvent(event: AccessibilityEvent) {
        val packageName = event.packageName?.toString() ?: "unknown"
        if (packageName == this.packageName) return

        when (event.eventType) {
            AccessibilityEvent.TYPE_VIEW_TEXT_SELECTION_CHANGED -> {
                val text = event.source?.text?.toString()
                val from = event.fromIndex
                val to = event.toIndex
                if (text != null && from >= 0 && to > from && to <= text.length) {
                    val newSelection = text.substring(from, to)
                    if (newSelection != selectedText) {
                        selectedText = newSelection
                        lastSelectedAt = SystemClock.elapsedRealtime()
                        AppState.log(this, "[调试] 已预选文字", isDebug = true)
                    }
                }
            }

            AccessibilityEvent.TYPE_VIEW_CLICKED -> {
                val node = event.source
                val label = node?.text?.toString() ?: ""
                if (isCopyLabel(label)) {
                    AppState.log(this, "[无障碍] 点击复制按钮", isDebug = true)
                    trySyncPreSelectedText("按钮点击")
                }
            }

            // 仅在窗口状态改变时检查（例如弹出层出现），减少 CPU 消耗和悬浮窗频率
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED -> {
                if (packageName.contains("systemui")) {
                    AppState.log(this, "[无障碍] 系统剪贴板界面弹出", isDebug = true)
                    trySyncPreSelectedText("系统 UI 触发")
                }
            }
        }
    }

    private fun trySyncPreSelectedText(reason: String) {
        val now = SystemClock.elapsedRealtime()
        if (selectedText.isNotEmpty() && (now - lastSelectedAt < 15000)) {
            if (selectedText != AppState.lastGlobalText || (now - AppState.lastGlobalAt > 10000)) {
                if (selectedText != lastProcessedText) {
                    lastProcessedText = selectedText
                    AppState.log(this, "[同步] $reason 触发上传", isDebug = false)
                    SyncService.startAction(this, SyncService.ACTION_SEND_TEXT) {
                        putExtra(SyncService.EXTRA_TEXT, selectedText)
                    }
                }
            }
        } else {
            // 只有在明确需要时才请求 Service 启动悬浮窗，避免无意义的闪烁
            SyncService.startAction(this, SyncService.ACTION_SYNC_CLIPBOARD)
        }
    }

    private fun isCopyLabel(text: String): Boolean {
        val t = text.lowercase()
        return t.contains("复制") || t.contains("copy") || t.contains("拷贝") || t.contains("链接文本")
    }

    override fun onInterrupt() {}
}
