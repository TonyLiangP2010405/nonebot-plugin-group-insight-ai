"""群聊报告命令（对应原作 ``apps/report.js`` 的 ``generateReport`` / ``forceGenerateReport``）。

- ``#群聊报告`` / ``#群聊总结``：所有人可用。今天报告受冷却（``cooldown_minutes``）与生成锁
  （300s）保护；历史报告靠 ``|当前消息数 - 缓存消息数| <= 50`` 定型，从不检查也从不设置冷却。
- ``#强制生成报告``：仅主人，``force_regenerate=True`` 忽略批次缓存，成功后设 ``master`` 冷却。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from nonebot import on_regex
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot, MessageSegment
from nonebot.exception import FinishedException
from nonebot.log import logger
from nonebot.matcher import Matcher
from nonebot.permission import SUPERUSER
from nonebot.rule import REGEX_MATCHED
from nonebot.typing import T_State

from ..analysis.report import ReportService, get_report_service
from ..collector import MessageCollector, get_collector
from ..config import Config, get_config
from ..render.report import build_template_data, render_report, send_text_summary
from ..storage import Storage, get_storage
from .common import FORCE_REPORT_PATTERN, GROUP_REPORT_PATTERN, ReportParams, parse_report_params

# 历史报告定型阈值：消息数差异不超过该值时直接使用缓存
HISTORY_MESSAGE_DIFF_THRESHOLD = 50

report_matcher = on_regex(GROUP_REPORT_PATTERN, priority=10, block=True)
force_report_matcher = on_regex(FORCE_REPORT_PATTERN, priority=10, block=True, permission=SUPERUSER)


@dataclass
class _ReportContext:
    """一次报告命令处理所需的上下文。"""

    bot: Bot
    matcher: Matcher
    storage: Storage
    collector: MessageCollector
    config: Config
    service: ReportService
    params: ReportParams
    report: dict[str, Any] | None
    group_hint: str


async def _render_report_image(
    report: dict[str, Any] | None,
    params: ReportParams,
    config: Config,
) -> bytes | None:
    """渲染报告长图；失败返回 ``None``（调用方走文本兜底或提示文案）。"""
    template_data = build_template_data(
        report,
        params.group_name,
        params.date_label,
        params.query_date,
        model=config.group_insight_ai_model,
        config=config,
    )
    return await render_report(template_data, config)


async def _save_report(
    storage: Storage,
    params: ReportParams,
    analysis: dict[str, Any],
    message_count: int,
) -> None:
    """把分析结果写入报告存储（``messageCount`` 用当前消息数）。"""
    await storage.save_report(
        params.group_id,
        params.query_date,
        {
            "stats": analysis.get("stats"),
            "topics": analysis.get("topics"),
            "goldenQuotes": analysis.get("goldenQuotes"),
            "userTitles": analysis.get("userTitles"),
            "messageCount": message_count,
            "tokenUsage": analysis.get("tokenUsage"),
        },
    )


@report_matcher.handle()
async def _handle_report(bot: Bot, event: Event, state: T_State, matcher: Matcher) -> None:
    storage = get_storage()
    collector = get_collector()
    config = get_config()
    service = get_report_service()

    try:
        params = await parse_report_params(bot, event, state.get(REGEX_MATCHED), reply=matcher.send)
        if params is None:
            return

        context = _ReportContext(
            bot=bot,
            matcher=matcher,
            storage=storage,
            collector=collector,
            config=config,
            service=service,
            params=params,
            report=await storage.get_report(params.group_id, params.query_date),
            group_hint=f" [{params.group_name}]" if params.is_remote else "",
        )

        if params.is_today:
            await _handle_today_report(context)
        else:
            await _handle_history_report(context)
    except FinishedException:
        raise
    except Exception as e:
        logger.opt(exception=e).error(f"[报告] 查询报告错误: {e}")
        await matcher.finish(f"查询报告失败: {e}")


async def _handle_today_report(context: _ReportContext) -> None:
    """今天的报告：冷却中且有缓存直接返回；否则重新生成并设置冷却。"""
    params = context.params
    cooldown = await context.storage.check_cooldown(params.group_id, date_str=params.query_date)

    if cooldown["inCooldown"] and context.report:
        elapsed_minutes = (cooldown.get("lastGenerated") or {}).get("elapsedMinutes", 0)
        logger.info(
            f"[报告] 查询群 {params.group_id} 的今天报告（冷却中，{elapsed_minutes}分钟前已生成）"
        )
        image = await _render_report_image(context.report, params, context.config)
        if image:
            await context.matcher.finish(MessageSegment.image(image))
        await context.matcher.finish("渲染失败")

    messages = await context.collector.get_messages(params.group_id, 1, params.query_date)
    if not messages:
        await context.matcher.finish("今天还没有消息，无法生成报告")

    if not await context.storage.acquire_generating_lock(params.group_id, params.query_date):
        await context.matcher.finish("报告正在生成中，请稍后再试")

    try:
        await context.matcher.send(
            f"正在生成今天的群聊报告{context.group_hint}（{len(messages)}条消息），请稍候..."
        )
        logger.info(
            f"[报告] 触发生成群 {params.group_id} ({params.group_name}) 的今天报告 (消息数: {len(messages)})"
        )

        analysis = await context.service.perform_analysis(messages, params.group_id, params.query_date)
        if not analysis:
            await context.matcher.finish("分析失败，请查看日志")

        await _save_report(context.storage, params, analysis, len(messages))
        await context.storage.set_cooldown(
            params.group_id, "user", len(messages), date_str=params.query_date
        )
        logger.info(f"[报告] 今天报告生成成功 - 群 {params.group_id}, 消息数: {len(messages)}")

        saved = await context.storage.get_report(params.group_id, params.query_date)
        image = await _render_report_image(saved or analysis, params, context.config)
        if image:
            await context.matcher.finish(MessageSegment.image(image))
        await context.matcher.finish("报告已生成并保存，但渲染失败")
    finally:
        await context.storage.release_generating_lock(params.group_id, params.query_date)


async def _handle_history_report(context: _ReportContext) -> None:
    """历史报告：保留期内按消息差异决定是否重新生成，消息已过期时只用缓存。"""
    params = context.params
    messages = await context.collector.get_messages(params.group_id, 1, params.query_date)
    retention_days = context.config.group_insight_retention_days
    days_since_query = (date.today() - date.fromisoformat(params.query_date)).days
    is_within_retention = days_since_query < retention_days

    if not messages:
        if context.report:
            logger.info(f"[报告] 查询群 {params.group_id} 的{params.date_label}报告（消息已过期，返回缓存报告）")
            image = await _render_report_image(context.report, params, context.config)
            if image:
                await context.matcher.finish(MessageSegment.image(image))
            await context.matcher.finish(send_text_summary(context.report, params.date_label, params.query_date))

        suffix = "（未收集到消息）" if is_within_retention else "（消息已过期且无缓存报告）"
        await context.matcher.finish(f"{params.date_label}没有消息记录{suffix}")

    if context.report:
        cached_message_count = context.report.get("messageCount") or 0
        message_diff = abs(len(messages) - cached_message_count)

        if message_diff <= HISTORY_MESSAGE_DIFF_THRESHOLD:
            logger.info(
                f"[报告] 查询群 {params.group_id} 的{params.date_label}报告（缓存有效，消息差异: {message_diff}条）"
            )
            image = await _render_report_image(context.report, params, context.config)
            if image:
                await context.matcher.finish(MessageSegment.image(image))
            await context.matcher.finish(send_text_summary(context.report, params.date_label, params.query_date))

        logger.info(
            f"[报告] 群 {params.group_id} 的{params.date_label}报告消息数差异过大"
            f" (缓存: {cached_message_count}, 当前: {len(messages)}, 差异: {message_diff})，将重新生成"
        )

    if not await context.storage.acquire_generating_lock(params.group_id, params.query_date):
        await context.matcher.finish("报告正在生成中，请稍后再试")

    try:
        await context.matcher.send(
            f"正在生成{params.date_label}的群聊报告{context.group_hint}（{len(messages)}条消息），请稍候..."
        )
        logger.info(
            f"[报告] 触发生成群 {params.group_id} ({params.group_name}) 的{params.date_label}报告"
            f" (消息数: {len(messages)})"
        )

        analysis = await context.service.perform_analysis(messages, params.group_id, params.query_date)
        if not analysis:
            await context.matcher.finish("分析失败，请查看日志")

        await _save_report(context.storage, params, analysis, len(messages))
        # 历史日期不设置冷却：生成后即为定型报告，再次触发会直接使用缓存
        logger.info(f"[报告] {params.date_label}报告生成成功 - 群 {params.group_id}, 消息数: {len(messages)}")

        saved = await context.storage.get_report(params.group_id, params.query_date)
        image = await _render_report_image(saved or analysis, params, context.config)
        if image:
            await context.matcher.finish(MessageSegment.image(image))
        await context.matcher.finish("报告已生成并保存，但渲染失败")
    finally:
        await context.storage.release_generating_lock(params.group_id, params.query_date)


@force_report_matcher.handle()
async def _handle_force_report(bot: Bot, event: Event, state: T_State, matcher: Matcher) -> None:
    storage = get_storage()
    collector = get_collector()
    config = get_config()
    service = get_report_service()

    try:
        params = await parse_report_params(bot, event, state.get(REGEX_MATCHED), reply=matcher.send)
        if params is None:
            return

        group_hint = f" [{params.group_name}]" if params.is_remote else ""
        await matcher.send(f"正在强制生成{params.date_label}的群聊报告{group_hint}，请稍候...")

        messages = await collector.get_messages(params.group_id, 1, params.query_date)
        if not messages:
            await matcher.finish(f"{params.date_label}还没有消息，无法生成报告")

        if not await storage.acquire_generating_lock(params.group_id, params.query_date):
            await matcher.finish("该日期的报告正在生成中，请稍后再试")

        try:
            logger.info(
                f"[报告] 强制生成群 {params.group_id} ({params.group_name}) 的{params.date_label}报告"
                f" (消息数: {len(messages)})"
            )

            analysis = await service.perform_analysis(
                messages, params.group_id, params.query_date, force_regenerate=True
            )
            if not analysis:
                await matcher.finish("分析失败，请查看日志")

            await _save_report(storage, params, analysis, len(messages))
            await storage.set_cooldown(params.group_id, "master", len(messages), date_str=params.query_date)
            logger.info(f"[报告] 强制生成{params.date_label}报告成功 - 群 {params.group_id}")

            saved = await storage.get_report(params.group_id, params.query_date)
            image = await _render_report_image(saved or analysis, params, config)
            if image:
                await matcher.finish(MessageSegment.image(image))
            await matcher.finish("报告已生成并保存，但渲染失败")
        finally:
            await storage.release_generating_lock(params.group_id, params.query_date)
    except FinishedException:
        raise
    except Exception as e:
        logger.opt(exception=e).error(f"[报告] 强制生成报告错误: {e}")
        await matcher.finish(f"生成报告失败: {e}")
