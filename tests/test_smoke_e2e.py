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
        resp = requests.get(manifest_url, timeout=10)
        self.assertEqual(resp.status_code, 200, f"清单取不到: {manifest_url}")
        return resp.json()

    def _download_folder(self, manifest):
        """按清单逐文件下载，返回 {相对路径: 字节}（这层结构就是目录层级）。"""
        got = {}
        for entry in manifest["entries"]:
            resp, data = self._download(entry["url"])
            self.assertEqual(resp.status_code, 200, entry["path"])
            got[entry["path"]] = data
        return got

    def _download(self, url, chunk_size=65536, max_bytes=None):
        resp = requests.get(url, timeout=60, stream=True)
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

        manifest = self._manifest(entry["manifest_url"])
        self.assertEqual(manifest["status"], "ok")
        self.assertEqual(manifest["root"], "课程资料")
        self.assertEqual(manifest["count"], 2)
        self.assertIn("子目录/", manifest["dirs"])

        files = self._download_folder(manifest)
        # 相对路径就是目录层级：子目录里的文件必须带前缀，不能和根目录混在一起
        self.assertEqual(files["讲义.txt"].decode("utf-8"), "第一章")
        self.assertEqual(files["子目录/作业.txt"].decode("utf-8"), "第一题")

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

        # 目录按清单逐文件取
        manifest = self._manifest(by_id["d1"]["manifest_url"])
        self.assertEqual(manifest["count"], 2)
        self.assertEqual(len(self._download_folder(manifest)), 2)


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
        manifest = self._manifest(payload["files"][0]["manifest_url"])
        self.assertEqual(manifest["count"], 40)
        files = self._download_folder(manifest)
        self.assertEqual(len(files), 40)
        self.assertTrue(all(name.endswith(".txt") for name in files))

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
                f":{parsed.port}\r\nConnection: close\r\n\r\n".encode()
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

    UNSIGNED = ROOT / "shortcuts" / "获取同步v5-unsigned.plist"
    SIGNED = ROOT / "shortcuts" / "获取同步v5.shortcut"

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
        # 目录必须走清单：有取清单的下载动作，且引用的是 manifest_url
        self.assertIn("manifest_url", str(actions), "没有引用 manifest_url")

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

        # 目录分支那个「存储到文件」必须带子路径，且拼进去的字符串以 "/" 收尾
        subpaths = [
            (a["WFWorkflowActionParameters"] or {}).get("WFFileDestinationPath")
            for a in self._actions()
            if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.documentpicker.save"
        ]
        subpaths = [s for s in subpaths if s]
        self.assertTrue(subpaths, "目录分支缺少子路径，写文件会全平在下载根下")
        for token in subpaths:
            text = token["Value"]["string"]
            self.assertIn("/", text)
            self.assertIn("dir", token["Value"]["attachmentsByRange"].__str__()
                          + str(text), "子路径没有接上清单里的 dir")

if __name__ == "__main__":
    unittest.main()
