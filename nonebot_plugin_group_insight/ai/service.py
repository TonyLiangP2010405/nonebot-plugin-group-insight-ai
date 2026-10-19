"""AI 服务层（对应原作 ``services/aiService.js``）。

使用 ``openai`` Python SDK 的 ``AsyncOpenAI``，兼容所有提供 OpenAI 格式 API 的服务商。
未配置 ``api_key`` 时服务不可用（``available`` 为 ``False``），分析器随之停用。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from nonebot.log import logger

from ..config import Config, get_config

DEFAULT_LLM_TIMEOUT = 100
PLUGIN_VERSION = "0.1.3"


def _build_default_headers(config: Config, api_key: str) -> dict[str, str]:
    """构造客户端默认请求头。

    OpenCode Go（``opencode.ai``）要求 ``x-opencode-session`` 头做路由亲和与
    prompt 缓存，缺了会被 400 拒绝（MissingSessionID）；并要求客户端用自己的
    User-Agent 而不是通用 SDK 名。session id 未配置时按 API Key 生成稳定 UUID，
    保证跨重启不变。
    """
    headers = {"User-Agent": f"nonebot-plugin-group-insight/{PLUGIN_VERSION}"}
    session_id = config.group_insight_ai_session_id
    if not session_id and "opencode.ai" in (config.group_insight_ai_base_url or ""):
        session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"group-insight:{api_key}"))
    if session_id:
        headers["x-opencode-session"] = session_id
    return headers


def _format_seconds(value: float) -> str:
    """把超时秒数渲染成 ``100`` / ``0.05`` 这样的紧凑形式。"""
    return f"{value:g}"


class AIService:
    """AI 服务：封装客户端初始化与单条 user 消息的对话请求。"""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or get_config()
        self.api_key = self.config.group_insight_ai_api_key
        self.model = self.config.group_insight_ai_model
        self.base_url = self.config.group_insight_ai_base_url
        # 配置为毫秒（原作），openai SDK 使用秒
        self.timeout = self.config.group_insight_ai_timeout / 1000
        self.client: Any = None
        self.initialized = False
        self._init_failed = False

    @property
    def available(self) -> bool:
        """服务是否可用（已配置 API Key 且客户端已初始化）。"""
        return self.initialized and self.client is not None

    async def init(self) -> bool:
        """初始化 AI 客户端；重复调用直接返回上次结果。"""
        if self.initialized:
            return True

        # 已经失败过（例如未配置 API Key）就不再重复尝试与告警
        if self._init_failed:
            return False

        if not self.api_key:
            logger.warning("AI API Key 未配置，AI 分析功能不可用")
            self._init_failed = True
            return False

        try:
            from openai import AsyncOpenAI
        except ImportError:  # pragma: no cover - 依赖缺失时才触发
            logger.error("openai SDK 未安装，请运行: pip install openai")
            self._init_failed = True
            return False

        try:
            self.client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url or None,
                timeout=self.timeout,
                default_headers=_build_default_headers(self.config, self.api_key),
            )
        except Exception as e:  # pragma: no cover - SDK 构造失败极少见
            logger.opt(exception=e).error("AI 客户端初始化失败")
            self._init_failed = True
            return False

        self.initialized = True
        logger.debug(f"AI 服务初始化成功，模型: {self.model}")
        return True

    async def chat(
        self,
        prompt: str,
        temperature: float = 0.7,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """通用聊天接口，返回 ``{"content": str, "usage": {...}}``。

        ``timeout`` 为秒级超时（默认取 ``group_insight_ai_llm_timeout``，100 秒），
        用 ``asyncio.wait_for`` 实现；超时抛 ``AI 请求超时 ({timeout}秒)``。
        """
        if not self.available and not await self.init():
            raise RuntimeError("AI 服务未初始化")

        configured_timeout = timeout if timeout is not None else self.config.group_insight_ai_llm_timeout
        # 原作 ``config.llm_timeout || 100``：0/None 都退回默认 100 秒
        effective_timeout = configured_timeout or DEFAULT_LLM_TIMEOUT

        try:
            return await asyncio.wait_for(self._make_request(prompt, temperature), effective_timeout)
        except asyncio.TimeoutError:
            logger.error(f"[AIService] AI 请求超时 ({_format_seconds(effective_timeout)}秒): {prompt[:100]}...")
            raise asyncio.TimeoutError(f"AI 请求超时 ({_format_seconds(effective_timeout)}秒)") from None
        except Exception as e:
            logger.error(f"[AIService] Chat 调用失败: {e}")
            raise

    async def _make_request(self, prompt: str, temperature: float) -> dict[str, Any]:
        """执行实际请求。

        不传 ``max_tokens``，交由服务商的默认上限控制：思考模型的推理 token 同样计入
        该上限，人为设值会把正文挤空。
        """
        response = await self.client.chat.completions.create(
            model=self.model,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )

        choice = response.choices[0] if response.choices else None
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        raw_usage = getattr(response, "usage", None)
        usage = {
            "prompt_tokens": getattr(raw_usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(raw_usage, "completion_tokens", 0) or 0,
            "total_tokens": getattr(raw_usage, "total_tokens", 0) or 0,
        }

        if not content:
            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason == "length":
                details = getattr(raw_usage, "completion_tokens_details", None)
                reasoning_tokens = getattr(details, "reasoning_tokens", None)
                reasoning_hint = f"（其中推理 {reasoning_tokens}）" if reasoning_tokens else ""
                raise RuntimeError(
                    f"AI 响应被截断 (finish_reason=length)，已消耗 {usage['completion_tokens']} tokens"
                    f"{reasoning_hint}，请提高服务商侧的输出长度上限"
                )
            raise RuntimeError(f"AI 返回内容为空 (finish_reason={finish_reason if finish_reason else 'unknown'})")

        return {"content": content, "usage": usage}


_ai_service: AIService | None = None


def get_ai_service() -> AIService:
    """获取 AI 服务单例（不主动初始化客户端，由 ``available`` 判断可用性）。"""
    global _ai_service
    if _ai_service is None:
        _ai_service = AIService(get_config())
    return _ai_service


def set_ai_service(service: AIService | None) -> None:
    """替换 AI 服务单例（测试或重新加载配置时使用）。"""
    global _ai_service
    _ai_service = service
