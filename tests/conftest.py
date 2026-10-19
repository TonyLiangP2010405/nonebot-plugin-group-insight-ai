"""测试公共工具：可控时钟、内存存储、事件与消息构造器。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender

# 插件包导入时会 require("nonebot_plugin_apscheduler")，需要先初始化 NoneBot。
# nonebot.init 幂等，nonebug 的 session fixture 会复用这里的 driver。
nonebot.init()

from nonebot_plugin_group_insight.config import Config  # noqa: E402
from nonebot_plugin_group_insight.storage import Storage  # noqa: E402

DAY = 86400
START = 1_700_000_000  # 2023-11-14 22:13:20 UTC


def day_str(timestamp: int = START) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


@pytest.fixture
def clock(monkeypatch):
    """可控时钟：替换 ``Storage._now``，使 TTL 语义可测。"""
    state = {"now": START}
    monkeypatch.setattr(Storage, "_now", lambda self: state["now"])
    return state


@pytest.fixture
async def storage(tmp_path, clock):
    """使用临时数据库的存储层实例。"""
    store = Storage(db_path=tmp_path / "test.db", config=Config())
    await store.init()
    yield store
    await store.close()


@pytest.fixture
async def live_storage(tmp_path):
    """使用真实时钟的存储层实例。

    命令层用系统时间判断"今天"，因此命令相关测试不能使用 ``clock``（会把存储的
    "今天"固定到 2023-11-14，与 ``date.today()`` 不一致）。
    """
    store = Storage(db_path=tmp_path / "live.db", config=Config())
    await store.init()
    yield store
    await store.close()


def make_message(
    *,
    user_id: int = 1001,
    text: str = "hi",
    time: int = START,
    nickname: str = "nick",
    **extra: Any,
) -> dict[str, Any]:
    """构造一条入库消息。"""
    record: dict[str, Any] = {
        "user_id": user_id,
        "nickname": nickname,
        "message": text,
        "time": time,
        "timestamp": time * 1000,
        "hour": datetime.fromtimestamp(time).hour,
        "length": len(text),
        "hasReply": False,
    }
    record.update(extra)
    return record


def make_at_data(
    *,
    user_id: int = 333,
    nickname: str = "sender",
    message: str = "hi",
    time: int = START,
    message_id: str = "99",
    context_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造一条 @ 记录数据（``user_id`` 为发起者）。"""
    return {
        "user_id": user_id,
        "nickname": nickname,
        "message": message,
        "images": [],
        "faces": {"face": [], "mface": [], "emoji": [], "total": 0},
        "links": [],
        "videos": [],
        "time": time,
        "messageId": message_id,
        "contextMessages": context_messages or [],
    }


def make_extracted(
    *,
    text: str = "",
    at_users: list[Any] | None = None,
    at_all: bool = False,
    images: list[str] | None = None,
    links: list[dict[str, Any]] | None = None,
    videos: list[dict[str, Any]] | None = None,
    faces: dict[str, Any] | None = None,
    has_reply: bool = False,
) -> dict[str, Any]:
    """构造 extract_message 的返回值结构。"""
    return {
        "text": text,
        "at_users": at_users or [],
        "at_all": at_all,
        "images": images or [],
        "links": links or [],
        "videos": videos or [],
        "faces": faces or {"face": [], "mface": [], "emoji": [], "total": 0},
        "has_reply": has_reply,
    }


def make_event(
    *,
    user_id: int = 1001,
    group_id: int = 2002,
    message: Message | None = None,
    message_id: int = 1,
    time: int = START,
    nickname: str = "nick",
    card: str | None = None,
    self_id: int = 10000,
    to_me: bool = False,
) -> GroupMessageEvent:
    """构造群消息事件（不经过适配器，因此不会剥离 @bot / reply 段）。"""
    return GroupMessageEvent(
        time=time,
        self_id=self_id,
        post_type="message",
        message_type="group",
        sub_type="normal",
        message_id=message_id,
        user_id=user_id,
        group_id=group_id,
        message=message if message is not None else Message("hi"),
        original_message=Message("hi"),
        raw_message="hi",
        font=0,
        sender=Sender(user_id=user_id, nickname=nickname, card=card),
        to_me=to_me,
    )


class FakeBot:
    """仅实现被测试代码用到的 API。"""

    def __init__(self, members: list[dict[str, Any]] | None = None, self_id: str = "10000") -> None:
        self.members = members or []
        self.self_id = self_id
        self.calls: list[tuple[str, Any]] = []

    async def get_group_member_list(self, group_id: int | None = None) -> list[dict[str, Any]]:
        self.calls.append(("get_group_member_list", group_id))
        return self.members


def make_private_event(
    *,
    user_id: int = 1001,
    message: Message | None = None,
    message_id: int = 1,
    time: int = START,
    nickname: str = "nick",
    self_id: int = 10000,
) -> PrivateMessageEvent:
    """构造私聊消息事件。"""
    return PrivateMessageEvent(
        time=time,
        self_id=self_id,
        post_type="message",
        message_type="private",
        sub_type="friend",
        message_id=message_id,
        user_id=user_id,
        message=message if message is not None else Message("hi"),
        original_message=Message("hi"),
        raw_message="hi",
        font=0,
        sender=Sender(user_id=user_id, nickname=nickname),
    )
