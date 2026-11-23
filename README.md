# SyncClipboard

## 简介

SyncClipboard 是一个跨设备实时同步剪贴板内容的工具，支持**文本**、**图片**、**文件**等多种数据类型。该项目旨在解决 Windows 电脑与 iOS 18 手机在同一局域网内的剪贴板共享需求，通过高效的实时同步机制为用户提供无缝的跨设备体验。

- **文本同步**：任意一端复制，其他设备自动更新剪贴板（实时）。
- **文件/图片传输**：手机可按需从电脑拉取文件，也可主动发送文件到电脑保存。

---

## 功能说明

| 功能 | 触发方式 | 行为 | 状态 |
|------|---------|------|------|
| 1. 文本全端实时同步 | 任意客户端复制文本 | 服务器中转，其他客户端自动拉取并写入剪贴板 |
| 2. 服务端本机文件 → 手机 | 手机手动点击拉取指令 | 服务器直接返回文件流，下载后自动清除最新文件记录 |
| 3. 手机文件上传至服务器 | 手机手动发送文件 | 服务器保存到预设下载目录 |
| 4. 其他电脑文件 → 手机 | 手机手动拉取 | 服务器返回源电脑的文件地址，手机直接下载；服务器发送一次文件后自动清除记录 |
| 5. 电脑A文件 → 电脑B | 点击图标后手动拉取 | 同手机拉取 |

**注意**：多台 Windows 电脑间**不进行文件剪贴板自动同步**。原因：若任意一端复制文件就要上传至服务器并分发给所有电脑，频繁复制时会产生大量不必要的磁盘读写和网络开销，而实际可能根本不会使用这些文件。文本同步已覆盖日常需求，文件传输仅设计为主动按需获取，手机点击快捷指令后手动拉取文件，电脑其它客户端通过点击弹窗或点击任务栏图标左键后手动拉取文件。文件请求一次后就会失效，文件共享会被取消。

---

## 使用

### 电脑端

1. 启动**服务器**（SyncClipboard Server）和**客户端**（SyncClipboard Client）。
2. 复制任意文字，其他电脑客户端和手机都会在几秒内自动同步。
3. 复制文件后（在任意电脑上）：
   - 服务器本机文件：手机直接拉取即可下载。
   - 其他电脑文件：手机拉取时会自动跳转到源电脑下载。
4. 手机发送的文件默认保存到客户端配置的 `save_path` 目录下。
5. 点击任务栏图标左键，会自动同步下载其它客户端上传的文件，没有文件就同步最新的文字内容。
6. 排查问题时，主界面「运行日志」区可分别查看**服务日志**、**客户端日志**和**界面日志**（文件在 `log/` 目录）；打开后每 2 秒自动刷新，窗口关闭即停止读取，不在后台占用资源。

### 手机端（iOS 18）

- **文字同步**：打开设置了自动化的 App（如微信、浏览器）时，快捷指令自动运行，拉取最新文本并写入剪贴板；手机复制文字也会自动推送到电脑。
- **获取文件**：手动点击控制中心的「获取文件」快捷指令，从电脑下载最近复制的文件/图片。
- **发送文件**：手动点击「发送文件」快捷指令，将手机上的文件/图片发送到电脑下载目录。

---

## 部署

### 系统要求

- **操作系统**：Windows 10+，iOS 18。
- **网络**：所有设备处于同一局域网。

### Docker 部署示例

可以使用 Docker 或 Docker Compose 启动服务端，并挂载本地配置、日志和 latest 数据目录：

```yaml
services:
  syncclipboard-server:
    image: yexundao/syncclipboard-server:latest
    container_name: syncclipboard-server
    restart: always
    ports:
      - "8000:8000"
    volumes:
      - ./config:/app/config
      - ./log:/app/log
      - ./latest:/app/latest
      - ./downloads:/app/downloads
    environment:
      - PORT=8000
      - TZ=Asia/Shanghai
```

`./config`、`./log`、`./latest` 分别保存配置、日志和共享状态；**`./downloads` 必须挂**：手机上传的文件默认落在容器的 `/app/downloads`（`SAVE_PATH`），不挂载则容器重建后文件全丢。镜像内已装 `tzdata`，`TZ` 才会真正生效；`PORT` 会同时写回 `app.config['port']`，保证 `/clients/online` 下发的中转地址端口与实际监听一致。换宿主端口时记得把 `PORT` 一起改。镜像带 `/status` 健康检查。

构建镜像时上下文是仓库根目录：

```bash
docker build -f docker/Dockerfile -t syncclipboard-server .
```

根目录的 `.dockerignore` 会排除 `.git`、`.venv`、运行期状态文件和 `config/*.json`（避免把本机 IP 和密钥打进镜像）。

### 统一鉴权契约

**所有端、所有 HTTP 请求一律在请求头带 `key`**，与 `server_config.json` 的 `key` 一致：

- 中心服务端：`require_key()` 是全站唯一入口，16 个路由共用；
- 电脑端 FileServer：除 `/ping` 探活外，全部路由（含 `/file`、`/manifest`、`/files`、`/check`）都校验；
- iOS 快捷指令：`/request_file`、每个下载地址、每份目录清单都带 `key`；
- Android：出站请求与入站回调都带 `key`，不再靠「来源 IP 等于服务器」免密放行。

JSON body 里的 `key` 与 `?key=` 仍被同一个校验函数接受，但只是等价入口，新接入方请只用请求头。升级后请把**所有端一起升级**：只升级服务端而留着旧快捷指令/旧 APK，下载地址会拿到 403。

本次契约更新重新生成了 4 份 `shortcuts/*/*-unsigned.plist`。仓库里的 `.shortcut` 是 Apple 签名产物，本机无法重签，需要把新的 unsigned plist 重新签名后导入 iOS（旧 `.shortcut` 已过期，导入后下载会 403）。

### 电脑配置

1. 从 [Releases](https://github.com/PlanetEditorX/SyncClipboard/releases) 下载 `SyncClipboard.zip`。
2. 解压后编辑 `server_config.json`：

   ```json
   {
       "key": "123456",
       "port": 8000,
       "local_name": "Server"
   }
   ```

3. 编辑 `client_config.json`，保证 `key`、`server_host`、`server_port` 与服务器一致，`file_server_port` 可自定义（默认 8899）：

   ```json
   {
       "server_host": "127.0.0.1",
       "server_port": 8000,
       "key": "123456",
       "local_name": "PC-01",
       "file_server_port": 8899
   }
   ```

4. 根据需求设置开机启动、启动服务器、启动客户端。

**多 Windows + 单 iOS 文字同步**：每台电脑使用不同的 `local_name` 即可，任意一端复制文字，其他端自动同步。

### 同步控制（面板）

主界面「同步控制」卡片可以精细控制两端行为，保存后客户端（必要时连同服务器）会自动重启生效：

| 选项 | 说明 | 配置字段 |
| --- | --- | --- |
| 文字同步 | 双向 / 只发送 / 只接收。只接收时本机不会再上传剪贴板，只发送时不会写回远端文字 | `text_sync_mode` |
| 文件同步 | 双向 / 只发送 / 只接收。只发送时不再提示远端发布的文件，只接收时不再共享本地文件 | `file_sync_mode` |
| 自动打包同步复制的文件夹 | 关闭后复制文件夹不会被传输 | `folder_sync_enabled` |
| 目录大小上限 | 默认 10 GB。小于上限的目录复制后直接登记共享；超过上限会弹窗确认，确认后才登记。配置键名沿用历史命名 `folder_zip_confirm_gb`（目录已不打包，改名会破坏老配置） | `folder_zip_confirm_gb` |
| 共享存活时间 | 默认 30 分钟。到期后自动取消共享并对接收端 404，服务器也会丢弃对应记录。从电脑上复制那一刻起算，目录逐文件下载也在这个窗口内完成 | `file_share_ttl_minutes` |

示例 `client_config.json`（旧版本缺少这些字段时会自动补默认值）：

```json
{
    "server_host": "127.0.0.1",
    "server_port": 8000,
    "key": "123456",
    "local_name": "PC-01",
    "file_server_port": 8899,
    "save_path": "D:/Downloads",
    "text_sync_mode": "both",
    "file_sync_mode": "both",
    "folder_sync_enabled": true,
    "folder_zip_confirm_gb": 10,
    "file_share_ttl_minutes": 30
}
```

`file_share_ttl_minutes` 会同时写入 `server_config.json`，Docker/Linux 部署可只改服务器配置里的这一项。

### 手机配置

#### iOS端

1. **安装 Actions**（用于检测主机可达性，避免快捷指令因网络异常报错）
   - 如果未安装过，可能需要借助 [ipatool](https://github.com/majd/ipatool) 购买历史版本。
   - 若实在无法安装，可手动删除快捷指令中涉及 Actions 的部分。

2. **添加快捷指令**（4 条，仓库 `shortcuts/` 下各有 `build.py` 与产物）
   - 文字同步：常驻轮询，双向同步剪贴板文字
   - 推送同步：把剪贴板内容（文字 / 文件）推给电脑
   - 获取同步：拉取文字、文件、目录（按合并清单逐文件落地）
   - 短信同步：从验证码短信里抽数字推到电脑剪贴板
   - 仓库里的 `.shortcut` 已按本次契约**重新签名**，可直接导入；`*-unsigned.plist` 是对应的源文件（改参数后用 `shortcuts/lib/sign.py` 重签）。所有请求一律带 `key` 请求头，旧版 `.shortcut` 导入后下载会 403，务必整组替换。
   - 每个 GitHub Release 也附带 `SyncClipboard-iOS-Shortcuts-<tag>.zip`，含 4 条签名产物；导入后在每条指令的「配置」词典里改主机、端口、密钥。
3. **修改快捷指令参数**
   - 主机地址、密钥、端口，与服务器配置保持一致。
   - 停止自动更新的电量阈值（如 40%），低于该电量不执行同步。
   - 循环时间策略：
     - 时间1：打开 App 前 1 分钟内的等待间隔
     - 时间2：1～3 分钟内的等待间隔
     - 时间3：3 分钟后的等待间隔
     （刚打开 App 时高频同步，后续逐步降低频率以省电）

4. **创建自动化**
   - 类型：App → 打开时运行快捷指令
   - 设置为“立即运行”，关闭“运行时通知”
   - 选择常用 App（如微信、浏览器等），运行「文字同步」快捷指令

5. **无需手动注册**
   - 服务端对任何带正确 `key`、能报出设备名的请求自动登记 / 续期；一周无活跃证据才清理。旧文档里的「注册服务」快捷指令已不存在。

6. **控制中心**
   - 添加「推送同步」和「获取同步」快捷指令，方便手动触发文件传输。

7. **关闭多余通知**
   - 进入 设置 → 屏幕使用时间 → 查看所有 App 与网站活动 → 通知
   - 将快捷指令的通知改为仅横幅，关闭其他提醒。

#### Android端
1. **安装SyncClipboard-Android**
 - 配置：主机、密钥、本机监听端口、设备名称、下载路径
 - 开启无障碍权限、悬浮窗权限

2. **安卓互联效果和限制**
   - 安卓、Windows和iOS客户端复制文字后，通过服务器正常同步文字到其它所有客户端。
   - Windows复制文件后，其它Windows客户端和安卓客户端上都会显示文件发布的通知，Windows客户端可直接点击通知进行下载，通知消失后也能通过点击图标进行下载，任意一端下载后取消文件共享。安卓点击通知进行下载，下载到之前指定的目录下，如果没有点击通知，60秒后通知将会被自动清理。
   - **目录不打包**：复制文件夹时只统计体积并登记共享（`文件夹名`、原始体积），接收端来取时按清单逐文件下载。不产生任何临时压缩包，因此本地复制大目录但不需要传给别的设备时零额外开销。
   - 对方来取目录时先取清单（`root` / `entries[{path, dir, name, size, url}]`），再按清单逐项 `GET /file/<file_id>/<相对路径>`，本地按相对路径重建目录结构。**不走归档格式** —— iOS 的「解压缩」会把包内目录摊平，那是 v1.12.3 以前所有文件堆在同一个目录的根因。
   - 手机上（iOS 快捷指令）只取**合并清单** `GET /manifest/batch?ids=d1,d2`：把多个目录的文件摊平成一张表，`dir` 已含「目录名/」前缀。因为「重复每一项」嵌两层时内层取不到值（v1.12.4 的真机问题），接收端全程只能跑一层循环。
   - Android 客户端直接对接新协议：识别 `kind=folder`，取 `manifest_batch_url`（多来源时逐目录取 `manifest_url`），按 `prefix` 逐文件下载并重建子目录；下载失败的文件写回待办队列，下一轮轮询自动重试，不会因 `/request_file` 一次性消费而永久丢失。入站回调只认请求头 `key`，已移除「服务器 IP 免密上传」开关。
   - 单文件下载失败只跳过该文件，其余继续；整份共享在接收端取完、到期或重新复制时才撤销。Windows 端下载中断会自动重试 3 次，续传用 HTTP Range 从断点接着来。
   - 下载不再询问保存位置：直接存到面板配置的下载目录（重名自动加 `(1)`），完成后资源管理器自动定位到文件；手机传到电脑的文件，点击通知同样会打开所在文件夹。

---

## 其他

### 运行失败排查

- 手动执行一次快捷指令，观察是否正常。
- 查看电脑客户端日志（`log/client.log`）或手机快捷指令运行日志。
- 快捷指令自动停止的可能原因：
  - 非手动运行（自动化未触发）
  - 电量低于设置阈值
  - 不在设置的 App 界面（如停留在桌面）
  - 服务器不可达（Actions 检测失败）

### 更新版本

在仓库中打标签并推送：

```bash
git tag -a v1.0.0 -m "Release 1.0.0"
git push origin v1.0.0
```

---

## 数据流简图

```
[电脑A]       --复制文本--> 服务器 <--轮询--> [电脑B]  (文本实时同步)
[手机]        --复制文本--> 服务器 --> 电脑自动拉取
[电脑A]       --复制文件--> 服务器 (记录元数据)
[手机/电脑B]  --请求文件--> 服务器
                ├── 文件在本地? 直接返回文件流
                └── 文件在远端? 返回跳转 --> [从源电脑A下载]
                    (任意客户端下载一次后共享取消)
[手机]        --上传文件--> 服务器保存到本地目录
```

## 项目结构

```
SyncClipboard/
├── client/
│   ├── autorun.py              # 开机自启动
│   ├── file_server.py          # 客户端路由
│   ├── main_menu.py            # 客户端主逻辑
│   ├── run.py                  # 客户端主入口
│   └── settings.py             # 客户端配置操作
├── common/
│   ├── file_watcher.py         # 文件监控
│   ├── notification.py         # 通知
│   └── utils.py                # 工具类
├── config/
│   ├── client_config.json      # 客户端配置
│   ├── client_ip.json          # 注册客户端IP信息
│   ├── gui_state.json          # 托盘状态
│   ├── server_config.json      # 服务器配置
│   └── example/                # 配置模板文件
├── gui/
│   ├── run.py                    # 托盘主入口
│   ├── tray_manager.py           # 托盘管理器
│   ├── tray_menu.py              # 菜单构建
│   ├── config_manager.py         # 配置管理
│   ├── clipboard_handler.py      # 剪贴板处理
│   ├── service_manager.py        # 服务启停管理
│   ├── file_handler.py           # 文件下载处理
│   ├── file_watcher_handler.py   # 文件监控处理
│   ├── download_dialog.py        # 下载进度对话框
│   └── icon/
│       ├── icon-active.png       # 运行图标
│       └── icon-stop.png         # 停止图标
├── latest/
│   ├── text_latest.json          # 最新文字信息
│   └── file_latest.json          # 最新文件信息
├── log/
│   ├── client.log                # 客户端日志
│   ├── gui.log                   # 托盘日志
│   └── server.log                # 服务器日志
├── release/
│   ├── build.py                  # 编译exe
│   ├── bat/
│   │   ├── RegisterApp.bat       # 添加到注册表
│   │   └── UnregisterApp.bat     # 从注册表移除
└── server/
    ├── run.py                    # 服务器主入口
    ├── api/
    │   └── flask_app.py          # 服务器路由
    └──core/
        ├── item_builder.py       # 构建item字典
        ├── text_tracker.py       # 最新文字信息的相关操作
        ├── file_latest.py        # 最新文件信息的相关操作
        ├── file_sync.py          # 最新文件信息同步
        └── file_handler.py       # 文件写入封装
```