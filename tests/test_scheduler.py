"""定时任务：单群生成 / 发送逻辑与任务注册（bot 与渲染被 mock）。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from conftest import make_message
from nonebot.adapters.onebot.v11 import MessageSegment

from nonebot_plugin_group_insight import scheduler as scheduler_module
from nonebot_plugin_group_insight.config import Config
from nonebot_plugin_group_insight.scheduler import (
    DAILY_REPORT_JOB_ID,
    DAILY_SEND_JOB_ID,
    generate_group_report,
    scheduler,
    send_group_report,
)
from nonebot_plugin_group_insight.storage import Storage

GROUP_ID = 2002
USER_ID = 1001
TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


def make_analysis(message_count: int = 3) -> dict[str, Any]:
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
        "tokenUsage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class FakeReportService:
    def __init__(self, analysis: dict[str, Any] | None = make_analysis()) -> None:
        self.analysis = analysis
        self.calls: list[dict[str, Any]] = []

    async def perform_analysis(self, messages, group_id=None, date_str=None, force_regenerate=False):
        self.calls.append({"count": len(messages), "group_id": group_id, "date": date_str})
        return self.analysis


class FakeBot:
    """记录发送到群的图片。"""

    def __init__(self, group_name: str = "测试群") -> None:
        self.group_name = group_name
        self.sent: list[tuple[int, Any]] = []

    async def get_group_info(self, group_id: int) -> dict[str, Any]:
        return {"group_id": group_id, "group_name": self.group_name}

    async def send_group_msg(self, group_id: int, message: Any) -> None:
        self.sent.append((group_id, message))


async def seed_messages(store: Storage, count: int) -> None:
    for index in range(count):
        await store.save_message(GROUP_ID, make_message(user_id=USER_ID, text=f"消息{index}"))


# ------------------------------------------------------------------ 单群生成


async def test_generate_group_report_skips_insufficient_messages(live_storage: Storage):
    await seed_messages(live_storage, 1)
    service = FakeReportService()

    result = await generate_group_report(
        GROUP_ID,
        TODAY,
        config=Config(group_insight_schedule_min_messages=99),
        storage=live_storage,
        service=service,
    )

    assert result == {"groupId": GROUP_ID, "status": "skipped", "reason": "insufficient_messages"}
    assert service.calls == []
    assert await live_storage.get_report(GROUP_ID, TODAY) is None


async def test_generate_group_report_skips_when_locked(live_storage: Storage):
    await seed_messages(live_storage, 5)
    service = FakeReportService()
    assert await live_storage.acquire_generating_lock(GROUP_ID, TODAY) is True
    try:
        result = await generate_group_report(
            GROUP_ID,
            TODAY,
            config=Config(group_insight_schedule_min_messages=1),
            storage=live_storage,
            service=service,
        )
    finally:
        await live_storage.release_generating_lock(GROUP_ID, TODAY)

    assert result == {"groupId": GROUP_ID, "status": "skipped", "reason": "already_generating"}
    assert service.calls == []


async def test_generate_group_report_success(live_storage: Storage):
    await seed_messages(live_storage, 3)
    service = FakeReportService()

    result = await generate_group_report(
        GROUP_ID,
        TODAY,
        config=Config(group_insight_schedule_min_messages=1),
        storage=live_storage,
        service=service,
        bot=FakeBot(),
    )

    assert result["status"] == "success"
    assert result["messageCount"] == 3
    assert service.calls == [{"count": 3, "group_id": GROUP_ID, "date": TODAY}]

    report = await live_storage.get_report(GROUP_ID, TODAY)
    assert report is not None
    assert report["messageCount"] == 3

    cooldown = await live_storage.check_cooldown(GROUP_ID, date_str=TODAY)
    assert cooldown["lastGenerated"]["generatedBy"] == "scheduled"

    # 生成锁已释放
    assert await live_storage.acquire_generating_lock(GROUP_ID, TODAY) is True
    await live_storage.release_generating_lock(GROUP_ID, TODAY)


async def test_generate_group_report_analysis_failed(live_storage: Storage):
    await seed_messages(live_storage, 3)
    service = FakeReportService(analysis=None)

    result = await generate_group_report(
        GROUP_ID,
        TODAY,
        config=Config(group_insight_schedule_min_messages=1),
        storage=live_storage,
        service=service,
    )

    assert result == {"groupId": GROUP_ID, "status": "failed", "error": "analysis_failed"}
    assert await live_storage.get_report(GROUP_ID, TODAY) is None


async def test_generate_group_report_sends_immediately(live_storage: Storage, monkeypatch):
    await seed_messages(live_storage, 3)
    bot = FakeBot()

    async def fake_render(template_data, config=None):
        assert template_data["groupName"] == "测试群"
        return b"scheduled-image"

    monkeypatch.setattr(scheduler_module, "render_report", fake_render)

    result = await generate_group_report(
        GROUP_ID,
        TODAY,
        config=Config(group_insight_schedule_min_messages=1, group_insight_send_mode="immediate"),
        storage=live_storage,
        service=FakeReportService(),
        bot=bot,
    )

    assert result["status"] == "success"
    assert result["sent"] is True
    assert bot.sent == [(GROUP_ID, MessageSegment.image(b"scheduled-image"))]


# ------------------------------------------------------------------ 单群发送


async def test_send_group_report_not_found(live_storage: Storage):
    result = await send_group_report(GROUP_ID, YESTERDAY, storage=live_storage, bot=FakeBot())

    assert result == {"success": False, "error": "report_not_found"}


async def test_send_group_report_group_not_found(live_storage: Storage):
    await live_storage.save_report(GROUP_ID, YESTERDAY, {"stats": make_analysis()["stats"]})

    result = await send_group_report(GROUP_ID, YESTERDAY, storage=live_storage, bot=None)

    assert result == {"success": False, "error": "group_not_found"}


async def test_send_group_report_render_failed(live_storage: Storage, monkeypatch):
    await live_storage.save_report(GROUP_ID, YESTERDAY, {"stats": make_analysis()["stats"]})

    async def fake_render(template_data, config=None):
        return None

    monkeypatch.setattr(scheduler_module, "render_report", fake_render)

    result = await send_group_report(
        GROUP_ID, YESTERDAY, group_name="测试群", storage=live_storage, bot=FakeBot()
    )

    assert result == {"success": False, "error": "render_failed"}


async def test_send_group_report_success(live_storage: Storage, monkeypatch):
    await live_storage.save_report(GROUP_ID, YESTERDAY, {"stats": make_analysis()["stats"]})
    bot = FakeBot()

    async def fake_render(template_data, config=None):
        return b"daily-image"

    monkeypatch.setattr(scheduler_module, "render_report", fake_render)

    result = await send_group_report(GROUP_ID, YESTERDAY, storage=live_storage, bot=bot)

    assert result == {"success": True}
    assert bot.sent == [(GROUP_ID, MessageSegment.image(b"daily-image"))]


# ------------------------------------------------------------------ 任务注册与调度


def test_register_jobs_adds_daily_report_cron():
    jobs = {job.id: job for job in scheduler.get_jobs()}

    assert DAILY_REPORT_JOB_ID in jobs
    trigger = str(jobs[DAILY_REPORT_JOB_ID].trigger)
    assert "hour='23'" in trigger
    assert "minute='59'" in trigger

    # 默认 send_mode=disabled，不注册定时发送任务
    assert DAILY_SEND_JOB_ID not in jobs


async def test_scheduled_report_disabled_skips(live_storage: Storage, monkeypatch):
    calls: list[int] = []

    async def fake_generate(group_id, target_date, **kwargs):
        calls.append(group_id)
        return {"groupId": group_id, "status": "success"}

    monkeypatch.setattr(scheduler_module, "get_config", lambda: Config(group_insight_schedule_enabled=False))
    monkeypatch.setattr(scheduler_module, "generate_group_report", fake_generate)

    await scheduler_module.scheduled_report()

    assert calls == []


async def test_scheduled_report_runs_whitelist(live_storage: Storage, monkeypatch):
    calls: list[int] = []

    async def fake_generate(group_id, target_date, **kwargs):
        calls.append(group_id)
        assert target_date == TODAY
        return {"groupId": group_id, "status": "success", "sent": None}

    monkeypatch.setattr(
        scheduler_module,
        "get_config",
        lambda: Config(
            group_insight_schedule_enabled=True,
            group_insight_schedule_whitelist=[1, 2, 3],
            group_insight_schedule_concurrency=2,
        ),
    )
    monkeypatch.setattr(scheduler_module, "generate_group_report", fake_generate)
    monkeypatch.setattr(scheduler_module, "get_bot", lambda: None)

    await scheduler_module.scheduled_report()

    assert sorted(calls) == [1, 2, 3]


async def test_scheduled_send_report_skips_when_mode_disabled(monkeypatch):
    calls: list[int] = []

    async def fake_send(group_id, target_date, **kwargs):
        calls.append(group_id)
        return {"success": True}

    monkeypatch.setattr(
        scheduler_module,
        "get_config",
        lambda: Config(
            group_insight_schedule_enabled=True,
            group_insight_schedule_whitelist=[1],
            group_insight_send_mode="disabled",
        ),
    )
    monkeypatch.setattr(scheduler_module, "send_group_report", fake_send)

    await scheduler_module.scheduled_send_report()

    assert calls == []


async def test_scheduled_send_report_uses_yesterday(monkeypatch):
    calls: list[tuple[int, str]] = []

    async def fake_send(group_id, target_date, **kwargs):
        calls.append((group_id, target_date))
        return {"success": True}

    monkeypatch.setattr(
        scheduler_module,
        "get_config",
        lambda: Config(
            group_insight_schedule_enabled=True,
            group_insight_schedule_whitelist=[1, 2],
            group_insight_send_mode="scheduled",
        ),
    )
    monkeypatch.setattr(scheduler_module, "send_group_report", fake_send)
    monkeypatch.setattr(scheduler_module, "get_bot", lambda: None)

    await scheduler_module.scheduled_send_report()

    assert calls == [(1, YESTERDAY), (2, YESTERDAY)]


@pytest.mark.parametrize("job_id", [DAILY_REPORT_JOB_ID])
def test_scheduler_job_is_replaceable(job_id):
    """重复注册不会报错（replace_existing=True）。"""
    scheduler_module.register_jobs()
    assert job_id in {job.id for job in scheduler.get_jobs()}
