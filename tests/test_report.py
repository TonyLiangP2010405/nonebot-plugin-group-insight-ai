"""报告编排测试：阈值短路、批次缓存/补全/重试、增量合并与阈值触发部分分析。

所有 AI 调用都由 ``FakeAIService`` 提供，不访问真实接口。
"""

from __future__ import annotations

import json

from conftest import START, day_str, make_message

from nonebot_plugin_group_insight import collector
from nonebot_plugin_group_insight.ai.analyzers import GoldenQuoteAnalyzer, TopicAnalyzer
from nonebot_plugin_group_insight.ai.service import AIService
from nonebot_plugin_group_insight.analysis import report as report_module
from nonebot_plugin_group_insight.analysis.report import ReportService, register_threshold_trigger
from nonebot_plugin_group_insight.analysis.statistics import StatisticsService
from nonebot_plugin_group_insight.config import Config

GROUP = 111
DATE = day_str(START)

TOPIC_JSON = json.dumps(
    [{"topic": "话题A", "contributors": ["1001"], "detail": "[1001] 说了点什么"}], ensure_ascii=False
)
QUOTE_JSON = json.dumps([{"quote": "金句一句", "sender": "1001", "reason": "有意思"}], ensure_ascii=False)
TITLE_JSON = json.dumps([{"user_id": "1001", "title": "话痨王", "mbti": "enfp", "reason": "话多"}], ensure_ascii=False)


class FakeAIService:
    """按 prompt 内容返回对应分析器的固定 JSON 结果。"""

    available = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat(self, prompt: str, temperature: float = 0.7, timeout: float | None = None) -> dict:
        self.calls.append(prompt)
        if "群圣经" in prompt:
            return {"content": QUOTE_JSON, "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}
        if "用户行为数据" in prompt:
            return {"content": TITLE_JSON, "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}
        if "主要话题" in prompt:
            return {"content": TOPIC_JSON, "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}
        raise AssertionError(f"未知 prompt: {prompt[:80]}")

    def calls_of(self, kind: str) -> list[str]:
        marker = {"topic": "主要话题", "quote": "群圣经", "title": "用户行为数据"}[kind]
        return [prompt for prompt in self.calls if marker in prompt]


class UnavailableAIService:
    available = False

    async def init(self) -> bool:
        return False


def messages(count: int, offset: int = 0) -> list[dict]:
    """构造 ``count`` 条消息（正文形如 ``M00 这是一条测试消息``，便于在 prompt 中定位）。"""
    result = []
    for index in range(offset, offset + count):
        user_id = 1001 if index % 2 == 0 else 1002
        nickname = "甲" if user_id == 1001 else "乙"
        result.append(
            make_message(user_id=user_id, nickname=nickname, text=f"M{index:02d} 这是一条测试消息", time=START + index)
        )
    return result


def batch_payload(index: int, topics=None, golden_quotes=None, token_usage=None, **extra) -> dict:
    payload = {
        "batchIndex": index,
        "startIndex": index * 10,
        "endIndex": (index + 1) * 10,
        "messageCount": 10,
        "topics": topics if topics is not None else [{"topic": f"批次{index}话题", "contributors": [], "detail": "d"}],
        "goldenQuotes": (
            golden_quotes
            if golden_quotes is not None
            else [{"quote": f"批次{index}金句", "sender": {"user_id": "1001", "nickname": "甲"}, "reason": "r"}]
        ),
        "tokenUsage": token_usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "analyzedAt": 1,
        "success": True,
    }
    payload.update(extra)
    return payload


# ------------------------------------------------------------------ 阈值与全量


async def test_perform_analysis_skips_below_min_threshold(storage):
    ai = FakeAIService()
    service = ReportService(
        config=Config(group_insight_min_messages_threshold=20), storage=storage, ai_service=ai
    )

    result = await service.perform_analysis(messages(5), GROUP, DATE)

    assert result["skipped"] is True
    assert result["reason"] == "消息数不足 (需要至少 20 条)"
    assert result["topics"] == []
    assert result["goldenQuotes"] == []
    assert result["userTitles"] == []
    assert result["stats"]["basic"]["totalMessages"] == 5
    assert "tokenUsage" not in result
    assert ai.calls == []


async def test_perform_analysis_full_mode(storage):
    ai = FakeAIService()
    service = ReportService(config=Config(), storage=storage, ai_service=ai)

    result = await service.perform_analysis(messages(25), GROUP, DATE)

    assert result["skipped"] is False
    assert result["useIncrementalAnalysis"] is False
    assert [topic["topic"] for topic in result["topics"]] == ["话题A"]
    assert result["topics"][0]["contributors"] == [{"user_id": "1001", "nickname": "甲"}]
    assert result["goldenQuotes"] == [
        {"quote": "金句一句", "sender": {"user_id": "1001", "nickname": "甲"}, "reason": "有意思"}
    ]
    assert result["userTitles"] == [{"user": "甲", "user_id": "1001", "title": "话痨王", "mbti": "ENFP", "reason": "话多"}]
    assert result["tokenUsage"] == {"prompt_tokens": 3, "completion_tokens": 6, "total_tokens": 9}
    assert len(ai.calls_of("topic")) == 1
    assert len(ai.calls_of("quote")) == 1
    assert len(ai.calls_of("title")) == 1


async def test_full_mode_truncates_to_last_max_messages(storage):
    ai = FakeAIService()
    service = ReportService(config=Config(group_insight_ai_max_messages=10), storage=storage, ai_service=ai)

    result = await service.perform_analysis(messages(25), None, None)

    assert result["useIncrementalAnalysis"] is False
    prompt = ai.calls_of("topic")[0]
    assert "M00" not in prompt
    assert "M15" in prompt
    assert "M24" in prompt


async def test_ai_unavailable_skips_ai_analysis(storage):
    service = ReportService(config=Config(), storage=storage, ai_service=UnavailableAIService())

    result = await service.perform_analysis(messages(25), GROUP, DATE)

    assert result["skipped"] is False
    assert result["topics"] == []
    assert result["goldenQuotes"] == []
    assert result["userTitles"] == []
    assert result["tokenUsage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


async def test_perform_analysis_returns_none_on_unexpected_error(storage, monkeypatch):
    def boom(self, msgs):
        raise RuntimeError("统计崩了")

    monkeypatch.setattr(StatisticsService, "analyze", boom)
    service = ReportService(config=Config(), storage=storage, ai_service=FakeAIService())

    assert await service.perform_analysis(messages(25), GROUP, DATE) is None


async def test_perform_analysis_with_real_ai_service_without_api_key(storage):
    service = ReportService(config=Config(), storage=storage, ai_service=AIService(Config()))

    result = await service.perform_analysis(messages(25), GROUP, DATE)

    assert result["skipped"] is False
    assert result["topics"] == []
    assert result["goldenQuotes"] == []
    assert result["userTitles"] == []
    assert result["tokenUsage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


async def test_disabled_analyzers_skip_ai_calls(storage):
    ai = FakeAIService()
    service = ReportService(
        config=Config(group_insight_topic_enabled=False, group_insight_golden_quote_enabled=False),
        storage=storage,
        ai_service=ai,
    )

    result = await service.perform_analysis(messages(25), GROUP, DATE)

    assert result["topics"] == []
    assert result["goldenQuotes"] == []
    assert ai.calls_of("topic") == []
    assert ai.calls_of("quote") == []
    assert len(ai.calls_of("title")) == 1


async def test_full_mode_topic_failure_degrades_to_empty(storage, monkeypatch):
    ai = FakeAIService()
    service = ReportService(config=Config(), storage=storage, ai_service=ai)

    async def boom(self, msgs, stats=None):
        raise RuntimeError("模型挂了")

    monkeypatch.setattr(TopicAnalyzer, "analyze", boom)

    result = await service.perform_analysis(messages(25), GROUP, DATE)

    assert result["topics"] == []
    assert [quote["quote"] for quote in result["goldenQuotes"]] == ["金句一句"]


# ------------------------------------------------------------------ 批次缓存


async def test_incremental_uses_cached_batches(storage):
    ai = FakeAIService()
    service = ReportService(config=Config(group_insight_ai_max_messages=10), storage=storage, ai_service=ai)
    await storage.save_batch(GROUP, DATE, 0, batch_payload(0))
    await storage.save_batch(GROUP, DATE, 1, batch_payload(1))

    result = await service.perform_analysis(messages(25), GROUP, DATE)

    assert result["useIncrementalAnalysis"] is True
    assert [topic["topic"] for topic in result["topics"]] == ["批次0话题", "批次1话题", "话题A"]
    assert [quote["quote"] for quote in result["goldenQuotes"]] == ["批次0金句", "批次1金句", "金句一句"]
    assert result["tokenUsage"] == {"prompt_tokens": 23, "completion_tokens": 16, "total_tokens": 39}
    # 仅增量部分调用 AI（批次缓存命中不消耗 token）
    assert len(ai.calls_of("topic")) == 1
    assert len(ai.calls_of("quote")) == 1
    assert len(ai.calls_of("title")) == 1


async def test_incremental_without_remaining_messages_skips_ai(storage):
    ai = FakeAIService()
    service = ReportService(config=Config(group_insight_ai_max_messages=10), storage=storage, ai_service=ai)
    await storage.save_batch(GROUP, DATE, 0, batch_payload(0))
    await storage.save_batch(GROUP, DATE, 1, batch_payload(1))

    result = await service.perform_analysis(messages(20), GROUP, DATE)

    assert result["useIncrementalAnalysis"] is True
    assert [topic["topic"] for topic in result["topics"]] == ["批次0话题", "批次1话题"]
    assert len(ai.calls_of("topic")) == 0
    assert len(ai.calls_of("quote")) == 0
    # 用户称号始终实时计算
    assert len(ai.calls_of("title")) == 1
    assert result["tokenUsage"] == {"prompt_tokens": 21, "completion_tokens": 12, "total_tokens": 33}


async def test_failed_batches_are_retried_once(storage):
    ai = FakeAIService()
    service = ReportService(config=Config(group_insight_ai_max_messages=10), storage=storage, ai_service=ai)
    await storage.save_batch(GROUP, DATE, 0, {"batchIndex": 0, "success": False, "error": "boom", "analyzedAt": 1})
    await storage.save_batch(
        GROUP, DATE, 1, {"batchIndex": 1, "success": False, "error": "boom", "analyzedAt": 1, "retried": True}
    )

    result = await service.perform_analysis(messages(25), GROUP, DATE)

    assert result["useIncrementalAnalysis"] is True
    retried = await storage.get_batch(GROUP, DATE, 0)
    assert retried["success"] is True
    assert retried["retried"] is True
    # 已重试过的失败批次不再覆盖
    skipped = await storage.get_batch(GROUP, DATE, 1)
    assert skipped["success"] is False
    assert skipped["analyzedAt"] == 1
    assert len(ai.calls_of("topic")) == 2  # 批次 0 补全 + 增量


async def test_missing_batch_is_filled_and_cached(storage):
    ai = FakeAIService()
    service = ReportService(config=Config(group_insight_ai_max_messages=10), storage=storage, ai_service=ai)
    await storage.save_batch(GROUP, DATE, 1, batch_payload(1))

    result = await service.perform_analysis(messages(25), GROUP, DATE)

    assert result["useIncrementalAnalysis"] is True
    filled = await storage.get_batch(GROUP, DATE, 0)
    assert filled["success"] is True
    assert filled["retried"] is True
    assert filled["startIndex"] == 0
    assert filled["endIndex"] == 10
    assert filled["messageCount"] == 10
    assert filled["tokenUsage"] == {"prompt_tokens": 2, "completion_tokens": 4, "total_tokens": 6}
    assert [topic["topic"] for topic in result["topics"]] == ["话题A", "批次1话题"]


async def test_force_regenerate_ignores_batch_cache(storage):
    ai = FakeAIService()
    service = ReportService(config=Config(group_insight_ai_max_messages=10), storage=storage, ai_service=ai)
    await storage.save_batch(GROUP, DATE, 0, batch_payload(0))
    await storage.save_batch(GROUP, DATE, 1, batch_payload(1))

    result = await service.perform_analysis(messages(25), GROUP, DATE, force_regenerate=True)

    assert result["useIncrementalAnalysis"] is True
    assert len(ai.calls_of("topic")) == 3  # 批次 0 + 批次 1 + 增量
    overwritten = await storage.get_batch(GROUP, DATE, 0)
    assert overwritten["retried"] is True
    assert overwritten["topics"] == [{"topic": "话题A", "contributors": [{"user_id": "1001", "nickname": "甲"}], "detail": (
        '<span class="user-capsule"><img class="user-capsule-avatar"'
        ' src="https://q1.qlogo.cn/g?b=qq&nk=1001&s=100" alt="甲"'
        " onerror=\"this.style.display='none'\">"
        '<span class="user-capsule-name">甲</span></span> 说了点什么'
    )}]


async def test_batch_failure_falls_back_to_full_analysis(storage, monkeypatch):
    ai = FakeAIService()
    service = ReportService(config=Config(group_insight_ai_max_messages=10), storage=storage, ai_service=ai)

    async def boom(*args, **kwargs):
        raise RuntimeError("批次崩了")

    monkeypatch.setattr(ReportService, "analyze_missing_batches", boom)

    result = await service.perform_analysis(messages(25), GROUP, DATE)

    assert result["useIncrementalAnalysis"] is False
    prompt = ai.calls_of("topic")[0]
    assert "M00" not in prompt
    assert "M24" in prompt


async def test_user_titles_use_full_message_set(storage):
    ai = FakeAIService()
    service = ReportService(config=Config(group_insight_ai_max_messages=10), storage=storage, ai_service=ai)
    await storage.save_batch(GROUP, DATE, 0, batch_payload(0))
    await storage.save_batch(GROUP, DATE, 1, batch_payload(1))

    await service.perform_analysis(messages(25), GROUP, DATE)

    title_prompt = ai.calls_of("title")[0]
    assert "1. 用户ID: 1001" in title_prompt
    assert "   - 消息数: 13 条" in title_prompt
    assert "   - 消息数: 12 条" in title_prompt


# ------------------------------------------------------------------ 合并算法


def test_merge_topics_appends_detail_and_dedupes_contributors():
    service = ReportService(config=Config())
    cached = [{"topic": "天气", "detail": "A", "contributors": [{"user_id": "1", "nickname": "甲"}]}]
    incremental = [
        {
            "topic": "天气",
            "detail": "B",
            "contributors": [{"user_id": "1", "nickname": "甲"}, {"user_id": None, "nickname": "乙"}],
        },
        {"topic": "新话题", "detail": "C", "contributors": []},
    ]

    merged = service.merge_topics(cached, incremental)

    assert [topic["topic"] for topic in merged] == ["天气", "新话题"]
    assert merged[0]["detail"] == "A\n\n[后续]: B"
    assert merged[0]["contributors"] == [{"user_id": "1", "nickname": "甲"}, {"user_id": None, "nickname": "乙"}]
    assert merged[1] == {"topic": "新话题", "detail": "C", "contributors": []}


def test_merge_golden_quotes_dedupes_by_sender_and_text():
    service = ReportService(config=Config())
    cached = [{"quote": "Q1", "sender": {"user_id": "1", "nickname": "甲"}, "reason": "r1"}]
    incremental = [
        {"quote": "Q1", "sender": {"user_id": "1", "nickname": "甲"}, "reason": "r2"},
        {"quote": "Q1", "sender": {"user_id": None, "nickname": "甲"}, "reason": "r3"},
        {"quote": "Q2", "sender": {"user_id": "2", "nickname": "乙"}, "reason": "r4"},
        {"quote": "Q3", "sender": None, "reason": "r5"},
    ]

    merged = service.merge_golden_quotes(cached, incremental)

    assert [quote["reason"] for quote in merged] == ["r1", "r3", "r4", "r5"]


# -------------------------------------------------------------- 阈值触发部分分析


async def test_trigger_partial_analysis_range_stats_and_cache(storage, monkeypatch):
    ai = FakeAIService()
    service = ReportService(config=Config(group_insight_ai_max_messages=10), storage=storage, ai_service=ai)
    for record in messages(75):
        await storage.save_message(GROUP, record)

    captured: dict = {}

    async def fake_topic_analyze(self, msgs, stats=None):
        captured["topic"] = (list(msgs), stats)
        return {
            "topics": [{"topic": "T", "contributors": [], "detail": "D"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }

    async def fake_quote_analyze(self, msgs, stats=None):
        captured["quote"] = (list(msgs), stats)
        return {
            "goldenQuotes": [{"quote": "Q", "sender": {"user_id": "1001", "nickname": "甲"}, "reason": "r"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
        }

    monkeypatch.setattr(TopicAnalyzer, "analyze", fake_topic_analyze)
    monkeypatch.setattr(GoldenQuoteAnalyzer, "analyze", fake_quote_analyze)

    await service.trigger_partial_analysis(GROUP, 6, DATE)

    topic_messages, stats = captured["topic"]
    assert len(topic_messages) == 60  # [60-50, 70) → [10, 70)
    assert topic_messages[0]["message"].startswith("M10")
    assert topic_messages[-1]["message"].startswith("M69")
    assert stats == {"users": [{"user_id": 1001, "nickname": "甲"}, {"user_id": 1002, "nickname": "乙"}]}
    assert captured["quote"][0] == topic_messages
    assert captured["quote"][1] == stats

    cache = await storage.get_batch(GROUP, DATE, 6)
    assert cache["success"] is True
    assert "retried" not in cache
    assert cache["batchIndex"] == 6
    assert cache["startIndex"] == 60
    assert cache["endIndex"] == 70
    assert cache["messageCount"] == 60
    assert cache["topics"] == [{"topic": "T", "contributors": [], "detail": "D"}]
    assert cache["goldenQuotes"] == [{"quote": "Q", "sender": {"user_id": "1001", "nickname": "甲"}, "reason": "r"}]
    assert cache["tokenUsage"] == {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}


async def test_trigger_partial_analysis_writes_failure_marker(storage, monkeypatch):
    service = ReportService(config=Config(group_insight_ai_max_messages=10), storage=storage, ai_service=FakeAIService())
    for record in messages(25):
        await storage.save_message(GROUP, record)

    async def boom(self, msgs, stats=None):
        raise RuntimeError("模型挂了")

    async def ok(self, msgs, stats=None):
        return {"goldenQuotes": [], "usage": None}

    monkeypatch.setattr(TopicAnalyzer, "analyze", boom)
    monkeypatch.setattr(GoldenQuoteAnalyzer, "analyze", ok)

    await service.trigger_partial_analysis(GROUP, 0, DATE)

    cache = await storage.get_batch(GROUP, DATE, 0)
    assert cache["success"] is False
    assert cache["error"] == "模型挂了"
    assert "retried" not in cache


async def test_trigger_partial_analysis_skips_empty_batch(storage):
    service = ReportService(config=Config(group_insight_ai_max_messages=10), storage=storage, ai_service=FakeAIService())

    await service.trigger_partial_analysis(GROUP, 3, DATE)

    assert await storage.get_batch(GROUP, DATE, 3) is None


async def test_register_threshold_trigger_delegates(monkeypatch):
    called: dict = {}

    class FakeReportService:
        async def trigger_partial_analysis(self, group_id, batch_index, date_str):
            called.update(group_id=group_id, batch_index=batch_index, date_str=date_str)

    monkeypatch.setattr(report_module, "_report_service", FakeReportService())
    original = collector.on_threshold_trigger
    try:
        register_threshold_trigger()
        assert collector.on_threshold_trigger is report_module._threshold_trigger

        await collector.on_threshold_trigger(GROUP, 2, DATE)

        assert called == {"group_id": GROUP, "batch_index": 2, "date_str": DATE}
    finally:
        collector.set_threshold_trigger(original)
