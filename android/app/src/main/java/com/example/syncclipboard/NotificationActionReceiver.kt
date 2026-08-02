package com.example.syncclipboard

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class NotificationActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == ACTION_DOWNLOAD) {
            SyncService.startAction(context, SyncService.ACTION_DOWNLOAD_PENDING)
        }
    }

    companion object {
        const val ACTION_DOWNLOAD = "com.example.syncclipboard.DOWNLOAD"
    }
}
