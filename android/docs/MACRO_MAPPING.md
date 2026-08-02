# MacroDroid 宏到原生应用的映射

本文件根据上传的 `macrodroid.zip` 中宏配置整理。

## 全局变量

| MacroDroid 变量 | 应用配置 | 默认值 |
|---|---|---|
| 主机 | 主机 | `http://192.168.1.10:8000` |
| 密钥 | 密钥 | `123456` |
| 本机服务监听端口 | 本机服务监听端口 | `8080` |

## 注册服务

```http
POST {主机}/register
Content-Type: application/json

{
  "file_server_port": 8080,
  "local_name": "设备名称",
  "key": "123456",
  "os": "Android"
}
```

原宏定时注册；应用在启动、网络恢复后以及每 12 小时注册。

## 同步文本

```http
POST {主机}/text_sync
Content-Type: application/json

{
  "key": "123456",
  "type": "text",
  "source": "设备名称",
  "content": "Base64文本",
  "encode": "base64"
}
```

应用会避免把刚收到的远程文字再次发回服务器。

## ping 服务

```http
GET /ping
```

返回：

```text
OK
```

## 接受更新

```http
POST /update/current_latest
Content-Type: application/json

{
  "key": "123456",
  "type": "text 或 file",
  "latest_global": {
    "source": "来源设备",
    "id": "更新ID",
    "content": "内容",
    "encode": "base64"
  }
}
```

`latest_global` 既支持 JSON 对象，也兼容包含 JSON 的字符串。

- `type=text`：解码文字并写入剪贴板。
- `type=file`：显示文件通知，或按设置自动下载。

## 下载文件

```http
POST {主机}/request_file
key: 123456
Content-Type: application/json

{
  "key": "123456",
  "type": "file",
  "source": "设备名称"
}
```

响应中的 `files` 可为对象或数组。每个文件至少包含：

```json
{
  "filename": "example.zip",
  "download_url": "http://server/file/example.zip"
}
```

下载时附带：

```http
source: 设备名称
key: 123456
```

## 接受服务器中转上传

```http
POST /upload_file?filename=example.zip
Content-Type: application/json

{
  "key": "123456",
  "download_url": "http://server/temp/example.zip",
  "clear_url": "http://server/temp/clear/id"
}
```

也兼容 `GET`。下载完成后，如有 `clear_url`，应用会调用该地址清理服务器中转文件。

旧宏没有校验此接口的密钥。应用默认要求以下条件之一：

1. 请求携带正确 `key`；
2. 开启兼容选项，且请求来源 IP 与配置的服务器主机解析结果一致。

## 新增的安卓文件发送

旧宏没有提供安卓上传接口。应用新增系统文件选择器和分享入口，并按 README 中定义的 multipart 协议上传。
