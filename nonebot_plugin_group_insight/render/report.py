"""报告渲染（对应原作 ``apps/report.js`` 的 ``renderReport`` 与 ``sendTextSummary``）。

- :func:`build_template_data`：把 ``perform_analysis`` 结果 / 存储中的报告组装为完整 templateData。
- :func:`render_report`：按 ``group_insight_summary_template`` 选模板，整页截图，异常返回 ``None``。
- :func:`send_text_summary`：渲染失败时的文本兜底（文案逐字对齐原作）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from nonebot.log import logger

from ..analysis.statistics import ActivityVisualizer
from ..config import Config, get_config
from .templates import (
    REPORT_TEMPLATE_VIEWPORTS,
    REPORT_VIEWPORT_HEIGHT,
    html_to_image,
    render_template_html,
)

DEFAULT_TEMPLATE = "default"


def build_template_data(
    analysis: dict[str, Any] | None,
    group_name: str = "未知群聊",
    date_label: str = "",
    query_date: str = "",
    *,
    model: str = "",
    saved_at: int | None = None,
    config: Config | None = None,
) -> dict[str, Any]:
    """组装报告模板数据（字段见 PORT_SPEC §6）。

    ``analysis`` 既可以是 ``perform_analysis`` 的返回值，也可以是存储层读出的报告
    （``{stats, topics, goldenQuotes, userTitles, messageCount, tokenUsage, savedAt, date}``）。
    """
    config = config or get_config()
    analysis = analysis or {}

    stats = analysis.get("stats") or {}
    basic = stats.get("basic") or {}
    hourly = stats.get("hourly") or {}
    links = stats.get("links") or {}

    activity_enabled = config.group_insight_activity_enabled
    activity_chart = ActivityVisualizer(config).prepare_chart_data(hourly) if activity_enabled else None

    date_range_start = (basic.get("dateRange") or {}).get("start") or ""
    date_range_end = (basic.get("dateRange") or {}).get("end") or ""
    date_range = (
        date_range_start if date_range_start == date_range_end else f"{date_range_start} ~ {date_range_end}"
    )

    timestamp = saved_at if saved_at else analysis.get("savedAt")
    create_time = (
        datetime.fromtimestamp(timestamp / 1000) if timestamp else datetime.now()
    ).strftime("%Y-%m-%d %H:%M:%S")

    raw_usage = analysis.get("tokenUsage")
    token_usage = None
    if raw_usage is not None:
        token_usage = {
            "prompt": raw_usage.get("prompt_tokens") or 0,
            "completion": raw_usage.get("completion_tokens") or 0,
            "total": raw_usage.get("total_tokens") or 0,
        }

    return {
        "model": model or "",
        "groupName": group_name or "未知群聊",

        # 基础统计
        "totalMessages": basic.get("totalMessages") or 0,
        "totalUsers": basic.get("totalUsers") or 0,
        "totalChars": basic.get("totalChars") or 0,
        "totalEmojis": basic.get("totalEmojis") or 0,
        "avgLength": basic.get("avgCharsPerMsg") or 0,
        "dateRange": date_range,
        "peakPeriod": hourly.get("peakPeriod") or "",

        # 链接和视频统计
        "totalLinks": links.get("total") or 0,
        "linksBySource": links.get("bySource") or {},
        "totalVideos": stats.get("videos") or 0,

        # 活跃度图表数据
        "enableActivityChart": activity_enabled and activity_chart is not None,
        "activityChart": activity_chart,

        # AI 分析结果
        "topics": _normalize_topics(analysis.get("topics")),
        "goldenQuotes": _normalize_quotes(analysis.get("goldenQuotes")),
        "userTitles": _normalize_titles(analysis.get("userTitles")),

        # 元数据
        "createTime": create_time,
        "tokenUsage": token_usage,

        # 调用方上下文（模板不使用，供文本兜底与日志使用）
        "dateLabel": date_label,
        "queryDate": query_date,
        "messageCount": analysis.get("messageCount") or basic.get("totalMessages") or 0,
    }


async def render_report(template_data: dict[str, Any], config: Config | None = None) -> bytes | None:
    """渲染群聊报告长图；任何异常返回 ``None``（调用方走文本兜底）。"""
    try:
        config = config or get_config()
        template_name = (config.group_insight_summary_template or DEFAULT_TEMPLATE).strip() or DEFAULT_TEMPLATE
        if template_name not in REPORT_TEMPLATE_VIEWPORTS:
            logger.warning(f"[报告] 未知的报告模板 {template_name}，回退到 {DEFAULT_TEMPLATE}")
            template_name = DEFAULT_TEMPLATE

        html = render_template_html(template_name, template_data)

        return await html_to_image(
            html,
            viewport={"width": REPORT_TEMPLATE_VIEWPORTS[template_name], "height": REPORT_VIEWPORT_HEIGHT},
            img_type=config.group_insight_render_img_type or "png",
            quality=config.group_insight_render_quality,
        )
    except Exception as e:
        logger.opt(exception=e).error(f"[报告] 渲染增强总结失败: {e}")
        return None


def send_text_summary(report: dict[str, Any] | None, date_label: str, query_date: str) -> str:
    """渲染失败时的文本摘要（对应原作 ``sendTextSummary``）。"""
    report = report or {}
    stats = report.get("stats") or {}
    basic = stats.get("basic") or {}

    text_summary = f"📊 {date_label}群聊报告\n\n"
    text_summary += f"消息总数: {basic.get('totalMessages') or report.get('messageCount') or 0}\n"
    text_summary += f"参与人数: {basic.get('totalUsers') or 0}\n"
    text_summary += f"日期: {query_date}\n\n"

    topics = report.get("topics") or []
    if topics:
        text_summary += "💬 热门话题:\n"
        for index, topic in enumerate(topics):
            text_summary += f"{index + 1}. {topic.get('topic')}\n"
        text_summary += "\n"

    titles = report.get("userTitles") or []
    if titles:
        text_summary += "🏆 群友称号:\n"
        for title in titles:
            text_summary += f"• {title.get('user')} - {title.get('title')} ({title.get('mbti')})\n"
        text_summary += "\n"

    quotes = report.get("goldenQuotes") or []
    if quotes:
        text_summary += "💎 群圣经:\n"
        for index, quote in enumerate(quotes):
            text_summary += f'{index + 1}. "{quote.get("quote")}" —— {_sender_name(quote.get("sender"))}\n'

    return text_summary


def _sender_name(sender: Any) -> str:
    """金句发送者展示名：字典取昵称（原作此处拼出 ``[object Object]``，属明显笔误）。"""
    if isinstance(sender, dict):
        return str(sender.get("nickname") or sender.get("user_id") or "")
    return "" if sender is None else str(sender)


def _normalize_topics(topics: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for topic in topics or []:
        if not isinstance(topic, dict):
            continue
        contributors = [
            {"user_id": contributor.get("user_id"), "nickname": contributor.get("nickname") or ""}
            for contributor in topic.get("contributors") or []
            if isinstance(contributor, dict)
        ]
        normalized.append(
            {
                "topic": topic.get("topic") or "",
                "detail": topic.get("detail") or "",
                "contributors": contributors,
            }
        )
    return normalized


def _normalize_quotes(quotes: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for quote in quotes or []:
        if not isinstance(quote, dict):
            continue
        sender = quote.get("sender")
        if not isinstance(sender, dict):
            sender = {"user_id": None, "nickname": _sender_name(sender)}
        normalized.append(
            {
                "quote": quote.get("quote") or "",
                "reason": quote.get("reason") or "",
                "sender": {
                    "user_id": sender.get("user_id"),
                    "nickname": sender.get("nickname") or "",
                },
            }
        )
    return normalized


def _normalize_titles(titles: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for title in titles or []:
        if not isinstance(title, dict):
            continue
        normalized.append(
            {
                "user_id": title.get("user_id"),
                "user": title.get("user") or "",
                "title": title.get("title") or "",
                "mbti": title.get("mbti") or "",
                "reason": title.get("reason") or "",
            }
        )
    return normalized
