package com.example.syncclipboard

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.nio.charset.StandardCharsets

class ProtocolClient(private val context: Context, private val config: AppConfig) {
    data class HttpResult(val code: Int, val body: String)
    data class RemoteFile(val filename: String, val downloadUrl: String, val clearUrl: String? = null)
    data class OnlineClient(
        val name: String,
        val displayName: String,
        val ip: String,
        val port: Int,
        val os: String,
        val uploadUrl: String,
    )

    fun register(): HttpResult {
        val body = JSONObject()
            .put("file_server_port", config.localPort)
            .put("local_name", config.deviceName)
            .put("key", config.secret)
            .put("os", "Android")
        return postJson(config.endpoint("/register"), body)
    }

    fun sendText(text: String): HttpResult {
        val encoded = Base64.encodeToString(text.toByteArray(StandardCharsets.UTF_8), Base64.NO_WRAP)
        val body = JSONObject()
            .put("key", config.secret)
            .put("type", "text")
            .put("source", config.deviceName)
            .put("content", encoded)
            .put("encode", "base64")
        return postJson(config.endpoint("/text_sync"), body)
    }

    fun getOnlineClients(): List<OnlineClient> {
        val endpoint = Uri.parse(config.endpoint("/clients/online"))
            .buildUpon()
            .appendQueryParameter("source", config.deviceName)
            .appendQueryParameter("source_port", config.localPort.toString())
            .build()
            .toString()
        val connection = open(
            endpoint,
            "GET",
            mapOf("key" to config.secret),
        )
        return try {
            connection.connect()
            val result = HttpResult(connection.responseCode, readResponse(connection))
            AppState.log(
                context,
                "[调试] 可发送客户端响应 ${result.code}: ${result.body.take(800)}",
                isDebug = true,
            )
            ensureSuccess(result)

            val root = JSONObject(result.body)
            if (!root.optString("status").equals("ok", ignoreCase = true)) {
                throw IOException(root.optString("message", "获取可发送客户端失败"))
            }

            val items = root.optJSONArray("clients")
                ?: throw IOException("服务器响应缺少 clients 数组，请同步升级服务器")

            buildList {
                for (i in 0 until items.length()) {
                    val item = items.optJSONObject(i)
                        ?: throw IOException("clients[$i] 不是有效对象")
                    val name = item.optString("name").trim()
                    val displayName = item.optString("display_name").trim()
                    val ip = item.optString("ip").trim()
                    val port = item.optInt("port", -1)
                    val osName = item.optString("os").trim()
                    val uploadUrl = item.optString("upload_url").trim()

                    if (
                        name.isEmpty() ||
                        displayName.isEmpty() ||
                        ip.isEmpty() ||
                        port <= 0 ||
                        osName.isEmpty() ||
                        uploadUrl.isEmpty()
                    ) {
                        throw IOException("clients[$i] 字段不完整")
                    }

                    // iOS 只能主动轮询拉取，不能作为文件直传目标。
                    if (osName.equals("iOS", ignoreCase = true) ||
                        osName.equals("iPadOS", ignoreCase = true)
                    ) {
                        AppState.log(context, "服务器返回了不可直传的 iOS 目标，已拒绝：$displayName")
                        continue
                    }

                    add(
                        OnlineClient(
                            name = name,
                            displayName = displayName,
                            ip = ip,
                            port = port,
                            os = osName,
                            uploadUrl = uploadUrl,
                        ),
                    )
                }
            }
        } finally {
            connection.disconnect()
        }
    }

    fun requestFiles(): List<RemoteFile> {
        val body = JSONObject()
            .put("key", config.secret)
            .put("type", "file")
            .put("source", config.deviceName)
        AppState.log(context, "[调试] 请求列表: $body", isDebug = true)
        val result = postJson(config.endpoint("/request_file"), body, mapOf("key" to config.secret))
        AppState.log(context, "[调试] 列表响应: ${result.body.take(200)}", isDebug = true)
        val root = JSONObject(result.body)
        val files = root.opt("files") ?: root
        return parseRemoteFiles(files)
    }

    fun download(remote: RemoteFile, onProgress: ((Long, Long) -> Unit)? = null): Uri {
        val connection = open(
            remote.downloadUrl,
            "GET",
            mapOf("source" to config.deviceName, "key" to config.secret),
        )
        try {
            connection.connect()
            if (connection.responseCode !in (200..299)) {
                throw IOException("下载失败 HTTP ${connection.responseCode}: ${readResponse(connection)}")
            }
            val saved = DownloadStore.save(
                context = context,
                treeUriText = config.downloadTreeUri,
                filename = remote.filename,
                mimeType = connection.contentType,
                source = BufferedInputStream(connection.inputStream),
                totalBytes = connection.contentLengthLong,
                onProgress = onProgress,
            )
            remote.clearUrl?.takeIf { it.isNotBlank() }?.let { clearRemote(it) }
            return saved
        } finally {
            connection.disconnect()
        }
    }

    fun clearRemote(url: String): HttpResult {
        val connection = open(url, "GET", mapOf("source" to config.deviceName, "key" to config.secret))
        return try {
            connection.connect()
            val result = HttpResult(connection.responseCode, readResponse(connection))
            result.also(::ensureSuccess)
        } finally {
            connection.disconnect()
        }
    }

    fun upload(uri: Uri, targetUrl: String, targetName: String = ""): HttpResult {
        val resolver = context.contentResolver
        val displayName = queryName(resolver, uri) ?: "upload-${System.currentTimeMillis()}"
        val mime = resolver.getType(uri) ?: "application/octet-stream"
        val length = querySize(resolver, uri)
        return uploadStream(displayName, mime, length, targetUrl, targetName) {
            resolver.openInputStream(uri) ?: throw IOException("无法读取文件：$uri")
        }
    }

    fun upload(
        file: File,
        targetUrl: String,
        targetName: String = "",
        mimeType: String = "application/octet-stream",
    ): HttpResult = uploadStream(file.name, mimeType, file.length(), targetUrl, targetName) { file.inputStream() }

    /**
     * 服务器文件上传协议：
     * - PUT 原始二进制请求体（不是 multipart/form-data）
     * - key/source/target 放在请求头
     * - filename 放在 URL 查询参数
     *
     * Android 中转地址形如：
     * /upload_file_to_download?redirect=http://android:8080/upload_file
     * 服务器会从 redirect 内层 URL 的 filename 参数读取文件名，所以这里必须把
     * filename 写入 redirect，而不是只写在外层查询参数。
     */
    private fun uploadStream(
        filename: String,
        mimeType: String,
        length: Long,
        targetUrl: String,
        targetName: String,
        input: () -> java.io.InputStream,
    ): HttpResult {
        val url = buildUploadUrl(targetUrl.trim(), filename)
        if (!(url.startsWith("http://") || url.startsWith("https://"))) {
            throw IOException("无效的客户端上传地址：$url")
        }

        val encodedSource = Uri.encode(config.deviceName)
        val encodedTarget = Uri.encode(targetName)
        AppState.log(
            context,
            "[调试] PUT 原始文件 -> $url\n文件: $filename ($length bytes)\n目标: $targetName",
            isDebug = true,
        )
        val headers = buildMap {
            put("key", config.secret)
            put("source", encodedSource)
            if (targetName.isNotBlank()) put("target", encodedTarget)
        }
        val connection = open(url, "PUT", headers)
        try {
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", mimeType.ifBlank { "application/octet-stream" })
            if (length >= 0L) {
                connection.setFixedLengthStreamingMode(length)
            } else {
                connection.setChunkedStreamingMode(256 * 1024)
            }

            BufferedOutputStream(connection.outputStream).use { out ->
                input().use { stream -> stream.copyTo(out, 256 * 1024) }
            }

            val result = HttpResult(connection.responseCode, readResponse(connection))
            AppState.log(context, "[调试] 文件上传响应 ${result.code}: ${result.body.take(500)}", isDebug = true)
            return result.also(::ensureSuccess)
        } finally {
            connection.disconnect()
        }
    }

    private fun buildUploadUrl(targetUrl: String, filename: String): String {
        if (!(targetUrl.startsWith("http://") || targetUrl.startsWith("https://"))) {
            throw IOException("无效的客户端上传地址：$targetUrl")
        }

        val outer = Uri.parse(targetUrl)
        val redirect = outer.getQueryParameter("redirect")
        if (redirect.isNullOrBlank()) {
            return replaceQueryParameter(outer, "filename", filename).toString()
        }

        val redirectWithFilename = replaceQueryParameter(Uri.parse(redirect), "filename", filename).toString()
        return replaceQueryParameter(outer, "redirect", redirectWithFilename).toString()
    }

    private fun replaceQueryParameter(uri: Uri, name: String, value: String): Uri {
        val rebuilt = uri.buildUpon().clearQuery()
        uri.queryParameterNames.forEach { existingName ->
            if (existingName == name) return@forEach
            uri.getQueryParameters(existingName).forEach { existingValue ->
                rebuilt.appendQueryParameter(existingName, existingValue)
            }
        }
        rebuilt.appendQueryParameter(name, value)
        return rebuilt.build()
    }

    private fun postJson(url: String, body: JSONObject, headers: Map<String, String> = emptyMap()): HttpResult {
        AppState.log(context, "[调试] POST JSON -> $url\nBody: $body\nHeaders: $headers", isDebug = true)
        val connection = open(url, "POST", headers)
        return try {
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            val bytes = body.toString().toByteArray(StandardCharsets.UTF_8)
            connection.setFixedLengthStreamingMode(bytes.size)
            BufferedOutputStream(connection.outputStream).use { it.write(bytes) }
            val result = HttpResult(connection.responseCode, readResponse(connection))
            AppState.log(context, "[调试] 响应 ${result.code}: ${result.body.take(500)}", isDebug = true)
            result.also(::ensureSuccess)
        } finally {
            connection.disconnect()
        }
    }

    private fun open(url: String, method: String, headers: Map<String, String> = emptyMap()): HttpURLConnection {
        val connection = URL(url).openConnection() as HttpURLConnection
        connection.requestMethod = method
        connection.connectTimeout = 15_000
        connection.readTimeout = 60_000
        connection.instanceFollowRedirects = true
        connection.setRequestProperty("User-Agent", "SyncClipboardAndroid/${BuildConfig.VERSION_NAME}")
        headers.forEach { (name, value) -> connection.setRequestProperty(name, value) }
        return connection
    }

    private fun parseRemoteFiles(value: Any): List<RemoteFile> {
        val result = mutableListOf<RemoteFile>()
        when (value) {
            is JSONArray -> for (i in 0 until value.length()) {
                val item = value.optJSONObject(i) ?: continue
                parseRemoteFile(item, i.toString())?.let(result::add)
            }
            is JSONObject -> {
                if (value.has("download_url")) {
                    parseRemoteFile(value, value.optString("filename", "download"))?.let(result::add)
                } else {
                    val keys = value.keys()
                    while (keys.hasNext()) {
                        val key = keys.next()
                        val item = value.optJSONObject(key) ?: continue
                        parseRemoteFile(item, key)?.let(result::add)
                    }
                }
            }
        }
        return result
    }

    private fun parseRemoteFile(item: JSONObject, fallbackName: String): RemoteFile? {
        val url = item.optString("download_url").takeIf { it.isNotBlank() } ?: return null
        val name = item.optString("filename")
            .ifBlank { item.optString("name") }
            .ifBlank { fallbackName }
        return RemoteFile(name, url, item.optString("clear_url").takeIf { it.isNotBlank() })
    }

    private fun readResponse(connection: HttpURLConnection): String {
        val input = if (connection.responseCode in (200..399)) connection.inputStream else connection.errorStream
        if (input == null) return ""
        return input.use { stream ->
            val out = ByteArrayOutputStream()
            stream.copyTo(out)
            out.toString(StandardCharsets.UTF_8.name())
        }
    }

    private fun ensureSuccess(result: HttpResult) {
        if (result.code !in (200..299)) throw IOException("HTTP ${result.code}: ${result.body.take(500)}")
    }

    private fun queryName(resolver: ContentResolver, uri: Uri): String? =
        resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
            if (cursor.moveToFirst()) cursor.getString(0) else null
        }

    private fun querySize(resolver: ContentResolver, uri: Uri): Long =
        resolver.query(uri, arrayOf(OpenableColumns.SIZE), null, null, null)?.use { cursor ->
            if (cursor.moveToFirst() && !cursor.isNull(0)) cursor.getLong(0) else -1L
        } ?: -1L

}
