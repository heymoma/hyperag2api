import unittest
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi.testclient import TestClient

from src.adapters.api.routes import app, chat_service


client = TestClient(app)


class TestAPIRoutes(unittest.TestCase):
    def test_root_endpoint(self):
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("hyperag2api Proxy", response.text)
        self.assertIn("LIVE", response.text)

    @patch("src.adapters.api.routes.PROXY_API_KEY", "")
    def test_list_models_no_key(self):
        mock_models = [{"id": "gpt-4o", "object": "model", "owned_by": "hyperagent"}]
        with patch.object(chat_service, "get_available_models", return_value=mock_models) as mock_method:
            response = client.get("/v1/models")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"object": "list", "data": mock_models})
            mock_method.assert_called_once()

    @patch("src.adapters.api.routes.PROXY_API_KEY", "secret-123")
    def test_list_models_with_key_unauthorized(self):
        response = client.get("/v1/models")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Missing API Key", response.json()["detail"])

    @patch("src.adapters.api.routes.PROXY_API_KEY", "secret-123")
    def test_list_models_with_key_invalid(self):
        response = client.get("/v1/models", headers={"Authorization": "Bearer wrong-key"})
        self.assertEqual(response.status_code, 401)
        self.assertIn("Invalid API Key", response.json()["detail"])

    @patch("src.adapters.api.routes.PROXY_API_KEY", "secret-123")
    def test_list_models_with_key_authorized(self):
        mock_models = []
        with patch.object(chat_service, "get_available_models", return_value=mock_models):
            response = client.get("/v1/models", headers={"Authorization": "Bearer secret-123"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"object": "list", "data": []})

    @patch("src.adapters.api.routes.PROXY_API_KEY", "")
    def test_chat_completions_non_stream(self):
        from src.adapters.api.schemas import ChatCompletionResponse, Choice, Message
        mock_response = ChatCompletionResponse(
            id="chat-123",
            created=1600000000,
            model="gpt-4o",
            choices=[Choice(index=0, message=Message(role="assistant", content="Hello, human!"))]
        )
        
        async def mock_non_stream(*args, **kwargs):
            return mock_response
            
        with patch.object(chat_service, "execute_chat_non_stream", side_effect=mock_non_stream) as mock_method:
            payload = {
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": False
            }
            response = client.post("/v1/chat/completions", json=payload)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["id"], "chat-123")
            self.assertEqual(data["choices"][0]["message"]["content"], "Hello, human!")
            mock_method.assert_called_once()

    @patch("src.adapters.api.routes.PROXY_API_KEY", "")
    def test_chat_completions_stream(self):
        async def mock_stream(*args, **kwargs):
            yield "data: chunk1\n\n"
            yield "data: chunk2\n\n"
            yield "data: [DONE]\n\n"
            
        with patch.object(chat_service, "execute_chat_stream", side_effect=mock_stream) as mock_method:
            payload = {
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True
            }
            response = client.post("/v1/chat/completions", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
            
            lines = list(response.iter_lines())
            self.assertIn("data: chunk1", lines)
            self.assertIn("data: chunk2", lines)
            self.assertIn("data: [DONE]", lines)
            mock_method.assert_called_once()


if __name__ == "__main__":
    unittest.main()
