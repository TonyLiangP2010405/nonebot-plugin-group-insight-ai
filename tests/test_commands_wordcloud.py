"""命令层：词云 / 个人词云（nonebug 端到端，词云渲染被 mock）。"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import make_event, make_message, make_private_event
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebug import App

from nonebot_plugin_group_insight import collector as collector_module
from nonebot_plugin_group_insight.collector import MessageCollector
from nonebot_plugin_group_insight.commands import wordcloud as command_wordcloud
from nonebot_plugin_group_insight.config import Config
from nonebot_plugin_group_insight.storage import Storage, set_storage

GROUP_ID = 2002
USER_ID = 1001
GROUP_INFO = {"group_id": GROUP_ID, "group_name": "测试群"}


@pytest.fixture
def commands(live_storage: Storage):
    set_storage(live_storage)
    collector_module.set_collector(MessageCollector(config=Config(), storage=live_storage))
    yield live_storage
    collector_module.set_collector(None)
    set_storage(None)


async def seed_messages(store: Storage, count: int, *, user_id: int = USER_ID) -> None:
    for index in range(count):
        await store.save_message(GROUP_ID, make_message(user_id=user_id, text=f"消息{index}"))


async def test_wordcloud_without_messages(app: App, commands: Storage):
    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#词云"))

    async with app.test_matcher(command_wordcloud.wordcloud_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_send(event, "正在生成当天的词云，请稍候...", result=None)
        ctx.should_call_send(event, "没有找到最近1天的消息记录", result=None)
        ctx.receive_event(bot, event)


async def test_wordcloud_generates_image(app: App, commands: Storage, monkeypatch):
    await seed_messages(commands, 3)
    calls: list[dict[str, Any]] = []

    async def fake_generate(messages, *, group_name, days, config=None):
        calls.append({"count": len(messages), "group_name": group_name, "days": days})
        return b"wordcloud-image"

    monkeypatch.setattr(command_wordcloud, "generate_wordcloud_image", fake_generate)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#群聊词云 三天"))

    async with app.test_matcher(command_wordcloud.wordcloud_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_send(event, "正在生成三天的词云，请稍候...", result=None)
        ctx.should_call_api("get_group_info", {"group_id": GROUP_ID}, result=GROUP_INFO)
        ctx.should_call_send(event, MessageSegment.image(b"wordcloud-image"), result=None)
        ctx.receive_event(bot, event)

    assert calls == [{"count": 3, "group_name": "测试群", "days": 3}]


async def test_wordcloud_generate_failed(app: App, commands: Storage, monkeypatch):
    await seed_messages(commands, 1)

    async def fake_generate(messages, *, group_name, days, config=None):
        return None

    monkeypatch.setattr(command_wordcloud, "generate_wordcloud_image", fake_generate)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#词云"))

    async with app.test_matcher(command_wordcloud.wordcloud_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_send(event, "正在生成当天的词云，请稍候...", result=None)
        ctx.should_call_api("get_group_info", {"group_id": GROUP_ID}, result=GROUP_INFO)
        ctx.should_call_send(event, "词云生成失败，请查看日志", result=None)
        ctx.receive_event(bot, event)


async def test_wordcloud_not_ready(app: App, commands: Storage, monkeypatch):
    monkeypatch.setattr(command_wordcloud, "is_wordcloud_available", lambda: False)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#词云"))

    async with app.test_matcher(command_wordcloud.wordcloud_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_send(event, "词云功能未就绪", result=None)
        ctx.receive_event(bot, event)


async def test_wordcloud_ignores_private_messages(app: App, commands: Storage):
    event = make_private_event(user_id=USER_ID, message=Message("#词云"))

    async with app.test_matcher(command_wordcloud.wordcloud_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule(command_wordcloud.wordcloud_matcher)


async def test_personal_wordcloud_without_messages(app: App, commands: Storage):
    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#个人词云"))

    async with app.test_matcher(command_wordcloud.personal_wordcloud_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_send(event, "正在生成 nick 当天的个人词云，请稍候...", result=None)
        ctx.should_call_send(event, "您在最近1天内没有消息记录", result=None)
        ctx.receive_event(bot, event)


async def test_personal_wordcloud_too_few_messages(app: App, commands: Storage):
    await seed_messages(commands, 2)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#个人词云 三天"))

    async with app.test_matcher(command_wordcloud.personal_wordcloud_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_send(event, "正在生成 nick 三天的个人词云，请稍候...", result=None)
        ctx.should_call_send(event, "您在最近3天内的消息太少（仅2条），无法生成词云", result=None)
        ctx.receive_event(bot, event)


async def test_personal_wordcloud_generates_image(app: App, commands: Storage, monkeypatch):
    await seed_messages(commands, 5)

    async def fake_generate(messages, *, group_name, user_name, days, config=None):
        assert user_name == "nick"
        return b"personal-image"

    monkeypatch.setattr(command_wordcloud, "generate_personal_wordcloud_image", fake_generate)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#个人词云 七天"))

    async with app.test_matcher(command_wordcloud.personal_wordcloud_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_send(event, "正在生成 nick 七天的个人词云，请稍候...", result=None)
        ctx.should_call_api("get_group_info", {"group_id": GROUP_ID}, result=GROUP_INFO)
        ctx.should_call_send(event, MessageSegment.image(b"personal-image"), result=None)
        ctx.receive_event(bot, event)


async def test_personal_wordcloud_uses_user_fallback_name(app: App, commands: Storage, monkeypatch):
    """昵称为空时使用 ``用户{id}``（对应原作 ``e.sender?.nickname || 用户${id}``）。"""
    await seed_messages(commands, 5)

    async def fake_generate(messages, *, group_name, user_name, days, config=None):
        return b"personal-image"

    monkeypatch.setattr(command_wordcloud, "generate_personal_wordcloud_image", fake_generate)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#个人词云"), nickname="")

    async with app.test_matcher(command_wordcloud.personal_wordcloud_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_send(event, f"正在生成 用户{USER_ID} 当天的个人词云，请稍候...", result=None)
        ctx.should_call_api("get_group_info", {"group_id": GROUP_ID}, result=GROUP_INFO)
        ctx.should_call_send(event, MessageSegment.image(b"personal-image"), result=None)
        ctx.receive_event(bot, event)
