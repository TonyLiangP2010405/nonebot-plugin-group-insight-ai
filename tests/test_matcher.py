"""端到端：driver 启动钩子与群消息 matcher。"""

from __future__ import annotations

from conftest import make_event
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender
from nonebug import App

from nonebot_plugin_group_insight import collector as collector_module
from nonebot_plugin_group_insight.collector import MessageCollector
from nonebot_plugin_group_insight.config import Config
from nonebot_plugin_group_insight.storage import (
    close_default_storage,
    get_storage,
    init_default_storage,
    register_lifecycle,
    set_storage,
)


def test_register_lifecycle_is_idempotent():
    register_lifecycle()
    assert register_lifecycle() is False


async def test_init_default_storage_creates_database():
    set_storage(None)
    try:
        await init_default_storage()

        store = get_storage()
        assert store.db_path.name == "group_insight.db"
        assert store.db_path.exists()

        rows = await store._query("SELECT name FROM sqlite_master WHERE type = 'table'")
        assert {"messages", "reports", "batches", "cooldowns", "at_records", "at_pending"} <= {
            row["name"] for row in rows
        }
    finally:
        await close_default_storage()
        set_storage(None)


async def test_matcher_collects_group_message(app: App, storage):
    set_storage(storage)
    collector_module.set_collector(MessageCollector(config=Config(), storage=storage))
    try:
        event = make_event(message=Message([MessageSegment.text("端到端"), MessageSegment.at(2222)]))

        async with app.test_matcher() as ctx:
            bot = ctx.create_bot(base=Bot)
            ctx.receive_event(bot, event)

        assert [m["message"] for m in await storage.get_messages(2002, days=1)] == ["端到端"]
        records = await storage.get_at_records(2002, 2222)
        assert len(records) == 1
        assert records[0]["user_id"] == 1001
        assert records[0]["message"] == "端到端"
    finally:
        collector_module.set_collector(None)
        set_storage(None)


async def test_matcher_ignores_private_messages(app: App, storage):
    set_storage(storage)
    collector_module.set_collector(MessageCollector(config=Config(), storage=storage))
    try:
        event = PrivateMessageEvent(
            time=1700000000,
            self_id=10000,
            post_type="message",
            message_type="private",
            sub_type="friend",
            message_id=1,
            user_id=1001,
            message=Message("私聊消息"),
            original_message=Message("私聊消息"),
            raw_message="私聊消息",
            font=0,
            sender=Sender(user_id=1001, nickname="nick"),
        )

        async with app.test_matcher() as ctx:
            bot = ctx.create_bot(base=Bot)
            ctx.receive_event(bot, event)

        assert await storage.get_messages(2002, days=1) == []
    finally:
        collector_module.set_collector(None)
        set_storage(None)
