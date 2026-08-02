# SyncClipboard Android 0.3.4（在线目标过滤版）

一个用于替代 MacroDroid 宏的原生 Android 客户端。0.3.4 在 0.3.3 基础上修正文件发送目标列表：隐藏当前 Android 设备自身，并排除只能主动轮询拉取的 iOS 客户端。

## 已实现

- 前台常驻服务，监听本机 HTTP 端口，默认 `8080`
- 开机启动、网络恢复后重新注册、每 12 小时定时注册
- 兼容 `POST /register`、`POST /text_sync`、`POST /request_file`
- 接收入站 `/ping`、`/update/current_latest`、`/upload_file`
- 接收远程文字并写入系统剪贴板，避免同步回环
- 接收文件发布通知并显示文件名；多文件过多时显示前 3 个名称和省略提示
- 使用 Android 系统目录选择器指定下载文件夹
- 使用系统文件选择器发送一个或多个文件
- 在其他应用中通过“分享到 SyncClipboard”发送文字或文件
- 文件发送前通过 `GET /clients/online` 获取可接收客户端；列表隐藏当前设备自身和 iOS，只保留支持被直接推送文件的目标
- 临时通知默认 60 秒自动清理，可在设置中改为 0–86400 秒（0 表示不自动清理）
- 可选息屏省电模式：息屏仅保活，暂停 HTTP 接收、剪贴板同步和文件传输
- 可选 CPU/Wi-Fi 唤醒锁、自动下载、日志查看
- 入站更新校验密钥；直接文件中转请求可校验密钥或配置的服务器 IP


## 0.3.4 新增功能

- Android 请求在线客户端时附带 `source` 和 `source_port`，供服务器识别请求者。
- 服务器 `/clients/online` 排除当前请求客户端、iOS/iPadOS 和离线客户端。
- 接口只返回新的 `clients` 数组；旧版 `online_clients`、`upload_url` 顶层字段已删除。
- Android 只解析新结构，并使用每个对象中的 `os` 和 `upload_url`。
- 主界面文件选择和系统“分享到 SyncClipboard”两条发送入口共用同一目标列表。

服务器和 Android 必须同时升级，协议说明见 [`docs/SERVER_CLIENT_FILTER_0.3.4.md`](docs/SERVER_CLIENT_FILTER_0.3.4.md)。

## 0.3.3 新增功能

- 服务器通过 `latest_global` 传来的文件数组会被完整解析，通知可显示具体文件名。
- 单文件显示完整名称；多文件显示数量和前 3 个名称，更多内容用省略号表示，展开后最多显示前 8 个。
- 新文件、下载完成、发送结果通知默认 60 秒后自动清理；设置为 `0` 可关闭自动清理。
- 启用“息屏省电模式”后，息屏期间只保留前台服务，不再监听本机 HTTP 端口，也不进行剪贴板、上传、下载或注册请求；亮屏后自动恢复。
- 桌面自适应图标的前景缩小到 72%，通知栏 `ic_sync` 图标保持不变。

详见 [`docs/CHANGES_0.3.3.md`](docs/CHANGES_0.3.3.md)。

## 构建

要求：Android Studio Koala Feature Drop（2024.1.2）或更新版本、JDK 17、Android SDK 35。

继续使用兼容构建组合 AGP 8.6.1 / Gradle 8.7 / API 35，以避免较旧 Android Studio 在导入新工程时出现 `An internal error has occurred`。

1. 用 Android Studio 打开本目录。
2. 等待 Gradle 同步完成。
3. 选择 `app`，构建或运行到 Android 设备。
4. 命令行也可执行：

```bash
./gradlew assembleDebug
```

调试 APK 生成在：

```text
app/build/outputs/apk/debug/app-debug.apk
```


## Android Studio 内部错误处理

如果 Android Studio 曾经打开过旧工程：

1. 关闭 Android Studio；
2. 将工程解压到较短的纯英文路径，例如 `C:\Android\SyncClipboardAndroid`；
3. Windows 运行 `reset-project-windows.bat`，macOS/Linux 运行 `./reset-project-macos-linux.sh`；
4. 重新打开工程根目录，不要只打开 `app`；
5. 在 Gradle 设置里选择 Embedded JDK 17；
6. 安装 Android SDK Platform 35 和 Build-Tools 35.x；
7. 执行 `File > Sync Project with Gradle Files`。

若仍显示内部错误，请通过 `Help > Collect Logs and Diagnostic Data` 收集日志，重点提供 `idea.log` 最后 100 行。

## 初次配置

1. 主机填写服务器地址，例如 `http://192.168.1.10:8000`。
2. 密钥填写服务器密钥，例如 `123456`。
3. 本机监听端口保持 `8080`，或与服务器登记的端口一致。
4. 设置设备名称。
5. 选择下载目录；不选择时文件保存在应用专属 Download 目录。
6. 点击“保存并启动”，允许通知权限。
7. 建议在系统电池设置中允许后台运行，并为该应用关闭电池优化。

服务器应能访问手机局域网 IP，例如：

```text
http://<手机局域网IP>:8080/ping
http://<手机局域网IP>:8080/update/current_latest
http://<手机局域网IP>:8080/upload_file?filename=example.zip
```

## Android 剪贴板限制与 0.2.1 修复

Android 10 及以上版本只允许当前获得焦点的应用或默认输入法读取剪贴板。因此，单纯把“同步剪贴板”命令发送给前台 Service 仍然会被系统拒绝；悬浮窗也不能可靠地获得满足剪贴板策略的应用焦点。

0.2.1 对此做了以下修复：

- 通知栏“同步剪贴板”改为启动一个 1×1 的透明辅助 Activity；
- 辅助 Activity 等待窗口真正获得焦点后再读取，并进行多次短间隔重试；
- 无障碍服务监听标准文本选择事件，用户点击“复制”时优先直接发送选中文字；
- 无障碍事件没有暴露选中文字时，自动尝试透明辅助 Activity；
- 密码字段和被系统标记为敏感的剪贴板内容不会自动发送；
- 删除了不能可靠绕过 Android 限制的悬浮窗权限和读取逻辑；
- 同一文字的去重改为短时间窗口，不再永久阻止以后再次发送相同文字。

推荐设置：

1. 在应用内保存配置并启动服务；
2. 打开“授予无障碍权限（辅助后台同步）”，启用 SyncClipboard；
3. 在普通原生文本框中选中文字并点击“复制”，观察运行日志；
4. 对浏览器、WebView 或定制应用，如果自动识别失败，点击通知栏“同步剪贴板”；
5. 也可以使用系统“分享”到 SyncClipboard，这是不依赖剪贴板读取权限的稳定方式。

受 Android 系统和各厂商 ROM 限制，第三方应用仍不能保证在所有应用中做到百分之百无感监听。部分应用不向无障碍服务公开选区或复制菜单，部分系统也会限制无障碍服务启动透明页面；这类场景应使用通知按钮或分享菜单。

## 文件上传流程

Android 端不再固定向主机的 `/upload_file` 上传。正确流程为：

1. 请求 `GET {主机}/clients/online?source=<本机名称>&source_port=<本机端口>`，密钥放在 `key` 请求头；
2. 读取返回 JSON 中的 `clients` 数组；
3. 服务器排除请求者、iOS/iPadOS 和离线客户端；若没有可发送目标则取消发送；
4. 有在线客户端时弹窗选择接收端；
5. 将本次选择得到的上传 URL 传给后台服务；
6. 多个文件按顺序逐个以原始二进制 `PUT` 上传到该 URL。

预期响应示例：

```json
{
  "status": "ok",
  "online_clients": ["电脑 (192.168.1.20)", "手机 (192.168.1.30)"],
  "upload_url": {
    "电脑 (192.168.1.20)": "http://192.168.1.20:8899/upload_file",
    "手机 (192.168.1.30)": "http://192.168.1.10:8000/upload_file_to_download?redirect=http://192.168.1.30:8080/upload_file"
  }
}
```

上传请求严格匹配服务器：`key`、`source`、`target` 位于请求头；请求体是文件原始字节；普通目标的文件名放在外层 `filename` 查询参数。安卓中转目标会把 `filename` 写入内层 `redirect` URL，因为服务器从该内层 URL 解析文件名。应用设置中的“旧版备用上传路径”仅为兼容旧配置保留，新客户端选择流程不会使用它。

## 0.3.2 文件上传协议修复

旧版 App 对 `upload_url` 使用 `POST multipart/form-data`，而实际服务器只声明了 `PUT`，因此 Flask 返回 HTTP 405。服务器还从请求头读取 `key`，并通过 URL 查询参数读取文件名。

0.3.2 改为：

```text
PUT <upload_url-with-filename>
key: <密钥>
source: <URL 编码后的设备名称>
target: <URL 编码后的目标客户端名称>
Content-Type: <文件 MIME 类型>

<原始文件字节>
```

直接电脑目标示例：

```text
PUT http://192.168.1.20:8899/upload_file?filename=照片.jpg
```

安卓中转目标示例（实际请求中 `redirect` 会被 URL 编码）：

```text
PUT http://192.168.1.10:8000/upload_file_to_download?redirect=http://192.168.1.30:8080/upload_file?filename=照片.jpg
```

详见 [`docs/SERVER_UPLOAD_PROTOCOL_0.3.2.md`](docs/SERVER_UPLOAD_PROTOCOL_0.3.2.md)。

## 安全提示

- 默认示例使用明文 HTTP，仅适合可信局域网。跨公网使用时应改为 HTTPS/VPN。
- 不要继续使用示例密钥 `123456`。
- `/update/current_latest` 必须携带正确密钥。
- `/upload_file` 接受正确密钥；也可选允许配置服务器的 IP 无密钥调用，以兼容旧宏。
- 内置 HTTP 服务只用于小型 JSON 控制请求，正文上限为 2 MiB；实际文件通过下载 URL 传输。

## 协议映射

详见 [`docs/MACRO_MAPPING.md`](docs/MACRO_MAPPING.md)。

## 当前验证状态

0.3.2 已完成 Kotlin 源码结构检查、上传调用点检查、URL 解析模拟测试、XML 解析、Gradle 配置配对和 Wrapper 完整性检查。本环境没有完整 Android SDK，因此未在这里执行完整 `assembleDebug`，交付包中不会把旧版构建产物冒充为新 APK。请用 Android Studio 打开项目后重新构建。详见 `docs/VALIDATION.md`。
