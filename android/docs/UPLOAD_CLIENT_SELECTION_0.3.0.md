# 0.3.0 文件目标选择修复

## 原问题

旧版分享文件时固定向应用配置的 `/upload_file` 发送 multipart POST。该路径在主服务器上可能只承担其他用途，因而返回 HTTP 405。

## 新流程

- 分享文件或在应用内选择文件后，先请求 `GET /clients/online`。
- 密钥同时放入 `key` 查询参数、`key` 请求头和 `X-API-Key` 请求头，以兼容现有 `get_api_key()`。
- 从 `online_clients` 保持显示顺序，并通过 `upload_url[客户端显示名]` 获取上传地址。
- 没有可用客户端时直接取消。
- 弹窗选择一个接收客户端。
- 后台服务按文件原顺序逐个 POST 到所选 URL。
- 完成后通过通知显示成功数量。

## 涉及源码

- `ProtocolClient.kt`：在线客户端查询及指定 URL 上传。
- `ShareActivity.kt`：系统分享入口的客户端选择。
- `MainActivity.kt`：应用内文件选择后的客户端选择。
- `SyncService.kt`：接收目标 URL 并串行上传。
- `NotificationHelper.kt`：上传结果通知。
