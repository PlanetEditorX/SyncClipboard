import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from common.update_apply import (
    UpdateError,
    extract_package,
    find_payload_root,
    resolve_app_dir,
)
from common.updater import (
    APP_VERSION,
    RELEASES_API,
    UpdateCancelled,
    UpdateSource,
    check_for_update,
    download_file,
    is_newer,
    mirror_url,
    parse_version,
    select_windows_asset,
)


class TestVersionComparison(unittest.TestCase):
    def test_parse_strips_prefix_and_build_suffix(self):
        self.assertEqual(parse_version("v1.7.0"), (1, 7, 0))
        self.assertEqual(parse_version("1.7.0"), (1, 7, 0))
        self.assertEqual(parse_version("v1.8.0-beta.2"), (1, 8, 0))

    def test_parse_pads_missing_segments(self):
        self.assertEqual(parse_version("v1.7"), (1, 7, 0))

    def test_parse_empty_value(self):
        self.assertEqual(parse_version(""), (0, 0, 0))
        self.assertEqual(parse_version(None), (0, 0, 0))

    def test_app_version_is_a_well_formed_release_version(self):
        self.assertEqual(parse_version(APP_VERSION), tuple(int(p) for p in APP_VERSION.split(".")))

    def test_numeric_segments_compare_correctly(self):
        self.assertTrue(is_newer("v1.7.10", "v1.7.9"))
        self.assertFalse(is_newer("v1.7.9", "v1.7.10"))
        self.assertFalse(is_newer("v1.7.0", "v1.7.0"))
        self.assertFalse(is_newer("v1.6.9", "v1.7.0"))


class TestCheckForUpdate(unittest.TestCase):
    @staticmethod
    def _response(payload):
        response = MagicMock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        return response

    def test_reports_update_when_tag_is_newer(self):
        payload = {
            "tag_name": "v1.8.0",
            "html_url": "https://github.com/PlanetEditorX/SyncClipboard/releases/tag/v1.8.0",
            "assets": [
                {
                    "name": "SyncClipboard-Windows-v1.8.0.zip",
                    "browser_download_url": "https://example.com/win.zip",
                }
            ],
        }
        with patch("common.updater.requests.get", return_value=self._response(payload)) as get:
            result = check_for_update("1.7.0")

        self.assertEqual(get.call_args.args[0], RELEASES_API)
        self.assertTrue(result.has_update)
        self.assertEqual(result.current_version, "1.7.0")
        self.assertEqual(result.latest_version, "1.8.0")
        self.assertEqual(result.download_url, "https://example.com/win.zip")
        self.assertIsNone(result.error)

    def test_reports_no_update_when_tag_matches(self):
        payload = {"tag_name": "v1.7.0", "html_url": "https://example.com/tag"}
        with patch("common.updater.requests.get", return_value=self._response(payload)):
            result = check_for_update("1.7.0")

        self.assertFalse(result.has_update)
        self.assertEqual(result.release_url, "https://example.com/tag")

    def test_network_failure_is_reported_without_raising(self):
        with patch("common.updater.requests.get", side_effect=requests.RequestException("boom")):
            result = check_for_update("1.7.0")

        self.assertFalse(result.has_update)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.current_version, "1.7.0")

    def test_missing_tag_is_reported_as_error(self):
        with patch("common.updater.requests.get", return_value=self._response({"assets": []})):
            result = check_for_update("1.7.0")

        self.assertFalse(result.has_update)
        self.assertIsNotNone(result.error)


class TestDownloadAndApply(unittest.TestCase):
    @staticmethod
    def _stream_response(chunks):
        response = MagicMock()
        response.headers = {"Content-Length": str(sum(len(chunk) for chunk in chunks))}
        response.iter_content.return_value = chunks
        response.raise_for_status.return_value = None
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        return response

    def test_select_windows_asset_ignores_other_platforms(self):
        assets = [
            {"name": "SyncClipboard-Android-v1.8.0.apk", "browser_download_url": "apk"},
            {"name": "SyncClipboard-Windows-v1.8.0.zip", "browser_download_url": "win"},
        ]
        self.assertEqual(select_windows_asset(assets), "win")
        self.assertIsNone(select_windows_asset([]))
        self.assertIsNone(select_windows_asset(None))

    def test_download_file_writes_stream_and_reports_progress(self):
        chunks = [b"abcde", b"fghij"]
        progress = []
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "update.zip"
            with patch("common.updater.requests.get", return_value=self._stream_response(chunks)):
                written = download_file(
                    "https://example.com/win.zip", target, progress=lambda done, total: progress.append(done)
                )
            self.assertEqual(written, 10)
            self.assertEqual(target.read_bytes(), b"abcdefghij")
        self.assertEqual(progress[-1], 10)

    def test_find_payload_root_locates_nested_executable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "SyncClipboard"
            nested.mkdir()
            (nested / "SyncClipboard.exe").write_bytes(b"exe")
            self.assertEqual(find_payload_root(root), nested)

    def test_find_payload_root_uses_top_level_executable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "SyncClipboard.exe").write_bytes(b"exe")
            self.assertEqual(find_payload_root(root), root)

    def test_find_payload_root_without_executable_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(UpdateError):
                find_payload_root(Path(temp_dir))

    def test_extract_package_reads_nested_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            zip_path = root / "update.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("SyncClipboard/SyncClipboard.exe", "exe")

            payload = extract_package(zip_path, root / "payload")

            self.assertEqual(payload, root / "payload" / "SyncClipboard")
            self.assertTrue((payload / "SyncClipboard.exe").exists())

    def test_extract_package_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            zip_path = root / "update.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("../evil.txt", "boom")

            with self.assertRaises(UpdateError):
                extract_package(zip_path, root / "payload")

    def test_resolve_app_dir_requires_frozen_executable(self):
        with self.assertRaises(UpdateError):
            resolve_app_dir()


class TestUpdateSource(unittest.TestCase):
    def test_mirror_url_joins_prefix_and_original(self):
        self.assertEqual(
            mirror_url("https://github.com/a/b.zip", "https://gh-proxy.com"),
            "https://gh-proxy.com/https://github.com/a/b.zip",
        )

    def test_mirror_url_tolerates_trailing_slash_and_blank_prefix(self):
        self.assertEqual(
            mirror_url("https://x/y", "https://m/"), "https://m/https://x/y"
        )
        self.assertEqual(mirror_url("https://x/y", ""), "https://x/y")
        self.assertEqual(mirror_url("https://x/y", None), "https://x/y")

    def test_direct_source_keeps_url_and_has_no_proxy(self):
        source = UpdateSource()
        self.assertEqual(source.mode, "direct")
        self.assertEqual(source.resolve("https://x/y"), "https://x/y")
        self.assertIsNone(source.proxies)

    def test_manual_source_sets_proxies_for_both_schemes(self):
        source = UpdateSource(mode="manual", proxy="http://127.0.0.1:7890")
        self.assertEqual(
            source.proxies,
            {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"},
        )
        # 手动代理不改 URL，只挂 proxies
        self.assertEqual(source.resolve("https://x/y"), "https://x/y")

    def test_mirror_source_rewrites_api_url(self):
        source = UpdateSource(mode="mirror", mirror_prefix="https://ghfast.top")
        self.assertEqual(
            source.resolve(RELEASES_API), f"https://ghfast.top/{RELEASES_API}"
        )

    def test_empty_proxy_in_manual_mode_yields_no_proxies(self):
        self.assertIsNone(UpdateSource(mode="manual", proxy="  ").proxies)


class TestCheckForUpdateWithSource(unittest.TestCase):
    @staticmethod
    def _response(payload):
        response = MagicMock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        return response

    def test_mirror_is_used_for_the_request(self):
        payload = {"tag_name": "v1.9.0", "assets": []}
        source = UpdateSource(mode="mirror", mirror_prefix="https://ghfast.top")
        with patch(
            "common.updater.requests.get", return_value=self._response(payload)
        ) as get:
            result = check_for_update("1.8.0", source=source)

        self.assertEqual(get.call_args.args[0], f"https://ghfast.top/{RELEASES_API}")
        self.assertTrue(result.has_update)
        self.assertEqual(result.source_mode, "mirror")

    def test_mirror_falls_back_to_direct_when_dead(self):
        payload = {"tag_name": "v1.9.0", "assets": []}
        source = UpdateSource(mode="mirror", mirror_prefix="https://dead.example")
        with patch(
            "common.updater.requests.get",
            side_effect=[requests.RequestException("boom"), self._response(payload)],
        ) as get:
            result = check_for_update("1.8.0", source=source)

        self.assertTrue(result.has_update)
        # 第二次回到了原始地址
        self.assertEqual(get.call_args_list[1].args[0], RELEASES_API)

    def test_manual_proxy_is_passed_through(self):
        payload = {"tag_name": "v1.9.0", "assets": []}
        source = UpdateSource(mode="manual", proxy="http://127.0.0.1:7890")
        with patch(
            "common.updater.requests.get", return_value=self._response(payload)
        ) as get:
            check_for_update("1.8.0", source=source)

        self.assertEqual(
            get.call_args.kwargs["proxies"]["https"], "http://127.0.0.1:7890"
        )

    def test_direct_source_does_not_pass_proxies(self):
        payload = {"tag_name": "v1.9.0", "assets": []}
        with patch(
            "common.updater.requests.get", return_value=self._response(payload)
        ) as get:
            check_for_update("1.8.0")

        self.assertNotIn("proxies", get.call_args.kwargs)


class TestDownloadWithSource(unittest.TestCase):
    @staticmethod
    def _stream_response(chunks):
        response = MagicMock()
        response.headers = {"Content-Length": str(sum(len(chunk) for chunk in chunks))}
        response.iter_content.return_value = chunks
        response.raise_for_status.return_value = None
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        return response

    def test_cancel_interrupts_download(self):
        cancel = threading.Event()
        chunks = [b"x" * 16] * 4

        def progress(_downloaded, _total):
            # 第一块写入后立刻取消，模拟用户点「取消下载」
            cancel.set()

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "update.zip"
            with patch(
                "common.updater.requests.get",
                return_value=self._stream_response(chunks),
            ):
                with self.assertRaises(UpdateCancelled):
                    download_file(
                        "https://example.com/win.zip",
                        target,
                        progress=progress,
                        cancel_event=cancel,
                    )
            # 已经写了一半也不能算下载完成，文件不该是完整大小
            self.assertLess(target.stat().st_size, 16 * 4)

    def test_mirror_download_falls_back_to_original_url(self):
        chunks = [b"abc"]
        source = UpdateSource(mode="mirror", mirror_prefix="https://dead.example")
        url = "https://github.com/a/b.zip"
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "update.zip"
            with patch(
                "common.updater.requests.get",
                side_effect=[
                    requests.RequestException("mirror dead"),
                    self._stream_response(chunks),
                ],
            ) as get:
                written = download_file(url, target, source=source)

            self.assertEqual(written, 3)
            self.assertEqual(get.call_args_list[0].args[0], f"https://dead.example/{url}")
            self.assertEqual(get.call_args_list[1].args[0], url)

    def test_mirror_download_uses_mirror_url_first(self):
        chunks = [b"abc"]
        source = UpdateSource(mode="mirror", mirror_prefix="https://ghfast.top")
        url = "https://github.com/a/b.zip"
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "update.zip"
            with patch(
                "common.updater.requests.get",
                return_value=self._stream_response(chunks),
            ) as get:
                download_file(url, target, source=source)

        self.assertEqual(get.call_args.args[0], f"https://ghfast.top/{url}")


if __name__ == "__main__":
    unittest.main()