import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.backend.hyperagent_client import HyperagentClient


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
        self.assertNotIn("attachmentIds", HyperagentClient()._build_chat_payload("hi", None, None, None))


if __name__ == "__main__":
    unittest.main()
