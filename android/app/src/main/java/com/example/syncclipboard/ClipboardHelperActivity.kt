package com.example.syncclipboard

import android.content.ClipDescription
import android.content.ClipboardManager
import android.content.Context
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity

/**
 * A tiny, transparent Activity used only after an explicit user action (notification tap)
 * or an accessibility copy action. Android 10+ only exposes clipboard contents to the
 * focused app (or the default IME), so the read must happen after this window gains focus.
 */
class ClipboardHelperActivity : AppCompatActivity() {
    private val handler = Handler(Looper.getMainLooper())
    private var completed = false
    private var attempts = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        setContentView(FrameLayout(this).apply {
            isFocusable = true
            isFocusableInTouchMode = true
            requestFocus()
        })

        window.attributes = window.attributes.apply {
            width = 1
            height = 1
            gravity = Gravity.TOP or Gravity.START
            dimAmount = 0f
        }
        window.addFlags(WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL)
    }

    override fun onPostResume() {
        super.onPostResume()
        scheduleRead(40)
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) scheduleRead(20)
    }

    private fun scheduleRead(delayMs: Long) {
        handler.removeCallbacksAndMessages(null)
        handler.postDelayed(::readClipboardWhenFocused, delayMs)
    }

    private fun readClipboardWhenFocused() {
        if (completed || isFinishing || isDestroyed) return

        if (!hasWindowFocus()) {
            retryOrFinish("窗口尚未获得焦点")
            return
        }

        val manager = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        val description = runCatching { manager.primaryClipDescription }.getOrNull()
        val sensitive = if (Build.VERSION.SDK_INT >= 33) {
            description?.extras?.getBoolean(ClipDescription.EXTRA_IS_SENSITIVE, false) == true
        } else {
            description?.extras?.getBoolean("android.content.extra.IS_SENSITIVE", false) == true
        }
        if (sensitive) {
            AppState.log(this, "剪贴板被标记为敏感内容，未发送")
            Toast.makeText(this, "敏感剪贴板内容不会自动发送", Toast.LENGTH_SHORT).show()
            finishHelper()
            return
        }

        val text = runCatching {
            val clip = manager.primaryClip ?: return@runCatching ""
            if (clip.itemCount == 0) "" else clip.getItemAt(0).coerceToText(this).toString()
        }.getOrDefault("").trimEnd('\u0000')

        if (text.isNotBlank()) {
            completed = true
            AppState.log(this, "已通过前台焦点读取剪贴板（${text.length} 字符）")
            SyncService.startAction(this, SyncService.ACTION_SEND_TEXT) {
                putExtra(SyncService.EXTRA_TEXT, text)
                putExtra(SyncService.EXTRA_REASON, "通知/辅助同步")
            }
            finishHelper()
        } else {
            retryOrFinish("剪贴板为空或系统仍拒绝读取")
        }
    }

    private fun retryOrFinish(reason: String) {
        attempts++
        if (attempts < MAX_ATTEMPTS) {
            scheduleRead(RETRY_DELAYS_MS.getOrElse(attempts - 1) { 250L })
            return
        }
        AppState.log(this, "同步剪贴板失败：$reason")
        Toast.makeText(this, "未能读取剪贴板，请重新复制后再点一次", Toast.LENGTH_SHORT).show()
        finishHelper()
    }

    private fun finishHelper() {
        handler.removeCallbacksAndMessages(null)
        finish()
        if (Build.VERSION.SDK_INT >= 34) {
            overrideActivityTransition(OVERRIDE_TRANSITION_CLOSE, 0, 0)
        } else {
            @Suppress("DEPRECATION")
            overridePendingTransition(0, 0)
        }
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        super.onDestroy()
    }

    companion object {
        const val ACTION_READ_CLIPBOARD = "com.example.syncclipboard.READ_CLIPBOARD"
        private const val MAX_ATTEMPTS = 6
        private val RETRY_DELAYS_MS = longArrayOf(70, 120, 180, 260, 350)
    }
}
