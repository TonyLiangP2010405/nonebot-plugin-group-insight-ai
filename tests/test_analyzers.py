"""分析器测试：parseJSON 四级回退、formatMessages、金句过滤与称号行为标签。"""

from __future__ import annotations

import json
from datetime import datetime

from nonebot_plugin_group_insight.ai.analyzers import (
    BaseAnalyzer,
    GoldenQuoteAnalyzer,
    TopicAnalyzer,
    UserTitleAnalyzer,
)
from nonebot_plugin_group_insight.config import Config

START = 1_700_000_000


class FakeAIService:
    """按顺序返回预置结果（异常则抛出），并记录每次调用参数。"""

    def __init__(self, results: list | None = None) -> None:
        self.available = True
        self.results = list(results or [])
        self.calls: list[dict] = []

    async def chat(self, prompt: str, temperature: float = 0.7, timeout: float | None = None) -> dict:
        self.calls.append({"prompt": prompt, "temperature": temperature, "timeout": timeout})
        if not self.results:
            raise RuntimeError("没有预置结果")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def usage(total: int = 3) -> dict:
    return {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": total}


def msg(user_id: int = 1001, nickname: str = "小明", text: str = "hi", time: int = START, **extra) -> dict:
    record = {"user_id": user_id, "nickname": nickname, "message": text, "time": time}
    record.update(extra)
    return record


# --------------------------------------------------------------------- parseJSON


def test_parse_json_direct():
    analyzer = BaseAnalyzer(FakeAIService())
    assert analyzer.parse_json('[{"a": 1}]') == [{"a": 1}]
    assert analyzer.parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_from_json_code_block():
    analyzer = BaseAnalyzer(FakeAIService())
    assert analyzer.parse_json('```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert analyzer.parse_json('好的，结果如下：\n```json\n[{"a": 1}]\n```\n希望有帮助') == [{"a": 1}]


def test_parse_json_from_plain_code_block():
    analyzer = BaseAnalyzer(FakeAIService())
    assert analyzer.parse_json('```\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_extracts_array_from_prose():
    analyzer = BaseAnalyzer(FakeAIService())
    assert analyzer.parse_json('结果如下：\n[{"a": 1}]\n以上') == [{"a": 1}]


def test_parse_json_extracts_object_from_prose():
    analyzer = BaseAnalyzer(FakeAIService())
    assert analyzer.parse_json('结果：{"a": 1} 完毕') == {"a": 1}


def test_parse_json_repairs_extracted_content():
    analyzer = BaseAnalyzer(FakeAIService())
    assert analyzer.parse_json('```json\n[{"a": 1,}]\n```') == [{"a": 1}]


def test_parse_json_repairs_raw_content():
    analyzer = BaseAnalyzer(FakeAIService())
    assert analyzer.parse_json('{"a": 1') == {"a": 1}


def test_parse_json_returns_none_for_garbage():
    analyzer = BaseAnalyzer(FakeAIService())
    assert analyzer.parse_json("这不是 JSON") is None
    assert analyzer.parse_json("") is None
    assert analyzer.parse_json(None) is None


# --------------------------------------------------------------- formatMessages


def test_format_messages_uses_user_id_and_placeholders():
    analyzer = BaseAnalyzer(FakeAIService())
    messages = [
        msg(user_id=1001, nickname="小明", text="大家好"),
        msg(
            user_id=1002,
            nickname="小红",
            text="你好",
            time=START + 60,
            links=[{"type": "miniapp", "source": "QQ小程序"}, {"type": "link"}],
            images=["a.jpg", "b.jpg"],
            videos=[{"url": "v"}],
            faces={"face": [1], "mface": [], "emoji": [1], "total": 2},
        ),
        msg(user_id=1001, nickname="小明明", text="再见", time=START + 120),
    ]

    text, user_map = analyzer.format_messages(messages)

    prefix_0 = datetime.fromtimestamp(START).strftime("%H:%M")
    prefix_1 = datetime.fromtimestamp(START + 60).strftime("%H:%M")
    prefix_2 = datetime.fromtimestamp(START + 120).strftime("%H:%M")

    lines = text.split("\n")
    assert lines[0] == f"[{prefix_0}] [1001]: 大家好"
    assert lines[1] == f"[{prefix_1}] [1002]: 你好 [小程序:QQ小程序] [链接:分享] [图片x2] [视频x1]"
    assert lines[2] == f"[{prefix_2}] [1001]: 再见"
    # 表情不写入 prompt，昵称不写入 prompt，user_map 保留最新昵称
    assert "表情" not in lines[1]
    assert "小明" not in text
    assert user_map == {"1001": "小明明", "1002": "小红"}


def test_format_messages_without_time():
    analyzer = BaseAnalyzer(FakeAIService())
    text, _ = analyzer.format_messages([msg(text="你好")], include_time=False)
    assert text == "[1001]: 你好"


def test_format_messages_supports_filter_and_limit():
    analyzer = BaseAnalyzer(FakeAIService())
    messages = [
        msg(text="一", hasReply=False),
        msg(text="二", time=START + 1, hasReply=True),
        msg(text="三", time=START + 2, hasReply=True),
    ]

    filtered, _ = analyzer.format_messages(messages, include_time=False, filter=lambda m: m["hasReply"])
    assert filtered == "[1001]: 二\n[1001]: 三"

    limited, _ = analyzer.format_messages(messages, include_time=False, max_messages=2)
    assert limited == "[1001]: 一\n[1001]: 二"


# ------------------------------------------------------------------ callAI 重试


async def test_call_ai_retries_with_backoff(monkeypatch):
    waits: list[float] = []

    async def fake_sleep(self, seconds):
        waits.append(seconds)

    monkeypatch.setattr(BaseAnalyzer, "sleep", fake_sleep)

    service = FakeAIService([RuntimeError("boom1"), RuntimeError("boom2"), {"content": "ok", "usage": usage(9)}])
    analyzer = BaseAnalyzer(service)

    result = await analyzer.call_ai("PROMPT", 0.7)

    assert result == {"content": "ok", "usage": usage(9)}
    assert len(service.calls) == 3
    assert waits == [2, 4]
    assert service.calls[0] == {"prompt": "PROMPT", "temperature": 0.7, "timeout": 100}


async def test_call_ai_returns_none_after_all_retries(monkeypatch):
    waits: list[float] = []

    async def fake_sleep(self, seconds):
        waits.append(seconds)

    monkeypatch.setattr(BaseAnalyzer, "sleep", fake_sleep)

    service = FakeAIService([RuntimeError("boom")] * 3)
    analyzer = BaseAnalyzer(service)

    assert await analyzer.call_ai("PROMPT") is None
    assert len(service.calls) == 3
    assert waits == [2, 4]


async def test_call_ai_treats_empty_content_as_failure(monkeypatch):
    async def fake_sleep(self, seconds):
        return None

    monkeypatch.setattr(BaseAnalyzer, "sleep", fake_sleep)

    service = FakeAIService([{"content": "", "usage": None}] * 3)
    analyzer = BaseAnalyzer(service)

    assert await analyzer.call_ai("PROMPT") is None
    assert len(service.calls) == 3


async def test_call_ai_respects_zero_retries(monkeypatch):
    waits: list[float] = []

    async def fake_sleep(self, seconds):
        waits.append(seconds)

    monkeypatch.setattr(BaseAnalyzer, "sleep", fake_sleep)

    service = FakeAIService([RuntimeError("boom")])
    analyzer = BaseAnalyzer(service, Config(group_insight_ai_llm_retries=0))

    assert await analyzer.call_ai("PROMPT") is None
    assert len(service.calls) == 1
    assert waits == []


# ------------------------------------------------------------------ TopicAnalyzer


async def test_topic_analyzer_empty_messages():
    service = FakeAIService()
    analyzer = TopicAnalyzer(service)

    assert await analyzer.analyze([]) == {"topics": [], "usage": None}
    assert service.calls == []


async def test_topic_analyzer_maps_contributors_and_user_capsules():
    payload = json.dumps(
        [
            {
                "topic": " 天气 ",
                "contributors": ["1001", "1002", "9999", "1003", "1004", "1005"],
                "detail": " [1001] 提到今天下雨，[9999] 不在了 ",
            },
            {"topic": "缺详情", "contributors": [], "detail": ""},
            {"topic": "", "contributors": [], "detail": "无话题名"},
        ],
        ensure_ascii=False,
    )
    service = FakeAIService([{"content": payload, "usage": usage(30)}])
    analyzer = TopicAnalyzer(service)

    result = await analyzer.analyze([msg(user_id=1001), msg(user_id=1002, nickname="小红", time=START + 1)])

    topics = result["topics"]
    assert len(topics) == 1
    topic = topics[0]
    assert topic["topic"] == "天气"
    assert topic["contributors"] == [
        {"user_id": "1001", "nickname": "小明"},
        {"user_id": "1002", "nickname": "小红"},
        {"user_id": None, "nickname": "9999"},
        {"user_id": None, "nickname": "1003"},
        {"user_id": None, "nickname": "1004"},
    ]
    assert topic["detail"] == (
        '<span class="user-capsule"><img class="user-capsule-avatar"'
        ' src="https://q1.qlogo.cn/g?b=qq&nk=1001&s=100" alt="小明"'
        " onerror=\"this.style.display='none'\">"
        '<span class="user-capsule-name">小明</span></span> 提到今天下雨，[9999] 不在了'
    )
    assert result["usage"]["total_tokens"] == 30

    prompt = service.calls[0]["prompt"]
    prefix = datetime.fromtimestamp(START).strftime("%H:%M")
    assert f"[{prefix}] [1001]: hi" in prompt
    assert "[1002]: hi" in prompt
    assert "小明" not in prompt
    assert service.calls[0]["temperature"] == 0.7


async def test_topic_analyzer_non_array_response_keeps_usage():
    service = FakeAIService([{"content": '{"topics": []}', "usage": usage(11)}])
    analyzer = TopicAnalyzer(service)

    result = await analyzer.analyze([msg()])

    assert result == {"topics": [], "usage": usage(11)}


# ----------------------------------------------------------- GoldenQuoteAnalyzer


def test_golden_quote_filter_rules():
    analyzer = GoldenQuoteAnalyzer(FakeAIService(), Config())
    messages = [
        msg(text="短"),
        msg(text="这是正常的"),
        msg(text="#命令啊"),
        msg(text="/命令啊"),
        msg(text=".命令啊"),
        msg(text="12345"),
        msg(text="12 34"),
        msg(text="https://example.com/a"),
        msg(text="!!!。,"),
        msg(text="啊" * 101),
    ]

    kept = [m["message"] for m in analyzer.filter_messages(messages)]

    assert kept == ["这是正常的"]


async def test_golden_quote_analyzer_maps_sender_and_limits_quotes():
    payload = json.dumps(
        [
            {"quote": " 金句一 ", "sender": "1001", "reason": " 理由 "},
            {"quote": "金句二", "sender": "9999", "reason": "理由二"},
            {"quote": "缺理由", "sender": "1001"},
        ],
        ensure_ascii=False,
    )
    service = FakeAIService([{"content": payload, "usage": usage(20)}])
    analyzer = GoldenQuoteAnalyzer(service, Config(group_insight_max_golden_quotes=1))

    result = await analyzer.analyze([msg(text="这是正常的", user_id=1001)])

    assert result["goldenQuotes"] == [
        {"quote": "金句一", "sender": {"user_id": "1001", "nickname": "小明"}, "reason": "理由"}
    ]
    assert result["usage"]["total_tokens"] == 20

    prompt = service.calls[0]["prompt"]
    assert "[1001]: 这是正常的" in prompt
    assert service.calls[0]["temperature"] == 0.8


async def test_golden_quote_analyzer_skips_ai_when_nothing_left():
    service = FakeAIService()
    analyzer = GoldenQuoteAnalyzer(service, Config())

    assert await analyzer.analyze([msg(text="短")]) == {"goldenQuotes": [], "usage": None}
    assert service.calls == []


# ------------------------------------------------------------- UserTitleAnalyzer


def title_stats() -> dict:
    return {
        "users": [
            {
                "user_id": 1,
                "nickname": "甲",
                "messageCount": 100,
                "avgLength": "60.0",
                "emojiRatio": "0.60",
                "replyRatio": "0.40",
                "nightRatio": "0.50",
                "shareRatio": "0.20",
                "mostActiveHour": 3,
                "linkCount": 2,
                "videoCount": 1,
            },
            {
                "user_id": 2,
                "nickname": "乙",
                "messageCount": 10,
                "avgLength": "10.0",
                "emojiRatio": "0.00",
                "replyRatio": "0.00",
                "nightRatio": "0.00",
                "shareRatio": "0.00",
                "mostActiveHour": 9,
                "linkCount": 0,
                "videoCount": 0,
            },
        ]
    }


async def test_user_title_analyzer_builds_prompt_and_maps_users():
    payload = json.dumps(
        [
            {"user_id": "1", "title": " 话痨王 ", "mbti": "enfp", "reason": " 话多 "},
            {"user_id": "9999", "title": "神秘人", "mbti": "intj", "reason": "未知"},
            {"user_id": "2", "title": "潜水员", "mbti": "istp"},
        ],
        ensure_ascii=False,
    )
    service = FakeAIService([{"content": payload, "usage": usage(40)}])
    analyzer = UserTitleAnalyzer(service, Config())

    result = await analyzer.analyze([msg()], title_stats())

    assert result["userTitles"] == [
        {"user": "甲", "user_id": "1", "title": "话痨王", "mbti": "ENFP", "reason": "话多"},
        {"user": "9999", "user_id": None, "title": "神秘人", "mbti": "INTJ", "reason": "未知"},
    ]
    assert result["usage"]["total_tokens"] == 40

    prompt = service.calls[0]["prompt"]
    assert "1. 用户ID: 1\n   - 消息数: 100 条" in prompt
    assert "   - 平均长度: 60.0 字\n   - 表情使用率: 60%\n   - 回复消息率: 40%\n   - 夜间活跃度: 50%" in prompt
    assert "   - 最活跃时段: 3 点\n   - 分享链接: 2 个\n   - 分享视频: 1 个\n   - 分享率: 20%" in prompt
    assert "   - 行为标签: 夜猫子、表情包达人、话痨、长文爱好者、互动积极、分享达人" in prompt
    assert "2. 用户ID: 2" in prompt
    assert "   - 行为标签: 普通用户" in prompt
    assert "最多选择 9 位最有特色的用户" in prompt
    assert service.calls[0]["temperature"] == 0.9


async def test_user_title_analyzer_requires_activity():
    service = FakeAIService()
    analyzer = UserTitleAnalyzer(service, Config())
    stats = {
        "users": [
            {"user_id": 1, "nickname": "甲", "messageCount": 4, "avgLength": "1.0", "emojiRatio": "0.00",
             "replyRatio": "0.00", "nightRatio": "0.00", "shareRatio": "0.00", "mostActiveHour": 1},
        ]
    }

    assert await analyzer.analyze([msg()], stats) == {"userTitles": [], "usage": None}
    assert await analyzer.analyze([msg()], None) == {"userTitles": [], "usage": None}
    assert service.calls == []


async def test_user_title_analyzer_caps_at_20_users():
    users = [
        {
            "user_id": index,
            "nickname": f"u{index}",
            "messageCount": 10,
            "avgLength": "10.0",
            "emojiRatio": "0.00",
            "replyRatio": "0.00",
            "nightRatio": "0.00",
            "shareRatio": "0.00",
            "mostActiveHour": 1,
        }
        for index in range(1, 26)
    ]
    service = FakeAIService([{"content": "[]", "usage": usage(1)}])
    analyzer = UserTitleAnalyzer(service, Config())

    await analyzer.analyze([msg()], {"users": users})

    prompt = service.calls[0]["prompt"]
    assert "20. 用户ID: 20" in prompt
    assert "21. 用户ID: 21" not in prompt
