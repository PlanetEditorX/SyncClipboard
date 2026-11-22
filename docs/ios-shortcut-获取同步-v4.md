# 获取同步 v4 说明（历史版本，已被 v5 取代）

> **已废弃（v1.12.4）**：v5 起压缩包分支从代码里彻底移除，「目录传输」配置键与
> 打包轮询都已删除。本文保留下来只为解释「为什么必须先放弃打包」这一段来龙去脉，
> 现行文档见 [`ios-shortcut-获取同步-v5.md`](./ios-shortcut-获取同步-v5.md)。


产物：

| 文件 | 说明 |
|---|---|
| `shortcuts/获取同步v4.shortcut` | 已签名，可直接导入 iPhone（38.7 KB / 189 个动作） |
| `shortcuts/获取同步v4-unsigned.plist` | 未签名源（生成物，别直接导入 iOS） |
| `shortcuts/build_shortcut.py` | 生成器，改逻辑改这里，不要手改 plist |
| `shortcuts/verify_shortcut.py` | 校验器：控制流配对 / 引用完整性 / 打印动作树 |
| `shortcuts/sign_shortcut.py` | 调用 HubSign 签名并解包回验，`python sign_shortcut.py v4` |

## v3 的问题：目录结构没了

v3 的目录走「等电脑打包 → 下载 zip → 解压缩 → 存进下载」。真机结果是：
**所有文件都平铺躺在「下载」根目录，包内层级全丢**。

根因不在服务端也不在压缩包：`zip_folder()` 打包时保留了同名根目录、
空目录也写进了包。问题出在 iOS 的「解压缩」动作——它把归档还原成一串
**扁平的文件对象**，包内目录层级在解压那一步就被丢掉了。只要还经过压缩包
这一层，包内多规整都没用。

## v4 的做法：不打包，按清单逐个取

```
手机                     电脑（FileServer）
 │  GET /manifest/<id>  →  扫描目录，返回相对路径清单 + 每项下载地址
 │  ←  {root, count, entries:[{path, dir, name, size, url}]}
 │
 │  GET /file/<id>/子目录/a.txt  →  只发这一个文件（不撤销共享）
 │  「存储到文件」写进「下载/<目录名>/子目录/」
 └─  循环 entries
```

关键在最后一行的**子路径**：`is.workflow.actions.documentpicker.save`
有个 Subpath 参数（plist 里是 `WFFileDestinationPath`），**必须以 `/` 结尾**
才被当成目录，否则 iOS 会拿它去改文件名。`dir` 由服务端直接算好（根目录下
是空串，其余以 `/` 结尾），快捷指令只做拼接，不在 iOS 上玩字符串处理。

### 两端新增的接口

| 接口 | 返回 |
|---|---|
| `GET /manifest/<file_id>` | 204/404 之外都是清单：`root`（目录名）、`count`、`total_size`、`dirs`、`empty_dirs`、`truncated`、`notice`、`entries[]`；每项含 `path` / `dir` / `name` / `size` / `url` |
| `GET /file/<file_id>/<path>` | 单文件下载。**不**在发完后撤销共享——一份目录要连着取很多次 |

`/request_file` 里目录条目现在同时给出 `prepare_url`（打包）和
`manifest_url`（清单），接收端自己选路。

实现位置：

| 文件 | 改动 |
|---|---|
| `client/folder_manifest.py` | 新增。扫描目录生成清单（跳过符号链接、读不到体积的跳过、超 20000 条截断并置 `notice`） |
| `client/file_server.py` | 新增 `/manifest/<id>`、`/file/<id>/<path>`；后者用 `realpath + commonpath` 把路径限死在共享目录内 |
| `server/api/flask_app.py` | `/request_file` 的目录条目补 `manifest_url` |
| `shortcuts/build_shortcut.py` | 目录分支按「目录传输」二分：`逐文件`（默认）/ `压缩包`（v3 老路） |

## 配置

导入后编辑第一个「词典」动作（输出名为 `配置`）：

| 键 | 默认值 | 说明 |
|---|---|---|
| 协议 | `http` | |
| 主机 | `192.168.1.24` | 电脑的局域网 IP |
| 端口 | `8000` | 中心服务端口（不是 8899） |
| 密钥 | `123456` | 与电脑端一致 |
| **目录传输** | `逐文件` | 想回 v3 老路（压缩包）就改成 `压缩包` |
| **根子路径** | 空 | 落盘路径前缀，见下 |
| 调试 | `开` | 改成 `关` 即全程静默 |

### 「根子路径」什么时候要改

子路径是相对**「存储到文件」里选中的文件夹**（即「下载」）解析的，正常留空
即可，文件落在 `下载/<目录名>/<子目录>/`。

万一你的 iOS 把它按「存储根」解析，结果会变成 `下载/下载/<目录名>/…`——
这时把「根子路径」填成 `下载/` 就好，不用重新生成快捷指令。

## 已知限制

| 限制 | 说明 |
|---|---|
| 空目录不会重建 | 「存储到文件」只能靠写文件顺带建出路径，没有文件就没有落点。快捷指令会在结尾提示「N 个空目录没有文件可写」，其余结构不受影响 |
| 文件多时慢 | 每个文件一次 HTTP 请求。几十个文件无感；上千个小文件会明显变慢，且逐条弹调试通知。超过 20000 条清单会被截断并提示改用压缩包 |
| 单个文件很大 | 「获取URL内容」会把内容整个读进内存，与 v3 下载 zip 时的行为一致 |
| 共享不会自动撤销 | 逐文件模式没有「传完」这一刻，共享靠到期清理（默认 30 分钟）或下次复制覆盖 |

## 回归

```bash
cd shortcuts && python build_shortcut.py && python verify_shortcut.py "获取同步v4-unsigned.plist"
.venv/Scripts/python.exe -m unittest discover -s tests     # 320 例
```

本仓库内新增：

| 测试 | 覆盖 |
|---|---|
| `tests/test_folder_manifest.py`（10 例） | 清单形状（`dir` 结尾斜杠/根为空串）、空目录单列、截断提示、URL 编码、单文件下载、`../` 穿越被拒、下载后共享仍在 |
| `tests/test_pull_contract.py::TestFolderPullContract`（2 例） | `/request_file` 的目录条目同时给出 `prepare_url` 与 `manifest_url`；混合列表不会污染顶层字段 |

另外用真实 HTTP 栈跑过一次端到端（含空格与中文路径）：清单 3 条全部取下、
内容一致、`/file/d1/../etc/passwd` 返回 404、共享仍存活。
