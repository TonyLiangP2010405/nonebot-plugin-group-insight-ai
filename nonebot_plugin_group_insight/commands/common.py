"""命令层共用工具：命令正则、报告参数解析、群信息与权限查询。

命令正则全部锚定全匹配（``^...$``），与原作 Yunzai 的 ``reg`` 语义一致；日期与群号
均可省略且顺序无关，由 :func:`parse_report_params` 按 token 形状分类。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.log import logger
from nonebot.permission import SUPERUSER

# ------------------------------------------------------------------ 命令正则

GROUP_ID_PATTERN = r"\d{5,12}"
DATE_TOKEN_PATTERN = r"今天|昨天|前天|\d{4}-\d{2}-\d{2}"
_ARG_TOKEN_PATTERN = rf"(?:{GROUP_ID_PATTERN}|{DATE_TOKEN_PATTERN})"
_REPORT_ARGS_PATTERN = rf"\s*(?P<arg1>{_ARG_TOKEN_PATTERN})?\s*(?P<arg2>{_ARG_TOKEN_PATTERN})?"

GROUP_REPORT_PATTERN = rf"^#群聊(?:总结|报告){_REPORT_ARGS_PATTERN}$"
FORCE_REPORT_PATTERN = rf"^#强制生成报告{_REPORT_ARGS_PATTERN}$"
WORDCLOUD_PATTERN = r"^#(?:群聊)?词云\s*(?:当天|三天|七天)?$"
PERSONAL_WORDCLOUD_PATTERN = r"^#个人词云\s*(?:当天|三天|七天)?$"
AT_ME_PATTERN = r"^(谁|哪个.*)(艾特|@|at)(我|他|她|它)$"
CLEAR_AT_PATTERN = r"^#?清除(艾特|at)数据$"
CLEAR_ALL_AT_PATTERN = r"^#?清除全部(艾特|at)数据$"

# 冷却与生成锁使用的日期键：均为查询日期（今天 / 昨天 / 指定日）
LOCK_TTL_SECONDS = 300

# 渲染失败时的文本兜底、进度提示等共用文案
PRIVATE_GROUP_REQUIRED = "私聊中请指定群号，例如：#群聊报告 123456789"
REMOTE_GROUP_DENIED = "仅主人可查看其他群的报告"
INVALID_DATE = "日期格式错误，请使用：YYYY-MM-DD（如 2024-11-01）"
GROUP_NOT_FOUND = "Bot 不在群 {group_id} 中，无法获取报告"
COLLECTION_DISABLED = "消息收集功能未启用"

ReplyFunc = Callable[[str], Awaitable[Any]]


@dataclass
class ReportParams:
    """报告命令的参数解析结果。"""

    group_id: int
    group_name: str
    query_date: str
    date_label: str
    is_today: bool
    is_remote: bool


# ---------------------------------------------------------------- 基础判断


def is_group_event(event: Event) -> bool:
    """命令是否仅在群聊中响应（对应原作 ``event: 'message.group'``）。"""
    return isinstance(event, GroupMessageEvent)


async def is_superuser(bot: Bot, event: Event) -> bool:
    """当前用户是否为主人（``SUPERUSER``）。"""
    try:
        return bool(await SUPERUSER(bot, event))
    except Exception as e:  # pragma: no cover - 权限判断异常按非主人处理
        logger.opt(exception=e).debug("判断主人权限失败")
        return False


async def fetch_group_info(bot: Bot | None, group_id: int) -> tuple[bool, str]:
    """查询群信息，返回 ``(Bot 是否在群内, 群名)``。

    OneBot 侧以 ``get_group_info`` 是否抛异常判断 Bot 是否在群；群名取 ``group_name``，
    失败时降级为 ``群{group_id}``。
    """
    fallback = f"群{group_id}"
    if bot is None:
        return False, fallback

    try:
        info = await bot.get_group_info(group_id=group_id)
    except Exception as e:
        logger.debug(f"获取群 {group_id} 信息失败: {e}")
        return False, fallback

    if not isinstance(info, dict) or not info:
        return False, fallback
    return True, str(info.get("group_name") or fallback)


# ---------------------------------------------------------------- 参数解析


def parse_days(message: str) -> int:
    """解析词云天数：无匹配 / ``当天`` → 1，``三天`` → 3，``七天`` → 7。"""
    matched = re.search(r"(当天|三天|七天)", message or "")
    if matched is None:
        return 1
    if matched.group(1) == "三天":
        return 3
    if matched.group(1) == "七天":
        return 7
    return 1


def days_label(days: int) -> str:
    """天数标签（原作 ``days === 1 ? '当天' : days === 3 ? '三天' : '七天'``）。"""
    if days == 1:
        return "当天"
    if days == 3:
        return "三天"
    return "七天"


def date_label_for(query_date: str) -> str:
    """日期 → 展示标签：今天 / 昨天 / 前天 / ``YYYY年MM月DD日``。"""
    today = date.today()
    if query_date == today.isoformat():
        return "今天"
    if query_date == (today - timedelta(days=1)).isoformat():
        return "昨天"
    if query_date == (today - timedelta(days=2)).isoformat():
        return "前天"

    try:
        parsed = date.fromisoformat(query_date)
    except ValueError:
        return query_date
    return f"{parsed.year}年{parsed.month:02d}月{parsed.day:02d}日"


def resolve_query_date(date_str: str | None) -> tuple[str, str, bool]:
    """解析日期参数，返回 ``(query_date, date_label, is_today)``。

    非法日期抛 ``ValueError``（由调用方回复日期格式错误文案）。
    """
    today = date.today()
    if not date_str or date_str == "今天":
        return today.isoformat(), "今天", True
    if date_str == "昨天":
        return (today - timedelta(days=1)).isoformat(), "昨天", False
    if date_str == "前天":
        return (today - timedelta(days=2)).isoformat(), "前天", False

    try:
        parsed = date.fromisoformat(date_str)
    except ValueError as e:
        raise ValueError(f"日期格式错误: {date_str}") from e
    return (
        parsed.isoformat(),
        f"{parsed.year}年{parsed.month:02d}月{parsed.day:02d}日",
        parsed == today,
    )


def _iter_tokens(matched: re.Match[str] | None) -> Iterator[str]:
    """按出现顺序产出命令后的参数 token（群号 / 日期）。"""
    if matched is None:
        return
    groups = matched.groupdict()
    for name in ("arg1", "arg2"):
        token = groups.get(name)
        if token:
            yield token


async def parse_report_params(
    bot: Bot,
    event: Event,
    matched: re.Match[str] | None,
    *,
    reply: ReplyFunc,
) -> ReportParams | None:
    """解析报告命令的群号 / 日期参数并做权限与群校验。

    对应原作 ``parseReportParams``：权限不足、私聊缺群号、Bot 不在群、日期非法时
    已通过 ``reply`` 回复用户并返回 ``None``。
    """
    is_private = not isinstance(event, GroupMessageEvent)
    specified_group_id: int | None = None
    date_str: str | None = None

    for token in _iter_tokens(matched):
        if re.fullmatch(GROUP_ID_PATTERN, token):
            specified_group_id = int(token)
        else:
            date_str = token

    is_remote = False
    if specified_group_id is not None:
        is_remote = is_private or specified_group_id != event.group_id
        if is_remote and not await is_superuser(bot, event):
            await reply(REMOTE_GROUP_DENIED)
            return None
        target_group_id = specified_group_id
    elif is_private:
        await reply(PRIVATE_GROUP_REQUIRED)
        return None
    else:
        target_group_id = event.group_id

    in_group, group_name = await fetch_group_info(bot, target_group_id)
    if not in_group:
        await reply(GROUP_NOT_FOUND.format(group_id=target_group_id))
        return None

    try:
        query_date, date_label, is_today = resolve_query_date(date_str)
    except ValueError:
        await reply(INVALID_DATE)
        return None

    return ReportParams(
        group_id=target_group_id,
        group_name=group_name,
        query_date=query_date,
        date_label=date_label,
        is_today=is_today,
        is_remote=is_remote,
    )
