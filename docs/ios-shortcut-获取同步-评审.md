# iOS 快捷指令「获取同步」评审与修复

> 分析对象：`获取同步.shortcut`（iCloud 导出，`AEA1` 封装 + LZFSE 压缩的 `Shortcut.wflow`）
> 解包链路：`AEA1` 头 → `bvx2`(LZFSE) → `AA01` AppleArchive → `Shortcut.wflow`(bplist) → `WFWorkflowActions`
> 原版 82 个动作；修复版 80 个动作，已签名。

## 0. 修订说明（重要）

上一版评审把 `files = URL的内容.files` 判定为 P0 —— 理由是「`URL的内容` 是魔术变量，会被循环里的
`/prepare` 请求覆盖」。**这个结论是错的，现予撤回。**

解开的 plist 显示，每个变量引用都带 `OutputUUID`，是**精确绑定到某个具体动作**的，
界面上显示成「URL的内容」只是标签：

```
[52] files = {Type: ActionOutput, OutputUUID: E18606E7-512D-4636-B162-DCAF3AFA5B0B, .files}
                                    └─ 这个 UUID = 动作 16，即 POST /request_file
```

[20] `类型`、[23] `prepare_url`、[52] `files`、[73] `内容Cache` 全都绑定在动作 16 上，
不受循环影响。**原版的这条链路本身是对的。**

## 1. 原版的实际问题

| 类别 | 动作 | 问题 | 后果 |
|---|---|---|---|
| 必坏 | [4] / [71] | 词典「清理文件」的值里混进了 Python 源码<br>`/latest/clear\r\n    handler = RotatingFileHandler(\r\n LOG_FILE, maxBytes=1*1024*1024...` | 拼出含换行/空格的非法 URL，[71] 请求报错中止 |
| 必坏 | [27] | 「显示结果」放在 200 次循环体内 | 打包期间每轮弹一张卡片，最多 200 张 |
| 干扰 | [17][22][24][51][75] | 调试残留的「显示结果」 | 每次运行都弹，[17] 弹整个 JSON |
| 缺口 | [65] | 目录下载到的是 zip，没有任何解压动作 | 落盘是 `xxx.zip` 而非文件夹 |
| 依赖 | [9] | `com.sindresorhus.Actions.IsHostReachable` | 手机上没装 Actions 会报「找不到 App Intent」 |
| 配置 | — | 200×5s = 最长 16.7 分钟，但共享存活默认 10 分钟 | 超时后 `/prepare` 返回 404，快捷指令直接报错 |

顺带确认几处**本来就没问题**的地方，不要再改：

- `[34]` `压缩状态 不是 ready → 等待 5 秒`、`[39]` `不是 ready → 打包超时` —— 方向正确。
  plist 里 `WFCondition 5` = 不是、`4` = 是、`2` = 大于，`[10]` 的 `IsHostReachable 不是 "是" → 服务器不在线` 同理正确。
- `[30]` 的多条件是 `WFActionParameterFilterPrefix: 0`（任一）+ 两个 `是` 判断 = `error 或 unknown`，正确。
- `[64]` `URL = Repeat Item.download_url`、header `source` = 编码后的设备名，正确。
- `[52]` `files` 的取值来源正确（见第 0 节）。

## 2. 已执行的修改

### 删除（7 个动作）

| 原索引 | 动作 | 原因 |
|---|---|---|
| 17 | 显示结果 `URL的内容` | 调试残留，每次运行弹整个 JSON |
| 22 | 显示结果 `类型` | 调试残留 |
| 24 | 显示结果 `prepare_url` | 调试残留 |
| 27 | 显示结果 `URL的内容` | **在 200 次循环体内** |
| 51 | 显示结果 `如果的结果` | If 的布尔输出，无意义 |
| 71 | 获取 URL 内容 `词典.清理文件` | 值非法；且 `/request_file` 服务端已 `clear()`，多余 |
| 75 | 显示结果 `文本` | [76] 已复制到剪贴板 |

### 修改（1 处）

- `[4]` 词典删掉「清理文件」项，只保留「获取文件」= `{URL}/request_file`。

### 新增（5 个动作，在下载后分叉）

```text
[59] 获取 URL 内容 {Repeat Item.download_url}
[60] 如果 是目录 是 true
  [61] 解压缩                              ← 输入隐式取 [59] 的 zip
  [62] 存储到文件 → 下载
[63] 否则
  [64] 存储到文件 → 下载                   ← 原 [65]，整体保留
[65] 结束 如果
```

`是目录` 这个变量原本就在 `[15]` 设 `false`、`[45]` 设 `true`，但全程没人读 —— 现在它终于用上了。

## 3. 交付与导入

文件：`shortcuts/获取同步.shortcut`（28.6 KB，已签名）

**签名方式**：iOS 15+ 拒绝导入未签名文件（「允许不受信任的快捷指令」在 iOS 15 已被移除），
Windows 上无法本地签名（`shortcut-sign` 需要从越狱设备 dump 的 Apple ID 私钥）。
本次通过 RoutineHub 社区的 HubSign 服务签名 —— 快捷指令完整内容被 POST 到
`hubs.routinehub.services`，其中包含内网 IP `192.168.1.24` 和密钥 `123456`。

导入：传到 iPhone（AirDrop / 文件 App / iMessage）→ 点开 → 「添加快捷指令」。

导入后建议确认三件事：

1. **「配置」词典**：IP / 端口 / 密钥沿用原值，换网络环境要改。
2. **「存储到文件」的目标目录**：`WFFolder` 里存的是这台设备的
   `deviceSpecific:2AECE2CD-...fid=1130423`，指向「下载」。换设备或换账号时可能失效，
   在编辑器里重新选一次目录即可。
3. **共享存活**：客户端面板把 `file_share_ttl_minutes` 从 10 调到 **30 分钟**以上，
   否则大目录压到一半 job 会被清理，`/prepare` 返回 404。

保留的第三方依赖：`[9]` 主机可达性检查用的是 Actions 的 App Intent。已装的不用动；
没装的话把 `[9]`–`[13]` 整段删掉也能跑，只是少一句「服务器不在线」的提示。

## 附录：修复版动作树（80 个动作）

条件码已翻译成「是 / 不是 / 大于」。

```text
[ 0] 词典 → 「配置」
  ·WFItems = {主机: 192.168.1.24, 密钥: 123456, 端口: 8000, 协议: http}
[ 1] 注释
  ·WFCommentActionText = "文件默认保存至下载目录"
[ 2] 文本
  ·WFTextActionText = {配置.协议}://{配置.主机}:{配置.端口}
[ 3] 设定变量
  ·WFInput = {文本}
  ·WFVariableName = "URL"
[ 4] 词典
  ·WFItems = {获取文件: {URL}/request_file}
[ 5] 获取设备详细信息
[ 6] URL 编码
  ·WFInput = {设备名称}
[ 7] 注释
  ·WFCommentActionText = "获取当前app，如果没有值说明在桌面，停止运行"
[ 8] 获取当前 App
[ 9] 主机是否可达（Actions）
  ·host = {配置.主机}
  ·port = {配置.端口}
[10] 如果 {Type="Variable", Variable={Is Host Reachable}} 不是 '是'
  ·WFInput = {Type="Variable", Variable={Is Host Reachable}}
  ·WFConditionalActionString = "是"
  ·WFCondition = 5
  [11] 显示结果
    ·Text = "服务器不在线"
  [12] 停止此快捷指令
── 结束 如果
[14] 文本
  ·WFTextActionText = "false"
[15] 设定变量
  ·WFInput = {文本}
  ·WFVariableName = "是目录"
[16] 获取 URL 内容
  ·WFHTTPHeaders = {key: {配置.密钥}}
  ·WFJSONValues = {type: file, source: {设备名称}}
  ·WFURL = {词典.获取文件}
  ·WFHTTPMethod = "POST"
[17] 获取项目类型
  ·WFInput = {URL的内容}
[18] 如果 {Type="Variable", Variable={类型}} 是 '字典'
  ·WFInput = {Type="Variable", Variable={类型}}
  ·WFConditionalActionString = "字典"
  ·WFCondition = 4
  [19] 设定变量
    ·WFInput = {URL的内容.type}
    ·WFVariableName = "类型"
  [20] 如果 {Type="Variable", Variable={类型}} 是 'folder'
    ·WFInput = {Type="Variable", Variable={类型}}
    ·WFConditionalActionString = "folder"
    ·WFCondition = 4
    [21] 设定变量
      ·WFInput = {URL的内容.prepare_url}
      ·WFVariableName = "prepare_url"
    [22] 重复 200 次
      ·WFRepeatCount = 200.0
      [23] 获取 URL 内容
        ·WFURL = {prepare_url}
      [24] 设定变量
        ·WFInput = {URL的内容}
        ·WFVariableName = "当前压缩"
      [25] 设定变量
        ·WFInput = {当前压缩.status}
        ·WFVariableName = "压缩状态"
      [26] 如果 (任一 {Type="Variable", Variable={压缩状态}} 是 'error' ; {Type="Variable", Variable={压缩状态}} 是 'unknown')
        ·WFConditions = (任一 {Type="Variable", Variable={压缩状态}} 是 'error' ; {Type="Variable", Variable={压缩状态}} 是 'unknown')
        [27] 显示结果
          ·Text = "打包失败/共享已失效"
        [28] 停止此快捷指令
      ── 否则
        [30] 如果 {Type="Variable", Variable={压缩状态}} 不是 'ready'
          ·WFInput = {Type="Variable", Variable={压缩状态}}
          ·WFConditionalActionString = "ready"
          ·WFCondition = 5
          [31] 等待
            ·WFDelayTime = 5.0
        ── 结束 如果
      ── 结束 如果
    ── 结束 重复
    [35] 如果 {Type="Variable", Variable={压缩状态}} 不是 'ready'
      ·WFInput = {Type="Variable", Variable={压缩状态}}
      ·WFConditionalActionString = "ready"
      ·WFCondition = 5
      [36] 显示结果
        ·Text = "打包超时"
      [37] 停止此快捷指令
    ── 否则
    ── 结束 如果
    [40] 文本
      ·WFTextActionText = "true"
    [41] 设定变量
      ·WFInput = {文本}
      ·WFVariableName = "是目录"
    [42] 文本
      ·WFTextActionText = "file_list"
    [43] 设定变量
      ·WFInput = {文本}
      ·WFVariableName = "类型"
  ── 否则
  ── 结束 如果
  [46] 如果 {Type="Variable", Variable={类型}} 是 'file_list'
    ·WFInput = {Type="Variable", Variable={类型}}
    ·WFConditionalActionString = "file_list"
    ·WFCondition = 4
    [47] 设定变量
      ·WFInput = {URL的内容.files}
      ·WFVariableName = "files"
    [48] 计数
      ·Input = {files}
    [49] 设定变量
      ·WFInput = {计数}
      ·WFVariableName = "文件数量"
    [50] 如果 {Type="Variable", Variable={文件数量}} 大于 None
      ·WFNumberValue = "1"
      ·WFInput = {Type="Variable", Variable={文件数量}}
      ·WFCondition = 2
      [51] 显示通知
        ·WFInput = {文件数量}
        ·WFNotificationActionBody = 共{文件数量}个文件
        ·WFNotificationActionSound = false
    ── 结束 如果
    [53] 重复（逐个）
      ·WFInput = {files}
      [54] 如果 {Type="Variable", Variable={文件数量}} 大于 None
        ·WFNumberValue = "1"
        ·WFInput = {Type="Variable", Variable={文件数量}}
        ·WFCondition = 2
        [55] 显示通知
          ·WFInput = {Repeat Item}
          ·WFNotificationActionBody = 正在下载第{Repeat Index}个文件
          ·WFNotificationActionSound = false
      ── 否则
        [57] 显示通知
          ·WFInput = {Repeat Item}
          ·WFNotificationActionBody = 正在下载{Repeat Item.name}
      ── 结束 如果
      [59] 获取 URL 内容
        ·WFHTTPHeaders = {source: {URL编码后的文本}}
        ·WFURL = {Repeat Item.download_url}
      [60] 如果 {Type="Variable", Variable={是目录}} 是 'true'
        ·WFInput = {Type="Variable", Variable={是目录}}
        ·WFConditionalActionString = "true"
        ·WFCondition = 4
        [61] 解压缩
        [62] 存储到文件
          ·WFFolder = {fileLocation={relativeSubpath="下载", fileProviderDomainID="com.apple.FileProvider.LocalStorage", crossDeviceItemID="deviceSpecific:2AECE2...
          ·WFInput = {Files}
      ── 否则
        [64] 存储到文件
          ·WFFolder = {fileLocation={relativeSubpath="下载", fileProviderDomainID="com.apple.FileProvider.LocalStorage", crossDeviceItemID="deviceSpecific:2AECE2...
          ·WFInput = {URL的内容}
      ── 结束 如果
    ── 结束 重复（逐个）
    [67] 如果 {Type="Variable", Variable={文件数量}} 大于 None
      ·WFNumberValue = "1"
      ·WFInput = {Type="Variable", Variable={文件数量}}
      ·WFCondition = 2
      [68] 显示通知
        ·WFInput = {重复结果}
        ·WFNotificationActionBody = "文件下载完成"
        ·WFNotificationActionSound = false
      [69] 文本
    ── 结束 如果
  ── 否则
    [72] 设定变量
      ·WFInput = {URL的内容.latest_global}
      ·WFVariableName = "内容Cache"
    [73] 文本
      ·WFTextActionText = {内容Cache.content}
    [74] 拷贝到剪贴板
      ·WFInput = {文本}
  ── 结束 如果
── 否则
  [77] 预览文档
    ·WFInput = {URL的内容}
── 结束 如果
[79] 停止此快捷指令
```
