"""Tests for the HTTP layer: routes, API-key enforcement, request statistics."""

import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.core import config
from src.adapters.api.app import app
from src.adapters.api.deps import chat_service, verify_api_key

client = TestClient(app)


class TestMonitoringRoutes(unittest.TestCase):
    def test_dashboard_is_served(self):
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("hyperag2api Proxy", response.text)
        self.assertIn("LIVE", response.text)

    def test_live_status_shape(self):
        payload = client.get("/api/live-status").json()
        for key in ("status", "uptime_seconds", "requests_total", "streams_active",
                    "sessions", "anti_detection", "recent_requests"):
            self.assertIn(key, payload)

    def test_health_reports_a_known_status(self):
        response = client.get("/health")
        self.assertIn(response.status_code, (200, 503))
        self.assertIn(response.json()["status"], ("ok", "degraded"))


class TestModelsRoute(unittest.TestCase):
    @patch.object(config, "PROXY_API_KEY", "")
    def test_list_models_without_a_key(self):
        models = [{"id": "gpt-4o", "object": "model", "owned_by": "hyperagent"}]
        with patch.object(chat_service, "get_available_models", return_value=models) as stub:
            response = client.get("/v1/models")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"object": "list", "data": models})
            stub.assert_called_once()

    @patch.object(config, "PROXY_API_KEY", "secret-123")
    def test_missing_key_is_rejected(self):
        response = client.get("/v1/models")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Missing API Key", response.json()["detail"])

    @patch.object(config, "PROXY_API_KEY", "secret-123")
    def test_wrong_key_is_rejected(self):
        response = client.get("/v1/models", headers={"Authorization": "Bearer wrong-key"})
        self.assertEqual(response.status_code, 401)
        self.assertIn("Invalid API Key", response.json()["detail"])

    @patch.object(config, "PROXY_API_KEY", "secret-123")
    def test_correct_key_is_accepted(self):
        with patch.object(chat_service, "get_available_models", return_value=[]):
            response = client.get("/v1/models", headers={"Authorization": "Bearer secret-123"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"object": "list", "data": []})


class TestChatCompletionsRoute(unittest.TestCase):
    @patch.object(config, "PROXY_API_KEY", "")
    def test_non_streaming_response(self):
        from src.core.schemas import ChatCompletionResponse, Choice, Message

        expected = ChatCompletionResponse(
            id="chat-123", created=1600000000, model="gpt-4o",
            choices=[Choice(index=0, message=Message(role="assistant", content="Hello, human!"))],
        )

        async def stub(*args, **kwargs):
            return expected

        with patch.object(chat_service, "execute_chat_non_stream", side_effect=stub) as method:
            response = client.post("/v1/chat/completions", json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": False,
            })
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["id"], "chat-123")
            self.assertEqual(body["choices"][0]["message"]["content"], "Hello, human!")
            method.assert_called_once()

    @patch.object(config, "PROXY_API_KEY", "")
    def test_streaming_response(self):
        async def stub(*args, **kwargs):
            yield "data: chunk1\n\n"
            yield "data: chunk2\n\n"
            yield "data: [DONE]\n\n"

        with patch.object(chat_service, "execute_chat_stream", side_effect=stub) as method:
            response = client.post("/v1/chat/completions", json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")

            lines = list(response.iter_lines())
            self.assertIn("data: chunk1", lines)
            self.assertIn("data: chunk2", lines)
            self.assertIn("data: [DONE]", lines)
            method.assert_called_once()

    @patch.object(config, "PROXY_API_KEY", "")
    def test_session_header_is_forwarded(self):
        seen = {}

        async def stub(req, session_id=None, meta=None):
            seen["session_id"] = session_id
            yield "data: [DONE]\n\n"

        with patch.object(chat_service, "execute_chat_stream", side_effect=stub):
            client.post(
                "/v1/chat/completions",
                headers={config.SESSION_HEADER: "pinned"},
                json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}],
                      "stream": True},
            )
        self.assertEqual(seen["session_id"], "pinned")

    @patch.object(config, "PROXY_API_KEY", "")
    def test_streaming_records_stats(self):
        from src.core.stats import STATS

        async def stub(req, session_id=None, meta=None):
            meta["thread_id"] = "T9"
            meta["completion_text"] = "hello"
            yield "data: [DONE]\n\n"

        before = STATS.summary()["requests_total"]
        with patch.object(chat_service, "execute_chat_stream", side_effect=stub):
            client.post("/v1/chat/completions", json={
                "model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": True,
            })

        self.assertEqual(STATS.summary()["requests_total"], before + 1)
        self.assertEqual(STATS.summary()["streams_active"], 0)  # stream was closed out
        latest = STATS.recent()[0]
        self.assertEqual(latest["thread_id"], "T9")
        self.assertEqual(latest["status"], "ok")


class TestVerifyApiKey(unittest.IsolatedAsyncioTestCase):
    async def test_no_key_configured_allows_anything(self):
        with patch.object(config, "PROXY_API_KEY", ""):
            await verify_api_key(None)
            await verify_api_key("Bearer some-token")

    async def test_missing_header_raises(self):
        with patch.object(config, "PROXY_API_KEY", "secret-key"):
            with self.assertRaises(HTTPException) as ctx:
                await verify_api_key(None)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("Missing API Key", ctx.exception.detail)

    async def test_wrong_key_raises(self):
        with patch.object(config, "PROXY_API_KEY", "secret-key"):
            with self.assertRaises(HTTPException) as ctx:
                await verify_api_key("Bearer wrong-key")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("Invalid API Key", ctx.exception.detail)

    async def test_valid_key_passes(self):
        with patch.object(config, "PROXY_API_KEY", "secret-key"):
            await verify_api_key("Bearer secret-key")


if __name__ == "__main__":
    unittest.main()
