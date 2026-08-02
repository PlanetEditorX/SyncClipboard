# 验证说明

本次 0.3.2 修改已执行：

- 使用 Kotlin 1.9 编译器和 Android/JSON 最小 API 桩对 `ProtocolClient.kt` 做编译级检查；
- 检查所有上传调用点均传递服务器返回的目标 URL 和目标客户端名称；
- 确认文件上传连接使用 `PUT`，不再使用 `POST multipart/form-data`；
- 确认 `key`、`source`、`target` 通过请求头发送；
- 模拟普通上传 URL 与 Android 中转嵌套 `redirect` URL 的构造和 Flask `parse_qs()` 解析；
- 使用包含中文、空格和 `#` 的文件名验证 URL 往返解析；
- 解析 AndroidManifest.xml 与全部资源 XML；
- 检查 AGP 8.6.1 / Gradle 8.7 / compileSdk 35 配置；
- 检查 Gradle Wrapper JAR ZIP 完整性；
- 检查发布 ZIP 完整性与 SHA-256。

当前执行环境无法联网下载 Gradle 8.7，且没有完整 Android SDK，因此未执行完整 `assembleDebug`。请在 Android Studio 中执行：

```bash
./gradlew assembleDebug
```

输出应位于：

```text
app/build/outputs/apk/debug/app-debug.apk
```
