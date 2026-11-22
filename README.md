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
    environment:
      - PORT=8000
      - TZ=Asia/Shanghai
```

该配置会将宿主机 `./config`、`./log`、`./latest` 目录映射到容器内对应目录，并使用 `PORT=8000` 和上海时区。

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
| 目录大小上限 | 默认 10 GB。小于上限的目录在复制后自动后台打包传输；超过上限会弹窗确认，确认后才打包 | `folder_zip_confirm_gb` |
| 共享存活时间 | 默认 10 分钟。到期后自动取消共享、删除临时压缩包，服务器也会丢弃对应记录 | `file_share_ttl_minutes` |

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
    "file_share_ttl_minutes": 10
}
```

`file_share_ttl_minutes` 会同时写入 `server_config.json`，Docker/Linux 部署可只改服务器配置里的这一项。

### 手机配置

#### iOS端

1. **安装 Actions**（用于检测主机可达性，避免快捷指令因网络异常报错）
   - 如果未安装过，可能需要借助 [ipatool](https://github.com/majd/ipatool) 购买历史版本。
   - 若实在无法安装，可手动删除快捷指令中涉及 Actions 的部分。

2. **添加快捷指令**（iCloud 链接）
   - iOS27
      - [文字同步](https://www.icloud.com/shortcuts/b3058f3aaa124e02aa090991431414bf)
      - [推送同步](https://www.icloud.com/shortcuts/80e3c0f8a61741498610de1a4ffce916)
      - [获取同步](https://www.icloud.com/shortcuts/49543480b425456387f74a5ad6646f30)
      - [注册服务](https://www.icloud.com/shortcuts/233c45575ea64e0eb782e777b82765f4)
      - [短信同步](https://www.icloud.com/shortcuts/130c98280a2c45e5a613b00544612979)
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

5. **创建自动注册**
   - 因为默认一周未使用，会删除客户端信息，避免长时间推送给不在线的设备，因此设置一周内任意一天运行「注册服务」快捷指令
   - 设置为“立即运行”，关闭“运行时通知”

6. **控制中心**
   - 添加「发送文件」和「获取文件」快捷指令，方便手动触发文件传输。

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
   - 复制文件夹时先自动打包成 `文件夹名.zip` 再共享（包内保留一层同名目录），对方下载完成后立即删除临时压缩包；取消共享、共享过期或退出程序时也会一并清理。压缩包存放在程序的 temp/folder_share 目录，打包在后台线程进行，不影响文字同步。

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