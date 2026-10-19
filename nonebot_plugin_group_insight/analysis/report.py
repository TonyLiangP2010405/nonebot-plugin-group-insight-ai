"""报告生成编排（对应原作 ``apps/report.js`` 的 ``performAnalysis`` 与阈值触发部分分析）。

- :meth:`ReportService.perform_analysis`：基础统计 → 阈值短路 → 批次缓存增量分析 / 全量分析 →
  用户称号 → token 汇总，任何异常回退全量、最外层异常返回 ``None``。
- :meth:`ReportService.trigger_partial_analysis`：消息数达到 ``ai_max_messages`` 整数倍时，
  后台补全该批次的话题与金句缓存。
- :func:`register_threshold_trigger`：把上述部分分析接到 ``collector``。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from nonebot.log import logger

from .. import collector
from ..ai.analyzers import BaseAnalyzer, GoldenQuoteAnalyzer, TopicAnalyzer, UserTitleAnalyzer
from ..ai.service import AIService, get_ai_service
from ..config import Config, get_config
from ..storage import Storage, get_storage
from .statistics import StatisticsService

CONTEXT_OVERLAP = 50
DEFAULT_MAX_MESSAGES = 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _empty_usage() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _accumulate_usage(target: dict[str, int], usage: dict[str, Any] | None) -> None:
    if not usage:
        return
    target["prompt_tokens"] += usage.get("prompt_tokens") or 0
    target["completion_tokens"] += usage.get("completion_tokens") or 0
    target["total_tokens"] += usage.get("total_tokens") or 0


async def _run_analyzer(
    analyzer: BaseAnalyzer | None,
    messages: list[dict[str, Any]],
    stats: dict[str, Any],
) -> dict[str, Any]:
    """直接调用分析器（不吞异常，供阈值触发路径复刻原作的失败标记行为）。"""
    if analyzer is None:
        return {}
    return await analyzer.analyze(messages, stats)


class ReportService:
    """报告分析编排服务。"""

    def __init__(
        self,
        config: Config | None = None,
        storage: Storage | None = None,
        ai_service: AIService | None = None,
        statistics_service: StatisticsService | None = None,
    ) -> None:
        self._config = config
        self._storage = storage
        self._ai_service = ai_service
        self._statistics = statistics_service
        self._analyzers: tuple[
            TopicAnalyzer | None, GoldenQuoteAnalyzer | None, UserTitleAnalyzer | None
        ] | None = None

    # ------------------------------------------------------------ 依赖解析

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

    @property
    def ai_service(self) -> AIService | None:
        if self._ai_service is None:
            self._ai_service = get_ai_service()
        return self._ai_service

    @property
    def statistics(self) -> StatisticsService:
        if self._statistics is None:
            self._statistics = StatisticsService(self.config)
        return self._statistics

    def get_analyzers(
        self,
    ) -> tuple[TopicAnalyzer | None, GoldenQuoteAnalyzer | None, UserTitleAnalyzer | None]:
        """三个分析器；AI 服务不可用时全部为 ``None``（对应原作的分析器停用）。"""
        if self._analyzers is None:
            service = self.ai_service
            if service is not None and service.available:
                self._analyzers = (
                    TopicAnalyzer(service, self.config),
                    GoldenQuoteAnalyzer(service, self.config),
                    UserTitleAnalyzer(service, self.config),
                )
            else:
                # 不缓存未就绪状态，AI 服务稍后初始化仍可拿到分析器
                return None, None, None
        return self._analyzers

    async def resolve_analyzers(
        self,
    ) -> tuple[TopicAnalyzer | None, GoldenQuoteAnalyzer | None, UserTitleAnalyzer | None]:
        """确保 AI 服务已尝试初始化后返回分析器（未配置 API Key 时为 ``None``）。"""
        service = self.ai_service
        if service is not None and not service.available:
            await service.init()
        return self.get_analyzers()

    # ------------------------------------------------------------ 分析编排

    async def perform_analysis(
        self,
        messages: list[dict[str, Any]],
        group_id: int | None = None,
        date_str: str | None = None,
        force_regenerate: bool = False,
    ) -> dict[str, Any] | None:
        """执行完整分析，返回 ``{stats, topics, goldenQuotes, userTitles, skipped, ...}``。"""
        try:
            config = self.config
            max_messages = config.group_insight_ai_max_messages or DEFAULT_MAX_MESSAGES

            topic_analyzer, quote_analyzer, title_analyzer = await self.resolve_analyzers()

            logger.info(f"[报告] 开始增强分析 (消息数: {len(messages)}{', 强制重新生成' if force_regenerate else ''})")

            stats = self.statistics.analyze(messages)
            logger.info(f"[报告] 基础统计完成 - 参与用户: {stats['basic']['totalUsers']}")

            min_threshold = config.group_insight_min_messages_threshold
            if len(messages) < min_threshold:
                logger.warning(f"[报告] 消息数 ({len(messages)}) 少于阈值 ({min_threshold}), 跳过 AI 分析")
                return {
                    "stats": stats,
                    "topics": [],
                    "goldenQuotes": [],
                    "userTitles": [],
                    "skipped": True,
                    "reason": f"消息数不足 (需要至少 {min_threshold} 条)",
                }

            topics: list[dict[str, Any]] = []
            golden_quotes: list[dict[str, Any]] = []
            topic_usage: dict[str, Any] | None = None
            quote_usage: dict[str, Any] | None = None
            use_incremental_analysis = False
            batch_token_usage = _empty_usage()

            if group_id and date_str and len(messages) > max_messages:
                try:
                    batch_result = await self._analyze_with_batches(
                        messages,
                        group_id,
                        date_str,
                        force_regenerate,
                        max_messages,
                        stats,
                        topic_analyzer,
                        quote_analyzer,
                        batch_token_usage,
                    )
                    if batch_result is not None:
                        use_incremental_analysis = True
                        topics = batch_result["topics"]
                        golden_quotes = batch_result["goldenQuotes"]
                        topic_usage = batch_result["topicUsage"]
                        quote_usage = batch_result["quoteUsage"]
                except Exception as e:
                    logger.opt(exception=e).error("[报告] 批次缓存处理失败，回退到全量分析")
                    use_incremental_analysis = False

            if not use_incremental_analysis:
                messages_to_analyze = messages
                if len(messages) > max_messages:
                    messages_to_analyze = messages[-max_messages:]
                    logger.info(f"[报告] 消息数{len(messages)}超过阈值，全量分析最新的{max_messages}条消息")

                topic_result, quote_result = await asyncio.gather(
                    self._topic_result(
                        topic_analyzer,
                        messages_to_analyze,
                        stats,
                        config.group_insight_topic_enabled,
                        "话题",
                    ),
                    self._quote_result(
                        quote_analyzer,
                        messages_to_analyze,
                        stats,
                        config.group_insight_golden_quote_enabled,
                        "金句",
                    ),
                )
                topics = topic_result["topics"]
                topic_usage = topic_result["usage"]
                golden_quotes = quote_result["goldenQuotes"]
                quote_usage = quote_result["usage"]

            user_titles: list[dict[str, Any]] = []
            title_usage: dict[str, Any] | None = None

            if config.group_insight_user_title_enabled and title_analyzer is not None:
                try:
                    title_result = await title_analyzer.analyze(messages, stats)
                    user_titles = title_result.get("userTitles") or []
                    title_usage = title_result.get("usage")
                except Exception as e:
                    logger.opt(exception=e).error("[报告] 用户称号分析失败")

            token_usage = _empty_usage()
            for usage in (batch_token_usage, topic_usage, quote_usage, title_usage):
                if usage and (usage.get("total_tokens") or 0) > 0:
                    _accumulate_usage(token_usage, usage)

            analysis_mode = "增量" if use_incremental_analysis else "全量"
            logger.info(
                f"[报告] {analysis_mode}分析完成 - 话题: {len(topics)}, 金句: {len(golden_quotes)}, "
                f"称号: {len(user_titles)}, Tokens: {token_usage['total_tokens']}"
            )

            return {
                "stats": stats,
                "topics": topics,
                "goldenQuotes": golden_quotes,
                "userTitles": user_titles,
                "skipped": False,
                "useIncrementalAnalysis": use_incremental_analysis,
                "tokenUsage": token_usage,
            }
        except Exception as e:
            logger.opt(exception=e).error("[报告] 增强分析失败")
            return None

    async def _analyze_with_batches(
        self,
        messages: list[dict[str, Any]],
        group_id: int,
        date_str: str,
        force_regenerate: bool,
        max_messages: int,
        stats: dict[str, Any],
        topic_analyzer: TopicAnalyzer | None,
        quote_analyzer: GoldenQuoteAnalyzer | None,
        batch_token_usage: dict[str, int],
    ) -> dict[str, Any] | None:
        """批次缓存 + 增量分析。

        返回 ``{topics, goldenQuotes, topicUsage, quoteUsage}``；没有可用批次缓存时返回 ``None``
        （调用方回退全量分析）。
        """
        config = self.config
        completed_batches = len(messages) // max_messages
        remaining_messages = len(messages) % max_messages

        logger.info(f"[报告] 消息总数: {len(messages)}, 完整批次: {completed_batches}, 剩余: {remaining_messages}")

        batch_caches: list[dict[str, Any]] = []
        failed_batches: list[int] = []
        missing_batches: list[int] = []

        if force_regenerate:
            logger.info(f"[报告] 强制重新生成模式，将重新分析所有 {completed_batches} 个批次")
            missing_batches = list(range(completed_batches))
        else:
            for batch_index in range(completed_batches):
                cached = await self.storage.get_batch(group_id, date_str, batch_index)

                if cached is None:
                    missing_batches.append(batch_index)
                    logger.info(f"[报告] 批次{batch_index}缓存不存在，需要补全")
                    continue

                if cached.get("success"):
                    batch_caches.append(cached)
                    _accumulate_usage(batch_token_usage, cached.get("tokenUsage"))
                    logger.info(
                        f"[报告] 批次{batch_index}缓存有效 - 话题: {len(cached.get('topics') or [])}, "
                        f"金句: {len(cached.get('goldenQuotes') or [])}, "
                        f"Tokens: {(cached.get('tokenUsage') or {}).get('total_tokens', 0)}"
                    )
                elif not cached.get("retried"):
                    failed_batches.append(batch_index)
                    logger.warning(f"[报告] 批次{batch_index}分析曾失败，将尝试重试")
                else:
                    logger.warning(f"[报告] 批次{batch_index}分析曾失败且已重试过，跳过此批次")

        logger.info(
            f"[报告] 批次状态检查: 成功={len(batch_caches)}, 失败={len(failed_batches)}, 缺失={len(missing_batches)}"
        )

        if missing_batches or failed_batches:
            retry_result = await self.analyze_missing_batches(
                messages, group_id, date_str, missing_batches, failed_batches, topic_analyzer, quote_analyzer
            )

            for success_batch in retry_result["successBatches"]:
                batch_caches.append(success_batch)
                _accumulate_usage(batch_token_usage, success_batch.get("tokenUsage"))

            if retry_result["stillFailedBatches"]:
                logger.warning(
                    f"[报告] 批次补全后仍有 {len(retry_result['stillFailedBatches'])} 个批次失败: "
                    f"[{', '.join(str(i) for i in retry_result['stillFailedBatches'])}]"
                )

        if not batch_caches:
            return None

        batch_caches.sort(key=lambda batch: batch.get("batchIndex") or 0)

        merged_topics: list[dict[str, Any]] = []
        merged_quotes: list[dict[str, Any]] = []
        for batch in batch_caches:
            merged_topics = self.merge_topics(merged_topics, batch.get("topics") or [])
            merged_quotes = self.merge_golden_quotes(merged_quotes, batch.get("goldenQuotes") or [])

        logger.info(
            f"[报告] 已合并{len(batch_caches)}/{completed_batches}个批次 - "
            f"话题: {len(merged_topics)}, 金句: {len(merged_quotes)}, Tokens: {batch_token_usage['total_tokens']}"
        )

        if remaining_messages > 0:
            last_batch_end = completed_batches * max_messages
            context_start = max(0, last_batch_end - CONTEXT_OVERLAP)
            incremental_messages = messages[context_start:last_batch_end] + messages[last_batch_end:]

            logger.info(
                f"[报告] 分析增量消息: {len(incremental_messages)}条 (含{last_batch_end - context_start}条上下文)"
            )

            incremental_topics, incremental_quotes = await asyncio.gather(
                self._topic_result(
                    topic_analyzer,
                    incremental_messages,
                    stats,
                    config.group_insight_topic_enabled,
                    "增量话题",
                ),
                self._quote_result(
                    quote_analyzer,
                    incremental_messages,
                    stats,
                    config.group_insight_golden_quote_enabled,
                    "增量金句",
                ),
            )

            topics = self.merge_topics(merged_topics, incremental_topics["topics"])
            golden_quotes = self.merge_golden_quotes(merged_quotes, incremental_quotes["goldenQuotes"])
            topic_usage = incremental_topics["usage"]
            quote_usage = incremental_quotes["usage"]

            logger.info(f"[报告] 增量合并完成 - 最终话题: {len(topics)}, 金句: {len(golden_quotes)}")
        else:
            topics = merged_topics
            golden_quotes = merged_quotes
            topic_usage = None
            quote_usage = None
            logger.info(
                f"[报告] 无增量消息，使用批次缓存结果 - 话题: {len(merged_topics)}, 金句: {len(merged_quotes)}"
            )

        return {
            "topics": topics,
            "goldenQuotes": golden_quotes,
            "topicUsage": topic_usage,
            "quoteUsage": quote_usage,
        }

    async def _topic_result(
        self,
        analyzer: TopicAnalyzer | None,
        messages: list[dict[str, Any]],
        stats: dict[str, Any],
        enabled: bool,
        label: str,
    ) -> dict[str, Any]:
        if not enabled or analyzer is None:
            return {"topics": [], "usage": None}
        try:
            result = await analyzer.analyze(messages, stats)
            return {"topics": result.get("topics") or [], "usage": result.get("usage")}
        except Exception as e:
            logger.opt(exception=e).error(f"[报告] {label}分析失败")
            return {"topics": [], "usage": None}

    async def _quote_result(
        self,
        analyzer: GoldenQuoteAnalyzer | None,
        messages: list[dict[str, Any]],
        stats: dict[str, Any],
        enabled: bool,
        label: str,
    ) -> dict[str, Any]:
        if not enabled or analyzer is None:
            return {"goldenQuotes": [], "usage": None}
        try:
            result = await analyzer.analyze(messages, stats)
            return {"goldenQuotes": result.get("goldenQuotes") or [], "usage": result.get("usage")}
        except Exception as e:
            logger.opt(exception=e).error(f"[报告] {label}分析失败")
            return {"goldenQuotes": [], "usage": None}

    # -------------------------------------------------------- 批次补全

    async def analyze_missing_batches(
        self,
        messages: list[dict[str, Any]],
        group_id: int,
        date_str: str,
        missing_batches: list[int],
        failed_batches: list[int],
        topic_analyzer: TopicAnalyzer | None = None,
        quote_analyzer: GoldenQuoteAnalyzer | None = None,
    ) -> dict[str, Any]:
        """顺序补全缺失/失败的批次并写缓存，返回 ``{successBatches, stillFailedBatches, tokenUsage}``。"""
        max_messages = self.config.group_insight_ai_max_messages or DEFAULT_MAX_MESSAGES
        batches_to_retry = sorted(missing_batches + failed_batches)

        results: dict[str, Any] = {
            "successBatches": [],
            "stillFailedBatches": [],
            "tokenUsage": _empty_usage(),
        }

        if not batches_to_retry:
            return results

        if topic_analyzer is None and quote_analyzer is None:
            topic_analyzer, quote_analyzer, _ = await self.resolve_analyzers()

        logger.info(f"[报告] 开始补全 {len(batches_to_retry)} 个批次: [{', '.join(str(i) for i in batches_to_retry)}]")

        for batch_index in batches_to_retry:
            try:
                start_index = batch_index * max_messages
                end_index = (batch_index + 1) * max_messages
                context_start = max(0, start_index - CONTEXT_OVERLAP)
                messages_to_analyze = messages[context_start:end_index]

                if not messages_to_analyze:
                    logger.warning(f"[报告] 批次{batch_index}消息为空，跳过")
                    continue

                actual_context_count = start_index - context_start
                logger.info(
                    f"[报告] 补全批次{batch_index}: 分析 [{context_start}-{min(len(messages), end_index)}] "
                    f"共 {len(messages_to_analyze)} 条 (含{actual_context_count}条上下文)"
                )

                stats = {"users": self._build_lightweight_users(messages_to_analyze)}

                topic_result, quote_result = await asyncio.gather(
                    self._topic_result(topic_analyzer, messages_to_analyze, stats, True, f"批次{batch_index}话题"),
                    self._quote_result(quote_analyzer, messages_to_analyze, stats, True, f"批次{batch_index}金句"),
                )

                batch_token_usage = _empty_usage()
                _accumulate_usage(batch_token_usage, topic_result["usage"])
                _accumulate_usage(batch_token_usage, quote_result["usage"])

                cache_data = {
                    "batchIndex": batch_index,
                    "startIndex": start_index,
                    "endIndex": end_index,
                    "messageCount": len(messages_to_analyze),
                    "topics": topic_result["topics"],
                    "goldenQuotes": quote_result["goldenQuotes"],
                    "tokenUsage": batch_token_usage,
                    "analyzedAt": _now_ms(),
                    "success": True,
                    "retried": True,
                }

                await self.storage.save_batch(group_id, date_str, batch_index, cache_data)

                results["successBatches"].append(cache_data)
                _accumulate_usage(results["tokenUsage"], batch_token_usage)

                logger.info(
                    f"[报告] 批次{batch_index}补全成功 - 话题: {len(cache_data['topics'])}, "
                    f"金句: {len(cache_data['goldenQuotes'])}, Tokens: {batch_token_usage['total_tokens']}"
                )
            except Exception as e:
                logger.opt(exception=e).error(f"[报告] 批次{batch_index}补全失败")

                try:
                    await self.storage.save_batch(
                        group_id,
                        date_str,
                        batch_index,
                        {
                            "batchIndex": batch_index,
                            "success": False,
                            "error": str(e),
                            "analyzedAt": _now_ms(),
                            "retried": True,
                        },
                    )
                except Exception as cache_error:
                    logger.opt(exception=cache_error).error(f"[报告] 保存批次{batch_index}失败标记失败")

                results["stillFailedBatches"].append(batch_index)

        logger.info(
            f"[报告] 批次补全完成 - 成功: {len(results['successBatches'])}, "
            f"失败: {len(results['stillFailedBatches'])}, 总Token: {results['tokenUsage']['total_tokens']}"
        )

        return results

    # -------------------------------------------------------- 阈值触发

    async def trigger_partial_analysis(self, group_id: int, batch_index: int, date_str: str) -> None:
        """消息数达到阈值倍数时补全该批次的话题与金句缓存（失败写失败标记，不抛异常）。"""
        try:
            max_messages = self.config.group_insight_ai_max_messages or DEFAULT_MAX_MESSAGES

            topic_analyzer, quote_analyzer, _ = await self.resolve_analyzers()

            start_index = batch_index * max_messages
            end_index = (batch_index + 1) * max_messages

            logger.info(f"批次{batch_index}: 准备分析消息 [{start_index}-{end_index}]")

            all_messages = await self.storage.get_messages(group_id, 1, date_str)

            context_start = max(0, start_index - CONTEXT_OVERLAP)
            messages_to_analyze = all_messages[context_start:end_index]

            if not messages_to_analyze:
                logger.warning(f"批次{batch_index}消息为空，跳过分析")
                return

            actual_end = min(len(all_messages), end_index)
            logger.info(
                f"批次{batch_index}开始分析 [{context_start}-{actual_end}] 共 {len(messages_to_analyze)} 条消息"
                f"（含{start_index - context_start}条上下文）"
            )

            stats = {"users": self._build_lightweight_users(messages_to_analyze)}

            topic_result, quote_result = await asyncio.gather(
                _run_analyzer(topic_analyzer, messages_to_analyze, stats),
                _run_analyzer(quote_analyzer, messages_to_analyze, stats),
            )

            token_usage = _empty_usage()
            _accumulate_usage(token_usage, (topic_result or {}).get("usage"))
            _accumulate_usage(token_usage, (quote_result or {}).get("usage"))

            cache_data = {
                "batchIndex": batch_index,
                "startIndex": start_index,
                "endIndex": end_index,
                "messageCount": len(messages_to_analyze),
                "topics": (topic_result or {}).get("topics") or [],
                "goldenQuotes": (quote_result or {}).get("goldenQuotes") or [],
                "tokenUsage": token_usage,
                "analyzedAt": _now_ms(),
                "success": True,
            }

            await self.storage.save_batch(group_id, date_str, batch_index, cache_data)

            logger.info(
                f"批次{batch_index}分析完成并缓存 [{start_index}-{end_index}]，"
                f"话题: {len(cache_data['topics'])}, 金句: {len(cache_data['goldenQuotes'])}, "
                f"Tokens: {token_usage['total_tokens']}"
            )
        except Exception as e:
            logger.opt(exception=e).error(f"批次{batch_index}触发分析失败")

            try:
                await self.storage.save_batch(
                    group_id,
                    date_str,
                    batch_index,
                    {
                        "batchIndex": batch_index,
                        "success": False,
                        "error": str(e),
                        "analyzedAt": _now_ms(),
                    },
                )
            except Exception as cache_error:
                logger.opt(exception=cache_error).error("保存失败标记失败")

    # -------------------------------------------------------- 合并算法

    def merge_topics(
        self,
        cached_topics: list[dict[str, Any]],
        incremental_topics: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """按 ``topic`` 同名合并：追加 ``[后续]`` 描述、贡献者按 ``user_id or nickname`` 去重。"""
        topic_map: dict[str, dict[str, Any]] = {}

        for topic in cached_topics:
            topic_map[topic.get("topic")] = topic

        for topic in incremental_topics:
            name = topic.get("topic")
            if name in topic_map:
                existing = topic_map[name]
                existing["detail"] = f"{existing.get('detail') or ''}\n\n[后续]: {topic.get('detail') or ''}"

                existing_user_ids = {
                    contributor.get("user_id") or contributor.get("nickname")
                    for contributor in existing.get("contributors") or []
                }
                for contributor in topic.get("contributors") or []:
                    key = contributor.get("user_id") or contributor.get("nickname")
                    if key not in existing_user_ids:
                        existing.setdefault("contributors", []).append(contributor)
            else:
                topic_map[name] = topic

        return list(topic_map.values())

    def merge_golden_quotes(
        self,
        cached_quotes: list[dict[str, Any]],
        incremental_quotes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """按 ``{sender.user_id or nickname or 'unknown'}_{quote}`` 去重合并。"""
        quote_keys: set[str] = set()
        all_quotes: list[dict[str, Any]] = []

        for quote in list(cached_quotes) + list(incremental_quotes):
            sender = quote.get("sender") or {}
            user_id = sender.get("user_id") or sender.get("nickname") or "unknown"
            key = f"{user_id}_{quote.get('quote') or ''}"
            if key not in quote_keys:
                quote_keys.add(key)
                all_quotes.append(quote)

        return all_quotes

    # -------------------------------------------------------- 内部工具

    @staticmethod
    def _build_lightweight_users(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """轻量用户列表：以 ``nickname`` 去重（首次出现为准）。"""
        user_map: dict[str, dict[str, Any]] = {}
        for msg in messages:
            user_id = msg.get("user_id")
            nickname = msg.get("nickname")
            if user_id and nickname and nickname not in user_map:
                user_map[nickname] = {"user_id": user_id, "nickname": nickname}
        return list(user_map.values())


_report_service: ReportService | None = None


def get_report_service() -> ReportService:
    """获取报告服务单例。"""
    global _report_service
    if _report_service is None:
        _report_service = ReportService()
    return _report_service


def set_report_service(service: ReportService | None) -> None:
    """替换报告服务单例（测试或重新加载配置时使用）。"""
    global _report_service
    _report_service = service


async def _threshold_trigger(group_id: int, batch_index: int, date_str: str) -> None:
    await get_report_service().trigger_partial_analysis(group_id, batch_index, date_str)


def register_threshold_trigger() -> None:
    """把阈值触发的部分分析接到 ``collector``（幂等，可重复调用）。"""
    collector.set_threshold_trigger(_threshold_trigger)
