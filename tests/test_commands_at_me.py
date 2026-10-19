"""命令层：谁艾特我 / 清除艾特数据（nonebug 端到端）。"""

from __future__ import annotations

import time

import nonebot
import pytest
from conftest import make_at_data, make_event, make_private_event
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebug import App

from nonebot_plugin_group_insight import collector as collector_module
from nonebot_plugin_group_insight.collector import MessageCollector
from nonebot_plugin_group_insight.commands import admin as command_admin
from nonebot_plugin_group_insight.commands import at_me as command_at_me
from nonebot_plugin_group_insight.config import Config
from nonebot_plugin_group_insight.storage import Storage, set_storage

GROUP_ID = 2002
USER_ID = 1001
SENDER_ID = 3333
NOW = int(time.time())


@pytest.fixture
def commands(live_storage: Storage):
    set_storage(live_storage)
    collector_module.set_collector(MessageCollector(config=Config(), storage=live_storage))
    yield live_storage
    collector_module.set_collector(None)
    set_storage(None)


async def seed_at_record(store: Storage, *, target_user_id: int = USER_ID, message: str = "在吗") -> None:
    await store.save_at_record(
        GROUP_ID,
        target_user_id,
        make_at_data(user_id=SENDER_ID, nickname="发起者", message=message, time=NOW),
    )


# ------------------------------------------------------------------ 谁艾特我


async def test_who_at_me_without_records(app: App, commands: Storage):
    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("谁艾特我"))

    async with app.test_matcher(command_at_me.at_me_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_send(event, "目前还没有人艾特过", result=None)
        ctx.receive_event(bot, event)


async def test_who_at_me_sends_forward_message(app: App, commands: Storage, monkeypatch):
    await seed_at_record(commands)
    nodes = [MessageSegment.node_custom(user_id=SENDER_ID, nickname="发起者", content=Message("在吗"))]
    built: list[dict] = []

    async def fake_build(records, *, bot=None, group_id=None):
        built.append({"count": len(records), "group_id": group_id})
        return nodes

    monkeypatch.setattr(command_at_me, "build_at_forward_messages", fake_build)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("谁@我"))

    async with app.test_matcher(command_at_me.at_me_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_api(
            "send_group_forward_msg", {"group_id": GROUP_ID, "messages": nodes}, result=None
        )
        ctx.receive_event(bot, event)

    assert built == [{"count": 1, "group_id": GROUP_ID}]


async def test_who_at_me_forward_failed(app: App, commands: Storage, monkeypatch):
    await seed_at_record(commands)
    nodes = [MessageSegment.node_custom(user_id=SENDER_ID, nickname="发起者", content=Message("在吗"))]

    async def fake_build(records, *, bot=None, group_id=None):
        return nodes

    monkeypatch.setattr(command_at_me, "build_at_forward_messages", fake_build)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("谁艾特我"))

    async with app.test_matcher(command_at_me.at_me_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_api(
            "send_group_forward_msg",
            {"group_id": GROUP_ID, "messages": nodes},
            exception=RuntimeError("发送失败"),
        )
        ctx.should_call_send(event, "发送消息失败，请查看日志", result=None)
        ctx.receive_event(bot, event)


async def test_who_at_me_collection_disabled(app: App, commands: Storage, monkeypatch):
    monkeypatch.setattr(
        command_at_me, "get_config", lambda: Config(group_insight_collection_enabled=False)
    )

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("谁艾特我"))

    async with app.test_matcher(command_at_me.at_me_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_send(event, "消息收集功能未启用", result=None)
        ctx.receive_event(bot, event)


async def test_who_at_me_ignores_private_messages(app: App, commands: Storage):
    event = make_private_event(user_id=USER_ID, message=Message("谁艾特我"))

    async with app.test_matcher(command_at_me.at_me_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule(command_at_me.at_me_matcher)


# ------------------------------------------------------------------ 清除艾特数据


async def test_clear_at_records_without_records(app: App, commands: Storage):
    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#清除艾特数据"))

    async with app.test_matcher(command_admin.clear_at_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_send(event, "你目前没有艾特记录", result=None)
        ctx.receive_event(bot, event)


async def test_clear_at_records_removes_own_records(app: App, commands: Storage):
    await seed_at_record(commands)
    await seed_at_record(commands, target_user_id=9999)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("清除at数据"))

    async with app.test_matcher(command_admin.clear_at_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_send(event, "已成功清除你的艾特记录", result=None)
        ctx.receive_event(bot, event)

    assert await commands.get_at_records(GROUP_ID, USER_ID) == []
    assert len(await commands.get_at_records(GROUP_ID, 9999)) == 1


async def test_clear_at_records_private(app: App, commands: Storage):
    event = make_private_event(user_id=USER_ID, message=Message("#清除艾特数据"))

    async with app.test_matcher(command_admin.clear_at_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_send(event, "此功能仅支持群聊使用", result=None)
        ctx.receive_event(bot, event)


async def test_clear_at_records_collection_disabled(app: App, commands: Storage, monkeypatch):
    monkeypatch.setattr(
        command_admin, "get_config", lambda: Config(group_insight_collection_enabled=False)
    )

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#清除艾特数据"))

    async with app.test_matcher(command_admin.clear_at_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_send(event, "消息收集功能未启用", result=None)
        ctx.receive_event(bot, event)


# ------------------------------------------------------------------ 清除全部艾特数据


async def test_clear_all_at_records(app: App, commands: Storage, monkeypatch):
    monkeypatch.setattr(nonebot.get_driver().config, "superusers", {str(USER_ID)}, raising=False)
    await seed_at_record(commands)
    await seed_at_record(commands, target_user_id=9999)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#清除全部艾特数据"))

    async with app.test_matcher(command_admin.clear_all_at_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_send(event, "已成功清除 2 条艾特记录", result=None)
        ctx.receive_event(bot, event)

    assert await commands.get_at_records(GROUP_ID, USER_ID) == []
    assert await commands.get_at_records(GROUP_ID, 9999) == []


async def test_clear_all_at_records_requires_superuser(app: App, commands: Storage):
    await seed_at_record(commands)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#清除全部艾特数据"))

    async with app.test_matcher(command_admin.clear_all_at_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.receive_event(bot, event)
        ctx.should_not_pass_permission(command_admin.clear_all_at_matcher)

    assert len(await commands.get_at_records(GROUP_ID, USER_ID)) == 1
