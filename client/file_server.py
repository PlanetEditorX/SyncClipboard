import os
import json
import time
import logging
import threading
import pyperclip
import requests
from threading import Thread
from urllib.parse import quote, unquote
from common.utils import (
    BASE_DIR,
    reveal_in_explorer,
    fix_mojibake_filename,
    normalize_local_path,
    request_key_matches,
)
from common.notification import show_notification_with_click
from client.folder_manifest import (
    MANIFEST_MAX_ENTRIES,
    build_manifest,
    folder_display_name,
)
from server.core.file_latest import (
    get_file_share_ttl_seconds,
    is_file_record_expired,
    set_file_share_ttl,
)
from server.core.text_tracker import TextTracker
from flask import Flask, jsonify, send_file, request

FILE_LATEST_FILE = BASE_DIR / "latest" / "file_latest.json"
FILE_LATEST_FILE.parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("client")

def get_api_key():
    return request.headers.get("key", "")

def get_files_latest_file():
    """读取最新文件数据，如果文件不存在或无效则返回 None"""
    if not FILE_LATEST_FILE.exists():
        return []
    try:
        with open(FILE_LATEST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except (json.JSONDecodeError, AttributeError):
        return None

class FileServer:
    """客户端网络服务"""
    def __init__(self, port=8899, center_host="127.0.0.1", center_port=8000, local_name="PC-01", key="123456", save_path="./uploads", share_ttl_seconds=None):
        # 共享存活时间：传入则同步到全局，后续所有过期判断都用这个值
        if share_ttl_seconds is not None:
            set_file_share_ttl(share_ttl_seconds)
        self.port = port
        self.center_host = center_host
        self.center_port = center_port
        self.shared_files = {}
        self.shared_file_registered_at = {}
        self._shared_files_lock = threading.RLock()
        # file_id -> {"path", "name", "size"}：目录共享。
        # 目录不再打包，接收端拿 /manifest 清单后逐文件来取，这里只存登记信息。
        self.shared_folders = {}
        # file_id -> 正在进行的下载数：多端同时取同一份文件时，
        # 先下完的一端不能把文件删掉，否则另一端会被截断
        self._active_downloads = {}
        self._remote_text_handler = None
        # 下载中断时用来「重新登记共享」的回调（由 SyncClient 注入）。
        # 服务端的文件记录在 /request_file 返回前就清空了，而这里会为断点
        # 续传保留共享；不重新登记的话，接收端再拉一次只会走文本分支。
        self._reannounce_handler = None
        self.app = Flask(__name__)
        # 关闭 Flask 默认访问日志
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        self._register_routes()
        # 全局锁，避免同时读写剪贴板
        self.clipboard_lock = threading.Lock()
        self.last_remote_id = None
        self._last_remote_content = None
        self.last_text = ""
        self.KEY = str(key)
        self.local_name = local_name
        # 配置里常手填 "D:/Downloads"，先归一成本机原生分隔符，
        # 免得后面拼出 "D:/Downloads\文件名" 这种混合路径
        self.save_path = normalize_local_path(save_path)
        self.tracker = TextTracker()
        # 添加运行状态和服务器引用
        self.running = False
        self._server_thread = None
        self._flask_server = None

    def _register_routes(self):
        @self.app.before_request
        def guard_file_server():
            """统一鉴权：除 /ping 探活外，所有路由都要求正确的 key。

            契约与中心服务端一致（common.utils.request_key_matches）：所有端
            一律在请求头带 key。以前 /files、/check、/manifest、/file 全是
            免密的，局域网里任何人都能列出本机共享的绝对路径并直接下载。
            """
            if request.path == "/ping":
                return None
            if not request_key_matches(self.KEY):
                return jsonify({"status": "error", "message": "密钥错误"}), 403
            return None

        @self.app.route("/ping", methods=["GET"])
        def ping():
            return jsonify({
                "status": "ok",
                "service": "file_server"
            })

        @self.app.route("/files", methods=["GET"])
        def files():
            client_ip = request.remote_addr
            logger.info(f"获取文件下载列表 - 请求来自: {client_ip}")
            self._purge_expired_files()
            with self._shared_files_lock:
                shared_files = self.shared_files.copy()
            return jsonify({
                "count": len(shared_files),
                "files": shared_files
            })

        @self.app.route("/update/current_latest", methods=["POST"])
        def update_client_latest():
            data = request.get_json()
            if not data:
                return jsonify({"status": "error", "message": "无效的请求数据"}), 400
            if not request_key_matches(self.KEY):
                return jsonify({"status": "error", "message": "密钥错误"}), 403
            client_ip = request.remote_addr
            if data.get("type") == "text":
                logger.info(f"更新文字列表 - 请求来自: {client_ip}")
                latest = data.get("latest_global")
                if latest and latest.get("source") != self.local_name:
                    if self._remote_text_handler is not None:
                        self._remote_text_handler(latest)
                    elif latest["id"] != self.last_remote_id:
                        # 更新到文件
                        if self.tracker.is_duplicate(latest["id"]):
                            return jsonify({"status": "duplicate", "message": "重复内容"}), 200
                        # 更新剪贴板
                        with self.clipboard_lock:
                            pyperclip.copy(latest["content"])
                        # 更新记录（同时注册 ID、更新客户端最新和全局最新）
                        self.tracker.update(latest)
                        self.last_remote_id = latest["id"]
                        self._last_remote_content = latest["content"]
                        self.last_text = latest["content"]
                        logging.info(f"更新剪贴板: {latest['content'][:50]} (来自 {latest['source']})")
            else:
                logger.info(f"更新文件列表 - 请求来自: {client_ip}")
                latest = data.get("latest_global") or []
                if isinstance(latest, dict):
                    latest = [latest]
                if not isinstance(latest, list):
                    latest = []
                latest = [
                    item for item in latest
                    if not is_file_record_expired(item)
                ]
                # 获取本地已存储的文件
                file_latest = get_files_latest_file()
                if latest == file_latest:
                    logger.info(f"文件未变化，跳过写入")
                else:
                    # 只有当 file_id 不一致时才写入
                    file_latest = latest
                    with open(FILE_LATEST_FILE, "w", encoding="utf-8") as f:
                        json.dump(file_latest, f, ensure_ascii=False, indent=2)
                    logger.info("file_latest文件已更新")
            return jsonify({
                "status": "ok",
            })

        @self.app.route('/upload_file', methods=['PUT'])
        def upload_file():
            """上传文件到电脑"""
            if not request_key_matches(self.KEY):
                return jsonify({"status": "error", "message": "密钥错误"}), 403

            # request.args 已做过一次 URL 解码，不能再 unquote：文件名里字面
            # 含 %20 会被二次解码成空格，文件被悄悄改名（服务端同处已同步修）。
            encoded_filename = request.args.get('filename', 'uploaded_file')
            filename = fix_mojibake_filename(encoded_filename)

            # 安全处理文件名：去除空字符，替换反斜杠，提取最后一部分作为纯文件名
            filename = filename.replace('\x00', '')
            filename = filename.replace('\\', '/')
            filename = filename.split('/')[-1]
            if not filename or filename in ('.', '..'):
                filename = 'uploaded_file'

            file_data = None
            try:
                file_data = request.get_data(cache=False, as_text=False)
            except Exception as e:
                logger.error(f"读取原始二进制数据时出错: {e}")

            if not file_data:
                return jsonify({"status": "error", "message": "未收到文件"}), 400

            # 保存到文件：整条路径（含通知里显示的那一串）统一用本机分隔符，
            # Windows 下就是 D:\Downloads\文件名，不再出现 / 和 \ 混排
            save_file_path = normalize_local_path(
                os.path.join(self.save_path, filename)
            )
            with open(save_file_path, 'wb') as f:
                f.write(file_data)

            logging.info(f"手机上传文件已保存: {save_file_path}")
            source = unquote(request.headers.get("source", ""))
            msg = f"来源：{source}\n文件：{filename}\n保存：{save_file_path}"
            # 点击通知直接在资源管理器里选中刚收到的文件，省得自己去找
            show_notification_with_click(
                "手机文件已保存",
                msg + "\n点击打开所在文件夹",
                lambda p=save_file_path: reveal_in_explorer(p),
            )
            return jsonify({"status": "ok", "message": "文件上传成功","path": save_file_path}), 200

        @self.app.route("/clear/file_latest", methods=["GET"])
        def clear_file_latest():
            if not request_key_matches(self.KEY):
                return jsonify({"status": "error", "message": "密钥错误"}), 403
            client_ip = request.remote_addr
            logger.info(f"清空文件列表 - 请求来自: {client_ip}")
            with open(FILE_LATEST_FILE, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)
            logger.info("file_latest文件已清空")
            return jsonify({
                "status": "ok",
            })

        @self.app.route("/check/<file_id>", methods=["GET"])
        def check_files(file_id):
            client_ip = request.remote_addr
            logger.info(f"检测文件状态 - 请求来自: {client_ip}")

            path = self.get_file_path(file_id)
            if not path:
                return jsonify({"status": "error", "message": "file_id不存在"}), 404

            # 目录共享：登记的是目录本身，接收端要按 /manifest 清单逐文件取
            if os.path.isdir(path):
                logger.info("目录共享待逐文件传输: %s -> %s", file_id, path)
                return jsonify({"status": "ok", "message": "目录共享待逐文件传输"}), 200

            if not os.path.isfile(path):
                return jsonify({"status": "error", "message": "文件不存在"}), 404

            logger.info("文件正常: %s -> %s", file_id, path)
            return jsonify({"status": "ok", "message": "文件正常"}), 200

        @self.app.route("/file/<file_id>", methods=["GET"])
        def download_file(file_id):
            client_ip = request.remote_addr
            source = request.headers.get("source", "未知设备")
            logger.info(f"获取文件下载 - 请求来自: {unquote(request.headers.get('source', ''))}({client_ip})")

            path = self.get_file_path(file_id)
            if not path:
                return jsonify({"status": "error", "message": "file_id不存在"}), 404

            # 目录共享不发整包（v1.12.4 起不再压缩）：整包会丢目录层级，
            # 也让接收端只能拿到一堆散装文件。这里只指路，让它去取清单。
            if os.path.isdir(path):
                return jsonify({
                    "status": "error",
                    "file_id": file_id,
                    "message": "目录共享请先取 /manifest/<file_id> 再逐文件下载",
                    "manifest_url": "{}/manifest/{}".format(
                        request.host_url.rstrip("/"), file_id),
                }), 409

            if not os.path.isfile(path):
                return jsonify({"status": "error", "message": "文件不存在"}), 404

            logger.info("文件下载请求: %s -> %s", file_id, path)

            response = send_file(path, as_attachment=True)
            # 用生成器把响应体包一层：只有内容真正发送完才取消共享。
            # 直接依赖 call_on_close 不可行 —— send_file 返回的是
            # direct_passthrough 响应，Werkzeug 不会回调 Response.close。
            inner = response.response
            sent_all = False
            self._begin_download(file_id)

            def body_stream():
                nonlocal sent_all
                try:
                    for chunk in inner:
                        yield chunk
                    sent_all = True
                finally:
                    # 先关文件句柄再删文件（Windows 下句柄未关会导致删除失败）
                    close_inner = getattr(inner, "close", None)
                    if callable(close_inner):
                        try:
                            close_inner()
                        except Exception:
                            pass
                    others = self._end_download(file_id)
                    if not sent_all:
                        self._keep_for_retry(file_id, path)
                    elif others == 0:
                        self.unregister_file(file_id)
                    else:
                        # 还有别的设备在取同一份内容，等它取完再清理
                        logger.info(
                            "其他设备仍在下同一份内容，暂不删除: %s", file_id
                        )

            response.response = body_stream()
            return response

        @self.app.route("/manifest/batch", methods=["GET"])
        def folder_manifest_batch():
            """多个目录的**合并**清单：把二维的「目录 → 条目」在一处摊平。

            为什么要有这个接口：iOS 快捷指令里「重复每一项」嵌两层时，内层拿到
            的 Repeat Item 是空的 —— v5 的真机现象就是内层取 `url` 得到空值，
            「获取 URL 内容」直接报「未指定 URL」。所以接收端全程只能遍历**一
            层**：所有目录的文件必须先合并成一张表，`dir` 也就得在这里补上
            「根目录名/」前缀，接收端不必再拼字符串。
            """
            self._purge_expired_files()
            ids, seen = [], set()
            for raw in (request.args.get("ids") or "").split(","):
                file_id = raw.strip()
                if file_id and file_id not in seen:
                    seen.add(file_id)
                    ids.append(file_id)
            if not ids:
                return jsonify({
                    "status": "error",
                    "message": "缺少 ids 参数（形如 ?ids=a,b）",
                }), 400

            base = request.host_url.rstrip("/")
            entries, empty_dirs, notices, roots = [], [], [], []
            total_size, total_dirs, truncated = 0, 0, False
            for file_id in ids:
                folder = self._folder_root(file_id)
                if not folder:
                    continue
                payload = build_manifest(folder)
                if payload is None:
                    continue
                root = payload["root"]
                roots.append(root)
                prefix = root.rstrip("/") + "/"
                total_size += payload["total_size"]
                total_dirs += len(payload["dirs"])
                if payload["truncated"]:
                    truncated = True
                if payload.get("notice") and payload["notice"] not in notices:
                    notices.append(payload["notice"])
                for rel_dir in payload["empty_dirs"]:
                    empty_dirs.append(prefix + rel_dir)
                for entry in payload["entries"]:
                    # dir 补上根目录名：接收端拿到就能直接当子路径用，
                    # 多个目录混在一起也不会撞车。
                    entry["dir"] = prefix + entry["dir"]
                    # prefix 是同一路径**去掉末尾斜杠**的版本，专供快捷指令
                    # 拼「prefix + "/" + 文件名」：斜杠必须是字面字符，不能
                    # 指望变量值自带的尾斜杠 —— iOS 的「存储到文件」会对子
                    # 路径做规范化，变量结尾的 "/" 会被吃掉，于是 `目录名/`
                    # 退化成 `目录名` 并被当成**文件名**，一阵覆盖之后「下载」
                    # 里只剩一个叫「目录名」的无扩展名文件（v6 真机现象）。
                    # 根目录下的文件原来 dir 为空串，这里靠根名兜住，保证
                    # prefix 永远非空，拼出来不会出现前导 "/"。
                    entry["prefix"] = entry["dir"].rstrip("/")
                    entry["url"] = "{}/file/{}/{}".format(
                        base, file_id, quote(entry["path"], safe="/"))
                    entries.append(entry)

            if not entries and not roots:
                return jsonify({
                    "status": "error",
                    "message": "目录共享都不存在或已失效，请回电脑重新复制",
                }), 404

            # 手工维护条数上限：单个目录时由 build_manifest 截断，多个目录
            # 合并后也要有总上限，否则接收端会被几千个请求拖死。
            if len(entries) > MANIFEST_MAX_ENTRIES:
                entries = entries[:MANIFEST_MAX_ENTRIES]
                truncated = True
                notices.append(
                    f"目录文件超过 {MANIFEST_MAX_ENTRIES} 个，只传前 "
                    f"{MANIFEST_MAX_ENTRIES} 个，请拆成几个目录分别同步")

            # 给每条补上「第几个 / 共几个」：接收端直接读，就能弹
            # 「正在下载 第 3/10 个：xxx.pdf」。让服务端算而不是让快捷指令
            # 自己累加 —— iOS 里做计数得靠「计算」动作维护变量，多一个动作就
            # 多一个可能在真机上炸的点，而这边的编号是确定的、可测试的。
            total_entries = len(entries)
            for position, entry in enumerate(entries, 1):
                entry["index"] = position
                entry["total"] = total_entries

            logger.info("合并清单: %s -> %s 个文件 / %s 个目录（截断=%s）",
                        ",".join(ids), len(entries), total_dirs, truncated)
            return jsonify({
                "status": "ok",
                "roots": roots,
                # 只共享一个目录时把名字也直接给出，通知里好显示
                "root": roots[0] if len(roots) == 1 else "、".join(roots),
                "count": len(entries),
                "dirs_count": total_dirs,
                "total_size": total_size,
                "truncated": truncated,
                "notice": "；".join(notices),
                "empty_dirs": empty_dirs,
                "entries": entries,
            }), 200

        @self.app.route("/manifest/<file_id>", methods=["GET"])
        def folder_manifest(file_id):
            """目录共享的文件清单：逐文件传输的唯一入口。

            目录不再打包 —— iOS 的「解压缩」会把包内目录摊平，接收端只能拿到
            一堆散装文件。清单给出每项的相对路径和下载地址，接收端逐项下载并
            写回子目录，层级由清单承载，不经过任何归档格式。
            """
            self._purge_expired_files()
            folder = self._folder_root(file_id)
            if not folder:
                return jsonify({
                    "status": "error",
                    "file_id": file_id,
                    "message": "file_id 不存在或不是目录共享",
                }), 404

            payload = build_manifest(folder)
            if payload is None:
                return jsonify({
                    "status": "error",
                    "file_id": file_id,
                    "message": "目录已不存在或无法读取",
                }), 404

            # 用请求带的 Host 拼绝对地址：接收端就是通过这个地址找到我们的，
            # 回给它的地址一定能用，不必猜本机网卡 IP。
            base = request.host_url.rstrip("/")
            for entry in payload["entries"]:
                entry["url"] = "{}/file/{}/{}".format(
                    base, file_id, quote(entry["path"], safe="/"))
            payload["file_id"] = file_id
            logger.info("目录清单: %s -> %s 个文件 / %s 个目录（截断=%s）",
                        file_id, payload["count"], len(payload["dirs"]),
                        payload["truncated"])
            return jsonify(payload), 200

        @self.app.route("/file/<file_id>/<path:rel_path>", methods=["GET"])
        def download_member(file_id, rel_path):
            """目录共享里的单个文件。

            与 `/file/<file_id>` 的区别：这里只发一个文件，且**不**在发完
            后撤销共享 —— 一份目录要连续取很多次，撤销得等接收端自己用完
            （或到期清理）。
            """
            self._purge_expired_files()
            folder = self._folder_root(file_id)
            if not folder:
                return jsonify({
                    "status": "error",
                    "message": "file_id 不存在或不是目录共享",
                }), 404

            base = os.path.realpath(folder)
            target = os.path.realpath(os.path.join(base, unquote(rel_path)))
            # 目录穿越（../、绝对路径、指向目录外的符号链接）一律拒掉
            try:
                inside = os.path.commonpath([base, target]) == base
            except ValueError:      # 不同盘符
                inside = False
            if not inside or not os.path.isfile(target):
                logger.warning("拒绝越界的目录内文件请求: %s -> %s",
                               file_id, rel_path)
                return jsonify({
                    "status": "error",
                    "message": "文件不存在或路径越界",
                }), 404

            logger.info("目录内文件下载: %s -> %s", file_id, target)
            return send_file(target, as_attachment=True,
                             download_name=os.path.basename(target))

    def _folder_root(self, file_id):
        """目录共享的本地根路径；不是目录共享则返回 None。"""
        with self._shared_files_lock:
            info = self.shared_folders.get(file_id)
        return info["path"] if info else None

    def _begin_download(self, file_id):
        with self._shared_files_lock:
            self._active_downloads[file_id] = self._active_downloads.get(file_id, 0) + 1

    def _end_download(self, file_id):
        """减掉一个下载中的计数，返回剩余数量。"""
        with self._shared_files_lock:
            left = self._active_downloads.get(file_id, 1) - 1
            if left > 0:
                self._active_downloads[file_id] = left
            else:
                self._active_downloads.pop(file_id, None)
                left = 0
            return left

    def _keep_for_retry(self, file_id, path):
        """下载中途失败：保留共享，等对方重试，到期由清理逻辑兜底。

        之前这里不分成功失败一律取消共享，结果是手机端传到一半断网、进程被杀，
        共享当场被撤，对方再点一次只剩 404，只能回电脑重新复制一遍。
        """
        with self._shared_files_lock:
            registered_at = self.shared_file_registered_at.get(file_id)
        if registered_at is None:
            return
        remaining = int(max(0, get_file_share_ttl_seconds() - (time.time() - registered_at)))
        logger.warning(
            "文件传输中断，保留共享以便重试: %s -> %s（剩余 %ss）",
            file_id, path, remaining,
        )
        # 光保留没用：服务端的文件记录在 /request_file 返回前就清空了，
        # 接收端再拉一次只会拿到文本分支。这里通知上层重新登记一次共享。
        self._trigger_reannounce()

    def _trigger_reannounce(self):
        handler = self._reannounce_handler
        if handler is None:
            return

        def run():
            try:
                handler()
            except Exception:
                logger.exception("重新登记共享失败")

        Thread(target=run, name="share-reannounce", daemon=True).start()

    def set_reannounce_handler(self, handler):
        """注入「共享需要重新登记」回调（下载中断时调用）。"""
        self._reannounce_handler = handler

    def share_entries(self):
        """把当前仍持有的共享整理成 `/file_sync` 需要的 file_list。

        用于下载中断后的重新登记：目录按 kind=folder 上报真实目录名与体积
        （接收端据此取 /manifest 清单），普通文件按路径现取体积。
        """
        entries = []
        with self._shared_files_lock:
            shared = dict(self.shared_files)
            folders = {fid: dict(info) for fid, info in self.shared_folders.items()}
        for file_id, path in shared.items():
            info = folders.get(file_id)
            if info is not None:
                entries.append({
                    "file_id": file_id,
                    "path": info["path"],
                    "name": info["name"],
                    "size": info.get("size") or 0,
                    "kind": "folder",
                })
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            entries.append({
                "file_id": file_id,
                "path": path,
                "name": os.path.basename(path),
                "size": size,
                "kind": "file",
            })
        return entries

    def set_remote_text_handler(self, handler):
        """将远程文字交给剪贴板监听器统一处理，避免接收后再次上传。"""
        self._remote_text_handler = handler

    def register_folder(self, file_id, folder_path, name=None, size=0):
        """登记一个目录共享。

        只登记路径与体积，不碰文件内容：接收端来取时按 /manifest 清单逐文件
        下载。这里既不做压缩也不做预览扫描，本地复制一个大目录但根本不传输时
        不会有任何额外开销；超限确认与体积统计在复制时就已完成，用户能当场
        看到结果。
        """
        info = {
            "path": folder_path,
            "name": name or folder_display_name(folder_path),
            "size": size,
        }
        with self._shared_files_lock:
            self.shared_files[file_id] = folder_path
            self.shared_file_registered_at[file_id] = time.time()
            self.shared_folders[file_id] = info
        logger.info("注册共享目录(逐文件传输): %s -> %s", file_id, folder_path)

    def register_file(self, file_id, path):
        """注册共享文件。"""
        with self._shared_files_lock:
            self.shared_files[file_id] = path
            self.shared_file_registered_at[file_id] = time.time()
        logger.info("注册共享文件: %s -> %s", file_id, path)

    def _release_file(self, file_id, reason="取消共享"):
        """移除一条共享记录。"""
        with self._shared_files_lock:
            path = self.shared_files.pop(file_id, None)
            self.shared_file_registered_at.pop(file_id, None)
            self.shared_folders.pop(file_id, None)
        if path is None:
            return
        logger.info("%s: %s -> %s", reason, file_id, path)

    def unregister_file(self, file_id):
        """
        取消共享
        """
        self._release_file(file_id, "取消共享")

    def clear_files(self):
        """
        清空共享列表
        """
        with self._shared_files_lock:
            self.shared_files.clear()
            self.shared_file_registered_at.clear()
            self.shared_folders.clear()
        logger.info("共享文件列表已清空")

    def _purge_expired_files(self, now=None):
        """从本机文件服务中移除已超过共享期限的文件/目录共享。"""
        current_time = time.time() if now is None else now
        ttl_seconds = get_file_share_ttl_seconds()
        with self._shared_files_lock:
            expired_ids = [
                file_id
                for file_id, registered_at in self.shared_file_registered_at.items()
                if current_time - registered_at >= ttl_seconds
            ]
        for file_id in expired_ids:
            self._release_file(file_id, "共享已过期")
        return len(expired_ids)

    def get_file_path(self, file_id):
        """
        获取文件路径
        """
        self._purge_expired_files()
        with self._shared_files_lock:
            return self.shared_files.get(file_id)

    def start(self):
        """启动文件服务器"""
        if self.running:
            logger.warning("文件服务器已在运行中")
            return

        self.running = True

        def run():
            logger.info("文件服务启动: 0.0.0.0:%s", self.port)
            try:
                # 使用 Werkzeug 服务器（Flask 内置）
                from werkzeug.serving import make_server
                self._flask_server = make_server('0.0.0.0', self.port, self.app, threaded=True)
                self._flask_server.serve_forever()
            except Exception as e:
                if self.running:  # 只在非主动停止时记录错误
                    logger.error(f"文件服务器异常: {e}")

        self._server_thread = Thread(target=run, daemon=True)
        self._server_thread.start()
        logger.info("文件服务器线程已启动")

    def stop(self):
        """停止文件服务器"""
        if not self.running:
            logger.warning("文件服务器未在运行")
            return

        logger.info("正在停止文件服务器...")
        self.running = False

        # 关闭 Werkzeug 服务器
        if self._flask_server:
            try:
                self._flask_server.shutdown()
                logger.info("Flask 服务器已关闭")
            except Exception as e:
                logger.error(f"关闭 Flask 服务器时出错: {e}")

        # 等待线程结束
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=5)
            if self._server_thread.is_alive():
                logger.warning("文件服务器线程未能在5秒内停止")

        # 退出前清掉仍在共享的临时压缩包，避免占用磁盘
        self.clear_files()
        logger.info("文件服务器已停止")
