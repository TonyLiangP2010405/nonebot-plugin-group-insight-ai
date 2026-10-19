"""命令层：群聊报告 / 强制生成报告（nonebug 端到端，AI、渲染与群信息被 mock）。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import nonebot
import pytest
from conftest import make_event, make_message, make_private_event
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebug import App

from nonebot_plugin_group_insight import collector as collector_module
from nonebot_plugin_group_insight.analysis import report as report_module
from nonebot_plugin_group_insight.collector import MessageCollector
from nonebot_plugin_group_insight.commands import report as command_report
from nonebot_plugin_group_insight.config import Config
from nonebot_plugin_group_insight.storage import Storage, set_storage

GROUP_ID = 2002
USER_ID = 1001
TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()

GROUP_INFO = {"group_id": GROUP_ID, "group_name": "测试群"}


def make_analysis(message_count: int = 3) -> dict[str, Any]:
    """构造 ``perform_analysis`` 的返回值。"""
    return {
        "stats": {
            "basic": {
                "totalMessages": message_count,
                "totalUsers": 1,
                "totalChars": 6,
                "totalEmojis": 0,
                "avgCharsPerMsg": 2.0,
                "dateRange": {"start": TODAY, "end": TODAY},
            },
            "hourly": {},
            "links": {"total": 0, "bySource": {}},
            "videos": 0,
        },
        "topics": [],
        "goldenQuotes": [],
        "userTitles": [],
        "skipped": False,
        "tokenUsage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class FakeReportService:
    """记录调用参数并返回固定分析结果。"""

    def __init__(self, analysis: dict[str, Any] | None = None) -> None:
        self.analysis = analysis if analysis is not None else make_analysis()
        self.calls: list[dict[str, Any]] = []

    async def perform_analysis(
        self,
        messages: list[dict[str, Any]],
        group_id: int | None = None,
        date_str: str | None = None,
        force_regenerate: bool = False,
    ) -> dict[str, Any] | None:
        self.calls.append(
            {
                "count": len(messages),
                "group_id": group_id,
                "date": date_str,
                "force_regenerate": force_regenerate,
            }
        )
        return self.analysis


@pytest.fixture
def commands(live_storage: Storage):
    """把命令层依赖指向临时存储与收集器。"""
    set_storage(live_storage)
    collector_module.set_collector(MessageCollector(config=Config(), storage=live_storage))
    yield live_storage
    collector_module.set_collector(None)
    report_module.set_report_service(None)
    set_storage(None)


@pytest.fixture
def superuser(monkeypatch):
    """把当前用户设为 SUPERUSER。"""
    config = nonebot.get_driver().config
    monkeypatch.setattr(config, "superusers", {str(USER_ID)}, raising=False)


async def seed_messages(
    store: Storage,
    count: int,
    *,
    date_str: str | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> None:
    """写入 ``count`` 条消息；``date_str`` 非空时写入该日期的消息桶。"""
    if date_str is None:
        for index in range(count):
            await store.save_message(GROUP_ID, make_message(user_id=USER_ID, text=f"消息{index}"))
        return

    assert monkeypatch is not None, "写入指定日期的消息需要 monkeypatch"
    with monkeypatch.context() as patched:
        patched.setattr(Storage, "_today", lambda self: date_str)
        for index in range(count):
            await store.save_message(GROUP_ID, make_message(user_id=USER_ID, text=f"消息{index}"))


# ------------------------------------------------------------------ 参数解析分支


async def test_group_report_without_messages(app: App, commands: Storage):
    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#群聊报告"))

    async with app.test_matcher(command_report.report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_api("get_group_info", {"group_id": GROUP_ID}, result=GROUP_INFO)
        ctx.should_call_send(event, "今天还没有消息，无法生成报告", result=None)
        ctx.receive_event(bot, event)


async def test_group_summary_alias_and_yesterday_label(app: App, commands: Storage):
    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#群聊总结 昨天"))

    async with app.test_matcher(command_report.report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_api("get_group_info", {"group_id": GROUP_ID}, result=GROUP_INFO)
        ctx.should_call_send(event, "昨天没有消息记录（未收集到消息）", result=None)
        ctx.receive_event(bot, event)


async def test_group_report_private_without_group_id(app: App, commands: Storage):
    event = make_private_event(user_id=USER_ID, message=Message("#群聊报告"))

    async with app.test_matcher(command_report.report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_send(event, "私聊中请指定群号，例如：#群聊报告 123456789", result=None)
        ctx.receive_event(bot, event)


async def test_group_report_remote_group_denied_for_non_superuser(app: App, commands: Storage):
    event = make_private_event(user_id=USER_ID, message=Message("#群聊报告 123456789"))

    async with app.test_matcher(command_report.report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_send(event, "仅主人可查看其他群的报告", result=None)
        ctx.receive_event(bot, event)


async def test_group_report_remote_group_allowed_for_superuser(app: App, commands: Storage, superuser):
    event = make_private_event(user_id=USER_ID, message=Message("#群聊报告 123456789"))

    async with app.test_matcher(command_report.report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_api("get_group_info", {"group_id": 123456789}, result={"group_name": "别的群"})
        ctx.should_call_send(event, "今天还没有消息，无法生成报告", result=None)
        ctx.receive_event(bot, event)


async def test_group_report_bot_not_in_group(app: App, commands: Storage, superuser):
    event = make_private_event(user_id=USER_ID, message=Message("#群聊报告 123456789"))

    async with app.test_matcher(command_report.report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_api(
            "get_group_info", {"group_id": 123456789}, exception=RuntimeError("群不存在")
        )
        ctx.should_call_send(event, "Bot 不在群 123456789 中，无法获取报告", result=None)
        ctx.receive_event(bot, event)


async def test_group_report_invalid_date(app: App, commands: Storage):
    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#群聊报告 2024-13-01"))

    async with app.test_matcher(command_report.report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_api("get_group_info", {"group_id": GROUP_ID}, result=GROUP_INFO)
        ctx.should_call_send(event, "日期格式错误，请使用：YYYY-MM-DD（如 2024-11-01）", result=None)
        ctx.receive_event(bot, event)


# ------------------------------------------------------------------ 生成与缓存


async def test_group_report_generates_today(app: App, commands: Storage, monkeypatch):
    service = FakeReportService()
    report_module.set_report_service(service)
    await seed_messages(commands, 3)

    rendered: list[dict[str, Any]] = []

    async def fake_render(template_data, config=None):
        rendered.append(template_data)
        return b"report-image"

    monkeypatch.setattr(command_report, "render_report", fake_render)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#群聊报告"))

    async with app.test_matcher(command_report.report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_api("get_group_info", {"group_id": GROUP_ID}, result=GROUP_INFO)
        ctx.should_call_send(event, "正在生成今天的群聊报告（3条消息），请稍候...", result=None)
        ctx.should_call_send(event, MessageSegment.image(b"report-image"), result=None)
        ctx.receive_event(bot, event)

    assert service.calls == [{"count": 3, "group_id": GROUP_ID, "date": TODAY, "force_regenerate": False}]
    assert rendered[0]["groupName"] == "测试群"

    saved = await commands.get_report(GROUP_ID, TODAY)
    assert saved is not None
    assert saved["messageCount"] == 3
    assert saved["tokenUsage"]["total_tokens"] == 15

    cooldown = await commands.check_cooldown(GROUP_ID, date_str=TODAY)
    assert cooldown["inCooldown"] is True
    assert cooldown["lastGenerated"]["generatedBy"] == "user"

    # 生成锁已释放
    assert await commands.acquire_generating_lock(GROUP_ID, TODAY) is True
    await commands.release_generating_lock(GROUP_ID, TODAY)


async def test_group_report_returns_cache_while_in_cooldown(app: App, commands: Storage, monkeypatch):
    service = FakeReportService()
    report_module.set_report_service(service)
    await commands.save_report(
        GROUP_ID,
        TODAY,
        {"stats": make_analysis(3)["stats"], "messageCount": 3, "tokenUsage": {"total_tokens": 15}},
    )
    await commands.set_cooldown(GROUP_ID, "user", 3, date_str=TODAY)

    async def fake_render(template_data, config=None):
        return b"cached-image"

    monkeypatch.setattr(command_report, "render_report", fake_render)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#群聊报告"))

    async with app.test_matcher(command_report.report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_api("get_group_info", {"group_id": GROUP_ID}, result=GROUP_INFO)
        ctx.should_call_send(event, MessageSegment.image(b"cached-image"), result=None)
        ctx.receive_event(bot, event)

    assert service.calls == []


async def test_group_report_locked(app: App, commands: Storage):
    await seed_messages(commands, 1)
    assert await commands.acquire_generating_lock(GROUP_ID, TODAY) is True
    try:
        event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#群聊报告"))

        async with app.test_matcher(command_report.report_matcher) as ctx:
            bot = ctx.create_bot(base=Bot)
            ctx.should_call_api("get_group_info", {"group_id": GROUP_ID}, result=GROUP_INFO)
            ctx.should_call_send(event, "报告正在生成中，请稍后再试", result=None)
            ctx.receive_event(bot, event)
    finally:
        await commands.release_generating_lock(GROUP_ID, TODAY)


async def test_group_report_analysis_failed(app: App, commands: Storage):
    class FailingService:
        async def perform_analysis(self, messages, group_id=None, date_str=None, force_regenerate=False):
            return None

    report_module.set_report_service(FailingService())
    await seed_messages(commands, 1)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#群聊报告"))

    async with app.test_matcher(command_report.report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_api("get_group_info", {"group_id": GROUP_ID}, result=GROUP_INFO)
        ctx.should_call_send(event, "正在生成今天的群聊报告（1条消息），请稍候...", result=None)
        ctx.should_call_send(event, "分析失败，请查看日志", result=None)
        ctx.receive_event(bot, event)

    assert await commands.get_report(GROUP_ID, TODAY) is None


async def test_group_report_history_regenerates_when_diff_large(app: App, commands: Storage, monkeypatch):
    service = FakeReportService()
    report_module.set_report_service(service)
    await commands.save_report(
        GROUP_ID,
        YESTERDAY,
        {"stats": make_analysis(100)["stats"], "messageCount": 100, "tokenUsage": {}},
    )
    await seed_messages(commands, 2, date_str=YESTERDAY, monkeypatch=monkeypatch)

    async def fake_render(template_data, config=None):
        return b"history-image"

    monkeypatch.setattr(command_report, "render_report", fake_render)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#群聊报告 昨天"))

    async with app.test_matcher(command_report.report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_api("get_group_info", {"group_id": GROUP_ID}, result=GROUP_INFO)
        ctx.should_call_send(event, "正在生成昨天的群聊报告（2条消息），请稍候...", result=None)
        ctx.should_call_send(event, MessageSegment.image(b"history-image"), result=None)
        ctx.receive_event(bot, event)

    assert service.calls[0]["date"] == YESTERDAY
    saved = await commands.get_report(GROUP_ID, YESTERDAY)
    assert saved is not None
    assert saved["messageCount"] == 2

    # 历史报告从不设置冷却
    cooldown = await commands.check_cooldown(GROUP_ID, date_str=YESTERDAY)
    assert cooldown["inCooldown"] is False


async def test_group_report_history_uses_cache_when_diff_small(app: App, commands: Storage, monkeypatch):
    service = FakeReportService()
    report_module.set_report_service(service)
    await commands.save_report(
        GROUP_ID,
        YESTERDAY,
        {"stats": make_analysis(1)["stats"], "messageCount": 1, "tokenUsage": {}},
    )
    await seed_messages(commands, 1, date_str=YESTERDAY, monkeypatch=monkeypatch)

    async def fake_render(template_data, config=None):
        return b"cache-image"

    monkeypatch.setattr(command_report, "render_report", fake_render)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#群聊报告 昨天"))

    async with app.test_matcher(command_report.report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_api("get_group_info", {"group_id": GROUP_ID}, result=GROUP_INFO)
        ctx.should_call_send(event, MessageSegment.image(b"cache-image"), result=None)
        ctx.receive_event(bot, event)

    assert service.calls == []


async def test_group_report_history_render_failure_falls_back_to_text(app: App, commands: Storage, monkeypatch):
    await commands.save_report(
        GROUP_ID,
        YESTERDAY,
        {
            "stats": make_analysis(1)["stats"],
            "topics": [{"topic": "话题A", "detail": "", "contributors": []}],
            "messageCount": 1,
            "tokenUsage": {},
        },
    )

    async def fake_render(template_data, config=None):
        return None

    monkeypatch.setattr(command_report, "render_report", fake_render)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#群聊报告 昨天"))

    async with app.test_matcher(command_report.report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_api("get_group_info", {"group_id": GROUP_ID}, result=GROUP_INFO)
        ctx.should_call_send(
            event,
            f"📊 昨天群聊报告\n\n消息总数: 1\n参与人数: 1\n日期: {YESTERDAY}\n\n💬 热门话题:\n1. 话题A\n\n",
            result=None,
        )
        ctx.receive_event(bot, event)


# ------------------------------------------------------------------ 强制生成


async def test_force_report_requires_superuser(app: App, commands: Storage):
    """非主人不触发强制生成（permission 拦截，无任何回复）。"""
    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#强制生成报告"))

    async with app.test_matcher(command_report.force_report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.receive_event(bot, event)
        ctx.should_not_pass_permission(command_report.force_report_matcher)


async def test_force_report_generates(app: App, commands: Storage, superuser, monkeypatch):
    service = FakeReportService()
    report_module.set_report_service(service)
    await seed_messages(commands, 2)

    async def fake_render(template_data, config=None):
        return b"force-image"

    monkeypatch.setattr(command_report, "render_report", fake_render)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#强制生成报告"))

    async with app.test_matcher(command_report.force_report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_api("get_group_info", {"group_id": GROUP_ID}, result=GROUP_INFO)
        ctx.should_call_send(event, "正在强制生成今天的群聊报告，请稍候...", result=None)
        ctx.should_call_send(event, MessageSegment.image(b"force-image"), result=None)
        ctx.receive_event(bot, event)

    assert service.calls[0]["force_regenerate"] is True
    cooldown = await commands.check_cooldown(GROUP_ID, date_str=TODAY)
    assert cooldown["lastGenerated"]["generatedBy"] == "master"


async def test_force_report_without_messages(app: App, commands: Storage, superuser):
    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#强制生成报告"))

    async with app.test_matcher(command_report.force_report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_api("get_group_info", {"group_id": GROUP_ID}, result=GROUP_INFO)
        ctx.should_call_send(event, "正在强制生成今天的群聊报告，请稍候...", result=None)
        ctx.should_call_send(event, "今天还没有消息，无法生成报告", result=None)
        ctx.receive_event(bot, event)


async def test_group_report_args_order_agnostic(app: App, commands: Storage, superuser):
    """群号与日期顺序无关（对应规格「均可省略、顺序无关」）。"""
    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#群聊报告 昨天 123456789"))

    async with app.test_matcher(command_report.report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_api(
            "get_group_info", {"group_id": 123456789}, result={"group_name": "别的群"}
        )
        ctx.should_call_send(event, "昨天没有消息记录（未收集到消息）", result=None)
        ctx.receive_event(bot, event)


async def test_group_report_remote_group_hint(app: App, commands: Storage, superuser, monkeypatch):
    """远程查询（群内指定其他群）时进度文案带 `` [群名]`` 提示。"""
    report_module.set_report_service(FakeReportService())
    await commands.save_message(123456789, make_message(user_id=USER_ID, text="hi"))

    async def fake_render(template_data, config=None):
        return b"remote-image"

    monkeypatch.setattr(command_report, "render_report", fake_render)

    event = make_event(user_id=USER_ID, group_id=GROUP_ID, message=Message("#群聊报告 123456789"))

    async with app.test_matcher(command_report.report_matcher) as ctx:
        bot = ctx.create_bot(base=Bot)
        ctx.should_call_api(
            "get_group_info", {"group_id": 123456789}, result={"group_name": "别的群"}
        )
        ctx.should_call_send(event, "正在生成今天的群聊报告 [别的群]（1条消息），请稍候...", result=None)
        ctx.should_call_send(event, MessageSegment.image(b"remote-image"), result=None)
        ctx.receive_event(bot, event)
