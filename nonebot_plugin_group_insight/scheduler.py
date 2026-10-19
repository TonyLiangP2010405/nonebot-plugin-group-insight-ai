"""定时任务（对应原作 ``apps/report.js`` 的 ``scheduledReport`` / ``scheduledSendReport``）。

用 ``nonebot-plugin-apscheduler`` 注册两个 cron 任务（任务在插件导入时注册，
发送时间等配置改动需重启生效）：

- ``59 23 * * *``：为白名单群生成当天报告，并发 ``schedule_concurrency``，单群消息数
  少于 ``schedule_min_messages`` 或抢锁失败时跳过，``send_mode == 'immediate'`` 时立即发群。
- ``{send_minute} {send_hour} * * *``（仅 ``send_mode == 'scheduled'``）：串行发送昨天的报告。

单群逻辑拆成 :func:`generate_group_report` / :func:`send_group_report`，便于单独测试。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any

from nonebot import require
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.log import logger

require("nonebot_plugin_apscheduler")

from nonebot_plugin_apscheduler import scheduler  # noqa: E402

from .analysis.report import ReportService, get_report_service  # noqa: E402
from .commands.common import date_label_for, fetch_group_info  # noqa: E402
from .config import Config, get_config  # noqa: E402
from .render.report import build_template_data, render_report  # noqa: E402
from .storage import Storage, get_storage  # noqa: E402

DAILY_REPORT_JOB_ID = "group_insight_daily_report"
DAILY_SEND_JOB_ID = "group_insight_daily_send"


def get_bot() -> Any | None:
    """取第一个在线 Bot 实例（多 bot 场景下够用，取不到时记日志）。"""
    try:
        from nonebot import get_bots

        bots = get_bots()
    except Exception as e:  # pragma: no cover - 未初始化 driver 时
        logger.debug(f"获取 Bot 实例失败: {e}")
        return None

    for bot in bots.values():
        return bot
    logger.warning("没有在线的 Bot 实例，定时任务无法发送消息")
    return None


async def generate_group_report(
    group_id: int,
    target_date: str,
    *,
    config: Config | None = None,
    storage: Storage | None = None,
    service: ReportService | None = None,
    bot: Any | None = None,
) -> dict[str, Any]:
    """为单个群生成定时报告，返回 ``{groupId, status, reason?, messageCount?, sent?}``。

    ``status`` 取值与原作一致：``skipped`` / ``failed`` / ``success`` / ``error``。
    """
    cfg = config or get_config()
    store = storage or get_storage()
    report_service = service or get_report_service()

    try:
        messages = await store.get_messages(group_id, 1, target_date)
        if len(messages) < cfg.group_insight_schedule_min_messages:
            logger.debug(
                f"[报告] 群 {group_id} {target_date} 消息数 ({len(messages)})"
                f" 少于阈值 ({cfg.group_insight_schedule_min_messages})，跳过报告"
            )
            return {"groupId": group_id, "status": "skipped", "reason": "insufficient_messages"}

        if not await store.acquire_generating_lock(group_id, target_date):
            logger.info(f"[报告] 群 {group_id} {target_date} 报告正在生成中，跳过定时任务")
            return {"groupId": group_id, "status": "skipped", "reason": "already_generating"}

        try:
            _, group_name = await fetch_group_info(bot, group_id)
            logger.info(
                f"[报告] 正在为群 {group_id} ({group_name}) 生成 {target_date} 报告 (消息数: {len(messages)})"
            )

            analysis = await report_service.perform_analysis(messages, group_id, target_date)
            if not analysis:
                logger.warning(f"[报告] 群 {group_id} 报告生成失败：分析失败")
                return {"groupId": group_id, "status": "failed", "error": "analysis_failed"}

            await store.save_report(
                group_id,
                target_date,
                {
                    "stats": analysis.get("stats"),
                    "topics": analysis.get("topics"),
                    "goldenQuotes": analysis.get("goldenQuotes"),
                    "userTitles": analysis.get("userTitles"),
                    "messageCount": len(messages),
                    "tokenUsage": analysis.get("tokenUsage"),
                },
            )
            await store.set_cooldown(group_id, "scheduled", len(messages), date_str=target_date)
            logger.info(f"[报告] 群 {group_id} {target_date} 报告生成成功 ({len(messages)} 条消息)")

            sent: bool | None = None
            if cfg.group_insight_send_mode == "immediate":
                send_result = await send_group_report(
                    group_id, target_date, group_name=group_name, config=cfg, storage=store, bot=bot
                )
                sent = bool(send_result.get("success"))
                if sent:
                    logger.info(f"[报告] 群 {group_id} 报告已立即发送")
                else:
                    logger.warning(f"[报告] 群 {group_id} 报告立即发送失败: {send_result.get('error')}")

            return {
                "groupId": group_id,
                "status": "success",
                "messageCount": len(messages),
                "sent": sent,
            }
        finally:
            await store.release_generating_lock(group_id, target_date)
    except Exception as e:
        logger.opt(exception=e).error(f"[报告] 群 {group_id} 定时报告异常: {e}")
        return {"groupId": group_id, "status": "error", "error": str(e)}


async def send_group_report(
    group_id: int,
    target_date: str,
    *,
    group_name: str | None = None,
    config: Config | None = None,
    storage: Storage | None = None,
    bot: Any | None = None,
) -> dict[str, Any]:
    """把某日的报告渲染成图片发到群里，返回 ``{success, error?}``。

    失败原因（``report_not_found`` / ``group_not_found`` / ``render_failed``）记日志。
    """
    cfg = config or get_config()
    store = storage or get_storage()

    try:
        report = await store.get_report(group_id, target_date)
        if not report:
            logger.warning(f"[报告发送] 群 {group_id} {target_date} 报告不存在，跳过发送")
            return {"success": False, "error": "report_not_found"}

        if group_name is None:
            in_group, resolved_name = await fetch_group_info(bot, group_id)
            if not in_group:
                logger.error(f"[报告发送] 无法获取群 {group_id} 对象（可能没有 Bot 在该群）")
                return {"success": False, "error": "group_not_found"}
            group_name = resolved_name

        template_data = build_template_data(
            report,
            group_name,
            date_label_for(target_date),
            target_date,
            model=cfg.group_insight_ai_model,
            config=cfg,
        )
        image = await render_report(template_data, cfg)
        if not image:
            logger.error(f"[报告发送] 群 {group_id} {target_date} 报告渲染失败")
            return {"success": False, "error": "render_failed"}

        if bot is None:
            logger.error(f"[报告发送] 群 {group_id} 没有可用的 Bot 实例，无法发送")
            return {"success": False, "error": "bot_not_found"}

        await bot.send_group_msg(group_id=group_id, message=MessageSegment.image(image))
        logger.info(f"[报告发送] 成功发送报告到群 {group_id} ({group_name})")
        return {"success": True}
    except Exception as e:
        logger.opt(exception=e).error(f"[报告发送] 群 {group_id} 发送异常: {e}")
        return {"success": False, "error": str(e)}


async def _run_for_whitelist(
    whitelist: list[int],
    handler: Callable[[int], Awaitable[dict[str, Any]]],
    concurrency: int,
) -> list[dict[str, Any]]:
    """按并发上限处理白名单群（复刻原作的并发限制语义）。"""
    semaphore = asyncio.Semaphore(max(int(concurrency), 1))

    async def run_one(group_id: int) -> dict[str, Any]:
        async with semaphore:
            return await handler(group_id)

    return list(await asyncio.gather(*(run_one(group_id) for group_id in whitelist)))


async def scheduled_report() -> None:
    """每日 23:59：为白名单群生成当天报告。"""
    cfg = get_config()
    if not cfg.group_insight_schedule_enabled or not cfg.group_insight_schedule_whitelist:
        logger.debug("[报告] 定时报告未启用或白名单为空，跳过")
        return

    target_date = date.today().isoformat()
    whitelist = list(cfg.group_insight_schedule_whitelist)
    concurrency = cfg.group_insight_schedule_concurrency
    send_mode = cfg.group_insight_send_mode
    logger.info(
        f"[报告] 开始执行定时报告任务 (目标日期: {target_date}, 白名单群数: {len(whitelist)}, "
        f"并发数: {concurrency}, 发送模式: {send_mode})"
    )

    bot = get_bot()
    results = await _run_for_whitelist(
        whitelist,
        lambda group_id: generate_group_report(group_id, target_date, config=cfg, bot=bot),
        concurrency,
    )

    summary = _summarize(results, key="status")
    logger.info(
        f"[报告] 定时报告任务执行完成 - 总数: {summary['total']}, 成功: {summary.get('success', 0)}, "
        f"失败: {summary.get('failed', 0)}, 跳过: {summary.get('skipped', 0)}, "
        f"异常: {summary.get('error', 0)}, 已发送: {sum(1 for r in results if r.get('sent'))}"
    )


async def scheduled_send_report() -> None:
    """每日 ``send_hour:send_minute``：串行发送昨天的报告（仅 ``send_mode == 'scheduled'``）。"""
    cfg = get_config()
    if not cfg.group_insight_schedule_enabled or not cfg.group_insight_schedule_whitelist:
        logger.debug("[报告发送] 定时发送未启用或白名单为空，跳过")
        return
    if cfg.group_insight_send_mode != "scheduled":
        logger.debug("[报告发送] 发送模式不是 scheduled，跳过定时发送")
        return

    target_date = (date.today() - timedelta(days=1)).isoformat()
    whitelist = list(cfg.group_insight_schedule_whitelist)
    logger.info(f"[报告发送] 开始执行定时发送任务 (目标日期: {target_date}, 白名单群数: {len(whitelist)})")

    bot = get_bot()
    results = await _run_for_whitelist(
        whitelist,
        lambda group_id: send_group_report(group_id, target_date, config=cfg, bot=bot),
        1,
    )

    summary = _summarize(results, key="success")
    logger.info(
        f"[报告发送] 定时发送任务完成 - 总数: {summary['total']}, 成功: {summary.get('success', 0)}, "
        f"失败: {summary.get('failed', 0)}"
    )


def _summarize(results: list[dict[str, Any]], *, key: str) -> dict[str, int]:
    summary = {"total": len(results), "success": 0, "failed": 0, "skipped": 0, "error": 0}
    for result in results:
        if key == "status":
            status = result.get("status")
            if status in summary:
                summary[status] += 1
        elif result.get(key):
            summary["success"] += 1
        else:
            summary["failed"] += 1
    return summary


def register_jobs() -> None:
    """注册定时任务（导入时调用；配置改动需重启生效）。"""
    scheduler.add_job(
        scheduled_report,
        "cron",
        hour=23,
        minute=59,
        id=DAILY_REPORT_JOB_ID,
        replace_existing=True,
    )

    cfg = get_config()
    if cfg.group_insight_send_mode == "scheduled":
        hour = cfg.group_insight_send_hour
        minute = cfg.group_insight_send_minute
        scheduler.add_job(
            scheduled_send_report,
            "cron",
            hour=hour,
            minute=minute,
            id=DAILY_SEND_JOB_ID,
            replace_existing=True,
        )
        logger.info(f"[报告] 定时发送任务已注册 (每天 {hour:02d}:{minute:02d})")


register_jobs()
