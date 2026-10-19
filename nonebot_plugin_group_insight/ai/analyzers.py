"""AI 分析器：话题 / 金句 / 用户称号（对应原作 ``services/analyzers/*.js``）。

三个分析器共用 ``BaseAnalyzer`` 的重试、JSON 解析与消息格式化逻辑；
prompt 全文见 :mod:`nonebot_plugin_group_insight.ai.prompts`。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any, Callable

from json_repair import repair_json
from nonebot.log import logger

from ..config import Config, get_config
from . import prompts
from .service import AIService

USER_CAPSULE_TEMPLATE = (
    '<span class="user-capsule">'
    '<img class="user-capsule-avatar" src="https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"'
    ' alt="{nickname}" onerror="this.style.display=\'none\'">'
    '<span class="user-capsule-name">{nickname}</span></span>'
)

CODE_BLOCK_JSON_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)
CODE_BLOCK_RE = re.compile(r"```\s*(.*?)\s*```", re.DOTALL)
ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

PURE_DIGITS_RE = re.compile(r"[0-9\s]+")
URL_RE = re.compile(r"https?://")
PURE_PUNCTUATION_RE = re.compile(r"[?!。,.、\s]+")


def _to_float(value: Any) -> float:
    """把 ``toFixed`` 产生的字符串比值转成 float（对应原作 ``parseFloat``）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class BaseAnalyzer:
    """AI 分析器基类：重试调用、JSON 解析、消息格式化。"""

    def __init__(self, ai_service: AIService, config: Config | None = None) -> None:
        self.ai_service = ai_service
        self.config = config or get_config()
        self.timeout = self.config.group_insight_ai_llm_timeout
        self.retries = self.config.group_insight_ai_llm_retries
        self.backoff = self.config.group_insight_ai_llm_backoff

    async def analyze(self, messages: list[dict[str, Any]], stats: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError("子类必须实现 analyze 方法")

    async def call_ai(self, prompt: str, temperature: float = 0.7) -> dict[str, Any] | None:
        """调用 AI（共 ``retries + 1`` 次，退避 ``backoff * attempt`` 秒），全失败返回 ``None``。"""
        last_error: Exception | None = None

        for attempt in range(1, self.retries + 2):
            try:
                logger.debug(f"[{type(self).__name__}] AI 调用 - 尝试 {attempt}/{self.retries + 1}")

                result = await self.ai_service.chat(prompt, temperature, self.timeout)

                if result and result.get("content"):
                    usage = result.get("usage") or {}
                    logger.info(f"[{type(self).__name__}] AI 调用成功 - Tokens: {usage.get('total_tokens', 'N/A')}")
                    return result

                raise RuntimeError("AI 返回内容为空")
            except Exception as e:
                last_error = e
                logger.warning(f"[{type(self).__name__}] AI 调用失败 (尝试 {attempt}): {e}")

                if attempt <= self.retries:
                    wait_time = self.backoff * attempt
                    logger.info(f"[{type(self).__name__}] {wait_time}秒后重试...")
                    await self.sleep(wait_time)

        logger.error(f"[{type(self).__name__}] AI 调用失败,已达最大重试次数: {last_error}")
        return None

    def parse_json(self, content: str | None) -> Any:
        """四级回退解析 AI 返回的 JSON，全部失败返回 ``None``。"""
        if not content:
            return None

        try:
            return json.loads(content)
        except (ValueError, TypeError):
            pass

        extracted: str | None = None
        try:
            match = CODE_BLOCK_JSON_RE.search(content) or CODE_BLOCK_RE.search(content)
            if match:
                extracted = match.group(1)
            else:
                array_match = ARRAY_RE.search(content)
                if array_match:
                    extracted = array_match.group(0)
                else:
                    object_match = OBJECT_RE.search(content)
                    if object_match:
                        extracted = object_match.group(0)

            if extracted:
                try:
                    return json.loads(extracted)
                except ValueError:
                    logger.info(f"[{type(self).__name__}] 尝试使用 json_repair 修复 JSON...")
                    repaired = self._repair(extracted)
                    if repaired is not None:
                        logger.info(f"[{type(self).__name__}] json_repair 修复成功！")
                        return repaired
                    logger.warning(f"[{type(self).__name__}] json_repair 修复失败")
        except Exception as e:
            logger.warning(f"[{type(self).__name__}] JSON 解析失败: {e}")

        logger.info(f"[{type(self).__name__}] 尝试对原始内容使用 json_repair...")
        repaired = self._repair(content)
        if repaired is not None:
            logger.info(f"[{type(self).__name__}] json_repair 修复原始内容成功！")
            return repaired

        logger.error(f"[{type(self).__name__}] 无法解析 JSON 响应")
        logger.debug(f"[{type(self).__name__}] 原始内容: {content[:200]}...")
        return None

    @staticmethod
    def _repair(text: str) -> Any:
        """``json_repair`` 修复并解析；结果不是对象/数组时视为失败。

        ``json_repair`` 对完全不是 JSON 的输入（如 ``hello``）会返回空字符串，
        原作 ``jsonrepair`` 此时抛错，这里用类型判断还原该语义。
        """
        try:
            parsed = json.loads(repair_json(text))
        except Exception:
            return None
        return parsed if isinstance(parsed, (dict, list)) else None

    def format_messages(
        self,
        messages: list[dict[str, Any]],
        include_time: bool = True,
        max_messages: int | None = None,
        filter: Callable[[dict[str, Any]], bool] | None = None,
    ) -> tuple[str, dict[str, str]]:
        """格式化消息为 ``[HH:MM] [user_id]: 消息``，返回 ``(text, user_map)``。

        始终使用 ``user_id`` 而非昵称；``user_map`` 为 ``user_id → 最新昵称``。
        """
        formatted: list[str] = []
        user_map: dict[str, str] = {}
        processed_count = 0

        for msg in messages:
            if filter is not None and not filter(msg):
                continue

            if max_messages and processed_count >= max_messages:
                break

            user_id = msg.get("user_id")
            user_map[str(user_id)] = msg.get("nickname") or ""

            line = ""

            if include_time:
                moment = datetime.fromtimestamp(int(msg.get("time") or 0))
                line += f"[{moment.hour:02d}:{moment.minute:02d}] "

            line += f"[{user_id}]: "
            line += str(msg.get("message") or "")

            links = msg.get("links") or []
            if links:
                link_texts = [
                    f"[{'小程序' if link.get('type') == 'miniapp' else '链接'}:{link.get('source') or '分享'}]"
                    for link in links
                ]
                line += " " + " ".join(link_texts)

            images = msg.get("images") or []
            if images:
                line += f" [图片x{len(images)}]"

            videos = msg.get("videos") or []
            if videos:
                line += f" [视频x{len(videos)}]"

            formatted.append(line)
            processed_count += 1

        return "\n".join(formatted), user_map

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class TopicAnalyzer(BaseAnalyzer):
    """话题分析器。"""

    async def analyze(self, messages: list[dict[str, Any]], stats: dict[str, Any] | None = None) -> dict[str, Any]:
        if not messages:
            logger.warning("[TopicAnalyzer] 消息列表为空")
            return {"topics": [], "usage": None}

        formatted_messages, user_map = self.format_messages(messages, include_time=True)

        prompt = self.build_prompt(formatted_messages)

        result = await self.call_ai(prompt, 0.7)

        if not result or not result.get("content"):
            logger.error("[TopicAnalyzer] AI 调用失败")
            return {"topics": [], "usage": None}

        topics = self.parse_json(result["content"])

        if not isinstance(topics, list):
            logger.error("[TopicAnalyzer] 返回格式错误,期望数组")
            return {"topics": [], "usage": result.get("usage")}

        valid_topics = []
        for topic in topics:
            if not isinstance(topic, dict) or not topic.get("topic") or not topic.get("detail"):
                continue

            contributors = []
            if isinstance(topic.get("contributors"), list):
                for raw_user_id in topic["contributors"][:5]:
                    user_id_str = str(raw_user_id).strip()
                    contributors.append(
                        {
                            "user_id": user_id_str if user_id_str in user_map else None,
                            "nickname": user_map.get(user_id_str) or user_id_str,
                        }
                    )

            valid_topics.append(
                {
                    "topic": str(topic["topic"]).strip(),
                    "contributors": contributors,
                    "detail": self.process_detail_user_references(str(topic["detail"]).strip(), user_map),
                }
            )

        logger.info(f"[TopicAnalyzer] 提取到 {len(valid_topics)} 个话题")

        return {"topics": valid_topics, "usage": result.get("usage")}

    def process_detail_user_references(self, detail: str, user_map: dict[str, str]) -> str:
        """把 ``detail`` 中的 ``[用户ID]`` 替换为带头像的 HTML 胶囊。"""

        def replace(match: re.Match) -> str:
            user_id = match.group(1)
            nickname = user_map.get(user_id)
            if nickname:
                return USER_CAPSULE_TEMPLATE.format(user_id=user_id, nickname=nickname)
            return match.group(0)

        return re.sub(r"\[(\d+)\]", replace, detail)

    def build_prompt(self, formatted_messages: str) -> str:
        return prompts.build_topic_prompt(formatted_messages)


class GoldenQuoteAnalyzer(BaseAnalyzer):
    """金句提取器。"""

    def __init__(self, ai_service: AIService, config: Config | None = None) -> None:
        super().__init__(ai_service, config)
        self.max_quotes = self.config.group_insight_max_golden_quotes
        self.min_length = self.config.group_insight_min_quote_length
        self.max_length = self.config.group_insight_max_quote_length

    def filter_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """前置过滤：长度、命令前缀、纯数字/纯链接、纯标点。"""
        filtered = []
        for msg in messages:
            text = str(msg.get("message") or "").strip()
            length = len(text)

            if length < self.min_length or length > self.max_length:
                continue

            if text.startswith(("#", "/", ".")):
                continue

            if PURE_DIGITS_RE.fullmatch(text) or URL_RE.match(text):
                continue

            if PURE_PUNCTUATION_RE.fullmatch(text):
                continue

            filtered.append(msg)
        return filtered

    async def analyze(self, messages: list[dict[str, Any]], stats: dict[str, Any] | None = None) -> dict[str, Any]:
        if not messages:
            logger.warning("[GoldenQuoteAnalyzer] 消息列表为空")
            return {"goldenQuotes": [], "usage": None}

        filtered_messages = self.filter_messages(messages)

        if not filtered_messages:
            logger.warning("[GoldenQuoteAnalyzer] 过滤后无可用消息")
            return {"goldenQuotes": [], "usage": None}

        logger.info(f"[GoldenQuoteAnalyzer] 过滤后剩余 {len(filtered_messages)} 条消息")

        formatted_messages, user_map = self.format_messages(filtered_messages, include_time=False)

        prompt = self.build_prompt(formatted_messages)

        result = await self.call_ai(prompt, 0.8)

        if not result or not result.get("content"):
            logger.error("[GoldenQuoteAnalyzer] AI 调用失败")
            return {"goldenQuotes": [], "usage": None}

        quotes = self.parse_json(result["content"])

        if not isinstance(quotes, list):
            logger.error("[GoldenQuoteAnalyzer] 返回格式错误,期望数组")
            return {"goldenQuotes": [], "usage": result.get("usage")}

        valid_quotes = []
        for quote in quotes:
            if not isinstance(quote, dict) or not quote.get("quote") or not quote.get("sender") or not quote.get("reason"):
                continue

            sender_id = str(quote["sender"]).strip()
            valid_quotes.append(
                {
                    "quote": str(quote["quote"]).strip(),
                    "sender": {
                        "user_id": sender_id if sender_id in user_map else None,
                        "nickname": user_map.get(sender_id) or sender_id,
                    },
                    "reason": str(quote["reason"]).strip(),
                }
            )

        valid_quotes = valid_quotes[: self.max_quotes]

        logger.info(f"[GoldenQuoteAnalyzer] 提取到 {len(valid_quotes)} 条金句")

        return {"goldenQuotes": valid_quotes, "usage": result.get("usage")}

    def build_prompt(self, formatted_messages: str) -> str:
        return prompts.build_golden_quote_prompt(formatted_messages, self.max_quotes)


class UserTitleAnalyzer(BaseAnalyzer):
    """用户称号分析器（基于行为模式分配称号与 MBTI）。"""

    def __init__(self, ai_service: AIService, config: Config | None = None) -> None:
        super().__init__(ai_service, config)
        self.max_titles = self.config.group_insight_max_user_titles
        self.min_messages = self.config.group_insight_min_messages_for_title

    async def analyze(self, messages: list[dict[str, Any]], stats: dict[str, Any] | None = None) -> dict[str, Any]:
        users = (stats or {}).get("users") or []
        if not users:
            logger.warning("[UserTitleAnalyzer] 无用户统计信息")
            return {"userTitles": [], "usage": None}

        active_users = [user for user in users if (user.get("messageCount") or 0) >= self.min_messages][:20]

        if not active_users:
            logger.warning("[UserTitleAnalyzer] 无活跃用户")
            return {"userTitles": [], "usage": None}

        logger.info(f"[UserTitleAnalyzer] 分析 {len(active_users)} 位活跃用户")

        user_descriptions = self.build_user_descriptions(active_users)

        user_id_to_nickname = {str(desc["user_id"]): desc["nickname"] for desc in user_descriptions}

        prompt = self.build_prompt(user_descriptions)

        result = await self.call_ai(prompt, 0.9)

        if not result or not result.get("content"):
            logger.error("[UserTitleAnalyzer] AI 调用失败")
            return {"userTitles": [], "usage": None}

        titles = self.parse_json(result["content"])

        if not isinstance(titles, list):
            logger.error("[UserTitleAnalyzer] 返回格式错误,期望数组")
            return {"userTitles": [], "usage": result.get("usage")}

        valid_titles = []
        for title in titles:
            if (
                not isinstance(title, dict)
                or not title.get("user_id")
                or not title.get("title")
                or not title.get("mbti")
                or not title.get("reason")
            ):
                continue

            user_id_str = str(title["user_id"]).strip()
            valid_titles.append(
                {
                    "user": user_id_to_nickname.get(user_id_str) or user_id_str,
                    "user_id": user_id_str if user_id_str in user_id_to_nickname else None,
                    "title": str(title["title"]).strip(),
                    "mbti": str(title["mbti"]).strip().upper(),
                    "reason": str(title["reason"]).strip(),
                }
            )

        valid_titles = valid_titles[: self.max_titles]

        logger.info(f"[UserTitleAnalyzer] 生成 {len(valid_titles)} 个用户称号")

        return {"userTitles": valid_titles, "usage": result.get("usage")}

    def build_user_descriptions(self, users: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """构建用户行为描述（含行为标签与百分比展示值）。"""
        top_message_count = (users[0].get("messageCount") or 0) if users else 0

        descriptions = []
        for user in users:
            night_ratio = _to_float(user.get("nightRatio"))
            emoji_ratio = _to_float(user.get("emojiRatio"))
            reply_ratio = _to_float(user.get("replyRatio"))
            share_ratio = _to_float(user.get("shareRatio"))

            tags = [
                tag
                for tag, matched in (
                    ("夜猫子", night_ratio > 0.3),
                    ("表情包达人", emoji_ratio > 0.5),
                    ("话痨", (user.get("messageCount") or 0) > top_message_count * 0.5),
                    ("长文爱好者", _to_float(user.get("avgLength")) > 50),
                    ("互动积极", reply_ratio > 0.3),
                    ("分享达人", share_ratio > 0.1),
                )
                if matched
            ]

            descriptions.append(
                {
                    "user_id": user.get("user_id"),
                    "nickname": user.get("nickname"),
                    "messageCount": user.get("messageCount"),
                    "avgLength": user.get("avgLength"),
                    "emojiRatio": f"{emoji_ratio * 100:.0f}%",
                    "replyRatio": f"{reply_ratio * 100:.0f}%",
                    "nightRatio": f"{night_ratio * 100:.0f}%",
                    "mostActiveHour": user.get("mostActiveHour"),
                    "linkCount": user.get("linkCount") or 0,
                    "videoCount": user.get("videoCount") or 0,
                    "shareRatio": f"{share_ratio * 100:.0f}%",
                    "tags": tags,
                }
            )

        return descriptions

    def build_prompt(self, user_descriptions: list[dict[str, Any]]) -> str:
        user_text = prompts.build_user_text(user_descriptions)
        return prompts.build_user_title_prompt(user_text, self.max_titles)
