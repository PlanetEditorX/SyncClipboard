# 更新说明

本版本必须与 SyncClipboard Server 0.3.4 同步更新。

主要变化：

- 请求 `/clients/online` 时提交本机名称和监听端口。
- 只解析服务器返回的 `clients` 数组。
- 目标选择框显示设备名、IP 和系统。
- 不再兼容旧版 `online_clients` 与 `upload_url` 顶层字段。
- 服务器负责隐藏请求者自己、iOS/iPadOS 和离线客户端。

构建要求：JDK 17、Android SDK 35、Gradle 8.7。
