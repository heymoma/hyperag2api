import unittest
from unittest.mock import AsyncMock, MagicMock, patch, mock_open
import subprocess
import socket
import os
import urllib.request

from src.adapters.browser.playwright_cdp import PlaywrightCDPCookieProvider
from src.adapters.backend.hyperagent_client import HyperagentClient
from src.adapters.launcher.processes import ProcessManager
from src.adapters.launcher.system import (
    check_command,
    get_local_ip,
    get_global_ip,
    is_port_in_use,
    get_random_port,
    get_installed_browsers
)


class TestPlaywrightCDPCookieProvider(unittest.IsolatedAsyncioTestCase):
    async def test_get_cookies_success(self):
        # Mock async_playwright context manager
        mock_context = AsyncMock()
        mock_context.cookies.return_value = [
            {"name": "session", "value": "xyz"},
            {"name": "user", "value": "john"}
        ]
        
        mock_browser = AsyncMock()
        mock_browser.contexts = [mock_context]
        
        mock_chromium = AsyncMock()
        mock_chromium.connect_over_cdp.return_value = mock_browser
        
        mock_playwright = MagicMock()
        mock_playwright.chromium = mock_chromium
        
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_playwright
        
        with patch("src.adapters.browser.playwright_cdp.async_playwright", return_value=mock_cm):
            provider = PlaywrightCDPCookieProvider(cdp_url="http://127.0.0.1:9222")
            cookies = await provider.get_cookies("https://hyperagent.com")
            
            self.assertEqual(cookies, {"session": "xyz", "user": "john"})
            mock_chromium.connect_over_cdp.assert_called_once_with("http://127.0.0.1:9222")
            mock_context.cookies.assert_called_once_with("https://hyperagent.com")

    async def test_get_cookies_connection_error(self):
        # Mock error during connection
        mock_chromium = AsyncMock()
        mock_chromium.connect_over_cdp.side_effect = Exception("CDP connection failed")
        
        mock_playwright = MagicMock()
        mock_playwright.chromium = mock_chromium
        
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_playwright
        
        with patch("src.adapters.browser.playwright_cdp.async_playwright", return_value=mock_cm):
            provider = PlaywrightCDPCookieProvider()
            with self.assertRaises(ConnectionError) as ctx:
                await provider.get_cookies()
            self.assertIn("Could not connect to the active browser session", str(ctx.exception))

    def test_clear_cookies_success(self):
        # Mock sync_playwright context manager
        mock_page = MagicMock()
        mock_context = MagicMock()
        mock_context.pages = [mock_page]
        
        mock_browser = MagicMock()
        mock_browser.contexts = [mock_context]
        
        mock_chromium = MagicMock()
        mock_chromium.connect_over_cdp.return_value = mock_browser
        
        mock_playwright = MagicMock()
        mock_playwright.chromium = mock_chromium
        
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_playwright
        
        with patch("src.adapters.browser.playwright_cdp.sync_playwright", return_value=mock_cm):
            provider = PlaywrightCDPCookieProvider()
            res = provider.clear_cookies()
            
            self.assertTrue(res)
            mock_context.clear_cookies.assert_called_once()
            mock_page.goto.assert_called_once()

    def test_clear_cookies_empty_pages(self):
        mock_page = MagicMock()
        mock_context = MagicMock()
        mock_context.pages = []
        mock_context.new_page.return_value = mock_page
        
        mock_browser = MagicMock()
        mock_browser.contexts = [mock_context]
        
        mock_chromium = MagicMock()
        mock_chromium.connect_over_cdp.return_value = mock_browser
        
        mock_playwright = MagicMock()
        mock_playwright.chromium = mock_chromium
        
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_playwright
        
        with patch("src.adapters.browser.playwright_cdp.sync_playwright", return_value=mock_cm):
            provider = PlaywrightCDPCookieProvider()
            res = provider.clear_cookies()
            
            self.assertTrue(res)
            mock_context.new_page.assert_called_once()
            mock_page.goto.assert_called_once()

    def test_clear_cookies_failure(self):
        mock_cm = MagicMock()
        mock_cm.__enter__.side_effect = Exception("Browser not launched")
        
        with patch("src.adapters.browser.playwright_cdp.sync_playwright", return_value=mock_cm):
            provider = PlaywrightCDPCookieProvider()
            res = provider.clear_cookies()
            self.assertFalse(res)


class TestHyperagentClient(unittest.IsolatedAsyncioTestCase):
    async def test_create_thread_success(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "thread_abc123"}
        mock_client.post.return_value = mock_response
        
        mock_client_cm = MagicMock()
        mock_client_cm.__aenter__.return_value = mock_client
        
        with patch("src.adapters.backend.hyperagent_client.httpx.AsyncClient", return_value=mock_client_cm):
            client = HyperagentClient()
            thread_id = await client.create_thread("sonnet-5", "System Message", {"cookie": "123"})
            self.assertEqual(thread_id, "thread_abc123")
            mock_client.post.assert_called_once()

    async def test_create_thread_failure(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_client.post.return_value = mock_response
        
        mock_client_cm = MagicMock()
        mock_client_cm.__aenter__.return_value = mock_client
        
        with patch("src.adapters.backend.hyperagent_client.httpx.AsyncClient", return_value=mock_client_cm):
            client = HyperagentClient(max_retries=0)
            with self.assertRaises(RuntimeError) as ctx:
                await client.create_thread("sonnet-5", "System Message", {"cookie": "123"})
            self.assertIn("Failed to initialize thread", str(ctx.exception))

    async def test_warm_thread(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post.return_value = mock_response
        
        mock_client_cm = MagicMock()
        mock_client_cm.__aenter__.return_value = mock_client
        
        with patch("src.adapters.backend.hyperagent_client.httpx.AsyncClient", return_value=mock_client_cm):
            client = HyperagentClient()
            # Should not raise exception
            await client.warm_thread("thread_abc123", {"cookie": "123"})
            mock_client.post.assert_called_once()

    async def test_stream_chat_success(self):
        async def mock_aiter_lines():
            yield "data: {\"type\": \"text\", \"content\": \"Hello\"}"
            yield "data: {\"type\": \"thinking\", \"content\": \"Thinking\"}"
            yield "data: [DONE]"
            
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.aiter_lines = mock_aiter_lines
        
        mock_stream_cm = AsyncMock()
        mock_stream_cm.__aenter__.return_value = mock_response
        
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=mock_stream_cm)
        
        mock_client_cm = MagicMock()
        mock_client_cm.__aenter__.return_value = mock_client
        
        with patch("src.adapters.backend.hyperagent_client.httpx.AsyncClient", return_value=mock_client_cm):
            client = HyperagentClient()
            events = []
            async for event in client.stream_chat("thread_abc123", "User prompt", {}):
                events.append(event)
                
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["content"], "Hello")
            self.assertEqual(events[1]["content"], "Thinking")

    async def test_stream_chat_failure(self):
        mock_response = AsyncMock()
        mock_response.status_code = 400
        
        mock_stream_cm = AsyncMock()
        mock_stream_cm.__aenter__.return_value = mock_response
        
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=mock_stream_cm)
        
        mock_client_cm = MagicMock()
        mock_client_cm.__aenter__.return_value = mock_client
        
        with patch("src.adapters.backend.hyperagent_client.httpx.AsyncClient", return_value=mock_client_cm):
            client = HyperagentClient()
            with self.assertRaises(RuntimeError):
                async for _ in client.stream_chat("thread_abc123", "User prompt", {}):
                    pass


class TestUploadFlow(unittest.IsolatedAsyncioTestCase):
    async def test_presigned_upload(self):
        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.json.return_value = {"fileId": "F1", "uploadUrl": "https://s3.example/x?sig"}
        put_resp = MagicMock()
        put_resp.status_code = 200
        client = AsyncMock()
        client.post.return_value = post_resp
        client.put.return_value = put_resp
        cm = MagicMock()
        cm.__aenter__.return_value = client
        with patch("src.adapters.backend.hyperagent_client.httpx.AsyncClient", return_value=cm):
            desc = await HyperagentClient().upload_file("T1", {"c": "1"}, "a.png", b"\x89PNG", "image/png")
        self.assertEqual(desc["id"], "F1")
        client.post.assert_called_once()   # /api/uploads
        client.put.assert_called_once()    # presigned S3 PUT

    def test_chat_payload_attachment_ids(self):
        payload = HyperagentClient()._build_chat_payload("hi", None, [{"id": "F1"}, {"id": "F2"}], None)
        self.assertEqual(payload["attachmentIds"], ["F1", "F2"])
        # no attachments -> key absent
        self.assertNotIn("attachmentIds", HyperagentClient()._build_chat_payload("hi", None, None, None))


class TestProcessManager(unittest.TestCase):
    @patch("socket.socket")
    def test_check_cdp_port_active(self, mock_socket_class):
        mock_socket_inst = MagicMock()
        mock_socket_inst.connect_ex.return_value = 0
        mock_socket_class.return_value.__enter__.return_value = mock_socket_inst
        
        pm = ProcessManager(browser_cdp_port=9222)
        self.assertTrue(pm.check_cdp_port_active())
        mock_socket_inst.connect_ex.assert_called_once_with(('127.0.0.1', 9222))

    @patch("subprocess.Popen")
    @patch("time.sleep")
    def test_launch_browser_port_inactive(self, mock_sleep, mock_popen):
        pm = ProcessManager()

        with patch.object(pm, "check_cdp_port_active", return_value=False), \
             patch.object(pm, "wait_for_cdp", return_value=True):
            res = pm.launch_browser("google-chrome")
            self.assertTrue(res)
            mock_popen.assert_called_once()
            self.assertIsNotNone(pm.browser_proc)
            # Chromium launch must use a dedicated --user-data-dir so the debug
            # port opens even when the browser is already running.
            launched_args = mock_popen.call_args[0][0]
            self.assertTrue(any(str(a).startswith("--user-data-dir=") for a in launched_args))
            self.assertIn("--remote-debugging-port=9222", launched_args)

    @patch("subprocess.Popen")
    def test_launch_browser_port_active(self, mock_popen):
        pm = ProcessManager()
        
        with patch.object(pm, "check_cdp_port_active", return_value=True):
            res = pm.launch_browser("google-chrome")
            self.assertTrue(res)
            mock_popen.assert_not_called()
            self.assertIsNone(pm.browser_proc)

    @patch("subprocess.Popen")
    def test_start_proxy_server(self, mock_popen):
        pm = ProcessManager()
        
        pm.start_proxy_server(8000, "my-key", "/app", {"ENV_VAR": "val"})
        mock_popen.assert_called_once()
        self.assertIsNotNone(pm.proxy_proc)

    def test_terminate_all(self):
        pm = ProcessManager()
        mock_proxy = MagicMock()
        mock_browser = MagicMock()
        
        pm.proxy_proc = mock_proxy
        pm.browser_proc = mock_browser
        
        pm.terminate_all()
        mock_proxy.terminate.assert_called_once()
        mock_browser.terminate.assert_called_once()
        self.assertIsNone(pm.proxy_proc)
        self.assertIsNone(pm.browser_proc)


class TestSystemUtils(unittest.TestCase):
    def test_check_command(self):
        with patch("shutil.which", return_value="/usr/bin/git"):
            self.assertTrue(check_command("git"))
        with patch("shutil.which", return_value=None):
            self.assertFalse(check_command("nonexistent-command"))

    @patch("socket.socket")
    def test_get_local_ip(self, mock_socket_class):
        mock_socket_inst = MagicMock()
        mock_socket_inst.getsockname.return_value = ["192.168.1.50"]
        mock_socket_class.return_value = mock_socket_inst
        
        ip = get_local_ip()
        self.assertEqual(ip, "192.168.1.50")

    @patch("urllib.request.urlopen")
    def test_get_global_ip_success(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b"203.0.113.1"
        mock_urlopen.return_value = mock_response
        
        ip = get_global_ip()
        self.assertEqual(ip, "203.0.113.1")

    @patch("urllib.request.urlopen")
    def test_get_global_ip_failure(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("Offline")
        ip = get_global_ip()
        self.assertEqual(ip, "Unknown / Offline")

    @patch("socket.socket")
    def test_is_port_in_use(self, mock_socket_class):
        mock_socket_inst = MagicMock()
        mock_socket_inst.connect_ex.return_value = 0
        mock_socket_class.return_value.__enter__.return_value = mock_socket_inst
        
        self.assertTrue(is_port_in_use(8000))

    @patch("random.randint", return_value=40000)
    def test_get_random_port(self, mock_randint):
        with patch("src.adapters.launcher.system.is_port_in_use", return_value=False):
            port = get_random_port()
            self.assertEqual(port, 40000)

    def test_get_installed_browsers_linux(self):
        with patch("src.adapters.launcher.system.get_platform", return_value="linux"), \
             patch("src.adapters.launcher.system.discover_playwright_browsers", return_value=[]), \
             patch("src.adapters.launcher.system.check_command", side_effect=lambda x: x == "google-chrome"), \
             patch("os.path.exists", return_value=False):
            browsers = get_installed_browsers()
            self.assertIn("Google Chrome", browsers)
            self.assertEqual(browsers["Google Chrome"], "google-chrome")


if __name__ == "__main__":
    unittest.main()
