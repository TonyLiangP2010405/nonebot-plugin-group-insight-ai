"""消息收集。

监听群消息并按规格解析、清洗、入库，同时驱动 @ 记录与批次阈值触发。

处理顺序（复刻原作）：仅群消息 → 机器人账号过滤 → 解析消息段 → 有内容则入库 →
pending @ 补全 → 阈值触发部分分析 → 含 @ 则记录 @。
"""

from __future__ import annotations

import asyncio
import json
import re
import time as time_module
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from nonebot import on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.log import logger

from . import at_tracker
from .config import Config, get_config
from .storage import Storage, get_storage

BOT_ACCOUNT_PATTERN = re.compile(r"^3889\d{6}$")
EMOJI_PATTERN = re.compile(
    "[\U0001f600-\U0001f64f\U0001f300-\U0001f5ff\U0001f680-\U0001f6ff"
    "\U0001f700-\U0001f77f\U0001f780-\U0001f7ff\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff\U0001fa00-\U0001fa6f\U0001fa70-\U0001faff"
    "\u2600-\u26ff\u2700-\u27bf]"
)
MFACE_PATTERN = re.compile(r"动画表情|表情|sticker|emoji", re.IGNORECASE)
BRACKET_PATTERN = re.compile(r"\[(.*?)\]")

_HTML_ENTITIES = (
    ("&#44;", ","),
    ("&#91;", "["),
    ("&#93;", "]"),
    ("&amp;", "&"),
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&quot;", '"'),
)

ThresholdTrigger = Callable[[int, int, str], Awaitable[None]]


def is_bot_account(user_id: int | str) -> bool:
    """机器人账号过滤：3889 开头的 10 位数字。"""
    return bool(BOT_ACCOUNT_PATTERN.match(str(user_id)))


def count_emojis(text: str) -> int:
    """统计文本中的 Emoji 数量。"""
    if not text:
        return 0
    return len(EMOJI_PATTERN.findall(text))


def parse_json_card(data: str | dict[str, Any] | None) -> dict[str, Any] | None:
    """解析 JSON 卡片消息（链接分享 / 小程序 / 音乐），失败返回 ``None``。"""
    try:
        if isinstance(data, str):
            json_str = data
            for entity, char in _HTML_ENTITIES:
                json_str = json_str.replace(entity, char)
            payload = json.loads(json_str)
        else:
            payload = data

        if not isinstance(payload, dict):
            return None

        app = payload.get("app") or ""
        meta = payload.get("meta") or {}

        if app == "com.tencent.tuwen.lua" or payload.get("view") == "news":
            news = meta.get("news") or {}
            return {
                "type": "link",
                "source": news.get("tag") or "unknown",
                "title": news.get("title") or payload.get("prompt") or "",
                "url": news.get("jumpUrl") or None,
            }

        if app == "com.tencent.miniapp_01":
            detail = meta.get("detail_1") or {}
            return {
                "type": "miniapp",
                "source": detail.get("title") or "unknown",
                "title": detail.get("desc") or payload.get("prompt") or "",
                "url": detail.get("qqdocurl") or detail.get("url") or None,
            }

        if app == "com.tencent.structmsg" and payload.get("view") == "music":
            music = meta.get("music") or {}
            return {
                "type": "music",
                "source": music.get("tag") or "unknown",
                "title": music.get("title") or payload.get("prompt") or "",
                "url": music.get("jumpUrl") or music.get("musicUrl") or None,
            }

        return {
            "type": "json_other",
            "source": app or "unknown",
            "title": payload.get("prompt") or "",
            "url": None,
        }
    except Exception as e:
        logger.debug(f"解析 JSON 卡片失败: {e}")
        return None


def extract_message(
    message: Message,
    config: Config,
    *,
    reply_detected: bool = False,
) -> dict[str, Any]:
    """解析消息段。

    ``reply_detected`` 用于 OneBot 适配器已消费 reply 段的场景（``event.reply`` 非空）。
    """
    text = ""
    at_users: list[Any] = []
    images: list[str] = []
    links: list[dict[str, Any]] = []
    videos: list[dict[str, Any]] = []
    faces: dict[str, Any] = {"face": [], "mface": [], "emoji": [], "total": 0}
    has_reply = reply_detected
    at_all = False

    for segment in message:
        seg_type = segment.type
        data = segment.data

        if seg_type == "text":
            seg_text = data.get("text") or ""
            text += seg_text
            if config.group_insight_collect_faces and seg_text:
                emoji_count = count_emojis(seg_text)
                if emoji_count > 0:
                    faces["emoji"].append(emoji_count)
                    faces["total"] += emoji_count
        elif seg_type == "at":
            qq = data.get("qq")
            if str(qq) == "all":
                at_all = True
            at_users.append(qq)
        elif seg_type == "image":
            summary = data.get("summary")
            if config.group_insight_collect_faces and summary and MFACE_PATTERN.search(summary):
                mface_url = data.get("url") or data.get("file")
                if mface_url:
                    faces["mface"].append(mface_url)
                    faces["total"] += 1
            elif config.group_insight_collect_images:
                img_url = data.get("url") or data.get("file")
                if img_url:
                    images.append(img_url)
        elif seg_type == "reply":
            has_reply = True
        elif seg_type == "face":
            if config.group_insight_collect_faces:
                face_id = str(data["id"]) if data.get("id") else None
                if face_id:
                    faces["face"].append(face_id)
                    faces["total"] += 1
        elif seg_type == "json":
            if config.group_insight_collect_links:
                link_data = parse_json_card(data.get("data"))
                if link_data:
                    links.append(link_data)
        elif seg_type == "video":
            if config.group_insight_collect_videos:
                video_url = data.get("url") or data.get("file")
                if video_url:
                    videos.append(
                        {
                            "url": video_url,
                            "file": data.get("file"),
                            "name": data.get("name") or None,
                        }
                    )

    text = BRACKET_PATTERN.sub("", text).strip()
    if len(text) > config.group_insight_max_message_length:
        text = text[: config.group_insight_max_message_length] + "..."

    return {
        "text": text,
        "at_users": at_users,
        "images": images,
        "links": links,
        "videos": videos,
        "faces": faces,
        "has_reply": has_reply,
        "at_all": at_all,
    }


def has_content(extracted: dict[str, Any]) -> bool:
    """保存条件：有文本、表情、图片、链接或视频。"""
    faces = extracted.get("faces") or {}
    return bool(
        extracted.get("text")
        or (faces.get("total") or 0) > 0
        or extracted.get("images")
        or extracted.get("links")
        or extracted.get("videos")
    )


def resolve_nickname(sender: Any, nickname_mode: str) -> str:
    """昵称：``card`` 模式优先群名片，否则始终使用 QQ 昵称。"""
    nickname = getattr(sender, "nickname", None) or ""
    if nickname_mode == "card":
        return getattr(sender, "card", None) or nickname
    return nickname


def build_message_record(
    *,
    user_id: int,
    nickname: str,
    event_time: int,
    extracted: dict[str, Any],
) -> dict[str, Any]:
    """构建入库消息结构（无文本时用 ``[表情]`` 占位）。"""
    message_text = extracted.get("text") or "[表情]"
    record: dict[str, Any] = {
        "user_id": int(user_id),
        "nickname": nickname,
        "message": message_text,
        "time": int(event_time),
        "timestamp": int(time_module.time() * 1000),
        "hour": datetime.fromtimestamp(int(event_time)).hour,
        "length": len(message_text),
        "hasReply": bool(extracted.get("has_reply")),
    }

    images = extracted.get("images") or []
    if images:
        record["images"] = images

    faces = extracted.get("faces") or {}
    if (faces.get("total") or 0) > 0:
        record["faces"] = {
            "face": faces.get("face") or [],
            "mface": faces.get("mface") or [],
            "emoji": faces.get("emoji") or [],
            "total": faces.get("total") or 0,
        }

    links = extracted.get("links") or []
    if links:
        record["links"] = links

    videos = extracted.get("videos") or []
    if videos:
        record["videos"] = videos

    return record


async def _default_threshold_trigger(group_id: int, batch_index: int, date_str: str) -> None:
    """阈值触发的默认实现：仅记录日志（由报告模块替换）。"""
    logger.info(f"群{group_id}达到批次阈值（批次{batch_index}，日期{date_str}），等待报告模块处理")


on_threshold_trigger: ThresholdTrigger = _default_threshold_trigger


def set_threshold_trigger(trigger: ThresholdTrigger) -> None:
    """替换阈值触发回调（报告模块使用）。"""
    global on_threshold_trigger
    on_threshold_trigger = trigger


def _log_trigger_error(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.opt(exception=exc).error("阈值触发回调执行失败")


class MessageCollector:
    """消息收集器。"""

    def __init__(self, config: Config | None = None, storage: Storage | None = None) -> None:
        self._config = config
        self._storage = storage

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = get_config()
        return self._config

    @property
    def storage(self) -> Storage:
        if self._storage is None:
            self._storage = get_storage()
        return self._storage

    def is_group_in_whitelist(self, group_id: int) -> bool:
        """白名单为空时视为未启用（返回 ``False``）。"""
        whitelist = self.config.group_insight_schedule_whitelist
        if not whitelist:
            return False
        return group_id in whitelist

    def get_whitelist_groups(self) -> list[int]:
        return list(self.config.group_insight_schedule_whitelist)

    async def handle_message(self, bot: Bot, event: GroupMessageEvent) -> None:
        """处理一条群消息。"""
        if not self.config.group_insight_collection_enabled:
            return
        if is_bot_account(event.user_id):
            return

        extracted = extract_message(
            event.get_message(),
            self.config,
            reply_detected=getattr(event, "reply", None) is not None,
        )

        if has_content(extracted):
            await self.save_message(event, extracted)
            await at_tracker.check_and_update_pending_ats(
                group_id=event.group_id,
                user_id=event.user_id,
                extracted=extracted,
                event_time=event.time,
                storage=self.storage,
                config=self.config,
            )
            await self.check_threshold_trigger(event.group_id)

        if extracted.get("at_users") or extracted.get("at_all"):
            await self.handle_at(bot, event, extracted)

    async def save_message(self, event: GroupMessageEvent, extracted: dict[str, Any]) -> None:
        """按入库字段构建并保存消息。"""
        record = build_message_record(
            user_id=event.user_id,
            nickname=resolve_nickname(event.sender, self.config.group_insight_nickname_mode),
            event_time=event.time,
            extracted=extracted,
        )
        await self.storage.save_message(event.group_id, record)

    async def handle_at(self, bot: Bot, event: GroupMessageEvent, extracted: dict[str, Any]) -> None:
        """记录 @ 消息（atall 展开全群成员）。"""
        await at_tracker.handle_at(
            bot,
            group_id=event.group_id,
            user_id=event.user_id,
            nickname=resolve_nickname(event.sender, self.config.group_insight_nickname_mode),
            extracted=extracted,
            event_time=event.time,
            reply_message_id=resolve_reply_message_id(event),
            storage=self.storage,
            config=self.config,
        )

    async def check_threshold_trigger(self, group_id: int) -> None:
        """消息数达到 ``max_messages`` 整数倍且该批次无缓存时，后台触发部分分析。"""
        try:
            whitelist = self.config.group_insight_schedule_whitelist
            if whitelist and group_id not in whitelist:
                return

            max_messages = self.config.group_insight_ai_max_messages
            if max_messages <= 0:
                return

            date_str = self.storage.today()
            message_count = await self.storage.get_message_count(group_id, date_str)
            if message_count <= 0 or message_count % max_messages != 0:
                return

            batch_index = message_count // max_messages - 1
            if await self.storage.get_batch(group_id, date_str, batch_index) is not None:
                return

            logger.info(f"群{group_id}达到{message_count}条消息（批次{batch_index}），触发部分分析")
            task = asyncio.create_task(on_threshold_trigger(group_id, batch_index, date_str))
            task.add_done_callback(_log_trigger_error)
        except Exception as e:
            logger.opt(exception=e).error("检查阈值触发失败")

    # ------------------------------------------------------ 供命令层使用的转发

    async def get_messages(
        self,
        group_id: int,
        days: int = 1,
        target_date: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.storage.get_messages(group_id, days, target_date)

    async def get_message_count(self, group_id: int, date_str: str | None = None) -> int:
        return await self.storage.get_message_count(group_id, date_str)

    async def get_at_records(self, group_id: int, user_id: int | str) -> list[dict[str, Any]]:
        return await self.storage.get_at_records(group_id, user_id)

    async def clear_at_records(self, group_id: int, user_id: int | str) -> int:
        return await self.storage.clear_at_records(group_id, user_id)

    async def clear_all_at_records(self) -> int:
        return await self.storage.clear_all_at_records()

    async def get_recent_user_messages(
        self,
        group_id: int,
        user_id: int | str,
        count: int = 1,
        before_time: int | None = None,
        days: int | None = None,
    ) -> list[dict[str, Any]]:
        return await self.storage.get_recent_user_messages(group_id, user_id, count, before_time, days)


def resolve_reply_message_id(event: GroupMessageEvent) -> str:
    """取被回复消息的 ID（适配器可能已消费 reply 段，此时用 ``event.reply``）。"""
    reply = getattr(event, "reply", None)
    reply_message_id = getattr(reply, "message_id", None) if reply is not None else None
    if reply_message_id:
        return str(reply_message_id)

    for segment in event.get_message():
        if segment.type == "reply":
            seg_id = segment.data.get("id")
            if seg_id:
                return str(seg_id)
    return ""


_collector: MessageCollector | None = None


def get_collector() -> MessageCollector:
    """获取消息收集器单例。"""
    global _collector
    if _collector is None:
        _collector = MessageCollector()
    return _collector


def set_collector(collector: MessageCollector | None) -> None:
    """替换消息收集器单例（测试或自定义场景使用）。"""
    global _collector
    _collector = collector


def _is_group_message(event: Event) -> bool:
    return isinstance(event, GroupMessageEvent)


matcher = on_message(rule=_is_group_message, priority=99, block=False)


@matcher.handle()
async def _handle_group_message(bot: Bot, event: GroupMessageEvent) -> None:
    try:
        await get_collector().handle_message(bot, event)
    except Exception as e:
        logger.opt(exception=e).error(f"消息收集失败: {e}")
