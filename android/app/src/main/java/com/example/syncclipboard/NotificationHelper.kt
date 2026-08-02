package com.example.syncclipboard

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import androidx.core.app.NotificationCompat

object NotificationHelper {
    const val SERVICE_NOTIFICATION_ID = 1001
    const val FILE_NOTIFICATION_ID = 2001
    private const val COMPLETE_NOTIFICATION_ID = 2002
    private const val UPLOAD_NOTIFICATION_ID = 2003
    private const val SERVICE_CHANNEL_ID = "syncclipboard_service"
    private const val FILE_CHANNEL_ID = "syncclipboard_files"

    fun createChannels(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java)

        manager.createNotificationChannel(
            NotificationChannel(
                SERVICE_CHANNEL_ID,
                "后台同步服务",
                NotificationManager.IMPORTANCE_LOW,
            )
        )

        val fileChannel = NotificationChannel(
            FILE_CHANNEL_ID,
            "文件同步通知",
            NotificationManager.IMPORTANCE_HIGH,
        ).apply {
            enableVibration(true)
            setShowBadge(true)
        }
        manager.createNotificationChannel(fileChannel)
    }

    fun serviceNotification(context: Context, detail: String): Notification {
        val openIntent = PendingIntent.getActivity(
            context, 1, Intent(context, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val syncClipIntent = PendingIntent.getService(
            context, 6, Intent(context, SyncService::class.java).setAction(SyncService.ACTION_SYNC_CLIPBOARD),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val stopIntent = PendingIntent.getService(
            context, 2, Intent(context, SyncService::class.java).setAction(SyncService.ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(context, SERVICE_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_sync)
            .setContentTitle("SyncClipboard 运行中")
            .setContentText(detail)
            .setContentIntent(openIntent)
            .setOngoing(true)
            .addAction(0, "同步剪贴板", syncClipIntent)
            .addAction(0, "停止", stopIntent)
            .build()
    }

    fun showFileAvailable(context: Context, filenames: List<String>) {
        val cleanNames = filenames.map { it.trim() }.filter { it.isNotBlank() }.distinct()
        val count = cleanNames.size
        val preview = cleanNames.take(3).joinToString("、")
        val title = when {
            count > 1 -> "检测到 $count 个新文件"
            count == 1 -> "检测到新文件"
            else -> "检测到新文件"
        }
        val text = when {
            count == 1 -> cleanNames.first()
            count in 2..3 -> preview
            count > 3 -> "$preview…（共 $count 个）"
            else -> "点击下载服务器发布的文件"
        }
        val expandedText = when {
            count in 1..8 -> cleanNames.joinToString("\n")
            count > 8 -> cleanNames.take(8).joinToString("\n") + "\n……共 $count 个文件"
            else -> text
        }
        val downloadIntent = PendingIntent.getBroadcast(
            context, 3, Intent(context, NotificationActionReceiver::class.java).setAction(NotificationActionReceiver.ACTION_DOWNLOAD),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(context, FILE_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_sync)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(expandedText))
            .setContentIntent(downloadIntent)
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .addAction(0, "立即下载", downloadIntent)
            .applyTemporaryTimeout(context)
            .build()
        context.getSystemService(NotificationManager::class.java).notify(FILE_NOTIFICATION_ID, notification)
    }

    fun showDownloadComplete(context: Context, filename: String, uri: Uri) {
        val viewIntent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, context.contentResolver.getType(uri) ?: "*/*")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        val pending = PendingIntent.getActivity(
            context, 5, viewIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(context, FILE_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_sync)
            .setContentTitle("下载成功")
            .setContentText(filename)
            .setContentIntent(pending)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setAutoCancel(true)
            .applyTemporaryTimeout(context)
            .build()
        context.getSystemService(NotificationManager::class.java).notify(COMPLETE_NOTIFICATION_ID, notification)
    }

    fun showUploadResult(context: Context, target: String, successCount: Int, totalCount: Int) {
        val openIntent = PendingIntent.getActivity(
            context,
            7,
            Intent(context, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val allSucceeded = totalCount > 0 && successCount == totalCount
        val notification = NotificationCompat.Builder(context, FILE_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_sync)
            .setContentTitle(if (allSucceeded) "文件发送完成" else "文件发送未全部完成")
            .setContentText("$target：成功 $successCount/$totalCount")
            .setContentIntent(openIntent)
            .setPriority(if (allSucceeded) NotificationCompat.PRIORITY_DEFAULT else NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .applyTemporaryTimeout(context)
            .build()
        context.getSystemService(NotificationManager::class.java).notify(UPLOAD_NOTIFICATION_ID, notification)
    }

    fun cancelFile(context: Context) {
        context.getSystemService(NotificationManager::class.java).cancel(FILE_NOTIFICATION_ID)
    }

    private fun NotificationCompat.Builder.applyTemporaryTimeout(context: Context): NotificationCompat.Builder {
        val timeoutSeconds = AppConfig.load(context).notificationTimeoutSeconds
        if (timeoutSeconds > 0) setTimeoutAfter(timeoutSeconds * 1000L)
        return this
    }
}
