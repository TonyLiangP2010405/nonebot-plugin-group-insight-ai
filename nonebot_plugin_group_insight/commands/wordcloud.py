"""词云命令（对应原作 ``apps/wordcloud.js``）。

- ``#词云`` / ``#群聊词云``：按 ``当天|三天|七天``（默认当天）统计群内消息。
- ``#个人词云``：统计自己的消息，少于 5 条时直接提示无法生成。

两个命令均仅支持群聊（对应原作 ``event: 'message.group'``）。
"""

from __future__ import annotations

from nonebot import on_regex
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.exception import FinishedException
from nonebot.log import logger
from nonebot.matcher import Matcher

from ..collector import get_collector
from ..wordcloud_gen import (
    generate_personal_wordcloud_image,
    generate_wordcloud_image,
    is_wordcloud_available,
)
from .common import (
    PERSONAL_WORDCLOUD_PATTERN,
    WORDCLOUD_PATTERN,
    days_label,
    fetch_group_info,
    is_group_event,
    parse_days,
)

# 个人词云最少消息数（原作硬编码 5 条）
MIN_PERSONAL_MESSAGES = 5

wordcloud_matcher = on_regex(WORDCLOUD_PATTERN, priority=10, block=True, rule=is_group_event)
personal_wordcloud_matcher = on_regex(
    PERSONAL_WORDCLOUD_PATTERN, priority=10, block=True, rule=is_group_event
)


@wordcloud_matcher.handle()
async def _handle_wordcloud(bot: Bot, event: GroupMessageEvent, matcher: Matcher) -> None:
    if not is_wordcloud_available():
        await matcher.finish("词云功能未就绪")

    days = parse_days(str(event.get_message()))
    await matcher.send(f"正在生成{days_label(days)}的词云，请稍候...")

    try:
        messages = await get_collector().get_messages(event.group_id, days)
        if not messages:
            await matcher.finish(f"没有找到最近{days}天的消息记录")

        _, group_name = await fetch_group_info(bot, event.group_id)
        image = await generate_wordcloud_image(messages, group_name=group_name, days=days)
        if not image:
            await matcher.finish("词云生成失败，请查看日志")

        await matcher.finish(MessageSegment.image(image))
    except FinishedException:
        raise
    except Exception as e:
        logger.opt(exception=e).error(f"词云生成错误: {e}")
        await matcher.finish(f"词云生成失败: {e}")


@personal_wordcloud_matcher.handle()
async def _handle_personal_wordcloud(bot: Bot, event: GroupMessageEvent, matcher: Matcher) -> None:
    if not is_wordcloud_available():
        await matcher.finish("词云功能未就绪")

    days = parse_days(str(event.get_message()))
    user_id = event.user_id
    user_name = event.sender.nickname or f"用户{user_id}"

    await matcher.send(f"正在生成 {user_name} {days_label(days)}的个人词云，请稍候...")

    try:
        messages = await get_collector().get_recent_user_messages(event.group_id, user_id, 1, None, days)
        if not messages:
            await matcher.finish(f"您在最近{days}天内没有消息记录")
        if len(messages) < MIN_PERSONAL_MESSAGES:
            await matcher.finish(f"您在最近{days}天内的消息太少（仅{len(messages)}条），无法生成词云")

        _, group_name = await fetch_group_info(bot, event.group_id)
        image = await generate_personal_wordcloud_image(
            messages, group_name=group_name, user_name=user_name, days=days
        )
        if not image:
            await matcher.finish("词云生成失败，请查看日志")

        await matcher.finish(MessageSegment.image(image))
    except FinishedException:
        raise
    except Exception as e:
        logger.opt(exception=e).error(f"个人词云生成错误: {e}")
        await matcher.finish(f"个人词云生成失败: {e}")
