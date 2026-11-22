# 获取同步 v10 说明（日志上不来的真正原因：设置变量拿到坏输入）

产物：

| 文件 | 说明 |
|---|---|
| `shortcuts/获取同步v10.shortcut` | 已签名，可直接导入 iPhone（153 动作） |
| `shortcuts/获取同步v10-unsigned.plist` | 未签名源（生成物，别直接导入 iOS） |
| `shortcuts/build_shortcut.py` | 生成器，改逻辑改这里，不要手改 plist |
| `shortcuts/sign_shortcut.py` | `python sign_shortcut.py v10`：HubSign 签名 + 解包回验 |

> **这次不用重新打包电脑端**。v9 的服务端改动（`/ios_log` 收原始正文、
> 失败时打印 body）已经在跑的 1.12.8 里了，v10 只改快捷指令。

## 电脑端日志给出的铁证

`release/dist/SyncClipboard/log/server.log`：

```
[WARNING] iOS 日志请求没有可写内容 | content-type=application/json | body='{"log":""}'
```

请求到了、Content-Type 对了、字段名也对了，**值是空的**。所以既不是 400 校验
太严，也不是网络问题 —— 是手机上这个日志变量本身就是空的。

## 根因：`b.text()` 返回的是 UUID 字符串，不是动作输出引用

```python
# v9（错）
b.setvar('日志', b.text([var('日志'), *parts, '\n']))
```

`b.text()` 返回 `uid`（一个字符串），而「设置变量」要的是**动作输出引用**
（`{OutputName, OutputUUID, Type: 'ActionOutput'}`）。`att()` 把它当成普通值
包了一层，plist 里就成了：

```json
"WFInput": {"Value": "373BB39A-4DA6-…", "WFSerializationType": "WFTextTokenAttachment"}
```

iOS 解不出这个输入，变量被赋成空的，于是每次 `POST /ios_log` 都是 `{"log":""}`
—— 而且**全程没有任何报错**，界面上还显示同步成功。

v9 里其它 `设置变量` 都写了 `act_out(uuid, '文本')`，唯独日志这条漏了。

## v10 的两处加固

1. **修好赋值**：`b.setvar('日志', act_out(b.text([...]), '文本'))`。
2. **日志改成动作输出链**：每行日志都引用**上一条文本动作的输出**拼接，不再
   引用「日志」变量，推送时 body 里用的也是这个动作输出（和 v1 里跑通的
   「JSON body + ActionOutput」完全同形）。少一处依赖实现细节，就少一个
   静默变空的可能。
3. **编译期拦截**：`att()` 和 `setvar()` 收到 UUID 字符串直接 `AssertionError`，
   这类笔误以后在生成快捷指令时就炸，不会再到真机上才发现。

## 日志在哪

```
release/dist/SyncClipboard/log/ios_shortcut.log
```

打包版 `BASE_DIR` = exe 所在目录，所以是这个路径；源码直跑则是项目根下的
`log/`。日志大概长这样：

```
SyncClipboard 同步日志
=== SyncClipboard 同步开始 ===
② 响应 type=file_list
② 原文 {"status":"ok","type":"file_list","files":[…],"folder_count":0}
③ 待下载 2 项
· 文件 （第1节）27数量夸夸刷复盘笔记.pdf
   OK 写入 （第1节）27数量夸夸刷复盘笔记.pdf
⑤ 同步结束：2 个文件
```

## 验证

| 项 | 结果 |
|---|---|
| `设置变量` 输入合法性 | 153 个动作里非法 0 个（脚本逐条校验 `WFInput.Value` 必须是 dict） |
| 日志链 | 16 条 gettext 首段引用逐条串成一条链（10→14→17→24→…），无环无前向引用 |
| 推送 body | `{"log": <文本动作输出>}`，附件类型 `ActionOutput` |
| 控制流 / 引用 / 可疑参数 | 三项校验全通过 |
| 全量测试 | 334 例 OK |
| 签名 | HubSign 200，AEA1+bvx2 正常，动作序列与源文件逐项一致 |

## 仍然存在的限制

- **空目录**建不出来（没有文件就没有落点），结尾会提示数量。
- 文件多时「每个文件一次下载 + 一次日志推送」，会慢。
- 多台电脑各登记了目录时无法合并清单，会明确提示「请分开同步」。
