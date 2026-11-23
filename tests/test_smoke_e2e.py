"""冒烟测试：真起两个 HTTP 服务，把整条链路跑一遍。

跟单测的区别 —— 这里不打桩传输层，真的走 socket：

    电脑端 FileServer（werkzeug）
        ↑ /file_sync、/request_file
    中心服务 flask_app（werkzeug）
        ↑ 手机端扮演者（requests）

一天里最容易坏、又最不容易被单测发现的是「接到一起才不对」：注册的
file_id 和下载路由对不上、清单里的地址在真机上打不开、手机拿到的 URL
指向一个没监听的端口。这些只有真跑一遍才会露出来。

跑通的标准（也是用户视角）：
    电脑上有一个目录 → 手机上拉 → 等到打包完成 → 下载 → 解开 → 文件在里面。
"""

import json
import os
import socket
import struct
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import requests
from werkzeug.serving import make_server

import server.core.file_latest as file_latest_mod
import server.core.text_tracker as text_tracker_mod
from client import file_server as file_server_mod
from client.file_server import FileServer
from client.main_menu import SyncClient
from server.api import flask_app

try:
    from tests.test_pull_contract import _FakeTracker
except ImportError:                     # 在 tests/ 目录里直接跑
    from test_pull_contract import _FakeTracker  # type: ignore

KEY = "secret"
PHONE = "熊熊的iPhone"
PC = "DESKTOP-SMOKE"
ROOT = Path(__file__).resolve().parent.parent


def _free_port():
    """借一个当前空闲的端口。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _CenterServer:
    """把 flask_app 真跑起来（werkzeug），不经过 test_client。"""

    def __init__(self):
        self.port = _free_port()
        self._srv = make_server("127.0.0.1", self.port, flask_app.app, threaded=True)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self._thread.start()
        self._wait_ready()
        return self

    def _wait_ready(self, timeout=10):
        """只探测端口是否已 listen —— 打 HTTP 会往日志里灌 404。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("中心服务没起来")

    def stop(self):
        self._srv.shutdown()
        self._thread.join(timeout=5)


class _SmokeCase(unittest.TestCase):
    """每条用例起一套完整环境：中心服务 + 电脑端 FileServer + 临时数据目录。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        # 各种临时目录都挪到 tmp 里，别碰真实的 latest/ 和共享缓存
        patcher = patch.object(
            file_latest_mod, "FILE_LATEST_FILE", self.root / "file_latest.json")
        patcher.start()
        self.addCleanup(patcher.stop)

        self.latest_file = file_latest_mod.FileLatestTracker()
        self.tracker = _FakeTracker()
        self._ttl_before = file_latest_mod.get_file_share_ttl_seconds()
        self.addCleanup(file_latest_mod.set_file_share_ttl, self._ttl_before)

        for patcher in (
            patch.object(flask_app, "KEY", KEY),
            patch.object(flask_app, "latest_file", self.latest_file),
            patch.object(flask_app, "tracker", self.tracker),
            patch.object(flask_app, "touch_client", MagicMock()),
            patch.object(flask_app, "load_clients_ip", MagicMock()),
            patch.object(flask_app, "notify_clients", MagicMock()),
            patch.dict(flask_app.IOS_NOTIFY, {}, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self.center = _CenterServer().start()
        self.addCleanup(self.center.stop)

        self.file_server = FileServer(
            port=_free_port(), center_host="127.0.0.1",
            center_port=self.center.port, local_name=PC, key=KEY,
        )
        self.file_server.start()
        self.addCleanup(self.file_server.stop)
        self._wait_ping(f"http://127.0.0.1:{self.file_server.port}/ping")

    def _wait_ping(self, url, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if requests.get(url, timeout=1).status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.05)
        raise RuntimeError(f"服务没起来: {url}")

    # ---- 手机端会做的三件事 ----

    def _push(self, file_list, port=None):
        return requests.post(
            f"{self.center.url}/file_sync", headers={"key": KEY},
            json={"file_list": file_list, "source": PC,
                  "port": self.file_server.port if port is None else port},
            timeout=10,
        )

    def _request_file(self, source=PHONE):
        resp = requests.post(
            f"{self.center.url}/request_file", headers={"key": KEY},
            json={"source": source}, timeout=10,
        )
        return resp

    def _manifest(self, manifest_url):
        """模拟快捷指令取目录清单。"""
        resp = requests.get(manifest_url, headers={"key": KEY}, timeout=10)
        self.assertEqual(resp.status_code, 200, f"清单取不到: {manifest_url}")
        return resp.json()

    def _download_folder(self, manifest):
        """按清单逐文件下载，键是手机端真实的落盘路径。

        手机端不做任何拼接：子路径直接取清单给的 `dir`（已含目录名、末尾带
        "/"），所以这里也用 `dir + name` 当键 —— 这一层就是目录层级。若哪天
        服务端忘了补根目录名，键会变成 `a.txt` 而不是 `课程资料/a.txt`，
        断言会立刻炸出来。
        """
        got = {}
        for entry in manifest["entries"]:
            resp, data = self._download(entry["url"])
            self.assertEqual(resp.status_code, 200, entry["path"])
            got[entry["dir"] + entry["name"]] = data
        return got

    def _download(self, url, chunk_size=65536, max_bytes=None):
        resp = requests.get(url, headers={"key": KEY}, timeout=60, stream=True)
        self.addCleanup(resp.close)
        data = b""
        for chunk in resp.iter_content(chunk_size):
            data += chunk
            if max_bytes and len(data) >= max_bytes:
                break
        return resp, data


class TestFolderRoundTrip(_SmokeCase):
    """电脑上有一个目录，手机端完整走一遍。"""

    def setUp(self):
        super().setUp()
        self.folder = self.root / "课程资料"
        (self.folder / "子目录").mkdir(parents=True)
        (self.folder / "讲义.txt").write_text("第一章", encoding="utf-8")
        (self.folder / "子目录" / "作业.txt").write_text("第一题", encoding="utf-8")
        self.file_server.register_folder("d1", str(self.folder), size=64)

    def test_folder_end_to_end(self):
        entries = self.file_server.share_entries()
        self.assertEqual(self._push(entries).status_code, 200)

        payload = self._request_file().json()
        self.assertEqual(payload["type"], "folder")
        self.assertEqual(len(payload["files"]), 1)
        entry = payload["files"][0]
        self.assertEqual(entry["kind"], "folder")
        self.assertEqual(entry["name"], "课程资料")
        self.assertEqual(payload["manifest_url"], entry["manifest_url"])

        # 清单地址必须指向真实在监听的端口，否则手机端就是在打空气
        parsed = urlparse(entry["manifest_url"])
        self.assertEqual(parsed.hostname, "127.0.0.1")
        self.assertEqual(parsed.port, self.file_server.port)

        # 手机端只认这条合并清单地址：它把所有目录摊成一层，
        # 手机上因此只需要一层循环（嵌套两层时内层取不到值）
        batch_url = payload["manifest_batch_url"]
        parsed_batch = urlparse(batch_url)
        self.assertEqual(parsed_batch.hostname, "127.0.0.1")
        self.assertEqual(parsed_batch.port, self.file_server.port)

        manifest = self._manifest(batch_url)
        self.assertEqual(manifest["status"], "ok")
        self.assertEqual(manifest["roots"], ["课程资料"])
        self.assertEqual(manifest["count"], 2)

        files = self._download_folder(manifest)
        # 键就是落盘路径：目录名在里、子目录在里面，一层都不能少
        self.assertEqual(files["课程资料/讲义.txt"].decode("utf-8"), "第一章")
        self.assertEqual(files["课程资料/子目录/作业.txt"].decode("utf-8"), "第一题")

        # 一次性消费：再拉一次只剩文本分支
        self.assertEqual(self._request_file().json()["type"], "text")

    def test_mixed_files_and_folders(self):
        note = self.root / "备忘.txt"
        note.write_text("记得交作业", encoding="utf-8")
        self.file_server.register_file("f1", str(note))

        payload = self._request_file().json() if self._push(
            self.file_server.share_entries()).ok else None
        self.assertIsNotNone(payload)
        # 混合场景不能报成 folder：顶层只有一份清单地址，混进普通文件会漏掉
        self.assertEqual(payload["type"], "file_list")
        kinds = {item["file_id"]: item["kind"] for item in payload["files"]}
        self.assertEqual(kinds, {"d1": "folder", "f1": "file"})

        by_id = {item["file_id"]: item for item in payload["files"]}
        # 普通文件直接下
        _, raw = self._download(by_id["f1"]["download_url"])
        self.assertEqual(raw.decode("utf-8"), "记得交作业")

        # 目录走顶层合并清单；普通文件不在清单里，走各自的 download_url
        manifest = self._manifest(payload["manifest_batch_url"])
        self.assertEqual(manifest["count"], 2)
        self.assertEqual(sorted(self._download_folder(manifest)),
                         ["课程资料/子目录/作业.txt", "课程资料/讲义.txt"])


class TestInterruptedDownloadRetry(_SmokeCase):
    """下载断掉之后，重跑一次快捷指令能拿到可用地址。"""

    def setUp(self):
        super().setUp()
        self.folder = self.root / "大目录"
        self.folder.mkdir()
        # 总量 ~4MB：必须大于 socket 发送缓冲，否则客户端 close 之前服务端早就
        # 把内容整个写出去了，造不出真正的断流。
        for i in range(40):
            (self.folder / f"part{i}.txt").write_text("x" * 100_000, encoding="utf-8")
        self.file_server.register_folder("d1", str(self.folder), size=4_000_000)

    def test_keep_for_retry_reannounces(self):
        client = SyncClient({
            "server_host": "127.0.0.1", "server_port": self.center.port,
            "key": KEY, "local_name": PC,
            "file_server_port": self.file_server.port,
        }, self.file_server)
        self.file_server.set_reannounce_handler(client.reannounce_shares)

        self._push(self.file_server.share_entries())
        payload = self._request_file().json()
        self.assertEqual(payload["type"], "folder")

        # 记录已被一次性消费
        self.assertEqual(self._request_file().json()["type"], "text")

        # 模拟下载中断：客户端保留共享并触发重登记
        fired = threading.Event()
        original = self.file_server._reannounce_handler

        def handler():
            fired.set()
            original()

        self.file_server.set_reannounce_handler(handler)
        self.file_server._keep_for_retry("d1", str(self.folder))
        self.assertTrue(fired.wait(5), "下载中断没有触发重新登记")
        # 重登记是后台线程做的，没有完成信号。这里再同步跑一次，把「登记请求
        # 本身对不对」和「线程有没有被唤起」两件事分开验证，避免偶发的时序抖动
        # 让整条用例变红。
        client.reannounce_shares()

        # 重登记是异步的，给服务端一点时间落库
        deadline = time.time() + 10
        payload = None
        while time.time() < deadline:
            payload = self._request_file().json()
            if payload["type"] != "text":
                break
            time.sleep(0.1)
        self.assertNotEqual(payload["type"], "text", "重登记之后仍然拉不到文件")
        self.assertEqual(payload["files"][0]["file_id"], "d1")

        # 重试这次要能真下完（清单还在，逐文件全取到）
        manifest = self._manifest(payload["manifest_batch_url"])
        self.assertEqual(manifest["count"], 40)
        files = self._download_folder(manifest)
        self.assertEqual(len(files), 40)
        self.assertTrue(all(name.startswith("大目录/") and name.endswith(".txt")
                            for name in files), sorted(files)[:3])

    def test_abrupt_client_close_keeps_share(self):
        """真断流：下到一半就掐，共享不能被当成「已下完」删掉。

        这里刻意用**单个大文件**而不是目录：目录现在是逐文件清单，单次响应
        只有一个小文件，8MB 的整块响应才够大 —— 小响应在客户端 close 之前
        就全进了 socket 缓冲，根本造不出失败（实测三次两次不触发）。
        """
        big = self.root / "big.bin"
        big.write_bytes(os.urandom(8 * 1024 * 1024))
        self.file_server.register_file("big1", str(big))
        # 用回调当探针：能证明断流确实走进了「保留以便重试」那条分支，
        # 而不是碰巧共享还没被清理
        fired = threading.Event()
        self.file_server.set_reannounce_handler(fired.set)

        entries = [e for e in self.file_server.share_entries()
                   if e["file_id"] == "big1"]
        self.assertEqual(self._push(entries).status_code, 200)
        entry = self._request_file().json()["files"][0]
        self.assertEqual(entry["file_id"], "big1")

        # 用裸 socket 强制 RST（SO_LINGER=0）。用 requests 的 close() 只关本地
        # 句柄，服务端往往已经把整个 4MB 塞进 socket 缓冲、判定为「发完了」，
        # 造不出真正的断流 —— 实测三次里两次不触发。
        parsed = urlparse(entry["download_url"])
        sock = socket.create_connection((parsed.hostname, parsed.port), timeout=10)
        try:
            sock.sendall(
                f"GET {parsed.path} HTTP/1.1\r\nHost: {parsed.hostname}"
                f":{parsed.port}\r\nkey: {KEY}\r\n"
                f"Connection: close\r\n\r\n".encode()
            )
            sock.recv(2048)                       # 收到响应头 + 一点点正文
            sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
        finally:
            sock.close()                          # 带 RST 关掉，服务端下次写必然失败

        # 服务端要等生成器收尾才判定失败，稍等再看
        self.assertTrue(fired.wait(10), "断流没有触发「保留以便重试」")
        self.assertTrue(self.file_server.running, "断流把整个文件服务带崩了")
        self.assertIn(
            "big1", self.file_server.shared_files,
            "断流被当成「已下完」，共享被删了 —— 重试只会拿到 404",
        )


class TestTextOnly(_SmokeCase):
    """没有文件时退化成文本同步，且只投递一次。"""

    def test_text_pull_once(self):
        self.tracker.latest = {
            "id": "t1", "type": "text", "content": "刚复制的文字", "source": PC,
        }
        self.tracker.data["latest_global"] = self.tracker.latest

        payload = self._request_file().json()
        self.assertEqual(payload["type"], "text")
        self.assertEqual(payload["latest_global"]["content"], "刚复制的文字")

        again = self._request_file().json()
        self.assertTrue(again["latest_global"]["pasted"])
        self.assertNotIn("content", again["latest_global"])


class TestShortcutArtifact(_SmokeCase):
    """快捷指令交付物本身也要能通过冒烟：容器完好、且不含已废弃的打包链路。"""

    UNSIGNED = ROOT / "shortcuts" / "获取同步" / "获取同步-unsigned.plist"
    SIGNED = ROOT / "shortcuts" / "获取同步" / "获取同步.shortcut"

    def _actions(self):
        import plistlib
        if not self.UNSIGNED.exists():
            self.skipTest("还没生成未签名 plist")
        with open(self.UNSIGNED, "rb") as handle:
            return plistlib.load(handle)["WFWorkflowActions"]

    def test_signed_bundle_is_valid(self):
        if not self.SIGNED.exists():
            self.skipTest("还没生成签名产物")
        data = self.SIGNED.read_bytes()
        self.assertGreater(len(data), 10_000)
        self.assertEqual(data[:4], b"AEA1", "不是 apple 归档容器")
        self.assertGreater(data.find(b"bvx2"), 0, "缺少 LZFSE 数据段")

    def test_no_archive_or_polling_left(self):
        """打包/解压/轮询已彻底移除：出现任何一个动作都说明退回了老设计。"""
        actions = self._actions()
        idents = [a["WFWorkflowActionIdentifier"] for a in actions]
        for gone in ("is.workflow.actions.unzip",        # 解压必然摊平目录
                     "is.workflow.actions.delay",         # 只在打包轮询里用过
                     "is.workflow.actions.repeat.count"):
            self.assertNotIn(gone, idents, f"{gone} 不该再出现")
        # 目录必须走合并清单：引用的是 manifest_batch_url（不是单目录的
        # manifest_url —— 后者要遍历每个目录，手机上就得多一层循环）
        self.assertIn("manifest_batch_url", str(actions), "没有引用合并清单地址")

    def test_every_save_has_input_folder_and_subpath(self):
        """「存储到文件」缺任何一项都会静默出问题。

        没有输入 → 不写盘还不报错；没有目标目录 → 写到默认位置；没有子路径
        （或子路径不以 "/" 结尾）→ iOS 会把路径当文件名，目录层级全平。
        """
        for i, action in enumerate(self._actions()):
            if action["WFWorkflowActionIdentifier"] != "is.workflow.actions.documentpicker.save":
                continue
            params = action.get("WFWorkflowActionParameters") or {}
            self.assertTrue((params.get("WFInput") or {}).get("Value"),
                            f"[{i}] 存储到文件没有输入，会静默不写盘")
            self.assertTrue(params.get("WFFolder"),
                            f"[{i}] 存储到文件没指定目标目录")

        # 目录分支那个「存储到文件」必须带子路径，且子路径要接上清单的 prefix。
        # 斜杠必须是**字面字符**：iOS 会对子路径做规范化，变量值结尾的 "/" 会被
        # 吃掉，`目录名/` 于是退化成文件名 `目录名`，一堆文件互相覆盖（v6 真机：
        # 最后只剩一个叫目录名的无扩展名文件）。所以 prefix 由服务端去尾斜杠给出，
        # 斜杠写在快捷指令里。
        folder_tokens = []
        for i, action in enumerate(self._actions()):
            if action["WFWorkflowActionIdentifier"] != "is.workflow.actions.documentpicker.save":
                continue
            token = (action.get("WFWorkflowActionParameters") or {}).get(
                "WFFileDestinationPath")
            if token and "prefix" in json.dumps(token["Value"], ensure_ascii=False):
                folder_tokens.append((i, token))
        self.assertTrue(folder_tokens, "目录分支的子路径没有接上清单的 prefix")
        for i, token in folder_tokens:
            self.assertIn("/", token["Value"]["string"],
                          f"[{i}] prefix 与文件名之间必须是字面斜杠")

        # 子路径存在时必须打开覆盖，否则同一个文件同步两次会变成「a 1.txt」
        for action in self._actions():
            params = action.get("WFWorkflowActionParameters") or {}
            if params.get("WFFileDestinationPath"):
                self.assertTrue(params.get("WFSaveFileOverwrite"),
                                "目录写入没打开覆盖，重复同步会存成「名字 1」")

class TestIOSPollProtocol(_SmokeCase):
    """按快捷指令的「一轮」真打 /ios 接口，验判据够不够用。

    `TestTextSyncArtifact` 只看产物形状，看不出判据对不对 —— 判据是和**服务端
    实现**对齐出来的：`/ios/latest` 与 `/ios/sync` 都走 `_take_ios_latest`，
    只在「这份该投递给本机」时才带 `content`，其余一律 `{pasted: true}`。
    这里用真的 `TextTracker`（不是 `_FakeTracker`）把客户端每轮的动作跑一遍。

    假 tracker 跑不了这条：它没有 `update` / `is_echo_of_delivered`，
    `/ios/sync` 一进来就 AttributeError。
    """

    def setUp(self):
        super().setUp()
        patcher = patch.object(
            text_tracker_mod, "TEXT_LATEST_FILE", self.root / "text_latest.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tracker = text_tracker_mod.TextTracker()
        patcher = patch.object(flask_app, "tracker", self.tracker)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 别真的去动开发机的剪贴板
        patcher = patch.object(flask_app, "copy_text_to_clipboard", MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---- 客户端每轮会做的事 ----

    def _sync(self, content, source=PHONE):
        return requests.post(
            f"{self.center.url}/ios/sync",
            headers={"key": KEY},
            json={"key": KEY, "content": content, "source": source},
            timeout=10,
        ).json()

    def _latest(self, source=PHONE):
        return requests.get(
            f"{self.center.url}/ios/latest",
            headers={"key": KEY}, params={"source": source}, timeout=10,
        ).json()

    def _pc_copy(self, content):
        """电脑端复制了一段文字（走 /text_sync，跟真实客户端一样）。"""
        requests.post(
            f"{self.center.url}/text_sync",
            headers={"key": KEY},
            json={"key": KEY, "content": content, "source": PC}, timeout=10,
        )

    def _round(self, clipboard, cache):
        """照 build.py 的一轮走：变了上行、没变拉取、有 content 且不同才写回。"""
        if clipboard and clipboard != cache:
            payload = self._sync(clipboard)
            cache = clipboard           # 上行成功 = 服务端已知，缓存记成本地
            uploaded = True
        else:
            payload = self._latest()
            uploaded = False

        content = (payload.get("latest_global") or {}).get("content")
        written = None
        if content:
            cache = content
            if content != clipboard:
                written = content
        return cache, written, uploaded

    # ---- 用例 ----

    def test_new_clipboard_uploads_exactly_once(self):
        """手机复制了新东西：只上行一次，之后每轮都是纯拉取。"""
        cache, uploads = "", []
        for i in range(5):
            cache, written, uploaded = self._round("甲", cache)
            self.assertIsNone(written, "刚上行完不该反过来盖手机剪贴板")
            if uploaded:
                uploads.append(i)
        self.assertEqual(uploads, [0], "同一段文字被重复上行")

        latest = self.tracker.get_global_latest()
        self.assertEqual(latest["content"], "甲")
        self.assertEqual(latest["source"], PHONE)

    def test_pc_text_delivered_once(self):
        """电脑复制了文字：手机拉一次、写一次，之后不再重复写。"""
        self._pc_copy("乙")

        cache, written, _ = self._round("甲", "甲")
        self.assertEqual(written, "乙", "电脑的新内容没投递下来")

        cache, written, _ = self._round("乙", cache)
        self.assertIsNone(written, "同一段内容被重复写进剪贴板")

    def test_nothing_new_never_writes(self):
        """服务端没东西时（{pasted: true}）永远不写剪贴板、也不上行。"""
        cache, written = "甲", "甲"
        for _ in range(3):
            cache, written, uploaded = self._round("甲", cache)
            self.assertFalse(uploaded)
            self.assertIsNone(written)

    def test_echo_keeps_original_source(self):
        """手机把电脑给的内容原样回传：不能变成手机的「新内容」。"""
        self._pc_copy("乙")

        cache, written, uploaded = self._round("乙", "丙")
        self.assertTrue(uploaded, "剪贴板和缓存不同就该上行")
        self.assertEqual(self.tracker.get_global_latest()["source"], PC,
                         "回音被当成了手机的新内容")
        self.assertIsNone(written)

    def test_stale_cache_never_overwrites_clipboard(self):
        """缓存旧、剪贴板新、服务端无话可说：不许拿缓存里的旧内容盖回去。

        以前写回用的是「缓存里的 content」，这种局面下会把旧内容盖到用户刚复制
        的东西上；现在只用这一轮响应里的 content，没有就什么都不写。
        """
        self._pc_copy("丁")
        self.tracker.mark_pasted(PHONE, dict(self.tracker.get_global_latest(),
                                             pasted=True))

        cache, written, uploaded = self._round("丁", "甲")
        self.assertTrue(uploaded)
        self.assertIsNone(written, "拿缓存里的旧内容盖了剪贴板")

        # 上行过之后缓存已经是「丁」，下一轮不该再上行一次
        cache, written, uploaded = self._round("丁", cache)
        self.assertFalse(uploaded, "上行过的内容又被上行了一遍")
        self.assertIsNone(written)


class TestTextSyncArtifact(_SmokeCase):
    """「文字同步」是轮询驻留型，交付物的硬约束跟获取同步不一样。

    它必须有循环和等待（获取同步恰恰不许有），但同样不许留调试残渣、不许
    只在某个分支里读缓存（读两次就等于两套状态）。
    """

    UNSIGNED = ROOT / "shortcuts" / "文字同步" / "文字同步-unsigned.plist"
    SIGNED = ROOT / "shortcuts" / "文字同步" / "文字同步.shortcut"

    def _actions(self):
        import plistlib
        if not self.UNSIGNED.exists():
            self.skipTest("还没生成未签名 plist")
        with open(self.UNSIGNED, "rb") as handle:
            return plistlib.load(handle)["WFWorkflowActions"]

    def _idents(self):
        return [a["WFWorkflowActionIdentifier"] for a in self._actions()]

    def test_signed_bundle_is_valid(self):
        if not self.SIGNED.exists():
            self.skipTest("还没生成签名产物")
        data = self.SIGNED.read_bytes()
        self.assertGreater(len(data), 10_000)
        self.assertEqual(data[:4], b"AEA1", "不是 apple 归档容器")
        self.assertGreater(data.find(b"bvx2"), 0, "缺少 LZFSE 数据段")

    def test_poll_loop_and_guard(self):
        """一轮一个「重复」+ 一个「等待」；守卫必须是「任一」的三条件 + 停止。"""
        idents = self._idents()
        self.assertEqual(idents.count("is.workflow.actions.repeat.count"), 2,
                         "重复 应该有开始和结束两条")
        self.assertEqual(idents.count("is.workflow.actions.delay"), 3,
                         "三档等待各一条")
        self.assertIn("is.workflow.actions.exit", idents, "守卫命中要能停下来")

        # 守卫 = 判断对象分别是「电量 / Wi-Fi / 可达性」的那一个多条件块。
        # 按 UUID 反查动作来源，比「三个判断对象互不相同」稳：写回剪贴板那个
        # 「全部为真」也是三个条件，只是同一个对象出现过两次。
        source_of = {}
        for action in self._actions():
            uuid = (action.get("WFWorkflowActionParameters") or {}).get("UUID")
            if uuid:
                source_of[uuid] = action["WFWorkflowActionIdentifier"]

        guards = []
        for action in self._actions():
            conds = (action.get("WFWorkflowActionParameters") or {}).get("WFConditions")
            if not conds:
                continue
            tpls = conds["Value"]["WFActionParameterFilterTemplates"]
            subjects = set()
            for tpl in tpls:
                var = ((tpl.get("WFInput") or {}).get("Variable") or {}).get("Value") or {}
                subjects.add(source_of.get(var.get("OutputUUID")))
            if subjects == {
                "is.workflow.actions.getbatterylevel",
                "is.workflow.actions.getwifi",
                "com.sindresorhus.Actions.IsReachableIntent",
            }:
                guards.append(conds["Value"])
        self.assertTrue(guards, "没找到电量 / Wi-Fi / 可达性 三项任一 的守卫")
        for value in guards:
            self.assertEqual(value.get("WFActionParameterFilterPrefix"), 0,
                             "守卫必须是「任一」（prefix=0）")

    def test_existence_conditions_use_table_form(self):
        """「有任何值 / 没有任何值」只能是表模板形态，不能写成单条件。

        iOS 对 100/101 这两种条件只认 `WFConditions`（内容谓词表）形态；写成
        单条件的 `WFCondition + WFInput`，导入后动作会标红报「请为此操作中的
        每个参数选取值」，运行到那里直接中断。原版里这几条也是表模板。
        """
        offenders = []
        for index, action in enumerate(self._actions()):
            if action["WFWorkflowActionIdentifier"] != "is.workflow.actions.conditional":
                continue
            params = action.get("WFWorkflowActionParameters") or {}
            if params.get("WFControlFlowMode") != 0:
                continue
            if params.get("WFCondition") in (100, 101):
                offenders.append(index)
        self.assertFalse(
            offenders, f"这些「如果」把存在性判断写成了单条件（会标红）：{offenders}")

    def test_app_existence_check_coerces_to_text(self):
        """「如果 当前App 没有任何值」的被判断对象必须先转成文本。

        获取当前App 的输出是 App 类型的内容项，而 iOS 的条件菜单对 App 类型
        没有「没有值」这一项 —— 选中就标红报「请为此操作中的每个参数选取值」。
        真机上确认过：手动把类型改成文本才能选「没有值」。因此在判空之前必须挂
        `WFStringContentItem` 强转（原版动作 22 也是这么写的）。
        """
        source_of = {}
        for action in self._actions():
            uuid = (action.get("WFWorkflowActionParameters") or {}).get("UUID")
            if uuid:
                source_of[uuid] = action["WFWorkflowActionIdentifier"]

        checked = 0
        for action in self._actions():
            if action["WFWorkflowActionIdentifier"] != "is.workflow.actions.conditional":
                continue
            params = action.get("WFWorkflowActionParameters") or {}
            if params.get("WFControlFlowMode") != 0:
                continue
            table = ((params.get("WFConditions") or {}).get("Value") or {})
            tpls = table.get("WFActionParameterFilterTemplates") or []
            if not tpls and params.get("WFCondition") is not None:
                tpls = [params]
            for tpl in tpls:
                if tpl.get("WFCondition") not in (100, 101):
                    continue
                value = ((tpl.get("WFInput") or {}).get("Variable") or {}).get("Value") or {}
                if source_of.get(value.get("OutputUUID")) != "is.workflow.actions.getcurrentapp":
                    continue
                checked += 1
                classes = [a.get("CoercionItemClass")
                           for a in value.get("Aggrandizements") or []]
                self.assertIn(
                    "WFStringContentItem", classes,
                    "App 类型判空没转文本，真机会标红「请为每个参数选取值」")
        self.assertTrue(checked, "没找到对当前 App 的判空动作")

    def test_app_whitelist_allows_shortcuts_app(self):
        """白名单里必须有「快捷指令」。

        手动在快捷指令 App 里点运行时，前台 App 就是「快捷指令」；不在名单里会
        被白名单整段挡掉 —— 表现是「点了没反应、日志一个字都没有」，很容易误判
        成快捷指令本身坏了。
        """
        found = []
        for action in self._actions():
            if action["WFWorkflowActionIdentifier"] != "is.workflow.actions.conditional":
                continue
            params = action.get("WFWorkflowActionParameters") or {}
            table = ((params.get("WFConditions") or {}).get("Value") or {})
            tpls = table.get("WFActionParameterFilterTemplates") or []
            strings = {t.get("WFConditionalActionString") for t in tpls
                       if isinstance(t.get("WFConditionalActionString"), str)}
            if "快捷指令" in strings and len(tpls) >= 5:
                found.append(tpls)
        self.assertTrue(found, "白名单里没有「快捷指令」，手动运行会被整段挡掉")

    def test_per_round_app_check_is_opt_in(self):
        """循环里读「当前 App」必须是可选项，默认不读。

        iOS 26/27 会把一次运行里读到的「app」内容项算进「允许发送给 <主机> 吗」
        这个弹窗；按内容项实例逐次评估，于是**同一轮循环里每轮都弹**，点「始终
        允许」也只对当次有效。所以默认「前台检查 = 仅入口」，循环里的那次读取
        必须被「如果 配置.前台检查 是 每轮」包住，改回每轮是用户的显式选择。
        """
        actions = self._actions()
        start = end = gid = None
        for index, action in enumerate(actions):
            if action["WFWorkflowActionIdentifier"] != "is.workflow.actions.conditional":
                continue
            params = action.get("WFWorkflowActionParameters") or {}
            if (params.get("WFConditionalActionString") == "每轮"
                    and params.get("WFControlFlowMode") == 0
                    and "前台检查" in str(params.get("WFInput"))):
                start, gid = index, params.get("GroupingIdentifier")
                break
        self.assertIsNotNone(
            start, "没有「如果 配置.前台检查 是 每轮」的分支，循环里的 App 判空会每轮都跑")
        for index, action in enumerate(actions):
            params = action.get("WFWorkflowActionParameters") or {}
            if (action["WFWorkflowActionIdentifier"] == "is.workflow.actions.conditional"
                    and params.get("GroupingIdentifier") == gid
                    and params.get("WFControlFlowMode") == 2):
                end = index
        self.assertIsNotNone(end, "「前台检查」分支没有结束标记")

        reads = [i for i, a in enumerate(actions)
                 if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.getcurrentapp"]
        self.assertGreaterEqual(len(reads), 1, "入口白名单必须读当前 App")
        stray = [i for i in reads[1:] if not (start < i < end)]
        self.assertFalse(
            stray, f"循环里有没被「前台检查」包住的「获取当前App」（会每轮弹窗）：{stray}")

    def test_cache_read_and_write_once(self):
        """缓存只在开头读一次、每轮写一次；读写必须用同一个键。"""
        reads, writes = [], []
        for action in self._actions():
            ident = action["WFWorkflowActionIdentifier"]
            params = action.get("WFWorkflowActionParameters") or {}
            if ident == "is.workflow.actions.getstoredcontent":
                reads.append(params.get("WFStoredContentKey"))
            elif ident == "is.workflow.actions.setstoredcontent":
                writes.append(params.get("WFStoredContentKey"))
        self.assertEqual(len(reads), 1, "缓存读一次就够，读两次等于两套状态")
        self.assertGreaterEqual(len(writes), 1, "缓存压根没落盘，下次运行会重新上行一遍")
        self.assertEqual(set(writes), set(reads), "读写用的不是同一个键")

    def test_cache_is_plain_text(self):
        """缓存只存一段文本，不再按词典取 content / pasted。

        原版把整份 latest_global 存进缓存，等于在手机上又养了一份 pasted 状态，
        跟服务端的 clients[source] 各自漂移。判断只需要「内容」这一项。
        """
        def walk(node):
            if isinstance(node, dict):
                if node.get("Type") == "Variable" and node.get("VariableName") == "缓存":
                    keys = [a.get("DictionaryKey")
                            for a in node.get("Aggrandizements") or []
                            if a.get("DictionaryKey")]
                    self.assertFalse(keys, f"缓存被当成词典用了: {keys}")
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self._actions())

    def test_writeback_ignores_pasted(self):
        """写回判据只有「有内容 + 和剪贴板不同」，不拿 pasted 跟中文「否」比。

        服务端带 content 时 pasted 恒为 false，「已投递过」的情况根本不带
        content —— 第三条已经由「有 content」覆盖；而拿 JSON 布尔去比字符串
        「否」是全流程最脆的一处（空值/类型一变就静默失效）。
        """
        all_of, strings = [], []
        for action in self._actions():
            conds = (action.get("WFWorkflowActionParameters") or {}).get("WFConditions")
            if not conds:
                continue
            value = conds["Value"]
            for tpl in value["WFActionParameterFilterTemplates"]:
                if "WFConditionalActionString" in tpl:
                    strings.append(tpl["WFConditionalActionString"])
            if value.get("WFActionParameterFilterPrefix") == 1:
                all_of.append(value)

        self.assertNotIn("否", strings, "还在拿 pasted 跟「否」比")
        self.assertEqual(len(all_of), 1, "「全部为真」只用来判写回，不该有第二处")
        self.assertEqual(len(all_of[0]["WFActionParameterFilterTemplates"]), 2,
                         "写回该是两条：有内容 + 和剪贴板不同")

    def test_no_debug_residue_and_all_requests_signed(self):
        idents = self._idents()
        self.assertNotIn("is.workflow.actions.showresult", idents,
                         "调试残渣会每次运行弹一张卡片")
        for i, action in enumerate(self._actions()):
            if action["WFWorkflowActionIdentifier"] != "is.workflow.actions.downloadurl":
                continue
            headers = json.dumps(
                (action.get("WFWorkflowActionParameters") or {}).get("WFHTTPHeaders")
                or {}, ensure_ascii=False)
            self.assertIn('"key"', headers, f"[{i}] 请求没带密钥头")


class TestSmsSyncArtifact(_SmokeCase):
    """「短信同步」是事件型：一条短信一次，不许有循环、不许等。

    它最容易退化的地方不是形状而是**判据**：正则换成一份手机上跑的、电脑上
    没验过的（这里锁住产物里的模式就是 `lib/sms.py` 那两条），以及推送给谁 ——
    上一版手搭的草稿把「推给电脑」写成了「运行另一条快捷指令」，产物里因此
    一条 `/ios/sync` 都没有。两条都写成断言。
    """

    UNSIGNED = ROOT / "shortcuts" / "短信同步" / "短信同步-unsigned.plist"
    SIGNED = ROOT / "shortcuts" / "短信同步" / "短信同步.shortcut"
    PENDING = "验证码待发"

    def _doc(self):
        import plistlib
        if not self.UNSIGNED.exists():
            self.skipTest("还没生成未签名 plist")
        with open(self.UNSIGNED, "rb") as handle:
            return plistlib.load(handle)

    def _actions(self):
        return self._doc()["WFWorkflowActions"]

    def _idents(self):
        return [a["WFWorkflowActionIdentifier"] for a in self._actions()]

    def test_signed_bundle_is_valid(self):
        if not self.SIGNED.exists():
            self.skipTest("还没生成签名产物")
        data = self.SIGNED.read_bytes()
        self.assertGreater(len(data), 10_000)
        self.assertEqual(data[:4], b"AEA1", "不是 apple 归档容器")
        self.assertGreater(data.find(b"bvx2"), 0, "缺少 LZFSE 数据段")

    def test_patterns_are_the_verified_ones(self):
        """产物里的正则必须就是 lib/sms.py 那两条（电脑上跑过用例的）。"""
        import re
        import sys
        sys.path.insert(0, str(ROOT / "shortcuts"))
        from lib.sms import PATTERN_A, PATTERN_B

        got = [(a.get("WFWorkflowActionParameters") or {}).get("WFMatchTextPattern")
               for a in self._actions()
               if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.text.match"]
        self.assertEqual(got, [PATTERN_A, PATTERN_B],
                         "产物里的正则和 lib/sms.py 不一致，先重跑 build.py")
        for pattern in got:
            # 两个分支都靠「获取匹配中的组」默认取组 1，多一个捕获组就取错东西
            self.assertEqual(re.compile(pattern).groups, 1,
                             f"模式里有多个捕获组：{pattern[:40]}…")

    def test_event_driven_no_loop_no_wait(self):
        idents = self._idents()
        self.assertNotIn("is.workflow.actions.repeat.count", idents,
                         "事件型不该有循环：一条短信跑一次")
        self.assertNotIn("is.workflow.actions.delay", idents,
                         "事件型不该等待")
        self.assertNotIn("is.workflow.actions.runworkflow", idents,
                         "不许再靠「运行另一条快捷指令」转发（草稿的毛病）")

    def test_push_hits_ios_sync(self):
        """推送必须是 POST /ios/sync，body 带 key / content / source。"""
        syncs = []
        for index, action in enumerate(self._actions()):
            if action["WFWorkflowActionIdentifier"] != "is.workflow.actions.downloadurl":
                continue
            params = action.get("WFWorkflowActionParameters") or {}
            if params.get("WFHTTPMethod") != "POST":
                continue
            items = ((params.get("WFJSONValues") or {}).get("Value") or {})
            keys = [((item.get("WFKey") or {}).get("Value") or {}).get("string")
                    for item in items.get("WFDictionaryFieldValueItems") or []]
            if "content" in keys:
                syncs.append((index, keys, json.dumps(
                    params.get("WFURL") or {}, ensure_ascii=False)))
        self.assertEqual(len(syncs), 2, "该有两条：先补推暂存的，再推这一条")
        for index, keys, url in syncs:
            self.assertEqual(sorted(keys), ["content", "key", "source"],
                             f"[{index}] /ios/sync 的 body 字段不对：{keys}")
            self.assertIn("同步", url, f"[{index}] 地址取自「接口.同步」")

    def test_pending_retry_reads_before_first_push(self):
        """暂存要在补推之前读出来，否则「不可达就下次补」只是写在文档里。"""
        stored_get = None
        first_push = None
        for index, action in enumerate(self._actions()):
            params = action.get("WFWorkflowActionParameters") or {}
            ident = action["WFWorkflowActionIdentifier"]
            if (ident == "is.workflow.actions.getstoredcontent"
                    and params.get("WFStoredContentKey") == self.PENDING
                    and stored_get is None):
                stored_get = index
            if (ident == "is.workflow.actions.downloadurl"
                    and params.get("WFHTTPMethod") == "POST"
                    and "content" in json.dumps(params.get("WFJSONValues") or {},
                                                ensure_ascii=False)
                    and first_push is None):
                first_push = index
        self.assertIsNotNone(stored_get, f"没读「{self.PENDING}」")
        self.assertIsNotNone(first_push, "没找到推送动作")
        self.assertLess(stored_get, first_push, "补推之前没先读暂存")

    def test_input_variable_is_declared(self):
        """正文取自「快捷指令输入」：不声明 HasShortcutInputVariables 就取不到值。"""
        doc = self._doc()
        self.assertTrue(doc.get("WFWorkflowHasShortcutInputVariables"))
        self.assertIn("ExtensionInput", json.dumps(doc, ensure_ascii=False),
                      "没有引用「快捷指令输入」")

    def test_existence_conditions_use_table_form(self):
        """「有任何值 / 没有任何值」只能是表模板形态，否则导入后标红中断。"""
        offenders = []
        for index, action in enumerate(self._actions()):
            if action["WFWorkflowActionIdentifier"] != "is.workflow.actions.conditional":
                continue
            params = action.get("WFWorkflowActionParameters") or {}
            if params.get("WFControlFlowMode") != 0:
                continue
            if params.get("WFCondition") in (100, 101):
                offenders.append(index)
        self.assertFalse(offenders, f"存在性判断写成了单条件（会标红）：{offenders}")


class TestPushSyncArtifact(_SmokeCase):
    """「推送同步」是手动一次型：分享文件 / 推剪贴板，不轮询。

    它最容易退化的地方，全在原版手工包踩过的那几个坑上：

    * 分享入口 —— `WFWorkflowTypes` 少了 `ActionExtension`，这条指令就不会出现
      在分享面板里，而「分享文件」正是它的主要用法；
    * 上传形状 —— `WFHTTPBodyType = File` + `WFRequestVariable` 才是「请求体是
      文件」，写成 JSON 字段的话服务端收到空 body，表现成「文件上传成功 0 字节」；
    * 死守卫 —— 原版注释写着「没有当前 App 就停止」，可那个「获取当前App」的输出
      一次都没被引用；
    * 两份重复的上传代码 —— 改一处忘一处，文件名规则都不一致。
    """

    UNSIGNED = ROOT / "shortcuts" / "推送同步" / "推送同步-unsigned.plist"
    SIGNED = ROOT / "shortcuts" / "推送同步" / "推送同步.shortcut"

    def _doc(self):
        import plistlib
        if not self.UNSIGNED.exists():
            self.skipTest("还没生成未签名 plist")
        with open(self.UNSIGNED, "rb") as handle:
            return plistlib.load(handle)

    def _actions(self):
        return self._doc()["WFWorkflowActions"]

    def _params(self, action):
        return action.get("WFWorkflowActionParameters") or {}

    def _requests(self, method=None):
        """[(下标, 参数)]：所有出网动作，可按方法过滤。"""
        rows = []
        for index, action in enumerate(self._actions()):
            if action["WFWorkflowActionIdentifier"] != "is.workflow.actions.downloadurl":
                continue
            params = self._params(action)
            if method is None or params.get("WFHTTPMethod", "GET") == method:
                rows.append((index, params))
        return rows

    def test_signed_bundle_is_valid(self):
        if not self.SIGNED.exists():
            self.skipTest("还没生成签名产物")
        data = self.SIGNED.read_bytes()
        self.assertGreater(len(data), 10_000)
        self.assertEqual(data[:4], b"AEA1", "不是 apple 归档容器")
        self.assertGreater(data.find(b"bvx2"), 0, "缺少 LZFSE 数据段")

    def test_share_sheet_entry_is_declared(self):
        """分享面板入口 + 快捷指令输入变量，两个都得声明。"""
        doc = self._doc()
        self.assertIn("ActionExtension", doc.get("WFWorkflowTypes") or [],
                      "没有 ActionExtension 就不会出现在分享面板里")
        self.assertTrue(doc.get("WFWorkflowHasShortcutInputVariables"))
        self.assertIn("ExtensionInput", json.dumps(doc, ensure_ascii=False),
                      "没有引用「快捷指令输入」，分享进来的文件拿不到")

    def test_put_carries_file_body_not_json(self):
        """上传必须是 PUT + File 请求体，而不是把文件塞进 JSON 字段。"""
        puts = self._requests("PUT")
        self.assertEqual(len(puts), 1, f"上传动作该只有一个：{len(puts)}")
        index, params = puts[0]
        self.assertEqual(params.get("WFHTTPBodyType"), "File",
                         f"[{index}] 少了 WFHTTPBodyType=File，服务端会收到空 body")
        self.assertIn("WFRequestVariable", params,
                      f"[{index}] 少了 WFRequestVariable，不知道该传哪个文件")
        url = json.dumps(params.get("WFURL") or {}, ensure_ascii=False)
        self.assertIn("?filename=", url,
                      f"[{index}] 文件名没拼进查询串，服务端只能存成 uploaded_file")

    def test_filename_is_url_encoded(self):
        """文件名要过一遍 URL 编码：中文、空格、# 不编码会被查询串截断。

        URL 里拼的是变量（先「设定变量 → 名称」再拼进去），所以要顺着
        「名称」这个变量反查一次：它的赋值源必须是 URL 编码动作的输出。
        """
        uuids = {(self._params(a).get("UUID")): a["WFWorkflowActionIdentifier"]
                 for a in self._actions() if self._params(a).get("UUID")}
        index, params = self._requests("PUT")[0]
        value = (params.get("WFURL") or {}).get("Value") or {}
        string = value.get("string") or ""
        fname_at = string.find("?filename=") + len("?filename=")
        slot = value.get("attachmentsByRange") or {}
        self.assertIn(f"{{{fname_at}, 1}}", slot,
                      f"[{index}] 查询串里 filename 的位置不对：{string!r}")
        name = slot[f"{{{fname_at}, 1}}"].get("VariableName")
        self.assertTrue(name, f"[{index}] filename 不是变量：{slot}")

        sources = []
        for action in self._actions():
            p = self._params(action)
            if (action["WFWorkflowActionIdentifier"] == "is.workflow.actions.setvariable"
                    and p.get("WFVariableName") == name):
                ref = (p.get("WFInput") or {}).get("Value") or {}
                sources.append(uuids.get(ref.get("OutputUUID")))
        self.assertEqual(sources, ["is.workflow.actions.urlencode"],
                         f"变量「{name}」的赋值源不是 URL 编码：{sources}")

    def test_filename_keeps_extension_and_chinese(self):
        """文件名要带后缀，且中文不能经过「文本」动作中转。

        真机两轮反馈各锁一条：`urlencode(文件)` 会丢后缀（a.png → a）；而为了
        补后缀把文件变量先塞进「文本」动作，中文会被按 GBK 读成乱码
        （「证书文件」→「璇佷功鏂囦欢」）。所以正解是：名称直接编码文件变量，
        纯 ASCII 的后缀单独拼在 URL 末尾。
        """
        acts = self._actions()
        props = [self._params(a) for a in acts
                 if a["WFWorkflowActionIdentifier"]
                 == "is.workflow.actions.properties.files"]
        self.assertEqual(len(props), 1, "该只有一处取扩展名的动作")
        self.assertEqual(props[0].get("WFContentItemPropertyName"),
                         "File Extension", "取错了文件属性")

        uuids = {self._params(a).get("UUID"): a for a in acts
                 if self._params(a).get("UUID")}

        def setvar_rows(name):
            return [a for a in acts
                    if a["WFWorkflowActionIdentifier"]
                    == "is.workflow.actions.setvariable"
                    and self._params(a).get("WFVariableName") == name]

        def source_dumps(row):
            uid = (((self._params(row).get("WFInput") or {})
                    .get("Value") or {}).get("OutputUUID"))
            src = uuids.get(uid)
            return json.dumps(self._params(src), ensure_ascii=False) if src else ""

        dots = setvar_rows("点后缀")
        self.assertTrue(dots and any("扩展名" in source_dumps(a) for a in dots),
                        "「点后缀」的来源里没有引用取回来的扩展名")
        self.assertFalse(setvar_rows("完整名"),
                         "不该有「完整名」中转变量：文件变量经「文本」会乱码中文")

        # 名称必须直接 URL 编码「文件」变量本身，且显式 Encode 模式
        name = setvar_rows("名称")
        self.assertEqual(len(name), 1)
        enc = uuids.get((((self._params(name[0]).get("WFInput") or {})
                          .get("Value") or {}).get("OutputUUID")))
        self.assertEqual(enc and enc["WFWorkflowActionIdentifier"],
                         "is.workflow.actions.urlencode")
        enc_params = self._params(enc)
        self.assertEqual(enc_params.get("WFEncodeMode"), "Encode",
                         "URL 编码没显式指定 Encode 模式")
        self.assertIn('"VariableName": "文件"',
                      json.dumps(enc_params, ensure_ascii=False),
                      "URL 编码的输入不是文件变量本身（经文本中转会把中文读成乱码）")

        # URL 里 filename 后面必须同时有「名称」和「点后缀」两个占位
        _, params = self._requests("PUT")[0]
        value = (params.get("WFURL") or {}).get("Value") or {}
        string = value.get("string") or ""
        slots = value.get("attachmentsByRange") or {}
        at = string.find("?filename=") + len("?filename=")
        self.assertEqual(slots.get("{%d, 1}" % at, {}).get("VariableName"),
                         "名称", "filename 第一段不是编码后的名称")
        self.assertEqual(slots.get("{%d, 1}" % (at + 1), {}).get("VariableName"),
                         "点后缀", "filename 后面没拼上扩展名后缀")

    def test_text_notification_uses_character_count(self):
        """文字通知里的「N 字」必须来自 Characters 计数，别再显示「1 字」。"""
        acts = self._actions()
        char_counts = {self._params(a).get("UUID") for a in acts
                       if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.count"
                       and self._params(a).get("WFCountType") == "Characters"}
        self.assertTrue(char_counts, "没有 Characters 计数，字数会退化成项目数 1")
        srcs = []
        for a in acts:
            p = self._params(a)
            if (a["WFWorkflowActionIdentifier"] == "is.workflow.actions.setvariable"
                    and p.get("WFVariableName") == "字数"):
                srcs.append(((p.get("WFInput") or {}).get("Value") or {})
                            .get("OutputUUID"))
        self.assertTrue(srcs and set(srcs) <= char_counts,
                        f"「字数」不是取自 Characters 计数：{srcs}")
        bodies = [json.dumps(self._params(a), ensure_ascii=False) for a in acts
                  if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.notification"]
        self.assertTrue(any("字数" in body for body in bodies),
                        "结果通知里没有引用「字数」")

    def test_text_goes_through_ios_sync(self):
        """文字走 POST /ios/sync，body 只有 key / content / source。"""
        posts = []
        for index, params in self._requests("POST"):
            items = ((params.get("WFJSONValues") or {}).get("Value") or {})
            keys = [((item.get("WFKey") or {}).get("Value") or {}).get("string")
                    for item in items.get("WFDictionaryFieldValueItems") or []]
            if "content" in keys:
                posts.append((index, keys, json.dumps(params.get("WFURL") or {},
                                                      ensure_ascii=False)))
        self.assertEqual(len(posts), 1, f"文字推送该只有一处：{len(posts)}")
        index, keys, url = posts[0]
        self.assertEqual(sorted(keys), ["content", "key", "source"],
                         f"[{index}] /ios/sync 的 body 字段不对：{keys}")
        self.assertIn("文字", url, f"[{index}] 地址应取自「接口.文字」")

    def test_upload_happens_once_per_item(self):
        """只有一处循环、一处上传 —— 别再退化成两段复制的粘贴代码。"""
        opens = [a for a in self._actions()
                 if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.repeat.each"
                 and self._params(a).get("WFControlFlowMode") == 0]
        self.assertEqual(len(opens), 1,
                         "上传循环该只有一处（原版复制了两份，文件名规则还不一样）")
        self.assertEqual(len(self._requests("PUT")), 1, "上传动作该只有一处")

    def test_no_dead_app_guard(self):
        """原版那个「获取当前App」守卫没接线，别把它当成有效逻辑抄回来。"""
        idents = [a["WFWorkflowActionIdentifier"] for a in self._actions()]
        self.assertNotIn("is.workflow.actions.getcurrentapp", idents,
                         "手动一次型不需要看前台 App；要看就接到「如果」上")

    def _log_posts(self):
        """往 /ios_log 推日志的那些 POST，不算「出网推内容」。"""
        rows = []
        for index, params in self._requests("POST"):
            items = ((params.get("WFJSONValues") or {}).get("Value") or {})
            keys = [((item.get("WFKey") or {}).get("Value") or {}).get("string")
                    for item in items.get("WFDictionaryFieldValueItems") or []]
            if keys == ["log"]:
                rows.append(index)
        return rows

    def test_every_request_carries_key(self):
        """所有出网请求都得带 `key` 请求头。

        服务端 `/ios_log`、`/ios/sync`、`/clients/online` 和目标设备的
        `/upload_file` 四处都读 `request.headers['key']`（`/status` 探活不校验，
        但它是 Actions 的 App Intent，不在 downloadurl 里）。漏在
        `/clients/online` 上最阴：弹出来的是「服务端拒绝了这次请求 / 密钥错误」，
        看着像配置写错了，实际是那条 GET 压根没带钥匙，文件分支整个走不通。
        """
        missing = []
        for index, params in self._requests():
            items = (((params.get("WFHTTPHeaders") or {}).get("Value") or {})
                     .get("WFDictionaryFieldValueItems") or [])
            keys = [((item.get("WFKey") or {}).get("Value") or {}).get("string")
                    for item in items]
            if "key" not in keys:
                missing.append((index, params.get("WFHTTPMethod", "GET"), keys))
        self.assertFalse(missing, f"这些出网请求没带 key 请求头：{missing}")

    def test_probe_before_any_push(self):
        """探活必须在出网动作之前，空内容检查又要在探活之前。"""
        idents = [a["WFWorkflowActionIdentifier"] for a in self._actions()]
        probe = idents.index("com.sindresorhus.Actions.IsReachableIntent")
        noise = set(self._log_posts())
        net = [index for index, _ in self._requests() if index not in noise]
        self.assertLess(probe, min(net), "探活排到了出网动作后面")

        empty_check = None
        for index, action in enumerate(self._actions()):
            if action["WFWorkflowActionIdentifier"] != "is.workflow.actions.conditional":
                continue
            tpls = ((self._params(action).get("WFConditions") or {}).get("Value") or {})
            for tpl in tpls.get("WFActionParameterFilterTemplates") or []:
                if tpl.get("WFCondition") == 101:
                    empty_check = index
                    break
            if empty_check is not None:
                break
        self.assertIsNotNone(empty_check, "没有「内容没有任何值」的判空")
        self.assertLess(empty_check, probe, "先探活再判空，等于白跑一次网络")

    def test_existence_conditions_use_table_form(self):
        """「有任何值 / 没有任何值」只能是表模板形态，否则导入后标红中断。"""
        offenders = []
        for index, action in enumerate(self._actions()):
            if action["WFWorkflowActionIdentifier"] != "is.workflow.actions.conditional":
                continue
            params = self._params(action)
            if params.get("WFControlFlowMode") != 0:
                continue
            if params.get("WFCondition") in (100, 101):
                offenders.append(index)
        self.assertFalse(offenders, f"存在性判断写成了单条件（会标红）：{offenders}")


if __name__ == "__main__":
    unittest.main()
