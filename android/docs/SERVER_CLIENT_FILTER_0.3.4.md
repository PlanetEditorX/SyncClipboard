# `/clients/online` 新协议

服务器和 Android 必须同时升级。

## 服务器返回结构

接口只返回：

```json
{
  "status": "ok",
  "clients": []
}
```

每个客户端对象包含：

- `name`
- `display_name`
- `ip`
- `port`
- `os`
- `upload_url`

服务器在返回前完成以下过滤：

1. 排除发起请求的客户端自己；
2. 排除所有 iOS/iPadOS 客户端；
3. 排除端口无效或 `/ping` 不可访问的客户端；
4. Android 目标使用服务器中转上传地址；
5. Windows、macOS 等目标使用其本机 `/upload_file` 地址。

旧版 `online_clients` 和 `upload_url` 顶层字段已删除。
