"""collector 测试：消息段解析、文本清洗、入库结构与阈值触发。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from conftest import START, FakeBot, day_str, make_event, make_message
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from nonebot_plugin_group_insight import collector as collector_module
from nonebot_plugin_group_insight.collector import (
    MessageCollector,
    build_message_record,
    count_emojis,
    extract_message,
    has_content,
    is_bot_account,
    parse_json_card,
    resolve_nickname,
)
from nonebot_plugin_group_insight.config import Config

MINIAPP_JSON = (
    '{"app":"com.tencent.miniapp_01","prompt":"[小程序]",'
    '"meta":{"detail_1":{"title":"小程序","desc":"描述","qqdocurl":"https://x/mini"}}}'
)
MUSIC_JSON = (
    '{"app":"com.tencent.structmsg","view":"music",'
    '"meta":{"music":{"tag":"网易云音乐","title":"歌曲","jumpUrl":"https://x/song"}}}'
)
VIDEO_SEGMENT = MessageSegment("video", {"file": "v.mp4", "url": "http://v/v.mp4", "name": "视频"})
MFACE_SEGMENT = MessageSegment("image", {"file": "b.jpg", "url": "http://img/b.jpg", "summary": "[动画表情]"})


def test_is_bot_account():
    assert is_bot_account(3889123456) is True
    assert is_bot_account("3889000000") is True
    assert is_bot_account(388912345) is False
    assert is_bot_account("13889123456") is False
    assert is_bot_account(10001) is False


def test_count_emojis():
    assert count_emojis("") == 0
    assert count_emojis("你好世界") == 0
    assert count_emojis("开心😀😀") == 2
    assert count_emojis("天气☀不错") == 1
    assert count_emojis("✨➿") == 2


def test_parse_json_card_link():
    raw = (
        '{"app":"com.tencent.tuwen.lua","view":"news","prompt":"[分享]",'
        '"meta":{"news":{"tag":"哔哩哔哩","title":"标题","jumpUrl":"https://b23.tv/x"}}}'
    )
    assert parse_json_card(raw) == {
        "type": "link",
        "source": "哔哩哔哩",
        "title": "标题",
        "url": "https://b23.tv/x",
    }


def test_parse_json_card_unescapes_html_entities():
    raw = '{"app":"com.tencent.tuwen.lua","meta":{"news":{"tag":"tag","title":"&#91;标题&#93;&#44;测试"}}}'
    card = parse_json_card(raw)
    assert card["title"] == "[标题],测试"
    assert card["url"] is None


def test_parse_json_card_miniapp_music_and_other():
    assert parse_json_card(MINIAPP_JSON) == {
        "type": "miniapp",
        "source": "小程序",
        "title": "描述",
        "url": "https://x/mini",
    }
    assert parse_json_card(MUSIC_JSON) == {
        "type": "music",
        "source": "网易云音乐",
        "title": "歌曲",
        "url": "https://x/song",
    }
    assert parse_json_card('{"app":"com.tencent.unknown","prompt":"提示"}') == {
        "type": "json_other",
        "source": "com.tencent.unknown",
        "title": "提示",
        "url": None,
    }
    assert parse_json_card({"app": "com.tencent.tuwen.lua", "meta": {"news": {"title": "t"}}})["title"] == "t"


def test_parse_json_card_invalid():
    assert parse_json_card("not a json") is None
    assert parse_json_card(None) is None
    assert parse_json_card("[1, 2]") is None


def test_extract_message_full_segments():
    message = Message(
        [
            MessageSegment.text("你好😀[微笑]"),
            MessageSegment.at(1001),
            MessageSegment("image", {"file": "a.jpg", "url": "http://img/a.jpg"}),
            MFACE_SEGMENT,
            MessageSegment.reply(555),
            MessageSegment.face("13"),
            MessageSegment.json(MINIAPP_JSON),
            VIDEO_SEGMENT,
        ]
    )

    extracted = extract_message(message, Config())

    assert extracted["text"] == "你好😀"
    assert extracted["at_users"] == ["1001"]
    assert extracted["at_all"] is False
    assert extracted["images"] == ["http://img/a.jpg"]
    assert extracted["faces"] == {"face": ["13"], "mface": ["http://img/b.jpg"], "emoji": [1], "total": 3}
    assert extracted["has_reply"] is True
    assert extracted["links"][0]["type"] == "miniapp"
    assert extracted["videos"] == [{"url": "http://v/v.mp4", "file": "v.mp4", "name": "视频"}]


def test_extract_message_at_all_and_reply_detected():
    extracted = extract_message(
        Message([MessageSegment.at("all"), MessageSegment.text("集合")]),
        Config(),
        reply_detected=True,
    )
    assert extracted["at_all"] is True
    assert extracted["at_users"] == ["all"]
    assert extracted["has_reply"] is True


def test_extract_message_cleans_and_truncates_text():
    extracted = extract_message(Message([MessageSegment.text("  [表情]你好 [链接] 世界  ")]), Config())
    assert extracted["text"] == "你好  世界"

    truncated = extract_message(Message([MessageSegment.text("字" * 10)]), Config(group_insight_max_message_length=5))
    assert truncated["text"] == "字" * 5 + "..."


def test_extract_message_respects_collect_switches():
    message = Message(
        [
            MessageSegment.text("😀"),
            MessageSegment("image", {"file": "a.jpg", "url": "http://img/a.jpg"}),
            MFACE_SEGMENT,
            MessageSegment.face("13"),
            MessageSegment.json(MINIAPP_JSON),
            VIDEO_SEGMENT,
        ]
    )

    all_off = extract_message(
        message,
        Config(
            group_insight_collect_images=False,
            group_insight_collect_faces=False,
            group_insight_collect_links=False,
            group_insight_collect_videos=False,
        ),
    )
    assert all_off["faces"]["total"] == 0
    assert all_off["faces"]["emoji"] == []
    assert all_off["images"] == []
    assert all_off["links"] == []
    assert all_off["videos"] == []

    faces_off = extract_message(message, Config(group_insight_collect_faces=False))
    assert faces_off["faces"]["total"] == 0
    assert faces_off["images"] == ["http://img/a.jpg", "http://img/b.jpg"]


def test_has_content():
    assert has_content({"text": "hi", "faces": {"total": 0}, "images": [], "links": [], "videos": []}) is True
    assert has_content({"text": "", "faces": {"total": 0}, "images": [], "links": [], "videos": []}) is False
    assert has_content({"text": "", "faces": {"total": 1}, "images": [], "links": [], "videos": []}) is True
    assert has_content({"text": "", "faces": {"total": 0}, "images": ["u"], "links": [], "videos": []}) is True
    assert has_content({"text": "", "faces": {"total": 0}, "images": [], "links": [{}], "videos": []}) is True
    assert has_content({"text": "", "faces": {"total": 0}, "images": [], "links": [], "videos": [{}]}) is True


def test_build_message_record_uses_placeholder():
    extracted = {
        "text": "",
        "faces": {"face": ["13"], "mface": [], "emoji": [], "total": 1},
        "images": [],
        "links": [],
        "videos": [],
        "has_reply": False,
    }

    record = build_message_record(user_id=1001, nickname="nick", event_time=START, extracted=extracted)

    assert record["message"] == "[表情]"
    assert record["length"] == 4
    assert record["hour"] == datetime.fromtimestamp(START).hour
    assert record["user_id"] == 1001
    assert record["nickname"] == "nick"
    assert record["time"] == START
    assert record["hasReply"] is False
    assert record["faces"] == {"face": ["13"], "mface": [], "emoji": [], "total": 1}
    assert "images" not in record
    assert "links" not in record
    assert "videos" not in record


def test_build_message_record_keeps_optional_fields():
    extracted = {
        "text": "hi",
        "faces": {"face": [], "mface": [], "emoji": [], "total": 0},
        "images": ["http://img/a.jpg"],
        "links": [{"type": "link", "source": "s", "title": "t", "url": None}],
        "videos": [{"url": "http://v/v.mp4", "file": "v.mp4", "name": None}],
        "has_reply": True,
    }

    record = build_message_record(user_id=1001, nickname="nick", event_time=START, extracted=extracted)

    assert record["hasReply"] is True
    assert record["images"] == ["http://img/a.jpg"]
    assert record["links"][0]["type"] == "link"
    assert record["videos"][0]["file"] == "v.mp4"
    assert "faces" not in record


def test_resolve_nickname():
    assert resolve_nickname(SimpleNamespace(nickname="昵称", card="群名片"), "card") == "群名片"
    assert resolve_nickname(SimpleNamespace(nickname="昵称", card="群名片"), "nickname") == "昵称"
    assert resolve_nickname(SimpleNamespace(nickname="昵称", card=None), "card") == "昵称"
    assert resolve_nickname(SimpleNamespace(nickname="昵称", card=""), "card") == "昵称"
    assert resolve_nickname(SimpleNamespace(nickname=None, card=None), "nickname") == ""


async def test_check_threshold_trigger_fires_hook(storage, monkeypatch):
    config = Config(group_insight_ai_max_messages=2)
    collector = MessageCollector(config=config, storage=storage)
    calls = []

    async def fake_trigger(group_id: int, batch_index: int, date_str: str) -> None:
        calls.append((group_id, batch_index, date_str))

    monkeypatch.setattr(collector_module, "on_threshold_trigger", fake_trigger)

    await storage.save_message(111, make_message(time=START))
    await collector.check_threshold_trigger(111)
    await asyncio.sleep(0)
    assert calls == []

    await storage.save_message(111, make_message(time=START + 1))
    await collector.check_threshold_trigger(111)
    await asyncio.sleep(0)
    assert calls == [(111, 0, day_str(START))]

    await storage.save_batch(111, day_str(START), 0, {"success": True})
    await storage.save_message(111, make_message(time=START + 2))
    await storage.save_message(111, make_message(time=START + 3))
    await collector.check_threshold_trigger(111)
    await asyncio.sleep(0)
    assert calls == [(111, 0, day_str(START)), (111, 1, day_str(START))]


async def test_check_threshold_trigger_respects_whitelist(storage, monkeypatch):
    config = Config(group_insight_ai_max_messages=1, group_insight_schedule_whitelist=[999])
    collector = MessageCollector(config=config, storage=storage)
    calls = []

    async def fake_trigger(group_id: int, batch_index: int, date_str: str) -> None:
        calls.append((group_id, batch_index, date_str))

    monkeypatch.setattr(collector_module, "on_threshold_trigger", fake_trigger)

    await storage.save_message(111, make_message(time=START))
    await collector.check_threshold_trigger(111)
    await asyncio.sleep(0)
    assert calls == []


async def test_handle_message_saves_message_and_at_record(storage):
    collector = MessageCollector(config=Config(), storage=storage)
    bot = FakeBot()
    event = make_event(
        message=Message([MessageSegment.text("你好"), MessageSegment.at(2222)]),
        message_id=77,
    )

    await collector.handle_message(bot, event)

    messages = await storage.get_messages(event.group_id, days=1)
    assert len(messages) == 1
    assert messages[0]["message"] == "你好"
    assert messages[0]["user_id"] == 1001

    records = await storage.get_at_records(event.group_id, 2222)
    assert len(records) == 1
    assert records[0]["user_id"] == 1001
    assert records[0]["nickname"] == "nick"
    assert records[0]["message"] == "你好"
    assert records[0]["messageId"] == ""
    assert bot.calls == []


async def test_handle_message_skips_bot_accounts_and_disabled_collection(storage):
    bot = FakeBot()
    collector = MessageCollector(config=Config(), storage=storage)

    await collector.handle_message(bot, make_event(user_id=3889123456))
    assert await storage.get_messages(2002, days=1) == []

    disabled = MessageCollector(config=Config(group_insight_collection_enabled=False), storage=storage)
    await disabled.handle_message(bot, make_event())
    assert await storage.get_messages(2002, days=1) == []


async def test_handle_message_at_all_expands_group_members(storage):
    collector = MessageCollector(config=Config(), storage=storage)
    bot = FakeBot(members=[{"user_id": 1}, {"user_id": 2}, {"user_id": "3"}, {"nickname": "no id"}])
    event = make_event(message=Message([MessageSegment.at("all")]))

    await collector.handle_message(bot, event)

    assert bot.calls == [("get_group_member_list", event.group_id)]
    assert await storage.get_messages(event.group_id, days=1) == []
    for member_id in (1, 2, 3):
        assert len(await storage.get_at_records(event.group_id, member_id)) == 1
    assert sorted(await storage.get_pending_ats(event.group_id, 1001)) == [
        f"{event.group_id}_1_{START}",
        f"{event.group_id}_2_{START}",
        f"{event.group_id}_3_{START}",
    ]


async def test_handle_message_completes_pending_at_with_next_message(storage):
    collector = MessageCollector(config=Config(), storage=storage)
    bot = FakeBot()

    await collector.handle_message(
        bot,
        make_event(message=Message([MessageSegment.at(2222)]), message_id=1, time=START),
    )
    assert await storage.get_pending_ats(2002, 1001) == [f"2002_2222_{START}"]

    await collector.handle_message(
        bot,
        make_event(message=Message([MessageSegment.text("这是后文")]), message_id=2, time=START + 5),
    )

    records = await storage.get_at_records(2002, 2222)
    assert records[0]["nextMessages"] == [
        {
            "message": "这是后文",
            "time": START + 5,
            "images": [],
            "faces": {"face": [], "mface": [], "emoji": [], "total": 0},
            "links": [],
            "videos": [],
        }
    ]
    assert await storage.get_pending_ats(2002, 1001) == []


async def test_handle_message_pure_reply_does_not_complete_pending(storage):
    collector = MessageCollector(config=Config(), storage=storage)
    bot = FakeBot()

    await collector.handle_message(bot, make_event(message=Message([MessageSegment.at(2222)]), time=START))
    await collector.handle_message(
        bot,
        make_event(message=Message([MessageSegment.reply(9)]), message_id=2, time=START + 5),
    )

    records = await storage.get_at_records(2002, 2222)
    assert records[0]["nextMessages"] == []
    assert await storage.get_pending_ats(2002, 1001) == [f"2002_2222_{START}"]


async def test_handle_message_uses_reply_segment_id(storage):
    collector = MessageCollector(config=Config(), storage=storage)
    event = make_event(message=Message([MessageSegment.reply(4321), MessageSegment.at(2222)]))

    await collector.handle_message(FakeBot(), event)

    records = await storage.get_at_records(2002, 2222)
    assert records[0]["messageId"] == "4321"


@pytest.mark.parametrize("nickname_mode", ["nickname", "card"])
async def test_handle_message_nickname_mode(storage, nickname_mode):
    collector = MessageCollector(config=Config(group_insight_nickname_mode=nickname_mode), storage=storage)
    event = make_event(nickname="昵称", card="群名片")

    await collector.handle_message(FakeBot(), event)

    messages = await storage.get_messages(2002, days=1)
    expected = "群名片" if nickname_mode == "card" else "昵称"
    assert messages[0]["nickname"] == expected
