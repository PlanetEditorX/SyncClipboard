import os
import json
import time
import shutil
import logging
import threading
import pyperclip
import requests
from threading import Thread
from urllib.parse import unquote
from common.utils import BASE_DIR, reveal_in_explorer
from common.notification import show_notification_with_click
from client.folder_archive import (
    FolderArchiveCancelled,
    zip_folder,
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

# 文件夹共享时生成的临时压缩包目录。压缩包在共享结束（对方下载完 / 取消共享 /
# 过期 / 程序退出）后立即删除，这里只存放仍在共享中的临时文件。
FOLDER_SHARE_TEMP_DIR = BASE_DIR / "temp" / "folder_share"

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
        # 由本进程生成的临时文件（文件夹压缩包），共享结束时需要一并删除
        self._temp_files = set()
        # file_id -> 目录共享任务（按需打包的状态机，详见 register_folder）
        self._folder_shares = {}
        # file_id -> 正在进行的下载数：多端同时取同一份文件时，
        # 先下完的一端不能把文件删掉，否则另一端会被截断
        self._active_downloads = {}
        self._remote_text_handler = None
        # 下载中断时用来「重新登记共享」的回调（由 SyncClient 注入）。
        # 服务端的文件记录在 /request_file 返回前就清空了，而这里会为断点
        # 续传保留压缩包；不重新登记的话，接收端再拉一次只会走文本分支。
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
        self.save_path = save_path
        self.tracker = TextTracker()
        # 添加运行状态和服务器引用
        self.running = False
        self._server_thread = None
        self._flask_server = None
        # 上一次异常退出可能留下未删除的临时压缩包（共享信息只存在内存里，
        # 重启后这些包已无人引用），启动时一律清掉。
        self._cleanup_stale_temp_files()

    def _register_routes(self):
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
            if data.get("key") != self.KEY:
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
            key = get_api_key()
            if key != self.KEY:
                return jsonify({"status": "error", "message": "密钥错误"}), 403

            # 从 URL 参数获取文件名，例如 ?filename=my%20file.txt
            encoded_filename = request.args.get('filename', 'uploaded_file')
            filename = unquote(encoded_filename)  # 解码 %20 为空格

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

            # 保存到文件
            save_file_path = os.path.join(self.save_path, filename)
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
            key = get_api_key()
            if key != self.KEY:
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

            # 目录共享：共享登记的是目录本身，压缩包要等接收端来取时才生成
            if os.path.isdir(path):
                logger.info("目录共享待打包: %s -> %s", file_id, path)
                return jsonify({"status": "ok", "message": "目录共享待打包"}), 200

            if not os.path.isfile(path):
                return jsonify({"status": "error", "message": "文件不存在"}), 404

            logger.info("文件正常: %s -> %s", file_id, path)
            return jsonify({"status": "ok", "message": "文件正常"}), 200

        @self.app.route("/prepare/<file_id>", methods=["GET", "POST"])
        def prepare_file(file_id):
            """接收端点下载前先调这里：触发后台打包并返回进度。

            按需打包意味着一份大目录可能压缩好几分钟，直接 GET 会让接收端一直
            等首字节（HTTP 读超时）。接收端先 prepare + 轮询，打包完成再 GET，
            就不会被超时打断；不认识这个接口的客户端仍可直接 GET（同步打包）。
            """
            self._purge_expired_files()
            with self._shared_files_lock:
                job = self._folder_shares.get(file_id)
                if job is None:
                    return jsonify({
                        "status": "unknown",
                        "message": "file_id 不存在或不是目录共享",
                    }), 404
                job = dict(job, cancel=None, lock=None)
                done = bool(job["path"] and os.path.isfile(job["path"]))

            if not done:
                self._start_folder_pack(file_id)
                with self._shared_files_lock:
                    job = dict(self._folder_shares.get(file_id) or job,
                               cancel=None, lock=None)

            state = "ready" if job.get("path") and os.path.isfile(job["path"]) else job["state"]
            payload = {
                "status": state,
                "file_id": file_id,
                "name": job["archive_name"],
                "progress": job.get("progress", ""),
                "size": job.get("size", 0),
            }
            if state == "error":
                payload["message"] = job.get("error") or "打包失败"
            return jsonify(payload), 200

        @self.app.route("/file/<file_id>", methods=["GET"])
        def download_file(file_id):
            client_ip = request.remote_addr
            source = request.headers.get("source", "未知设备")
            logger.info(f"获取文件下载 - 请求来自: {unquote(request.headers.get('source', ''))}({client_ip})")

            path = self.get_file_path(file_id)
            if not path:
                return jsonify({"status": "error", "message": "file_id不存在"}), 404

            # 目录共享：这里同步打包（没走 /prepare 的客户端会走到这条路上），
            # 同一份共享的并发请求由 job 的锁串行化，不会压两遍。
            if os.path.isdir(path):
                logger.info("目录共享开始按需打包: %s", file_id)
                path = self._ensure_archive(file_id)
                if not path:
                    return jsonify({
                        "status": "error",
                        "message": "目录打包失败或共享已失效",
                    }), 500

            if not os.path.isfile(path):
                return jsonify({"status": "error", "message": "文件不存在"}), 404

            logger.info("文件下载请求: %s -> %s", file_id, path)

            response = send_file(path, as_attachment=True)
            # 用生成器把响应体包一层：只有内容真正发送完才取消共享。
            # 临时压缩包会在取消共享时被删除，若在响应构造阶段就删，下载会被截断；
            # 而直接依赖 call_on_close 不可行 —— send_file 返回的是
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
        压缩包当场被删，对方再点一次只剩 404，只能回电脑重新复制一遍。
        """
        with self._shared_files_lock:
            registered_at = self.shared_file_registered_at.get(file_id)
            keep_temp = path in self._temp_files
        if registered_at is None:
            return
        remaining = int(max(0, get_file_share_ttl_seconds() - (time.time() - registered_at)))
        logger.warning(
            "文件传输中断，保留共享以便重试: %s -> %s（剩余 %ss%s）",
            file_id,
            path,
            remaining,
            "，压缩包到期后删除" if keep_temp else "",
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

        用于下载中断后的重新登记：目录仍按 kind=folder 上报（压缩包是否已经
        打好由接收端 /prepare 判定），普通文件按路径现取体积。
        """
        entries = []
        with self._shared_files_lock:
            shared = dict(self.shared_files)
            jobs = {fid: dict(job) for fid, job in self._folder_shares.items()}
        for file_id, path in shared.items():
            job = jobs.get(file_id)
            if job is not None:
                entries.append({
                    "file_id": file_id,
                    # 上报原始目录路径，而不是临时压缩包路径
                    "path": job.get("folder") or path,
                    "name": job.get("archive_name") or os.path.basename(path),
                    "size": job.get("size") or 0,
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

    def register_folder(self, file_id, folder_path, archive_name, size=0):
        """登记一个目录共享，按需打包。

        复制文件夹时只登记、不压缩：压缩要等接收端 `/prepare` 或直接 GET 时
        才做。这样本地只是复制大目录、并不需要传给别的设备时，不会白耗 CPU
        和临时磁盘；超限确认、体积统计仍在复制时就完成，用户能当场看到结果。
        """
        job = {
            "folder": folder_path,
            "archive_name": archive_name,
            "size": size,
            "path": None,
            "state": "idle",
            "error": None,
            "progress": "",
            "cancel": threading.Event(),
            "lock": threading.RLock(),
        }
        with self._shared_files_lock:
            self.shared_files[file_id] = folder_path
            self.shared_file_registered_at[file_id] = time.time()
            self._folder_shares[file_id] = job
        logger.info("注册共享目录(待打包): %s -> %s", file_id, folder_path)

    def _start_folder_pack(self, file_id):
        """后台线程打包；已在打包或已完成时不重复启动。"""
        with self._shared_files_lock:
            job = self._folder_shares.get(file_id)
            if job is None:
                return False
            if job["state"] in ("packing", "ready") or (
                job["path"] and os.path.isfile(job["path"])
            ):
                return False
        Thread(
            target=self._ensure_archive, args=(file_id,),
            name=f"folder-pack-{file_id[:8]}", daemon=True,
        ).start()
        return True

    def _ensure_archive(self, file_id):
        """确保该目录共享已有压缩包，返回压缩包路径；失败返回 None。

        同一份共享的并发调用由 job 上的锁串行化：后到的请求等前一个压完直接
        复用结果，不会同时压两遍。
        """
        with self._shared_files_lock:
            job = self._folder_shares.get(file_id)
        if job is None:
            logger.info("目录共享已失效，放弃打包: %s", file_id)
            return None

        with job["lock"]:
            with self._shared_files_lock:
                if job["path"] and os.path.isfile(job["path"]):
                    return job["path"]
                job["state"] = "packing"
                job["error"] = None
                job["progress"] = "准备打包…"

            try:
                zip_path = zip_folder(
                    job["folder"],
                    FOLDER_SHARE_TEMP_DIR,
                    progress=lambda note: self._set_pack_progress(job, note),
                    cancel_event=job["cancel"],
                    total_size=job.get("size") or None,
                )
            except FolderArchiveCancelled:
                logger.info("目录打包已取消（共享已撤销）: %s", job["folder"])
                with self._shared_files_lock:
                    job["state"] = "cancelled"
                return None
            except OSError as e:
                logger.error("目录打包失败: %s", e)
                with self._shared_files_lock:
                    job["state"] = "error"
                    job["error"] = str(e)
                return None

            with self._shared_files_lock:
                still_shared = self._folder_shares.get(file_id) is job
                if still_shared:
                    job["path"] = str(zip_path)
                    job["state"] = "ready"
                    job["progress"] = "打包完成"
                    self.shared_files[file_id] = str(zip_path)
                    self._temp_files.add(str(zip_path))

            if not still_shared:
                # 打包期间共享被撤销/被新内容替换：这份包没人要，直接删掉
                logger.info("共享已变更，丢弃刚打好的压缩包: %s", zip_path)
                self._remove_temp_file(str(zip_path))
                return None
            logger.info("目录已打包完成: %s -> %s", file_id, zip_path)
            return str(zip_path)

    @staticmethod
    def _set_pack_progress(job, note):
        job["progress"] = note

    def register_file(self, file_id, path, temporary=False):
        """
        注册共享文件
        Parameters
        ----------
        file_id : str
            文件唯一ID
        path : str
            本地文件路径
        temporary : bool
            是否为本次共享临时生成的文件（例如文件夹压缩包），
            取消共享时需要删除
        """
        with self._shared_files_lock:
            self.shared_files[file_id] = path
            self.shared_file_registered_at[file_id] = time.time()
            if temporary:
                self._temp_files.add(path)
        if temporary:
            logger.info("注册共享文件(临时包): %s -> %s", file_id, path)
        else:
            logger.info("注册共享文件: %s -> %s", file_id, path)

    def _release_file(self, file_id, reason="取消共享"):
        """移除一条共享记录；若它是临时文件/临时压缩包则顺手删除。"""
        temp_paths = []
        job = None
        with self._shared_files_lock:
            path = self.shared_files.pop(file_id, None)
            self.shared_file_registered_at.pop(file_id, None)
            job = self._folder_shares.pop(file_id, None)
            candidates = [path]
            if job is not None:
                candidates.append(job.get("path"))
            for candidate in candidates:
                if candidate and candidate in self._temp_files:
                    self._temp_files.discard(candidate)
                    temp_paths.append(candidate)
        if job is not None:
            # 还在打包的目录共享：让它尽快停下来，别继续占着 CPU 写一个没人要的包
            job["cancel"].set()
        if path is None and job is None:
            return
        logger.info("%s文件: %s -> %s", reason, file_id, path)
        for temp_path in temp_paths:
            self._remove_temp_file(temp_path)

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
            jobs = list(self._folder_shares.values())
            temp_paths = list(self._temp_files)
            self.shared_files.clear()
            self.shared_file_registered_at.clear()
            self._temp_files.clear()
            self._folder_shares.clear()
        for job in jobs:
            job["cancel"].set()
        logger.info("共享文件列表已清空")
        for path in temp_paths:
            self._remove_temp_file(path)

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

    def _remove_temp_file(self, path):
        """删除本进程生成的临时文件（压缩包），失败只记日志。"""
        try:
            if os.path.isfile(path):
                os.remove(path)
                logger.info("已删除临时压缩包: %s", path)
        except OSError as e:
            logger.warning("删除临时压缩包失败: %s, 错误: %s", path, e)

    def _cleanup_stale_temp_files(self):
        """清空临时压缩包目录（仅供启动时清理历史残留）。"""
        try:
            if FOLDER_SHARE_TEMP_DIR.exists():
                shutil.rmtree(FOLDER_SHARE_TEMP_DIR, ignore_errors=True)
                logger.info("已清理残留的临时压缩包目录: %s", FOLDER_SHARE_TEMP_DIR)
        except OSError as e:
            logger.warning("清理临时压缩包目录失败: %s", e)

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
