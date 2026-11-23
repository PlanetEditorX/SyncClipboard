package com.example.syncclipboard

import android.content.Context
import android.net.Uri
import androidx.documentfile.provider.DocumentFile
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.net.URLConnection

object DownloadStore {
    fun save(
        context: Context,
        treeUriText: String,
        filename: String,
        mimeType: String?,
        source: InputStream,
        totalBytes: Long,
        onProgress: ((Long, Long) -> Unit)? = null,
        /** 目录共享里该文件相对根目录的子路径，逐段创建后重建目录层级。 */
        relativeDir: String = "",
    ): Uri {
        val safeName = sanitize(filename)
        AppState.log(context, "准备下载文件: $safeName")

        // 策略 1: 如果用户选择了自定义目录，尝试使用 SAF
        if (treeUriText.isNotBlank()) {
            try {
                val treeUri = Uri.parse(treeUriText)
                val rootDir = DocumentFile.fromTreeUri(context, treeUri)
                if (rootDir != null && rootDir.canWrite()) {
                    var dir: DocumentFile = rootDir
                    for (segment in sanitizeDir(relativeDir)) {
                        dir = dir.findFile(segment)
                            ?: dir.createDirectory(segment)
                            ?: throw IOException("无法创建目录：$segment")
                    }
                    dir.findFile(safeName)?.delete()
                    val type = mimeType?.substringBefore(';') ?: URLConnection.guessContentTypeFromName(safeName) ?: "application/octet-stream"
                    val target = dir.createFile(type, safeName)
                    if (target != null) {
                        context.contentResolver.openOutputStream(target.uri, "w")!!.use { output ->
                            copyWithProgress(source, output, totalBytes, onProgress)
                        }
                        AppState.log(context, "[下载] 成功保存到自定义目录: ${target.name} (大小: $totalBytes)")
                        return target.uri
                    }
                }
            } catch (e: Exception) {
                AppState.log(context, "自定义目录写入失败: ${e.message}")
            }
        }

        // 策略 2: 兜底方案 - 强制保存到公共 Download/SyncClipboard (物理路径)
        // 这种方式 MT 管理器绝对能看到
        try {
            val downloadDir = File(android.os.Environment.getExternalStoragePublicDirectory(android.os.Environment.DIRECTORY_DOWNLOADS), "SyncClipboard")
            val targetDir = resolveDir(downloadDir, relativeDir)
            val targetFile = File(targetDir, safeName)
            targetFile.outputStream().use { output ->
                copyWithProgress(source, output, totalBytes, onProgress)
            }
            
            // 扫描文件让相册和文件管理器更新
            android.media.MediaScannerConnection.scanFile(context, arrayOf(targetFile.absolutePath), null, null)
            
            AppState.log(context, "已保存到公共目录 (MT管理器可见): ${targetFile.absolutePath}")
            return androidx.core.content.FileProvider.getUriForFile(context, "${context.packageName}.files", targetFile)
        } catch (e: Exception) {
            AppState.log(context, "公共目录写入失败: ${e.message}，将存入私有目录")
            
            // 策略 3: 最后的挣扎，存入私有目录
            val privateDir = context.getExternalFilesDir(null) ?: context.filesDir
            val targetFile = File(resolveDir(privateDir, relativeDir), safeName)
            targetFile.outputStream().use { output -> copyWithProgress(source, output, totalBytes, onProgress) }
            return androidx.core.content.FileProvider.getUriForFile(context, "${context.packageName}.files", targetFile)
        }
    }

    private fun copyWithProgress(input: InputStream, output: java.io.OutputStream, total: Long, callback: ((Long, Long) -> Unit)?) {
        input.use { source ->
            val buffer = ByteArray(256 * 1024)
            var copied = 0L
            while (true) {
                val read = source.read(buffer)
                if (read < 0) break
                output.write(buffer, 0, read)
                copied += read
                callback?.invoke(copied, total)
            }
        }
    }

    /** 清单给的 prefix 可能带 "\" 或 "/"；过滤 . / .. 防止目录穿越。 */
    private fun sanitizeDir(raw: String): List<String> =
        raw.replace('\\', '/').split('/')
            .filter { it.isNotBlank() && it != "." && it != ".." }

    private fun resolveDir(base: File, raw: String): File {
        var dir = base
        for (segment in sanitizeDir(raw)) dir = File(dir, segment)
        if (!dir.exists() && !dir.mkdirs()) {
            throw IOException("无法创建目录：${dir.absolutePath}")
        }
        return dir
    }

    private fun sanitize(name: String): String {
        return name.substringAfterLast('/').substringAfterLast('\\')
            .replace(Regex("[\\\\/:*?\"<>|]"), "_")
            .ifBlank { "download-${System.currentTimeMillis()}" }
    }
}
