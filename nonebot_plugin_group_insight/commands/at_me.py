"""谁艾特我（对应原作 ``apps/at-me.js``）。

仅群聊可用；以合并转发消息返回被 @ 的记录，目标用户由 :func:`resolve_at_target` 决定
（@ 了 bot → bot 自己；@ 了别人 → 被 @ 者；否则自己）。
"""

from __future__ import annotations

from nonebot import on_regex
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.log import logger
from nonebot.matcher import Matcher

from ..at_tracker import build_at_forward_messages, resolve_at_target
from ..collector import get_collector
from ..config import get_config
from .common import AT_ME_PATTERN, COLLECTION_DISABLED, is_group_event

at_me_matcher = on_regex(AT_ME_PATTERN, priority=10, block=True, rule=is_group_event)


@at_me_matcher.handle()
async def _handle_at_me(bot: Bot, event: GroupMessageEvent, matcher: Matcher) -> None:
    if not get_config().group_insight_collection_enabled:
        await matcher.finish(COLLECTION_DISABLED)

    target_user_id = resolve_at_target(event)
    records = await get_collector().get_at_records(event.group_id, target_user_id)
    if not records:
        await matcher.finish("目前还没有人艾特过")

    try:
        nodes = await build_at_forward_messages(records, bot=bot, group_id=event.group_id)
        await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=nodes)
    except Exception as e:
        logger.opt(exception=e).error(f"发送合并转发消息失败: {e}")
        await matcher.finish("发送消息失败，请查看日志")
