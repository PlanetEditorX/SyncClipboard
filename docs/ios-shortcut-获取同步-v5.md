# 获取同步 v5 说明（历史版本，有 bug，勿用）

> **已废弃（v1.12.5）**：v5 的目录分支是「外层文件循环 + 内层清单循环」两层
> 嵌套，iOS 在第二层解析魔法变量 `Repeat Item` 时会落到外层，内层取 `url`
> 得到空值，真机报「未指定 URL」。v6 改成服务端合并清单 + 两段并列的单层循环。
> 现行文档见 [`ios-shortcut-获取同步-v6.md`](./ios-shortcut-获取同步-v6.md)。


产物：

| 文件 | 说明 |
|---|---|
| `shortcuts/获取同步v5.shortcut` | 已签名，可直接导入 iPhone（33.8 KB / 126 个动作） |
| `shortcuts/获取同步v5-unsigned.plist` | 未签名源（生成物，别直接导入 iOS） |
| `shortcuts/build_shortcut.py` | 生成器，改逻辑改这里，不要手改 plist |
| `shortcuts/verify_shortcut.py` | 校验器：控制流配对 / 引用完整性 / 打印动作树 |
| `shortcuts/sign_shortcut.py` | `python sign_shortcut.py v5`：HubSign 签名 + 解包回验 |

## 这一版删掉了什么

压缩包这条路**从代码里彻底移除**，不是加开关隐藏：

| 删除项 | 位置 | 原因 |
|---|---|---|
| `zip_folder()` / `FolderArchiveCancelled` | `client/folder_archive.py`（整文件删除） | iOS 的「解压缩」把包内目录摊平，走归档必然丢层级 |
| `/prepare/<file_id>` 打包状态机 | `client/file_server.py` | 只为等打包而存在，没有打包就没有等待 |
| 临时压缩包目录 `temp/folder_share` + 启动清理 | `client/file_server.py` | 不再产生临时包 |
| `_temp_files`、`register_file(temporary=)`、`_release_file` 中的删除逻辑 | `client/file_server.py` | 不再有本进程生成的中间文件，也绝不碰用户文件 |
| `/file/<file_id>` 对目录的整包下载 | `client/file_server.py` | 改为返回 409 + `manifest_url`，指路到清单 |
| GUI 的 `_prepare_folder_share()` 打包轮询 | `gui/file_handler.py` | 换成 `_download_folder()`：按清单逐文件落盘 |
| 快捷指令的打包轮询 / 解压 / 存包兜底 | `shortcuts/build_shortcut.py` | 动作数 189 → 126 |
| 服务端下发的 `prepare_url` | `server/api/flask_app.py` | 只剩 `manifest_url` |

目录名也不再挂 `.zip`：上报的是真实目录名（`课程` 而不是 `课程.zip`）。

## 现在的链路

```
手机                                    电脑（FileServer）
 │  POST /request_file（中心服务）       
 │  ← {type:"folder", files:[{kind:"folder", manifest_url, ...}]}
 │
 │  GET /manifest/<file_id>
 │  ← {root:"课程", count:2, dirs:["子目录/"], empty_dirs:[],
 │     entries:[{path:"讲义.txt", dir:"", url:…},
 │              {path:"子目录/作业.txt", dir:"子目录/", url:…}]}
 │
 │  GET /file/<file_id>/讲义.txt            → 一个文件，共享不撤销
 │  「存储到文件」写进 下载/课程/            
 │  GET /file/<file_id>/子目录/作业.txt
 │  「存储到文件」写进 下载/课程/子目录/     ← 子路径由清单的 dir 提供
 └─  循环 entries
```

### 为什么子路径必然带斜杠

「存储到文件」的 Subpath 参数（plist 里 `WFFileDestinationPath`）**必须以 `/`
结尾**才被当成目录；不带斜杠 iOS 会理解为「用这个字符串改名」。所以服务端
把 `dir` 直接算好（根目录为空串，其余以 `/` 结尾），快捷指令只做拼接：

```
子路径 = 根子路径 + 清单.root + "/" + entry.dir
```

`empty_dirs` 里的空目录没法凭空建出来——「存储到文件」只能靠写文件顺带建路径。
快捷指令会在结尾提示「N 个空目录没有文件可写，未能重建」，其余结构不受影响。

## 配置

导入后编辑第一个「词典」动作（输出名为 `配置`）：

| 键 | 默认值 | 说明 |
|---|---|---|
| 协议 | `http` | |
| 主机 | `192.168.1.24` | 电脑的局域网 IP |
| 端口 | `8000` | 中心服务端口（不是 8899） |
| 密钥 | `123456` | 与电脑端一致 |
| **根子路径** | 空 | 落盘路径前缀，见下 |
| 调试 | `开` | 改成 `关` 即全程静默 |

`根子路径` 留空即「子路径相对所选文件夹（下载）」。若你的 iOS 按存储根解析，
结果会是 `下载/下载/<目录名>/…`，把它填成 `下载/` 即可，不必重新生成快捷指令。

## 接口一览（电脑端 FileServer）

| 接口 | 说明 |
|---|---|
| `GET /manifest/<file_id>` | 目录清单。`file_id` 不是目录共享 → 404 |
| `GET /file/<file_id>/<相对路径>` | 单文件下载。`realpath + commonpath` 限死在共享目录内，`../` 一律 404；**不撤销共享** |
| `GET /file/<file_id>` | 只服务普通文件；目录返回 409 + `manifest_url` |
| `GET /check/<file_id>` | 状态探测（目录返回「待逐文件传输」） |

清单上限 20000 条，超出截断并给 `notice`（快捷指令会弹提示），避免手机上跑
几万次请求。

## 已知限制

| 限制 | 说明 |
|---|---|
| 空目录不重建 | 只能靠写文件顺带建路径，见上文 |
| 文件多时慢 | 每个文件一次 HTTP 请求；几十个无感，上千个小文件明显变慢，且逐条弹调试通知 |
| 单个文件很大 | 「获取URL内容」会把内容整个读进内存（与之前下载整包时相同） |
| 共享不自动撤销 | 没有「传完」这一刻，靠 30 分钟 TTL 或下次复制覆盖 |

## 回归

```bash
cd shortcuts && python build_shortcut.py && python verify_shortcut.py "获取同步v5-unsigned.plist"
.venv/Scripts/python.exe -m unittest discover -s tests     # 315 例
```

| 测试 | 覆盖 |
|---|---|
| `tests/test_folder_manifest.py`（10 例） | 清单形状（`dir` 结尾斜杠 / 根为空串）、空目录单列、截断提示、URL 编码、单文件下载、`../` 穿越被拒、下载后共享仍在 |
| `tests/test_file_push_invalid.py::TestFolderShareManifest`（9 例） | 目录登记不再打包、清单结构、`/file/<id>` 对目录返回 409、单文件下载不撤销共享、读不到体积的文件跳过、过期/清空只清登记不动用户目录 |
| `tests/test_download_resume.py::TestFolderManifestDownload`（6 例） | GUI 按清单重建目录、单个文件失败不中断其余、清单报错/缺地址的提示、`../` 条目被跳过、空目录只建根 |
| `tests/test_smoke_e2e.py` | 真起两个 werkzeug 服务走完整链路；并静态检查产物里没有 `unzip` / `delay` / `repeat.count`，每个「存储到文件」都有输入、目录与以 `/` 收尾的子路径 |
| `tests/test_pull_contract.py::TestFolderPullContract` | `/request_file` 的目录条目只下发 `manifest_url`，不再出现 `prepare_url` |
