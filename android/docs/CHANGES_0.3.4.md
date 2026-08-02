# SyncClipboard Android 0.3.4

## 在线文件目标协议升级

本版本与服务器同步升级，不兼容旧版 `/clients/online` 返回格式。

### 请求

```http
GET /clients/online?source=<本机名称>&source_port=<本机监听端口>
key: <密钥>
```

### 响应

```json
{
  "status": "ok",
  "clients": [
    {
      "name": "办公室电脑",
      "display_name": "办公室电脑 (192.168.1.20)",
      "ip": "192.168.1.20",
      "port": 8899,
      "os": "Windows",
      "upload_url": "http://192.168.1.20:8899/upload_file"
    }
  ]
}
```

### 行为变化

- 服务器根据请求来源 IP、设备名称和监听端口排除请求者自己。
- 服务器排除 iOS、iPadOS、iPhone 和 iPad 客户端。
- Android 只解析 `clients` 数组，不再解析旧版 `online_clients` 和 `upload_url` 字段。
- Android 选择框显示 `设备名 (IP) · 系统`。
- Android 对异常响应再次校验，拒绝本机和 iOS 目标。
