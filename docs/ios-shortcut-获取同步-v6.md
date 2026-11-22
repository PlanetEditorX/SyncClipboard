# 获取同步 v6 说明（修掉 v5 的「未指定 URL」）

产物：

| 文件 | 说明 |
|---|---|
| `shortcuts/获取同步v6.shortcut` | 已签名，可直接导入 iPhone（33.3 KB / 131 个动作） |
| `shortcuts/获取同步v6-unsigned.plist` | 未签名源（生成物，别直接导入 iOS） |
| `shortcuts/build_shortcut.py` | 生成器，改逻辑改这里，不要手改 plist |
| `shortcuts/verify_shortcut.py` | 校验器：控制流配对 / 引用完整性 / 打印动作树 |
| `shortcuts/sign_shortcut.py` | `python sign_shortcut.py v6`：HubSign 签名 + 解包回验 |

> v5 的问题见本文「根因」一节。v5 产物与说明保留，但**不要再用**。

## 根因：嵌套两层的「重复每一项」

v5 跑目录同步时停在「获取 URL 内容」：

```
未指定 URL
请确保向"获取 URL 内容"操作传递了有效 URL。
```

v5 的结构是两层循环：

```
重复 文件列表 每一项：          ← 第一层（Repeat Item = 一个文件条目）
    如果 kind 是 folder
        取清单
        重复 清单.entries 每一项：   ← 第二层（Repeat Item 本该是清单条目）
            获取「Repeat Item.url」   ← 实际取到**空**
```

`Repeat Item` 是**魔法变量**（plist 里只有 `{"Type":"Variable","VariableName":"Repeat Item"}`，没有 UUID 绑定），靠位置解析作用域。嵌套到第二层时它解析到了外层那一层，而外层条目只有 `download_url`、没有 `url` —— 取出来是空串，「获取 URL 内容」于是报「未指定 URL」。

这个写法本身没错（参考样本 v1 里 `Repeat Item.download_url` 就是这么用的，v3 单层循环也在真机上跑通过），错在**嵌套**。所以 v6 的做法不是绕开这个变量，而是**让循环永远只有一层**。

## 修法：服务端先把目录摊平

新增 `GET /manifest/batch?ids=d1,d2`：多个目录的清单在服务端合并成一张表，`dir` 补上「根目录名/」前缀。

```json
{
  "status": "ok",
  "roots": ["项目 A", "课程"],
  "count": 5,
  "empty_dirs": ["课程/空目录/"],
  "notice": "",
  "entries": [
    {"path": "readme.txt",   "dir": "项目 A/",       "name": "readme.txt", "url": "…/file/d1/readme.txt"},
    {"path": "子 目录/a.txt", "dir": "项目 A/子 目录/", "name": "a.txt",      "url": "…/file/d1/%E5%AD%90%20%E7%9B%AE%E5%BD%95/a.txt"},
    {"path": "讲义.txt",      "dir": "课程/",         "name": "讲义.txt",   "url": "…/file/d2/…"}
  ]
}
```

`dir` 已经含目录名、末尾带 `/`，接收端拿到就能直接当「存储到文件」的子路径，**不再自己拼字符串**。

`/request_file` 因此多给一条顶层地址（仅在目录都来自同一个文件服务时）：

```json
{"type":"folder", "manifest_url":"…/manifest/d1",
 "manifest_batch_url":"http://192.168.1.24:8899/manifest/batch?ids=d1",
 "manifest_count":1}
```

目录来自两个不同端口时不给这条，改给 `manifest_batch_error`——宁可报错，也不悄悄少传一半。

## 快捷指令里的两段并列循环

```
重复 文件列表 每一项：                     ← 第 1 层
    如果 kind 是 file → 下载 → 存储到文件
结束

如果 有 manifest_batch_url：                ← 与上面并列，不在循环里
    取合并清单
    重复 清单.entries 每一项：              ← 第 1 层（不是第 2 层）
        dir  = Repeat Item.dir
        name = Repeat Item.name
        url  = Repeat Item.url
        如果 url 有任何值 → 下载 → 存储到文件（子路径 = 根子路径 + dir）
        否则 → 通知「跳过（清单条目没有下载地址）」
    结束
结束
```

任何时刻循环深度都是 1。`url` 为空时**跳过并通知**，不再让整条指令崩在「未指定 URL」上——这也是这次真机调试最花时间的地方：崩溃点离根因隔了一整个循环。

## 配置项

只有两个还值得动：

| 键 | 默认 | 说明 |
|---|---|---|
| 主机 / 端口 / 密钥 | `192.168.1.24` / `8000` / `123456` | 中心服务地址 |
| 调试 | 开 | 每个步骤弹一条静音通知；跑不通时看 ② ③ ④ 三条 |

「根子路径」默认留空。若你的 iOS 把它当存储根解析、文件落到 `下载/下载/…`，填 `下载/`。

## 验证

| 项 | 结果 |
|---|---|
| 全量测试 | **326 例 OK**（新增 `BatchManifestTest` 8 例 + 契约 3 例） |
| 真实两服务端到端 | 单目录 / 混合 / 断线重试三条路径全通，落盘路径带 `课程资料/`、`课程资料/子目录/` |
| 真 HTTP 合并清单 | 中文与空格路径一致、`dir` 前缀正确、`../` 穿越 404、`?ids=` 缺失 400、未知 id 忽略 |
| 签名回验 | 131 动作、动作序列与源文件逐项一致 |
| 产物静态检查 | 无 `unzip` / `delay` / `repeat.count`；每个「存储到文件」都有输入 + 目标目录；带子路径的都开了覆盖 |

## 仍然存在的限制

- **空目录**建不出来（没有文件就没有落点），结尾会提示「N 个空目录没有文件可写」。
- 文件多时每个文件一次请求，会慢；清单超 20000 条会截断并提示拆目录。
- 共享不像归档那样「传完即撤」，靠 30 分钟 TTL 或下次复制覆盖。
