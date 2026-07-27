from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

try:
    if __package__:
        from .agents_ccomp import ChatCom, ChatComE, ChatCompletion, ChatHistory, ImageCreate, ImageDescription  # type: ignore
    else:
        from agents_ccomp import ChatCom, ChatComE, ChatCompletion, ChatHistory, ImageCreate, ImageDescription  # type: ignore
except ImportError as e:
    msg = str(e)
    if "attempted relative import" in msg or "no known parent package" in msg:
        from alde.agents_ccomp import ChatCom, ChatComE, ChatCompletion, ChatHistory, ImageCreate, ImageDescription  # type: ignore
    else:
        raise


@runtime_checkable
class ChatProviderProtocol(Protocol):
    provider_name: str

    def create_chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> Any:
        ...


class OpenAIChatProvider:
    provider_name = "openai"

    def create_chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> Any:
        return ChatCompletion._get_client().chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice or "auto",
        )


class GitHubModelsChatProvider:
    """OpenAI-compatible provider targeting the GitHub Models inference endpoint.

    Conforms to :class:`ChatProviderProtocol` via structural subtyping.
    """

    provider_name = "github"

    def create_chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> Any:
        return ChatCompletion._get_github_client().chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice or "auto",
        )


class ChatRuntimeService:
    def load_provider(self, provider_name: str | None = None) -> ChatProviderProtocol:
        normalized_provider = str(provider_name or "openai").strip().lower()
        if normalized_provider == "github":
            return GitHubModelsChatProvider()
        if normalized_provider != "openai":
            raise ValueError(f"Unsupported chat provider: {provider_name}")
        return OpenAIChatProvider()

    def load_chat_object(self, **kwargs: Any) -> ChatCom:
        return ChatCom(**kwargs)

    def load_history_object(self) -> ChatHistory:
        return ChatHistory()


__all__ = [
    "ChatCom",
    "ChatComE",
    "ChatCompletion",
    "ChatHistory",
    "ChatProviderProtocol",
    "ChatRuntimeService",
    "GitHubModelsChatProvider",
    "ImageCreate",
    "ImageDescription",
    "OpenAIChatProvider",
]