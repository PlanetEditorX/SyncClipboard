package com.example.syncclipboard

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import android.view.Gravity
import android.widget.FrameLayout
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import java.io.File
import java.util.UUID
import java.util.concurrent.Executors

class ShareActivity : Activity() {
    private val executor = Executors.newSingleThreadExecutor()
    private lateinit var message: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(buildUi())
        handleShare(intent)
    }

    override fun onDestroy() {
        executor.shutdownNow()
        super.onDestroy()
    }

    private fun handleShare(intent: Intent) {
        val action = intent.action
        val uris = when (action) {
            Intent.ACTION_SEND -> getSingleUri(intent)?.let(::listOf).orEmpty()
            Intent.ACTION_SEND_MULTIPLE -> getMultipleUris(intent)
            else -> emptyList()
        }
        if (uris.isNotEmpty()) {
            loadOnlineClients(uris)
            return
        }

        val text = intent.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString().orEmpty()
        if (text.isNotBlank()) {
            SyncService.startAction(this, SyncService.ACTION_SEND_TEXT) {
                putExtra(SyncService.EXTRA_TEXT, text)
            }
            Toast.makeText(this, "文字已加入同步队列", Toast.LENGTH_SHORT).show()
        }
        finish()
    }

    private fun loadOnlineClients(uris: List<Uri>) {
        message.text = "正在获取可发送的在线客户端…"
        executor.execute {
            val result = runCatching {
                ProtocolClient(this, AppConfig.load(this)).getOnlineClients()
            }
            runOnUiThread {
                result.onSuccess { clients ->
                    if (clients.isEmpty()) {
                        Toast.makeText(this, "没有可发送的在线客户端（已排除本机和 iOS）", Toast.LENGTH_LONG).show()
                        finish()
                    } else {
                        showClientPicker(uris, clients)
                    }
                }.onFailure {
                    Toast.makeText(this, "获取在线客户端失败：${it.message}", Toast.LENGTH_LONG).show()
                    AppState.log(this, "获取在线客户端失败：${it.message}")
                    finish()
                }
            }
        }
    }

    private fun showClientPicker(uris: List<Uri>, clients: List<ProtocolClient.OnlineClient>) {
        message.text = "请选择接收客户端"
        val names = clients.map { "${it.displayName} · ${it.os}" }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle("发送 ${uris.size} 个文件到")
            .setItems(names) { _, index ->
                prepareAndUpload(uris, clients[index])
            }
            .setNegativeButton("取消") { _, _ -> finish() }
            .setOnCancelListener { finish() }
            .show()
    }

    private fun prepareAndUpload(uris: List<Uri>, target: ProtocolClient.OnlineClient) {
        message.text = "正在准备 ${uris.size} 个文件…"
        executor.execute {
            val paths = uris.mapNotNull { copyToCache(it) }
            runOnUiThread {
                if (paths.size != uris.size) {
                    paths.forEach { runCatching { File(it).delete() } }
                    Toast.makeText(this, "部分文件无法读取，已取消上传", Toast.LENGTH_LONG).show()
                    finish()
                    return@runOnUiThread
                }
                SyncService.startAction(this, SyncService.ACTION_UPLOAD_FILES) {
                    putStringArrayListExtra(SyncService.EXTRA_FILES, ArrayList(paths))
                    putExtra(SyncService.EXTRA_UPLOAD_URL, target.uploadUrl)
                    putExtra(SyncService.EXTRA_UPLOAD_CLIENT, target.displayName)
                }
                Toast.makeText(this, "正在发送到 ${target.displayName}", Toast.LENGTH_SHORT).show()
                finish()
            }
        }
    }

    private fun copyToCache(uri: Uri): String? = runCatching {
        val dir = File(cacheDir, "shared").apply { mkdirs() }
        val name = queryName(uri)?.replace(Regex("[/\\\\]"), "_") ?: "shared-file"
        val target = File(dir, "${UUID.randomUUID()}-$name")
        contentResolver.openInputStream(uri)!!.use { input ->
            target.outputStream().use { output -> input.copyTo(output, 256 * 1024) }
        }
        target.absolutePath
    }.getOrNull()

    private fun queryName(uri: Uri): String? =
        contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
            if (cursor.moveToFirst()) cursor.getString(0) else null
        }

    @Suppress("DEPRECATION")
    private fun getSingleUri(intent: Intent): Uri? = if (Build.VERSION.SDK_INT >= 33) {
        intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
    } else {
        intent.getParcelableExtra(Intent.EXTRA_STREAM)
    }

    @Suppress("DEPRECATION")
    private fun getMultipleUris(intent: Intent): List<Uri> = if (Build.VERSION.SDK_INT >= 33) {
        intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java).orEmpty()
    } else {
        intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM).orEmpty()
    }

    private fun buildUi(): FrameLayout {
        val frame = FrameLayout(this)
        val progress = ProgressBar(this)
        frame.addView(progress, FrameLayout.LayoutParams(-2, -2, Gravity.CENTER).apply { bottomMargin = 80 })
        message = TextView(this).apply {
            text = "正在处理分享内容…"
            textSize = 16f
            gravity = Gravity.CENTER
        }
        frame.addView(message, FrameLayout.LayoutParams(-1, -2, Gravity.CENTER).apply { topMargin = 100 })
        return frame
    }
}
