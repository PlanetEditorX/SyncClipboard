# 0.3.2 服务器文件上传协议修复

## 问题

0.3.0/0.3.1 已经会先请求 `/clients/online` 并让用户选择客户端，但上传实现仍使用：

- `POST`
- `multipart/form-data`
- 把 `key` 和 `filename` 放在 multipart 字段中

实际 Flask 服务器的 `/upload_file` 和 `/upload_file_to_download` 都只允许 `PUT`，因此会返回 `405 Method Not Allowed`。

## 客户端现在发送的请求

### 直接上传到 Windows/电脑客户端

```http
PUT /upload_file?filename=<URL编码文件名>
key: <密钥>
source: <URL编码设备名>
target: <URL编码目标名>
Content-Type: <MIME类型>

<原始文件二进制>
```

### 通过服务器中转给 Android

服务器返回的地址类似：

```text
http://SERVER:8000/upload_file_to_download?redirect=http://ANDROID:8080/upload_file
```

由于服务器代码从 `redirect` 内层 URL 的查询参数读取 `filename`，App 会将其转换为等价于：

```text
http://SERVER:8000/upload_file_to_download?redirect=http://ANDROID:8080/upload_file?filename=<文件名>
```

最终网络请求会正确 URL 编码 `redirect` 参数。

## 多文件

选定一个客户端后，文件按列表顺序逐个上传。每个文件都会重新构造自己的文件名 URL，并分别检查 HTTP 响应。某个文件失败不会阻止后续文件继续尝试。

## 调试日志

打开 App 的调试日志后，应看到：

```text
[调试] PUT 原始文件 -> http://...
[调试] 文件上传响应 200: ...
```

不应再看到：

```text
POST multipart/form-data
```
