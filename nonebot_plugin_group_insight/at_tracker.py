"""@ 消息追踪。

记录"谁 @ 了谁"，并支持查询展示（合并转发节点构造）。

- 记录（:func:`handle_at`）：atall 展开为全群成员；纯 @ 消息额外收集发起人的上下文消息并写入 pending，
  等待发起者的下一条消息补全。
- 补全（:func:`check_and_update_pending_ats`）：仅在当前消息有内容时触发，``nextMessages`` 恒只补 1 条。
- 展示（:func:`build_at_forward_messages`）：把记录转换为 OneBot v11 合并转发节点，由命令层发送。
"""

from __future__ import annotations

from typing import Any

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.log import logger

from .config import Config, get_config
from .storage import Storage, get_storage

_LINK_ICONS = {
    "link": "🔗",
    "miniapp": "📱",
    "music": "🎵",
    "json_other": "📄",
}

_CONTEXT_PREFIX = "💬 [前文]: "
_NEXT_PREFIX = "💬 [后文]: "
_EMOJI_PLACEHOLDER = "[表情]"


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_pure_at(text: str) -> bool:
    """判断是否为纯 @ 消息（清洗后的文本为空）。"""
    return not (text or "").strip()


def collect_next_messages(
    extracted: dict[str, Any],
    event_time: int,
    context_message_count: int = 1,
) -> list[dict[str, Any]]:
    """构建"下一条消息"数据。

    复刻原作：循环内 ``break``，无论 ``context_message_count`` 为多少都只产生 1 条。
    """
    if context_message_count <= 0:
        return []
    return [
        {
            "message": extracted.get("text") or "",
            "time": int(event_time),
            "images": extracted.get("images") or [],
            "faces": extracted.get("faces") or {},
            "links": extracted.get("links") or [],
            "videos": extracted.get("videos") or [],
        }
    ]


def format_link_text(link: dict[str, Any] | None) -> str:
    """格式化链接分享为文本（图标 + 来源 + 标题 + 链接）。"""
    if not link:
        return "[链接]"

    icon = _LINK_ICONS.get(link.get("type") or "", "🔗")
    source = link.get("source") or "未知来源"
    title = link.get("title") or "未知内容"
    url = link.get("url") or ""

    result = f"{icon}[{source}] {title}"
    if url:
        result += f"\n🔗 {url}"
    return result


def resolve_at_target(event: GroupMessageEvent) -> int:
    """确定"谁艾特我"查询的目标用户：at 了 bot → bot 自己；at 了别人 → 被 at 者；否则自己。"""
    message = event.get_message()
    self_id = str(event.self_id)

    for segment in message:
        if segment.type == "at" and str(segment.data.get("qq") or "") == self_id:
            target = _to_int(event.self_id)
            if target is not None:
                return target

    if event.to_me:
        target = _to_int(event.self_id)
        if target is not None:
            return target

    for segment in message:
        if segment.type != "at":
            continue
        qq = segment.data.get("qq")
        if not qq or str(qq) == "all":
            continue
        target = _to_int(qq)
        if target is not None:
            return target

    target = _to_int(event.user_id)
    return target if target is not None else 0


async def _get_group_member_ids(bot: Bot, group_id: int) -> list[int]:
    """atall → 展开为全群成员（大群注意写入量）。"""
    try:
        members = await bot.get_group_member_list(group_id=group_id)
    except Exception as e:
        logger.opt(exception=e).error(f"获取群 {group_id} 成员列表失败")
        return []

    member_ids = []
    for member in members or []:
        member_id = _to_int(member.get("user_id") if isinstance(member, dict) else None)
        if member_id is not None:
            member_ids.append(member_id)
    return member_ids


async def handle_at(
    bot: Bot,
    *,
    group_id: int,
    user_id: int,
    nickname: str,
    extracted: dict[str, Any],
    event_time: int,
    reply_message_id: str = "",
    storage: Storage | None = None,
    config: Config | None = None,
) -> None:
    """处理一条含 @ 的消息：为每个被 @ 用户写入记录，纯 @ 时写入 pending。

    与原作差异：``links`` / ``videos`` 也会写入记录（原作未持久化这两个字段，
    导致展示层的链接/视频分支永远不会命中）。
    """
    cfg = config or get_config()
    store = storage or get_storage()

    at_users: list[Any] = list(extracted.get("at_users") or [])
    if extracted.get("at_all"):
        at_users = list(await _get_group_member_ids(bot, group_id))

    is_pure = is_pure_at(extracted.get("text") or "")

    context_messages: list[dict[str, Any]] = []
    if cfg.group_insight_context_message_count > 0 and is_pure:
        context_messages = await store.get_recent_user_messages(
            group_id,
            user_id,
            cfg.group_insight_context_message_count,
            event_time,
        )
        logger.debug(f"获取到 {len(context_messages)} 条上下文消息")

    for target in at_users:
        target_id = _to_int(target)
        if target_id is None:
            continue

        at_data = {
            "user_id": user_id,
            "nickname": nickname,
            "message": extracted.get("text") or "",
            "images": extracted.get("images") or [],
            "faces": extracted.get("faces") or {},
            "links": extracted.get("links") or [],
            "videos": extracted.get("videos") or [],
            "time": int(event_time),
            "messageId": reply_message_id,
            "contextMessages": context_messages,
        }
        record_id = await store.save_at_record(group_id, target_id, at_data)

        if is_pure and cfg.group_insight_context_message_count > 0:
            expire_time = int(event_time) + cfg.group_insight_next_message_timeout
            await store.save_pending_at(group_id, user_id, record_id, expire_time)
            logger.debug(f"纯@消息，等待收集下一条消息，超时时间: {cfg.group_insight_next_message_timeout}秒")


async def check_and_update_pending_ats(
    *,
    group_id: int,
    user_id: int,
    extracted: dict[str, Any],
    event_time: int,
    storage: Storage | None = None,
    config: Config | None = None,
) -> None:
    """把当前消息补全到该发起者待处理的 @ 记录上（仅在消息有内容时调用）。"""
    cfg = config or get_config()
    store = storage or get_storage()

    try:
        pending_record_ids = await store.get_pending_ats(group_id, user_id)
        if not pending_record_ids:
            return

        logger.debug(f"发现 {len(pending_record_ids)} 条待更新的 @ 记录")
        next_messages = collect_next_messages(extracted, event_time, cfg.group_insight_context_message_count)

        for record_id in pending_record_ids:
            if await store.update_at_record_next_message(record_id, next_messages):
                await store.remove_pending_at(group_id, user_id, record_id)
                logger.debug(f"成功更新 @ 记录的下一条消息: {record_id}")
    except Exception as e:
        logger.opt(exception=e).error("检查并更新 pending @ 记录失败")


def _append_media(content: list[MessageSegment], message_data: dict[str, Any]) -> None:
    """按 表情 → 图片 → 动画表情 → 链接 → 视频 顺序追加媒体内容。"""
    faces = message_data.get("faces") or {}
    for face_id in faces.get("face") or []:
        if face_id:
            content.append(MessageSegment.face(face_id))

    images = message_data.get("images") or []
    if images:
        urls = [url for url in images if url]
        if urls:
            content.extend(MessageSegment.image(url) for url in urls)
        else:
            content.append(MessageSegment.text(f"[图片x{len(images)}]"))

    mface = faces.get("mface") or []
    if mface:
        urls = [url for url in mface if url]
        if urls:
            content.extend(MessageSegment.image(url) for url in urls)
        else:
            content.append(MessageSegment.text(f"[动画表情x{len(mface)}]"))

    for link in message_data.get("links") or []:
        content.append(MessageSegment.text(format_link_text(link)))

    for video in message_data.get("videos") or []:
        url = video.get("url") if isinstance(video, dict) else None
        if url:
            content.append(MessageSegment.video(url))
        else:
            file_name = (video.get("file") or video.get("name")) if isinstance(video, dict) else None
            content.append(MessageSegment.text(f"[视频: {file_name or '未知'}]"))


def _build_node(record: dict[str, Any], content: list[MessageSegment]) -> MessageSegment:
    return MessageSegment.node_custom(
        user_id=_to_int(record.get("user_id")) or 0,
        nickname=str(record.get("nickname") or ""),
        content=Message(content),
    )


def _build_context_nodes(record: dict[str, Any]) -> list[MessageSegment]:
    nodes = []
    context_messages = sorted(record.get("contextMessages") or [], key=lambda msg: msg.get("time") or 0)
    for ctx_msg in context_messages:
        content = [MessageSegment.text(_CONTEXT_PREFIX)]
        if ctx_msg.get("message") and ctx_msg.get("message") != _EMOJI_PLACEHOLDER:
            content.append(MessageSegment.text(ctx_msg["message"]))
        _append_media(content, ctx_msg)
        if len(content) == 1:
            continue
        nodes.append(_build_node(record, content))
    return nodes


def _build_main_node(record: dict[str, Any]) -> MessageSegment:
    content = []
    if record.get("messageId"):
        content.append(MessageSegment.reply(record["messageId"]))
    content.append(MessageSegment.text(record.get("message") or "[仅@]"))
    _append_media(content, record)
    return _build_node(record, content)


def _build_next_nodes(record: dict[str, Any]) -> list[MessageSegment]:
    nodes = []
    next_messages = sorted(record.get("nextMessages") or [], key=lambda msg: msg.get("time") or 0)
    for next_msg in next_messages:
        content = [MessageSegment.text(_NEXT_PREFIX)]
        if next_msg.get("message"):
            content.append(MessageSegment.text(next_msg["message"]))
        _append_media(content, next_msg)
        if len(content) == 1:
            continue
        nodes.append(_build_node(record, content))
    return nodes


async def build_at_forward_messages(
    records: list[dict[str, Any]],
    *,
    bot: Bot | None = None,
    group_id: int | None = None,
) -> list[MessageSegment]:
    """把 @ 记录转换为 OneBot v11 合并转发节点（``node_custom`` 段）。

    每条记录按序输出：前文节点（``💬 [前文]: `` 前缀，时间升序）→ 主消息节点（可带 reply 引用）→
    后文节点（``💬 [后文]: `` 前缀）。内容仅剩标注的节点会被跳过。

    ``bot`` / ``group_id`` 仅为接口占位（后续如需刷新图片 rkey 可直接使用），当前不影响构造结果。
    由命令层发送，例如::

        nodes = await build_at_forward_messages(records, bot=bot, group_id=group_id)
        await bot.call_api("send_group_forward_msg", group_id=group_id, messages=nodes)
    """
    nodes: list[MessageSegment] = []
    for record in records or []:
        nodes.extend(_build_context_nodes(record))
        nodes.append(_build_main_node(record))
        nodes.extend(_build_next_nodes(record))
    return nodes
