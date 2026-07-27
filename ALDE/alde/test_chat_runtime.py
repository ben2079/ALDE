from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("AI_IDE_HISTORY_AUTOSAVE", "0")
os.environ.setdefault("AI_IDE_DISABLE_HISTORY_FLUSH", "1")

PKG_ROOT = Path(__file__).resolve().parents[2]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from ALDE.alde.chat_runtime import (  # noqa: E402
    ChatProviderProtocol,
    ChatRuntimeService,
    GitHubModelsChatProvider,
    OpenAIChatProvider,
)


class TestOpenAIChatProvider(unittest.TestCase):
    def test_provider_name(self) -> None:
        provider = OpenAIChatProvider()
        self.assertEqual(provider.provider_name, "openai")

    def test_implements_protocol(self) -> None:
        self.assertIsInstance(OpenAIChatProvider(), ChatProviderProtocol)


class TestGitHubModelsChatProvider(unittest.TestCase):
    def test_provider_name(self) -> None:
        provider = GitHubModelsChatProvider()
        self.assertEqual(provider.provider_name, "github")

    def test_implements_protocol(self) -> None:
        self.assertIsInstance(GitHubModelsChatProvider(), ChatProviderProtocol)

    def test_create_chat_completion_uses_github_client(self) -> None:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch(
            "ALDE.alde.agents_ccomp.ChatCompletion._get_github_client",
            return_value=mock_client,
        ):
            provider = GitHubModelsChatProvider()
            result = provider.create_chat_completion(
                model="gpt-4o",
                messages=[{"role": "user", "content": "hello"}],
            )

        mock_client.chat.completions.create.assert_called_once_with(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            tool_choice="auto",
        )
        self.assertIs(result, mock_response)


class TestChatRuntimeServiceLoadProvider(unittest.TestCase):
    def _make_service(self) -> ChatRuntimeService:
        return ChatRuntimeService()

    def test_default_returns_openai_provider(self) -> None:
        svc = self._make_service()
        provider = svc.load_provider()
        self.assertIsInstance(provider, OpenAIChatProvider)

    def test_openai_string_returns_openai_provider(self) -> None:
        svc = self._make_service()
        provider = svc.load_provider("openai")
        self.assertIsInstance(provider, OpenAIChatProvider)

    def test_github_string_returns_github_provider(self) -> None:
        svc = self._make_service()
        provider = svc.load_provider("github")
        self.assertIsInstance(provider, GitHubModelsChatProvider)

    def test_github_string_case_insensitive(self) -> None:
        svc = self._make_service()
        provider = svc.load_provider("GitHub")
        self.assertIsInstance(provider, GitHubModelsChatProvider)

    def test_unsupported_provider_raises(self) -> None:
        svc = self._make_service()
        with self.assertRaises(ValueError):
            svc.load_provider("anthropic")


class TestChatCompletionGitHubToken(unittest.TestCase):
    def test_read_github_token_from_env(self) -> None:
        from ALDE.alde.agents_ccomp import ChatCompletion

        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"}):
            token = ChatCompletion._read_github_token()
        self.assertEqual(token, "ghp_test123")

    def test_read_github_token_missing_raises(self) -> None:
        from ALDE.alde.agents_ccomp import ChatCompletion

        env_without_token = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
        with patch.dict(os.environ, env_without_token, clear=True):
            with self.assertRaises(RuntimeError, msg="GITHUB_TOKEN not found"):
                ChatCompletion._read_github_token()

    def test_github_models_base_url(self) -> None:
        from ALDE.alde.agents_ccomp import ChatCompletion

        self.assertEqual(
            ChatCompletion.GITHUB_MODELS_BASE_URL,
            "https://models.inference.ai.azure.com",
        )

    def test_get_github_client_uses_token_and_base_url(self) -> None:
        from ALDE.alde.agents_ccomp import ChatCompletion

        original = ChatCompletion._github_client
        try:
            ChatCompletion._github_client = None
            mock_openai = MagicMock()

            with patch("ALDE.alde.agents_ccomp.OpenAI", return_value=mock_openai) as mock_cls, \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_abc"}):
                client = ChatCompletion._get_github_client()

            mock_cls.assert_called_once()
            call_kwargs = mock_cls.call_args.kwargs
            self.assertEqual(call_kwargs["api_key"], "ghp_abc")
            self.assertEqual(call_kwargs["base_url"], "https://models.inference.ai.azure.com")
            self.assertIs(client, mock_openai)
        finally:
            ChatCompletion._github_client = original


if __name__ == "__main__":
    unittest.main()
