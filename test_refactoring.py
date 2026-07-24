import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException

from src.services.chat_service import ChatService
from src.adapters.api.routes import verify_api_key
from src.core.config import MODEL_MAPPING
from src.core.session_store import SessionStore
from src.adapters.api.schemas import ChatCompletionRequest, Message


def _mem_store():
    """An isolated in-memory session store (no SQLite file side effects)."""
    return SessionStore(persist=False)


class TestChatService(unittest.TestCase):
    def setUp(self):
        self.mock_cookie_provider = MagicMock()
        self.mock_chat_backend = MagicMock()
        self.chat_service = ChatService(
            self.mock_cookie_provider, self.mock_chat_backend, session_store=_mem_store()
        )

    def test_get_available_models(self):
        models = self.chat_service.get_available_models()
        self.assertEqual(len(models), len(MODEL_MAPPING))
        for model in models:
            self.assertIn("id", model)
            self.assertIn("object", model)
            self.assertEqual(model["object"], "model")

    def test_prepare_prompts_with_system(self):
        messages = [
            Message(role="system", content="System instruction"),
            Message(role="user", content="Hello"),
            Message(role="assistant", content="Hi user"),
            Message(role="user", content="How are you?")
        ]
        system_prompt, combined_prompt = self.chat_service._prepare_prompts(messages)
        self.assertEqual(system_prompt, "System instruction")
        self.assertEqual(combined_prompt, "User: Hello\n\nAssistant: Hi user\n\nUser: How are you?")

    def test_prepare_prompts_fallback(self):
        messages = [
            Message(role="system", content="Only system message")
        ]
        system_prompt, combined_prompt = self.chat_service._prepare_prompts(messages)
        self.assertEqual(system_prompt, "Only system message")
        self.assertEqual(combined_prompt, "Only system message")


class TestAPIKeyVerification(unittest.IsolatedAsyncioTestCase):
    async def test_no_key_configured(self):
        # When PROXY_API_KEY is empty, any or no key should pass without error
        with patch("src.adapters.api.routes.PROXY_API_KEY", ""):
            # Should not raise exception
            await verify_api_key(None)
            await verify_api_key("Bearer some-token")

    async def test_key_configured_missing_header(self):
        with patch("src.adapters.api.routes.PROXY_API_KEY", "secret-key"):
            with self.assertRaises(HTTPException) as ctx:
                await verify_api_key(None)
            self.assertEqual(ctx.exception.status_code, 401)
            self.assertIn("Missing API Key", ctx.exception.detail)

    async def test_key_configured_invalid_header(self):
        with patch("src.adapters.api.routes.PROXY_API_KEY", "secret-key"):
            with self.assertRaises(HTTPException) as ctx:
                await verify_api_key("Bearer wrong-key")
            self.assertEqual(ctx.exception.status_code, 401)
            self.assertIn("Invalid API Key", ctx.exception.detail)

    async def test_key_configured_valid_header(self):
        with patch("src.adapters.api.routes.PROXY_API_KEY", "secret-key"):
            # Should not raise exception
            await verify_api_key("Bearer secret-key")


class TestChatServiceAsync(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_cookie_provider = AsyncMock()
        self.mock_chat_backend = MagicMock()
        self.mock_chat_backend.create_thread = AsyncMock()
        self.mock_chat_backend.warm_thread = AsyncMock()
        self.mock_chat_backend.stream_chat = MagicMock()
        self.mock_chat_backend.upload_file = AsyncMock(return_value=None)
        self.mock_chat_backend.interrupt = AsyncMock(return_value=None)
        self.chat_service = ChatService(
            self.mock_cookie_provider, self.mock_chat_backend, session_store=_mem_store()
        )

    async def test_execute_chat_stream_success(self):
        # Setup mocks
        self.mock_cookie_provider.get_cookies.return_value = {"session": "abc"}
        self.mock_chat_backend.create_thread.return_value = "thread-123"
        self.mock_chat_backend.warm_thread.return_value = None
        
        async def mock_stream(*args, **kwargs):
            yield {"type": "thinking", "content": "Thinking about it..."}
            yield {"type": "text", "content": "Hello world!"}
            yield {
                "type": "tool_input_ready",
                "toolName": "mcp__t__AskQuestion",
                "toolInput": {
                    "questions": [{
                        "question": "What model?",
                        "options": [{"label": "Standard", "value": "std"}]
                    }]
                }
            }
        
        self.mock_chat_backend.stream_chat.side_effect = mock_stream
        
        req = ChatCompletionRequest(
            model="opus-4.8",
            messages=[Message(role="user", content="Hello")],
            stream=True
        )
        
        chunks = []
        async for chunk in self.chat_service.execute_chat_stream(req):
            chunks.append(chunk)
            
        # Verify chunks received
        self.assertTrue(len(chunks) > 0)
        # First chunk should have assistant role
        self.assertIn("assistant", chunks[0])
        # Check thinking chunk
        self.assertTrue(any("Thinking about it..." in c for c in chunks))
        # Check content chunk
        self.assertTrue(any("Hello world!" in c for c in chunks))
        # Check steering option content
        self.assertTrue(any("Action Required" in c for c in chunks))
        # Last chunk is DONE
        self.assertEqual(chunks[-1], "data: [DONE]\n\n")

    async def test_execute_chat_stream_cookie_error(self):
        self.mock_cookie_provider.get_cookies.side_effect = Exception("CDP connection failed")
        
        req = ChatCompletionRequest(
            model="opus-4.8",
            messages=[Message(role="user", content="Hello")],
            stream=True
        )
        
        chunks = []
        async for chunk in self.chat_service.execute_chat_stream(req):
            chunks.append(chunk)
            
        self.assertTrue(any("CDP connection failed" in c for c in chunks))
        self.assertEqual(chunks[-1], "data: [DONE]\n\n")

    async def test_execute_chat_stream_backend_error(self):
        self.mock_cookie_provider.get_cookies.return_value = {"session": "abc"}
        self.mock_chat_backend.create_thread.side_effect = Exception("Hyperagent offline")
        
        req = ChatCompletionRequest(
            model="opus-4.8",
            messages=[Message(role="user", content="Hello")],
            stream=True
        )
        
        chunks = []
        async for chunk in self.chat_service.execute_chat_stream(req):
            chunks.append(chunk)
            
        self.assertTrue(any("Hyperagent offline" in c for c in chunks))

    async def test_execute_chat_non_stream_success(self):
        self.mock_cookie_provider.get_cookies.return_value = {"session": "abc"}
        self.mock_chat_backend.create_thread.return_value = "thread-123"
        
        async def mock_stream(*args, **kwargs):
            yield {"type": "thinking", "content": "Thought process"}
            yield {"type": "text", "content": "Response content"}
            
        self.mock_chat_backend.stream_chat.side_effect = mock_stream
        
        req = ChatCompletionRequest(
            model="opus-4.8",
            messages=[Message(role="user", content="Hello")],
            stream=False
        )
        
        response = await self.chat_service.execute_chat_non_stream(req)
        self.assertEqual(response.model, "opus-4.8")
        self.assertEqual(response.choices[0].message.content, "Response content")
        self.assertEqual(response.choices[0].message.reasoning_content, "Thought process")


class TestAuthService(unittest.TestCase):
    def test_reset_authentication(self):
        from src.services.auth_service import AuthService
        mock_cookie_provider = MagicMock()
        mock_cookie_provider.clear_cookies.return_value = True
        
        auth_service = AuthService(mock_cookie_provider)
        res = auth_service.reset_authentication()
        
        self.assertTrue(res)
        mock_cookie_provider.clear_cookies.assert_called_once()


if __name__ == "__main__":
    unittest.main()

