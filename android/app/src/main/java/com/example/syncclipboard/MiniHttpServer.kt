package com.example.syncclipboard

import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URI
import java.net.URL
import java.net.URLDecoder
import java.nio.charset.StandardCharsets
import java.util.Locale
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

class MiniHttpServer(
    private val configProvider: () -> AppConfig,
    private val listener: Listener,
) {
    interface Listener {
        fun onTextUpdate(payload: JSONObject)
        fun onFileUpdate(payload: Any)
        fun onDirectFile(file: ProtocolClient.RemoteFile)
        fun onServerLog(message: String, isDebug: Boolean = false)
    }

    private val running = AtomicBoolean(false)
    private var serverSocket: ServerSocket? = null
    private var acceptThread: Thread? = null
    private var workers: ExecutorService? = null

    fun start(port: Int) {
        if (!running.compareAndSet(false, true)) return
        try {
            workers = Executors.newCachedThreadPool()
            serverSocket = ServerSocket().apply {
                reuseAddress = true
                bind(InetSocketAddress(port))
            }
            acceptThread = Thread({ acceptLoop() }, "SyncClipboard-HttpAccept").also { it.start() }
            listener.onServerLog("本机 HTTP 服务已监听 0.0.0.0:$port")
        } catch (e: Exception) {
            stop()
            throw e
        }
    }

    fun stop() {
        running.set(false)
        runCatching { serverSocket?.close() }
        workers?.shutdownNow()
        acceptThread?.interrupt()
        serverSocket = null
        workers = null
        acceptThread = null
    }

    private fun acceptLoop() {
        while (running.get()) {
            try {
                val socket = serverSocket?.accept() ?: break
                workers?.execute { handle(socket) }
            } catch (e: Exception) {
                if (running.get()) listener.onServerLog("HTTP 服务异常：${e.message}")
            }
        }
    }

    private fun handle(socket: Socket) {
        socket.use { client ->
            client.soTimeout = 20_000
            val input = BufferedInputStream(client.getInputStream())
            val output = BufferedOutputStream(client.getOutputStream())
            try {
                val requestLine = readLine(input) ?: return
                val parts = requestLine.split(' ')
                if (parts.size < 2) return respond(output, 400, "Bad Request")
                val method = parts[0].uppercase(Locale.ROOT)
                val target = parts[1]
                val headers = linkedMapOf<String, String>()
                while (true) {
                    val line = readLine(input) ?: break
                    if (line.isEmpty()) break
                    val index = line.indexOf(':')
                    if (index > 0) headers[line.substring(0, index).trim().lowercase(Locale.ROOT)] = line.substring(index + 1).trim()
                }
                val contentLength = headers["content-length"]?.toIntOrNull()?.coerceIn(0, 2 * 1024 * 1024) ?: 0
                val bodyBytes = ByteArray(contentLength)
                var offset = 0
                while (offset < contentLength) {
                    val count = input.read(bodyBytes, offset, contentLength - offset)
                    if (count < 0) break
                    offset += count
                }
                val body = String(bodyBytes, 0, offset, StandardCharsets.UTF_8)
                val uri = URI(target)
                val path = uri.path.trimEnd('/').ifBlank { "/" }
                val query = parseQuery(uri.rawQuery)
                route(
                    method = method,
                    path = path,
                    query = query,
                    headers = headers,
                    body = body,
                    remoteAddress = client.inetAddress,
                    output = output,
                )
            } catch (e: Exception) {
                listener.onServerLog("处理入站请求失败：${e.message}")
                runCatching { respond(output, 500, "Internal Server Error") }
            }
        }
    }

    private fun route(
        method: String,
        path: String,
        query: Map<String, String>,
        headers: Map<String, String>,
        body: String,
        remoteAddress: java.net.InetAddress,
        output: java.io.BufferedOutputStream,
    ) {
        val config = configProvider()
        listener.onServerLog("[调试] 入站请求: $method $path\nQuery: $query\nHeaders: $headers\nBody: ${body.take(200)}", isDebug = true)
        when (path) {
            "/ping" -> respond(output, 200, "OK")
            "/update/current_latest" -> {
                if (method != "POST") return respond(output, 405, "Method Not Allowed")
                val root = JSONObject(body)
                if (root.optString("key") != configProvider().secret) return respond(output, 403, "Invalid key")
                val rawPayload = root.opt("latest_global") ?: JSONObject()
                val payload = when (rawPayload) {
                    is JSONObject -> rawPayload
                    is String -> runCatching { JSONObject(rawPayload) }.getOrElse { rawPayload }
                    else -> rawPayload
                }
                when (root.optString("type").lowercase(Locale.ROOT)) {
                    "text" -> listener.onTextUpdate(if (payload is JSONObject) payload else JSONObject().put("content", payload.toString()))
                    "file" -> listener.onFileUpdate(payload)
                    else -> return respond(output, 400, "Unknown type")
                }
                respond(output, 200, "OK")
            }
            "/upload_file" -> {
                if (method != "POST" && method != "GET") return respond(output, 405, "Method Not Allowed")
                val root = if (body.isBlank()) JSONObject() else JSONObject(body)
                if (!trustedDirectRequest(root, query, headers, remoteAddress)) return respond(output, 403, "Invalid key or host")
                val downloadUrl = root.optString("download_url")
                if (!isHttpUrl(downloadUrl)) return respond(output, 400, "Invalid download_url")
                val filename = query["filename"]
                    ?: root.optString("filename").takeIf { it.isNotBlank() }
                    ?: URL(downloadUrl).path.substringAfterLast('/').ifBlank { "download" }
                val clearUrl = root.optString("clear_url").takeIf { isHttpUrl(it) }
                listener.onDirectFile(ProtocolClient.RemoteFile(filename, downloadUrl, clearUrl))
                respond(output, 200, "OK")
            }
            else -> respond(output, 404, "Not Found")
        }
    }

    private fun trustedDirectRequest(
        root: JSONObject,
        query: Map<String, String>,
        headers: Map<String, String>,
        remoteAddress: InetAddress,
    ): Boolean {
        val config = configProvider()
        val suppliedKey = root.optString("key").ifBlank { query["key"] ?: headers["key"].orEmpty() }
        if (suppliedKey == config.secret) return true
        if (!config.allowServerHostWithoutKey) return false
        val serverHost = config.serverHost() ?: return false
        val addresses = runCatching { InetAddress.getAllByName(serverHost).toSet() }.getOrDefault(emptySet())
        return addresses.any { it.hostAddress == remoteAddress.hostAddress }
    }

    private fun isHttpUrl(value: String): Boolean = runCatching {
        val url = URL(value)
        url.protocol == "http" || url.protocol == "https"
    }.getOrDefault(false)

    private fun parseQuery(raw: String?): Map<String, String> {
        if (raw.isNullOrBlank()) return emptyMap()
        return raw.split('&').mapNotNull { part ->
            val pieces = part.split('=', limit = 2)
            val key = URLDecoder.decode(pieces[0], StandardCharsets.UTF_8.name())
            val value = URLDecoder.decode(pieces.getOrElse(1) { "" }, StandardCharsets.UTF_8.name())
            key to value
        }.toMap()
    }

    private fun readLine(input: BufferedInputStream): String? {
        val out = ByteArrayOutputStream()
        var previous = -1
        while (true) {
            val current = input.read()
            if (current < 0) return if (out.size() == 0) null else out.toString(StandardCharsets.UTF_8.name())
            if (previous == '\r'.code && current == '\n'.code) {
                val bytes = out.toByteArray()
                return String(bytes, 0, (bytes.size - 1).coerceAtLeast(0), StandardCharsets.UTF_8)
            }
            out.write(current)
            previous = current
            if (out.size() > 16 * 1024) error("HTTP header line too long")
        }
    }

    private fun respond(output: BufferedOutputStream, status: Int, body: String) {
        val reason = when (status) {
            200 -> "OK"
            400 -> "Bad Request"
            403 -> "Forbidden"
            404 -> "Not Found"
            405 -> "Method Not Allowed"
            else -> "Internal Server Error"
        }
        val bytes = body.toByteArray(StandardCharsets.UTF_8)
        output.write("HTTP/1.1 $status $reason\r\n".toByteArray())
        output.write("Content-Type: text/plain; charset=utf-8\r\n".toByteArray())
        output.write("Content-Length: ${bytes.size}\r\n".toByteArray())
        output.write("Connection: close\r\n\r\n".toByteArray())
        output.write(bytes)
        output.flush()
    }
}
