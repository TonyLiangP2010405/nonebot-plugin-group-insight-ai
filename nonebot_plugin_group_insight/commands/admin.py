"""艾特记录清理命令（对应原作 ``apps/admin.js``）。

- ``#清除艾特数据``：清除自己被 @ 的记录，仅群聊可用。
- ``#清除全部艾特数据``：仅主人，清除所有 @ 记录。
"""

from __future__ import annotations

from nonebot import on_regex
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.matcher import Matcher
from nonebot.permission import SUPERUSER

from ..collector import get_collector
from ..config import get_config
from .common import CLEAR_ALL_AT_PATTERN, CLEAR_AT_PATTERN, COLLECTION_DISABLED

clear_at_matcher = on_regex(CLEAR_AT_PATTERN, priority=10, block=True)
clear_all_at_matcher = on_regex(CLEAR_ALL_AT_PATTERN, priority=10, block=True, permission=SUPERUSER)


@clear_at_matcher.handle()
async def _handle_clear_at(bot: Bot, event: Event, matcher: Matcher) -> None:
    if not isinstance(event, GroupMessageEvent):
        await matcher.finish("此功能仅支持群聊使用")

    if not get_config().group_insight_collection_enabled:
        await matcher.finish(COLLECTION_DISABLED)

    deleted = await get_collector().clear_at_records(event.group_id, event.user_id)
    if deleted > 0:
        await matcher.finish("已成功清除你的艾特记录")
    await matcher.finish("你目前没有艾特记录")


@clear_all_at_matcher.handle()
async def _handle_clear_all_at(bot: Bot, event: Event, matcher: Matcher) -> None:
    if not get_config().group_insight_collection_enabled:
        await matcher.finish(COLLECTION_DISABLED)

    count = await get_collector().clear_all_at_records()
    await matcher.finish(f"已成功清除 {count} 条艾特记录")
