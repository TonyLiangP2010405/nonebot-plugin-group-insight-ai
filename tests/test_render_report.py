"""报告渲染测试：templateData 组装、模板选择/截图参数、文本兜底。

截图通过 monkeypatch 替换，不启动浏览器。
"""

from __future__ import annotations

import sys
import types
from datetime import datetime

from nonebot_plugin_group_insight.analysis.statistics import StatisticsService
from nonebot_plugin_group_insight.config import Config
from nonebot_plugin_group_insight.render import report as report_module
from nonebot_plugin_group_insight.render.report import build_template_data, render_report, send_text_summary
from nonebot_plugin_group_insight.render.templates import REPORT_TEMPLATE_VIEWPORTS, html_to_image

DAY = datetime(2023, 11, 14)


def at(hour: int, minute: int = 0) -> int:
    return int(DAY.replace(hour=hour, minute=minute).timestamp())


def sample_messages() -> list[dict]:
    return [
        {
            "user_id": 1001,
            "nickname": "小明",
            "message": "今天天气真不错",
            "time": at(9),
            "hour": 9,
            "length": 7,
            "hasReply": False,
            "faces": {"face": [], "mface": [], "emoji": [], "total": 0},
            "links": [],
            "videos": [],
        },
        {
            "user_id": 1002,
            "nickname": "小红",
            "message": "好呀好呀",
            "time": at(10),
            "hour": 10,
            "length": 4,
            "hasReply": False,
            "faces": {"face": [], "mface": [], "emoji": [], "total": 1},
            "links": [{"source": "B站"}],
            "videos": [],
        },
    ]


def sample_analysis(**overrides) -> dict:
    analysis = {
        "stats": StatisticsService(Config()).analyze(sample_messages()),
        "topics": [{"topic": "周末出行", "detail": "d", "contributors": [{"user_id": "1001", "nickname": "小明"}]}],
        "goldenQuotes": [{"quote": "好呀好呀", "reason": "敷衍", "sender": {"user_id": "1002", "nickname": "小红"}}],
        "userTitles": [
            {"user_id": "1001", "user": "小明", "title": "话痨王", "mbti": "ENFP", "reason": "话最多"},
            {"user_id": None, "user": "小红", "title": "夜猫子", "mbti": "INFP", "reason": "半夜活跃"},
        ],
        "tokenUsage": {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46},
        "messageCount": 2,
    }
    analysis.update(overrides)
    return analysis


# ------------------------------------------------------------ templateData 组装


def test_build_template_data_maps_basic_fields():
    data = build_template_data(sample_analysis(), group_name="测试群", model="gpt-4.1")

    assert data["groupName"] == "测试群"
    assert data["model"] == "gpt-4.1"
    assert data["totalMessages"] == 2
    assert data["totalUsers"] == 2
    assert data["totalEmojis"] == 1
    assert data["totalLinks"] == 1
    assert data["totalVideos"] == 0
    assert data["avgLength"] == "5.5"
    assert data["peakPeriod"] == "09:00-10:00"
    assert data["dateRange"] == "2023/11/14"
    assert data["linksBySource"] == {"B站": 1}
    assert data["messageCount"] == 2
    assert data["topics"][0]["topic"] == "周末出行"
    assert data["goldenQuotes"][0]["sender"] == {"user_id": "1002", "nickname": "小红"}
    assert data["userTitles"][0]["mbti"] == "ENFP"


def test_build_template_data_defaults_group_name_and_context_fields():
    data = build_template_data(sample_analysis(), date_label="今天", query_date="2023-11-14")

    assert data["groupName"] == "未知群聊"
    assert data["dateLabel"] == "今天"
    assert data["queryDate"] == "2023-11-14"


def test_build_template_data_date_range_span():
    stats = StatisticsService(Config()).analyze(
        [
            {**sample_messages()[0], "time": at(9), "hour": 9},
            {**sample_messages()[1], "time": int(DAY.replace(day=15, hour=10).timestamp()), "hour": 10},
        ]
    )
    data = build_template_data(sample_analysis(stats=stats))

    assert data["dateRange"] == "2023/11/14 ~ 2023/11/15"


def test_build_template_data_handles_empty_analysis():
    data = build_template_data(None)

    assert data["totalMessages"] == 0
    assert data["dateRange"] == ""
    assert data["activityChart"] is None
    assert data["topics"] == []
    assert data["goldenQuotes"] == []
    assert data["userTitles"] == []
    assert data["tokenUsage"] is None


def test_build_template_data_token_usage_mapping():
    assert build_template_data(sample_analysis())["tokenUsage"] == {"prompt": 12, "completion": 34, "total": 46}

    # 存储层读出的空 tokenUsage（原作 saveReport 会写入 "{}"）→ 展示 0
    assert build_template_data(sample_analysis(tokenUsage={}))["tokenUsage"] == {
        "prompt": 0,
        "completion": 0,
        "total": 0,
    }

    # 未参与 AI 分析（skipped）→ 无 tokenUsage 字段 → 不展示
    analysis = sample_analysis()
    analysis.pop("tokenUsage")
    assert build_template_data(analysis)["tokenUsage"] is None

    partial = build_template_data(sample_analysis(tokenUsage={"total_tokens": 7}))
    assert partial["tokenUsage"] == {"prompt": 0, "completion": 0, "total": 7}


def test_build_template_data_create_time_uses_saved_at():
    saved_at = int(datetime(2024, 1, 2, 3, 4, 5).timestamp() * 1000)
    data = build_template_data(sample_analysis(), saved_at=saved_at)

    assert data["createTime"] == "2024-01-02 03:04:05"

    # 未显式传入时取报告自带的 savedAt
    assert build_template_data(sample_analysis(savedAt=saved_at))["createTime"] == "2024-01-02 03:04:05"


def test_build_template_data_create_time_defaults_to_now():
    data = build_template_data(sample_analysis())
    parsed = datetime.strptime(data["createTime"], "%Y-%m-%d %H:%M:%S")

    assert abs((datetime.now() - parsed).total_seconds()) < 60


def test_build_template_data_activity_chart_follows_config():
    enabled = build_template_data(sample_analysis())
    assert enabled["enableActivityChart"] is True
    assert len(enabled["activityChart"]["hours"]) == 24
    assert enabled["activityChart"]["peakHour"] == 9

    config = Config()
    config.group_insight_activity_enabled = False
    disabled = build_template_data(sample_analysis(), config=config)
    assert disabled["enableActivityChart"] is False
    assert disabled["activityChart"] is None


def test_build_template_data_normalizes_missing_keys():
    analysis = sample_analysis(
        topics=[{"topic": "无贡献者"}, {"detail": "无标题"}, "脏数据"],
        goldenQuotes=[{"quote": "字符串发送者", "sender": "某人"}, "脏数据"],
        userTitles=[{"user": "小明"}, "脏数据"],
    )
    data = build_template_data(analysis)

    assert data["topics"] == [
        {"topic": "无贡献者", "detail": "", "contributors": []},
        {"topic": "", "detail": "无标题", "contributors": []},
    ]
    assert data["goldenQuotes"] == [
        {"quote": "字符串发送者", "reason": "", "sender": {"user_id": None, "nickname": "某人"}}
    ]
    assert data["userTitles"] == [{"user_id": None, "user": "小明", "title": "", "mbti": "", "reason": ""}]


# ---------------------------------------------------------------- 渲染与截图


async def test_render_report_default_template(monkeypatch):
    calls: list[dict] = []

    async def fake_html_to_image(html: str, **kwargs):
        calls.append({"html": html, **kwargs})
        return b"image-bytes"

    monkeypatch.setattr(report_module, "html_to_image", fake_html_to_image)

    data = build_template_data(sample_analysis(), group_name="测试群")
    image = await render_report(data, Config())

    assert image == b"image-bytes"
    assert len(calls) == 1
    assert calls[0]["viewport"]["width"] == REPORT_TEMPLATE_VIEWPORTS["default"] == 840
    assert "群聊分析报告" in calls[0]["html"]
    assert calls[0]["img_type"] == "png"
    assert calls[0]["quality"] == 100


async def test_render_report_scrapbook_template(monkeypatch):
    calls: list[dict] = []

    async def fake_html_to_image(html: str, **kwargs):
        calls.append({"html": html, **kwargs})
        return b"image-bytes"

    monkeypatch.setattr(report_module, "html_to_image", fake_html_to_image)

    config = Config()
    config.group_insight_summary_template = "scrapbook"
    config.group_insight_render_img_type = "jpeg"
    config.group_insight_render_quality = 80

    image = await render_report(build_template_data(sample_analysis()), config)

    assert image == b"image-bytes"
    assert calls[0]["viewport"]["width"] == REPORT_TEMPLATE_VIEWPORTS["scrapbook"] == 1040
    assert "五彩斑斓的一天" in calls[0]["html"]
    assert calls[0]["img_type"] == "jpeg"
    assert calls[0]["quality"] == 80


async def test_render_report_unknown_template_falls_back_to_default(monkeypatch):
    calls: list[dict] = []

    async def fake_html_to_image(html: str, **kwargs):
        calls.append({"html": html, **kwargs})
        return b"image-bytes"

    monkeypatch.setattr(report_module, "html_to_image", fake_html_to_image)

    config = Config()
    config.group_insight_summary_template = "not-exist"
    assert await render_report(build_template_data(sample_analysis()), config) == b"image-bytes"

    assert calls[0]["viewport"]["width"] == 840
    assert "群聊分析报告" in calls[0]["html"]


async def test_render_report_returns_none_on_screenshot_failure(monkeypatch):
    async def boom(html: str, **kwargs):
        raise RuntimeError("浏览器炸了")

    monkeypatch.setattr(report_module, "html_to_image", boom)

    assert await render_report(build_template_data(sample_analysis()), Config()) is None


async def test_render_report_returns_none_on_missing_template_field(monkeypatch):
    async def fake_html_to_image(html: str, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("不应渲染")

    monkeypatch.setattr(report_module, "html_to_image", fake_html_to_image)

    # 缺少 dateRange 等字段 → StrictUndefined 抛错 → 返回 None
    assert await render_report({"totalMessages": 1}, Config()) is None


async def test_html_to_image_passes_viewport_and_drops_quality_for_png(monkeypatch):
    captured: dict = {}

    async def fake_html_to_pic(**kwargs):
        captured.update(kwargs)
        return b"png"

    module = types.ModuleType("nonebot_plugin_htmlrender")
    module.html_to_pic = fake_html_to_pic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nonebot_plugin_htmlrender", module)

    assert await html_to_image("<html></html>", viewport={"width": 840, "height": 600}, img_type="png", quality=100) == b"png"

    assert captured["type"] == "png"
    assert captured["quality"] is None  # Playwright 不接受 png + quality
    assert captured["viewport"] == {"width": 840, "height": 600}
    assert captured["full_page"] is True
    assert captured["template_path"].startswith("file://")

    await html_to_image("<html></html>", viewport={"width": 100, "height": 100}, img_type="jpeg", quality=70)
    assert captured["type"] == "jpeg"
    assert captured["quality"] == 70


# ---------------------------------------------------------------- 文本兜底


def test_send_text_summary_full():
    text = send_text_summary(sample_analysis(), "今天", "2023-11-14")

    assert text == (
        "📊 今天群聊报告\n"
        "\n"
        "消息总数: 2\n"
        "参与人数: 2\n"
        "日期: 2023-11-14\n"
        "\n"
        "💬 热门话题:\n"
        "1. 周末出行\n"
        "\n"
        "🏆 群友称号:\n"
        "• 小明 - 话痨王 (ENFP)\n"
        "• 小红 - 夜猫子 (INFP)\n"
        "\n"
        "💎 群圣经:\n"
        '1. "好呀好呀" —— 小红\n'
    )


def test_send_text_summary_uses_message_count_fallback_and_skips_empty_sections():
    text = send_text_summary(
        {"stats": {"basic": {"totalMessages": 0, "totalUsers": 0}}, "messageCount": 12},
        "昨天",
        "2023-11-13",
    )

    assert text == (
        "📊 昨天群聊报告\n"
        "\n"
        "消息总数: 12\n"
        "参与人数: 0\n"
        "日期: 2023-11-13\n"
        "\n"
    )


def test_send_text_summary_handles_missing_report_and_string_sender():
    assert send_text_summary(None, "今天", "2023-11-14").startswith("📊 今天群聊报告\n\n消息总数: 0\n参与人数: 0\n")

    text = send_text_summary({"goldenQuotes": [{"quote": "q", "sender": "路人"}]}, "今天", "2023-11-14")
    assert '1. "q" —— 路人\n' in text

    text = send_text_summary({"goldenQuotes": [{"quote": "q", "sender": {"user_id": "1001"}}]}, "今天", "2023-11-14")
    assert '1. "q" —— 1001\n' in text


class _FakePage:
    def __init__(self):
        self.content = None

    async def set_content(self, html, wait_until=None):
        self.content = html

    async def wait_for_timeout(self, ms):
        pass

    async def screenshot(self, full_page=True, type="png", quality=None):
        return b"fallback-image"


class _FakePageCtx:
    def __init__(self, page):
        self._page = page

    async def __aenter__(self):
        return self._page

    async def __aexit__(self, *exc):
        return False


async def test_html_to_image_fallback_when_html_to_pic_missing(monkeypatch):
    """旧版 nonebot-plugin-htmlrender 无 html_to_pic 时回退 get_new_page 截图。"""
    import types

    from nonebot_plugin_group_insight.render import templates as templates_module

    page = _FakePage()
    fake_module = types.ModuleType("nonebot_plugin_htmlrender")
    fake_module.get_new_page = lambda **kwargs: _FakePageCtx(page)
    monkeypatch.setitem(sys.modules, "nonebot_plugin_htmlrender", fake_module)

    result = await templates_module.html_to_image("<html>hi</html>", viewport={"width": 840, "height": 600})

    assert result == b"fallback-image"
    assert page.content == "<html>hi</html>"
