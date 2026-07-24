"""Tests for platform / CDP / browser detection and the logging config."""

import logging
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.core import logging_config
from src.adapters.launcher import system
from src.adapters.launcher.system import (
    BrowserInfo,
    get_platform,
    is_windows,
    is_macos,
    is_linux,
    platform_label,
    is_cdp_endpoint_ready,
    discover_playwright_browsers,
    discover_browsers,
)
from src.adapters.launcher.processes import ProcessManager


class TestPlatformDetection(unittest.TestCase):
    def test_windows(self):
        with patch.object(system.os, "name", "nt"):
            self.assertEqual(get_platform(), "windows")
            self.assertTrue(is_windows())
            self.assertEqual(platform_label(), "Windows")

    def test_macos(self):
        with patch.object(system.os, "name", "posix"), \
             patch.object(system.sys, "platform", "darwin"):
            self.assertEqual(get_platform(), "macos")
            self.assertTrue(is_macos())
            self.assertEqual(platform_label(), "macOS")

    def test_linux(self):
        with patch.object(system.os, "name", "posix"), \
             patch.object(system.sys, "platform", "linux"):
            self.assertEqual(get_platform(), "linux")
            self.assertTrue(is_linux())
            self.assertEqual(platform_label(), "Linux")


class TestBrowserInfo(unittest.TestCase):
    def test_is_chromium(self):
        self.assertTrue(BrowserInfo("X", "/x", "chromium").is_chromium)
        self.assertFalse(BrowserInfo("F", "/f", "firefox").is_chromium)


class TestCDPDetection(unittest.TestCase):
    def test_endpoint_ready_true(self):
        resp = MagicMock()
        resp.status = 200
        cm = MagicMock()
        cm.__enter__.return_value = resp
        with patch("src.adapters.launcher.system.urllib.request.urlopen", return_value=cm):
            self.assertTrue(is_cdp_endpoint_ready(port=9222))

    def test_endpoint_ready_false(self):
        with patch("src.adapters.launcher.system.urllib.request.urlopen",
                   side_effect=Exception("connection refused")):
            self.assertFalse(is_cdp_endpoint_ready(port=9222))


class TestPlaywrightDiscovery(unittest.TestCase):
    def test_api_discovery(self):
        mock_chromium = MagicMock()
        mock_chromium.executable_path = "/pw/chrome-linux/chrome"
        mock_p = MagicMock()
        mock_p.chromium = mock_chromium
        cm = MagicMock()
        cm.__enter__.return_value = mock_p
        with patch("playwright.sync_api.sync_playwright", return_value=cm), \
             patch("os.path.exists", return_value=True):
            res = discover_playwright_browsers()
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].source, "playwright")
        self.assertTrue(res[0].is_chromium)
        self.assertEqual(res[0].command, "/pw/chrome-linux/chrome")

    def test_cache_fallback(self):
        with patch("playwright.sync_api.sync_playwright", side_effect=Exception("no driver")), \
             patch("src.adapters.launcher.system.get_platform", return_value="linux"), \
             patch("src.adapters.launcher.system.glob.glob",
                   return_value=["/cache/ms-playwright/chromium-1097/chrome-linux/chrome"]), \
             patch("os.path.exists", return_value=True):
            res = discover_playwright_browsers()
        self.assertTrue(res)
        self.assertEqual(res[0].source, "playwright")


class TestDiscoverBrowsers(unittest.TestCase):
    def test_ordering(self):
        sysb = [
            BrowserInfo("Firefox", "firefox", "firefox", "system"),
            BrowserInfo("Google Chrome", "google-chrome", "chromium", "system"),
        ]
        pw = [BrowserInfo("Playwright Chromium", "/pw/chrome", "chromium", "playwright")]
        with patch("src.adapters.launcher.system.discover_system_browsers", return_value=sysb), \
             patch("src.adapters.launcher.system.discover_playwright_browsers", return_value=pw):
            res = discover_browsers()
        names = [b.name for b in res]
        self.assertEqual(names[0], "Google Chrome")   # chromium system first
        self.assertIn("Playwright Chromium", names)     # playwright included
        self.assertEqual(names[-1], "Firefox")          # firefox last

    def test_dedup_by_path(self):
        sysb = [BrowserInfo("Chromium", "/usr/bin/chromium", "chromium", "system")]
        pw = [BrowserInfo("Playwright Chromium", "/usr/bin/chromium", "chromium", "playwright")]
        with patch("src.adapters.launcher.system.discover_system_browsers", return_value=sysb), \
             patch("src.adapters.launcher.system.discover_playwright_browsers", return_value=pw), \
             patch("os.path.realpath", side_effect=lambda p: p):
            res = discover_browsers()
        self.assertEqual(len(res), 1)


class TestProcessManagerCDP(unittest.TestCase):
    @patch("src.adapters.launcher.processes.time.sleep")
    def test_wait_for_cdp_ready(self, _sleep):
        pm = ProcessManager()
        with patch("src.adapters.launcher.processes.is_cdp_endpoint_ready", return_value=True):
            self.assertTrue(pm.wait_for_cdp(timeout=5.0))

    @patch("src.adapters.launcher.processes.time.sleep")
    def test_wait_for_cdp_timeout(self, _sleep):
        pm = ProcessManager()
        with patch("src.adapters.launcher.processes.is_cdp_endpoint_ready", return_value=False):
            self.assertFalse(pm.wait_for_cdp(timeout=1.0, interval=0.5))

    def test_user_data_dir_created(self):
        pm = ProcessManager()
        with patch("src.adapters.launcher.processes.os.makedirs") as mk:
            path = pm.user_data_dir()
        self.assertIn("cdp-profile", path)
        mk.assert_called_once()


class TestLoggingConfig(unittest.TestCase):
    def _reset_root(self):
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        if hasattr(root, logging_config._CONFIGURED_FLAG):
            delattr(root, logging_config._CONFIGURED_FLAG)

    def setUp(self):
        self._reset_root()

    def tearDown(self):
        self._reset_root()

    def test_setup_is_idempotent(self):
        l1 = logging_config.setup_logging(level="INFO")
        count1 = len(logging.getLogger().handlers)
        l2 = logging_config.setup_logging(level="INFO")
        count2 = len(logging.getLogger().handlers)
        self.assertEqual(count1, count2)  # no duplicate handlers
        self.assertIs(l1, l2)

    def test_get_logger_child_name(self):
        self.assertEqual(logging_config.get_logger("thing").name, "hyperagent-proxy.thing")
        self.assertEqual(logging_config.get_logger().name, "hyperagent-proxy")

    def test_level_from_env(self):
        with patch.dict(os.environ, {"LOG_LEVEL": "DEBUG"}):
            logging_config.setup_logging(force=True)
            self.assertEqual(logging.getLogger().level, logging.DEBUG)

    def test_file_logging(self):
        tmp = tempfile.mkdtemp()
        log_path = os.path.join(tmp, "proxy.log")
        logging_config.setup_logging(level="INFO", log_file=log_path, force=True)
        logging_config.get_logger("filetest").info("hello-file-log")
        for h in logging.getLogger().handlers:
            h.flush()
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("hello-file-log", content)


if __name__ == "__main__":
    unittest.main()
