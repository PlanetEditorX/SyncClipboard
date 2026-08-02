# 剪贴板同步修复（0.2.1）

## 原因

旧版本通知栏按钮调用 `SyncService.ACTION_SYNC_CLIPBOARD`。Android 10+ 会检查读取方是否为当前焦点应用或默认输入法，前台 Service 并不等于界面焦点，因此按钮看起来被点击了，但 `ClipboardManager.primaryClip` 仍可能返回空。

旧版 `ClipboardHelperActivity` 又在 `onCreate()` 立即读取，此时透明窗口通常还没有获得焦点，因此仍会失败。

## 修改

- `NotificationHelper.kt`：通知动作由 `PendingIntent.getService()` 改为 `PendingIntent.getActivity()`。
- `ClipboardHelperActivity.kt`：在 `onWindowFocusChanged(true)` / `onPostResume()` 后读取，最多重试 6 次。
- `SyncAccessibilityService.kt`：监听 `TYPE_VIEW_TEXT_SELECTION_CHANGED`，缓存非密码选区；点击标准“复制/Copy”动作时直接发送选区。
- `accessibility_service_config.xml`：只订阅复制识别需要的点击和文本选择事件，减少误触发。
- `SyncService.kt`：删除无效悬浮窗读取；重复抑制改为时间窗口。
- `AndroidManifest.xml`：删除 `SYSTEM_ALERT_WINDOW` 权限。

## 测试

1. 安装新版后重新打开应用并启动服务。
2. 重新确认无障碍服务已启用；覆盖安装后部分系统会自动关闭无障碍服务。
3. 在记事本类原生应用中选择一段文字并点“复制”。
4. 查看 SyncClipboard 日志，应出现“无障碍捕获到复制文字”或“已通过前台焦点读取剪贴板”。
5. 在浏览器中测试；若自动捕获失败，点通知栏“同步剪贴板”，应短暂切换焦点并发送。

## 已知限制

- 某些 WebView、游戏、远程桌面和自绘控件不公开文字选择范围。
- 某些厂商 ROM 会阻止无障碍服务拉起透明 Activity。
- 系统标记为敏感或密码字段的内容不会自动发送。
