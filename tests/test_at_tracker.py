"""at_tracker 测试：纯 @ 判定、上下文收集、pending 补全与合并转发构造。"""

from __future__ import annotations

from conftest import START, FakeBot, make_at_data, make_event, make_extracted, make_message
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from nonebot_plugin_group_insight.at_tracker import (
    build_at_forward_messages,
    check_and_update_pending_ats,
    collect_next_messages,
    format_link_text,
    handle_at,
    is_pure_at,
    resolve_at_target,
)
from nonebot_plugin_group_insight.config import Config


def test_is_pure_at():
    assert is_pure_at("") is True
    assert is_pure_at("   ") is True
    assert is_pure_at("你好") is False
    assert is_pure_at(" 你好 ") is False


def test_collect_next_messages_is_always_single():
    extracted = make_extracted(text="hi", images=["u"], faces={"face": [], "mface": [], "emoji": [], "total": 1})

    messages = collect_next_messages(extracted, START, context_message_count=3)

    assert len(messages) == 1
    assert messages[0] == {
        "message": "hi",
        "time": START,
        "images": ["u"],
        "faces": {"face": [], "mface": [], "emoji": [], "total": 1},
        "links": [],
        "videos": [],
    }
    assert collect_next_messages(extracted, START, context_message_count=0) == []


def test_format_link_text():
    assert format_link_text(None) == "[链接]"
    assert format_link_text({"type": "link", "source": "哔哩哔哩", "title": "标题", "url": "https://b23.tv/x"}) == (
        "🔗[哔哩哔哩] 标题\n🔗 https://b23.tv/x"
    )
    assert format_link_text({"type": "miniapp", "source": "", "title": "", "url": None}) == "📱[未知来源] 未知内容"
    assert format_link_text({"type": "music", "source": "网易云音乐", "title": "歌曲"}) == "🎵[网易云音乐] 歌曲"
    assert format_link_text({"type": "json_other", "source": "app", "title": "标题"}) == "📄[app] 标题"
    assert format_link_text({"type": "unknown", "source": "s", "title": "t"}) == "🔗[s] t"


def test_resolve_at_target():
    assert resolve_at_target(make_event(message=Message("hi"))) == 1001
    assert (
        resolve_at_target(make_event(message=Message([MessageSegment.at(2222), MessageSegment.text("在吗")]))) == 2222
    )
    assert resolve_at_target(make_event(message=Message([MessageSegment.at("all")]))) == 1001
    assert resolve_at_target(make_event(message=Message([MessageSegment.at(10000)]), self_id=10000)) == 10000
    assert resolve_at_target(make_event(message=Message("hi"), to_me=True)) == 10000


async def test_build_at_forward_messages():
    records = [
        {
            "user_id": 1001,
            "nickname": "发起者",
            "message": "在吗",
            "messageId": "555",
            "time": START,
            "faces": {"face": ["13"], "mface": [], "emoji": [], "total": 1},
            "images": [],
            "links": [],
            "videos": [],
            "contextMessages": [
                {
                    "message": "[表情]",
                    "time": START - 10,
                    "faces": {"face": ["1"], "mface": [], "emoji": [], "total": 1},
                    "images": [],
                    "links": [],
                    "videos": [],
                },
                {"message": "前文一", "time": START - 20, "faces": {}, "images": [], "links": [], "videos": []},
                {"message": "", "time": START - 5, "faces": {}, "images": [], "links": [], "videos": []},
            ],
            "nextMessages": [
                {"message": "后文", "time": START + 10, "faces": {}, "images": [], "links": [], "videos": []}
            ],
        }
    ]

    nodes = await build_at_forward_messages(records, bot=FakeBot(), group_id=2002)

    assert len(nodes) == 4
    assert all(node.type == "node" for node in nodes)
    assert all(node.data["user_id"] == "1001" for node in nodes)
    assert all(node.data["nickname"] == "发起者" for node in nodes)

    context_text, context_face = nodes[0].data["content"], nodes[1].data["content"]
    assert context_text[0].data["text"] == "💬 [前文]: "
    assert context_text[1].data["text"] == "前文一"
    assert context_face[0].data["text"] == "💬 [前文]: "
    assert context_face[1].type == "face"

    main = nodes[2].data["content"]
    assert main[0].type == "reply"
    assert main[0].data["id"] == "555"
    assert main[1].data["text"] == "在吗"
    assert main[2].type == "face"

    next_content = nodes[3].data["content"]
    assert next_content[0].data["text"] == "💬 [后文]: "
    assert next_content[1].data["text"] == "后文"


async def test_build_at_forward_messages_placeholders():
    records = [
        {
            "user_id": 1001,
            "nickname": "发起者",
            "message": "",
            "images": ["", ""],
            "faces": {"face": [], "mface": ["", ""], "emoji": [], "total": 2},
            "videos": [{"file": "v.mp4", "url": None}],
            "links": [{"type": "link", "source": "来源", "title": "标题", "url": "https://x"}],
            "contextMessages": [],
            "nextMessages": [],
        }
    ]

    nodes = await build_at_forward_messages(records)

    texts = [seg.data.get("text") for seg in nodes[0].data["content"] if seg.type == "text"]
    assert texts == ["[仅@]", "[图片x2]", "[动画表情x2]", "🔗[来源] 标题\n🔗 https://x", "[视频: v.mp4]"]


async def test_handle_at_writes_records_and_pending(storage):
    bot = FakeBot()
    extracted = make_extracted(at_users=["2222"])

    await handle_at(
        bot,
        group_id=111,
        user_id=1001,
        nickname="发起者",
        extracted=extracted,
        event_time=START,
        reply_message_id="555",
        storage=storage,
        config=Config(),
    )

    records = await storage.get_at_records(111, 2222)
    assert len(records) == 1
    assert records[0]["user_id"] == 1001
    assert records[0]["nickname"] == "发起者"
    assert records[0]["message"] == ""
    assert records[0]["messageId"] == "555"
    assert records[0]["contextMessages"] == []
    assert await storage.get_pending_ats(111, 1001) == [f"111_2222_{START}"]


async def test_handle_at_collects_context_only_for_pure_at(storage):
    await storage.save_message(111, make_message(user_id=1001, text="上一条", time=START - 30))
    await storage.save_message(111, make_message(user_id=1002, text="别人的消息", time=START - 20))

    await handle_at(
        FakeBot(),
        group_id=111,
        user_id=1001,
        nickname="发起者",
        extracted=make_extracted(at_users=["2222"]),
        event_time=START,
        storage=storage,
        config=Config(),
    )

    records = await storage.get_at_records(111, 2222)
    assert [msg["message"] for msg in records[0]["contextMessages"]] == ["上一条"]

    await handle_at(
        FakeBot(),
        group_id=111,
        user_id=1001,
        nickname="发起者",
        extracted=make_extracted(text="在吗", at_users=["3333"]),
        event_time=START + 10,
        storage=storage,
        config=Config(),
    )

    other = await storage.get_at_records(111, 3333)
    assert other[0]["contextMessages"] == []
    assert await storage.get_pending_ats(111, 1001) == [f"111_2222_{START}"]


async def test_handle_at_all_expands_members(storage):
    bot = FakeBot(members=[{"user_id": 1}, {"user_id": 2}, {"user_id": "bad"}])

    await handle_at(
        bot,
        group_id=111,
        user_id=1001,
        nickname="发起者",
        extracted=make_extracted(at_users=["all"], at_all=True),
        event_time=START,
        storage=storage,
        config=Config(),
    )

    assert bot.calls == [("get_group_member_list", 111)]
    assert len(await storage.get_at_records(111, 1)) == 1
    assert len(await storage.get_at_records(111, 2)) == 1
    assert await storage.get_at_records(111, "bad") == []


async def test_check_and_update_pending_ats(storage):
    await handle_at(
        FakeBot(),
        group_id=111,
        user_id=1001,
        nickname="发起者",
        extracted=make_extracted(at_users=["2222"]),
        event_time=START,
        storage=storage,
        config=Config(),
    )

    await check_and_update_pending_ats(
        group_id=111,
        user_id=1001,
        extracted=make_extracted(text="后文"),
        event_time=START + 5,
        storage=storage,
        config=Config(),
    )

    records = await storage.get_at_records(111, 2222)
    assert records[0]["nextMessages"] == [
        {
            "message": "后文",
            "time": START + 5,
            "images": [],
            "faces": {"face": [], "mface": [], "emoji": [], "total": 0},
            "links": [],
            "videos": [],
        }
    ]
    assert await storage.get_pending_ats(111, 1001) == []


async def test_check_and_update_pending_ats_skips_expired_pending(storage, clock):
    await handle_at(
        FakeBot(),
        group_id=111,
        user_id=1001,
        nickname="发起者",
        extracted=make_extracted(at_users=["2222"]),
        event_time=START,
        storage=storage,
        config=Config(),
    )

    clock["now"] = START + 301
    await check_and_update_pending_ats(
        group_id=111,
        user_id=1001,
        extracted=make_extracted(text="太晚了"),
        event_time=START + 301,
        storage=storage,
        config=Config(),
    )

    assert await storage.get_pending_ats(111, 1001) == []


async def test_handle_at_context_message_count_disabled(storage):
    await storage.save_message(111, make_message(user_id=1001, text="上一条", time=START - 30))

    await handle_at(
        FakeBot(),
        group_id=111,
        user_id=1001,
        nickname="发起者",
        extracted=make_extracted(at_users=["2222"]),
        event_time=START,
        storage=storage,
        config=Config(group_insight_context_message_count=0),
    )

    records = await storage.get_at_records(111, 2222)
    assert records[0]["contextMessages"] == []
    assert await storage.get_pending_ats(111, 1001) == []


async def test_handle_at_skips_expired_records_but_keeps_pending(tmp_path, clock):
    from nonebot_plugin_group_insight.storage import Storage

    store = Storage(db_path=tmp_path / "expired.db", config=Config(group_insight_at_retention_hours=0))
    await store.init()
    try:
        await handle_at(
            FakeBot(),
            group_id=111,
            user_id=1001,
            nickname="发起者",
            extracted=make_extracted(at_users=["2222"]),
            event_time=START,
            storage=store,
            config=Config(group_insight_at_retention_hours=0),
        )

        assert await store.get_at_records(111, 2222) == []
        assert await store.get_pending_ats(111, 1001) == [None]
    finally:
        await store.close()


async def test_build_at_forward_messages_empty_records():
    assert await build_at_forward_messages([]) == []

    nodes = await build_at_forward_messages([make_at_data()])
    assert len(nodes) == 1
    assert nodes[0].data["content"][0].type == "reply"
    assert nodes[0].data["content"][1].data["text"] == "hi"
