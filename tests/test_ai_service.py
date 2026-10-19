"""AI 服务测试：请求形态、超时、截断/空内容分支与 usage 映射（不发起真实请求）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nonebot_plugin_group_insight.ai.service import AIService
from nonebot_plugin_group_insight.config import Config


def make_response(
    content: str | None = "ok",
    finish_reason: str | None = "stop",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    total_tokens: int = 15,
    reasoning_tokens: int | None = None,
) -> SimpleNamespace:
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        completion_tokens_details=(
            SimpleNamespace(reasoning_tokens=reasoning_tokens) if reasoning_tokens is not None else None
        ),
    )
    choice = SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


class FakeCompletions:
    def __init__(self, response=None, error: Exception | None = None, delay: float = 0.0) -> None:
        self.response = response
        self.error = error
        self.delay = delay
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.response


def make_service(
    response=None,
    error: Exception | None = None,
    delay: float = 0.0,
    config: Config | None = None,
) -> tuple[AIService, FakeCompletions]:
    service = AIService(config or Config())
    service.initialized = True
    completions = FakeCompletions(response=response, error=error, delay=delay)
    service.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return service, completions


async def test_chat_sends_single_user_message_without_max_tokens():
    service, completions = make_service(make_response(content="你好", prompt_tokens=7, completion_tokens=3, total_tokens=10))

    result = await service.chat("PROMPT", temperature=0.9)

    assert result == {"content": "你好", "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}
    assert len(completions.calls) == 1
    request = completions.calls[0]
    assert request["messages"] == [{"role": "user", "content": "PROMPT"}]
    assert request["temperature"] == 0.9
    assert request["model"] == "gpt-4.1"
    assert "max_tokens" not in request


async def test_chat_uses_configured_model():
    service, completions = make_service(
        make_response(), config=Config(group_insight_ai_model="deepseek-v4.1-flash")
    )

    await service.chat("PROMPT")

    assert completions.calls[0]["model"] == "deepseek-v4.1-flash"


async def test_chat_truncated_response_reports_reasoning_tokens():
    service, _ = make_service(
        make_response(content=None, finish_reason="length", completion_tokens=100, reasoning_tokens=80)
    )

    with pytest.raises(RuntimeError) as excinfo:
        await service.chat("PROMPT")

    assert str(excinfo.value) == (
        "AI 响应被截断 (finish_reason=length)，已消耗 100 tokens（其中推理 80），请提高服务商侧的输出长度上限"
    )


async def test_chat_truncated_response_without_reasoning_details():
    service, _ = make_service(make_response(content=None, finish_reason="length", completion_tokens=42))

    with pytest.raises(RuntimeError) as excinfo:
        await service.chat("PROMPT")

    assert str(excinfo.value) == (
        "AI 响应被截断 (finish_reason=length)，已消耗 42 tokens，请提高服务商侧的输出长度上限"
    )


async def test_chat_empty_content_reports_finish_reason():
    service, _ = make_service(make_response(content=None, finish_reason="content_filter"))

    with pytest.raises(RuntimeError) as excinfo:
        await service.chat("PROMPT")

    assert str(excinfo.value) == "AI 返回内容为空 (finish_reason=content_filter)"


async def test_chat_empty_content_without_finish_reason():
    service, _ = make_service(make_response(content=None, finish_reason=None))

    with pytest.raises(RuntimeError) as excinfo:
        await service.chat("PROMPT")

    assert str(excinfo.value) == "AI 返回内容为空 (finish_reason=unknown)"


async def test_chat_timeout_uses_second_level_wait_for():
    service, _ = make_service(make_response(), delay=1.0, config=Config(group_insight_ai_llm_timeout=0.05))

    with pytest.raises(asyncio.TimeoutError) as excinfo:
        await service.chat("PROMPT")

    assert str(excinfo.value) == "AI 请求超时 (0.05秒)"


async def test_chat_default_timeout_is_100_seconds():
    service, _ = make_service(make_response(), error=asyncio.TimeoutError())

    with pytest.raises(asyncio.TimeoutError) as excinfo:
        await service.chat("PROMPT")

    assert str(excinfo.value) == "AI 请求超时 (100秒)"


async def test_chat_without_api_key_is_unavailable():
    service = AIService(Config())

    assert service.available is False
    assert await service.init() is False
    assert service.available is False

    with pytest.raises(RuntimeError, match="AI 服务未初始化"):
        await service.chat("PROMPT")


async def test_init_builds_async_client():
    service = AIService(Config(group_insight_ai_api_key="test-key"))

    assert await service.init() is True
    assert service.available is True
    assert str(service.client.base_url).rstrip("/") == "https://api.openai.com/v1"

    # 已初始化时重复调用不重建客户端
    client = service.client
    assert await service.init() is True
    assert service.client is client


async def test_init_uses_custom_base_url():
    service = AIService(
        Config(group_insight_ai_api_key="test-key", group_insight_ai_base_url="https://opencode.ai/zen/go/v1")
    )

    assert await service.init() is True
    assert str(service.client.base_url).rstrip("/") == "https://opencode.ai/zen/go/v1"
