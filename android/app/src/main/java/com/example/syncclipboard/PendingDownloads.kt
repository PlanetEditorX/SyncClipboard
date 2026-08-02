package com.example.syncclipboard

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

object PendingDownloads {
    private const val PREFS = "syncclipboard_pending"

    @Synchronized
    fun replace(context: Context, files: List<ProtocolClient.RemoteFile>) {
        val array = JSONArray()
        files.forEach { file ->
            array.put(
                JSONObject()
                    .put("filename", file.filename)
                    .put("download_url", file.downloadUrl)
                    .put("clear_url", file.clearUrl ?: "")
            )
        }
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString("files", array.toString())
            .apply()
    }

    @Synchronized
    fun append(context: Context, file: ProtocolClient.RemoteFile) {
        val files = load(context).toMutableList()
        files.removeAll { it.filename == file.filename && it.downloadUrl == file.downloadUrl }
        files.add(file)
        replace(context, files)
    }

    fun load(context: Context): List<ProtocolClient.RemoteFile> {
        val raw = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString("files", "[]") ?: "[]"
        val array = runCatching { JSONArray(raw) }.getOrElse { JSONArray() }
        return buildList {
            for (i in 0 until array.length()) {
                val item = array.optJSONObject(i) ?: continue
                val url = item.optString("download_url")
                if (url.isBlank()) continue
                add(
                    ProtocolClient.RemoteFile(
                        filename = item.optString("filename", "download"),
                        downloadUrl = url,
                        clearUrl = item.optString("clear_url").takeIf { it.isNotBlank() },
                    )
                )
            }
        }
    }

    fun clear(context: Context) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().remove("files").apply()
    }
}
