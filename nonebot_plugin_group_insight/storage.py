"""基于 aiosqlite 的异步存储层。

复刻原作 Redis 的 List / Hash / Sorted Set + TTL 语义，用 SQL 表 + ``expire_at`` 字段实现：

- ``messages``：群消息按 ``{group_id}:{YYYY-MM-DD}`` 分桶，``expire_at`` 仅在该日桶首次写入时设置
  （``retention_days * 86400``），后续写入不刷新。
- ``reports``：群聊报告，永久保存（``expire_at`` 为空）。
- ``batches``：批次缓存，TTL 86400s。
- ``cooldowns``：生成冷却，TTL 86400s。
- ``at_records``：@ 记录，``expire_at = time + at_retention_hours * 3600``。
- ``at_pending``：待补全的 @ 记录（纯 @ 消息后等待发起者的下一条消息）。

所有读写方法在出错时都不会抛出异常，而是返回中性值（与原作 Redis 调用一致），
避免存储抖动影响消息收集主流程。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
from nonebot.log import logger

from .config import Config, get_config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    time INTEGER NOT NULL,
    expire_at INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_group_date ON messages (group_id, date);
CREATE INDEX IF NOT EXISTS idx_messages_group_user_time ON messages (group_id, user_id, time);

CREATE TABLE IF NOT EXISTS reports (
    group_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    analysis_data TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    token_usage TEXT NOT NULL DEFAULT '{}',
    last_update_time INTEGER NOT NULL DEFAULT 0,
    saved_at INTEGER NOT NULL DEFAULT 0,
    expire_at INTEGER,
    PRIMARY KEY (group_id, date)
);

CREATE TABLE IF NOT EXISTS batches (
    group_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    batch_index INTEGER NOT NULL,
    data TEXT NOT NULL,
    expire_at INTEGER NOT NULL,
    PRIMARY KEY (group_id, date, batch_index)
);

CREATE TABLE IF NOT EXISTS cooldowns (
    group_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    generated_at INTEGER NOT NULL,
    generated_by TEXT NOT NULL DEFAULT 'user',
    message_count INTEGER NOT NULL DEFAULT 0,
    expire_at INTEGER NOT NULL,
    PRIMARY KEY (group_id, date)
);

CREATE TABLE IF NOT EXISTS at_records (
    record_id TEXT PRIMARY KEY,
    group_id INTEGER NOT NULL,
    target_user_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    time INTEGER NOT NULL,
    expire_at INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_at_records_target ON at_records (group_id, target_user_id, time);

CREATE TABLE IF NOT EXISTS at_pending (
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    record_id TEXT NOT NULL DEFAULT '',
    expire_at INTEGER NOT NULL,
    PRIMARY KEY (group_id, user_id, record_id)
);
"""

_BATCH_TTL = 86400
_COOLDOWN_TTL = 86400


def _to_int(value: Any, default: int | None = None) -> int | None:
    """尽力转换为 int，失败时返回 ``default``。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _no_cooldown() -> dict[str, Any]:
    return {"inCooldown": False, "remainingMinutes": 0, "lastGenerated": None}


class Storage:
    """异步存储层。默认使用插件数据目录下的 ``group_insight.db``。"""

    def __init__(self, db_path: str | Path | None = None, config: Config | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else None
        self._config = config
        self._db: aiosqlite.Connection | None = None
        self._init_lock = asyncio.Lock()
        self._generating_locks: dict[str, tuple[int, int]] = {}

    # ------------------------------------------------------------------ 基础

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = get_config()
        return self._config

    @property
    def db_path(self) -> Path:
        if self._db_path is None:
            from nonebot_plugin_localstore import get_data_dir, get_plugin_data_dir

            try:
                data_dir = Path(get_plugin_data_dir())
            except RuntimeError:
                # 模块未经 NoneBot 加载（如单元测试直接导入）时无法探测调用方插件
                logger.debug("无法探测插件数据目录，回退到默认插件目录")
                data_dir = Path(get_data_dir("nonebot_plugin_group_insight"))
            self._db_path = data_dir / "group_insight.db"
        return self._db_path

    @property
    def data_dir(self) -> Path:
        """插件数据目录（词云词典等文件所在目录）。"""
        return self.db_path.parent

    def _now(self) -> int:
        """当前秒级时间戳。"""
        return int(time.time())

    def _now_ms(self) -> int:
        """当前毫秒级时间戳。"""
        return self._now() * 1000

    def _today(self) -> str:
        """当前本地日期（YYYY-MM-DD）。"""
        return datetime.fromtimestamp(self._now()).strftime("%Y-%m-%d")

    def today(self) -> str:
        """当前本地日期（YYYY-MM-DD），与消息分桶使用的日期一致。"""
        return self._today()

    @staticmethod
    def _parse_date(value: str | date | None) -> date | None:
        if value is None:
            return None
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            logger.debug(f"日期格式无法解析，按今天处理: {value}")
            return None

    def _date_range(self, target_date: str | date | None, days: int) -> list[str]:
        base = self._parse_date(target_date) or date.fromisoformat(self._today())
        return [(base - timedelta(days=i)).isoformat() for i in range(max(int(days), 0))]

    async def _conn(self) -> aiosqlite.Connection:
        if self._db is not None:
            return self._db
        async with self._init_lock:
            if self._db is None:
                path = self.db_path
                path.parent.mkdir(parents=True, exist_ok=True)
                db = await aiosqlite.connect(path)
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("PRAGMA busy_timeout=5000")
                await db.executescript(_SCHEMA)
                await db.commit()
                self._db = db
                logger.debug(f"存储层已就绪: {path}")
        return self._db

    async def init(self) -> None:
        """初始化连接并建表（driver startup 时调用，重复调用无副作用）。"""
        await self._conn()

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self._db is not None:
            try:
                await self._db.close()
            except Exception as e:  # pragma: no cover - 关闭失败仅记录
                logger.opt(exception=e).warning("关闭存储连接失败")
            finally:
                self._db = None

    async def _execute(self, sql: str, params: Any = ()) -> int:
        try:
            db = await self._conn()
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        except Exception as e:
            logger.opt(exception=e).error("存储写入失败")
            return 0

    async def _query(self, sql: str, params: Any = ()) -> list[aiosqlite.Row]:
        try:
            db = await self._conn()
            cursor = await db.execute(sql, params)
            return list(await cursor.fetchall())
        except Exception as e:
            logger.opt(exception=e).error("存储查询失败")
            return []

    async def _query_one(self, sql: str, params: Any = ()) -> aiosqlite.Row | None:
        rows = await self._query(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def _loads(raw: Any) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as e:
            logger.debug(f"JSON 解析失败: {e}")
            return None

    # ------------------------------------------------------------ 群消息存储

    async def save_message(self, group_id: int, message: dict[str, Any]) -> None:
        """保存一条群消息。

        消息按当前日期分桶；``expire_at`` 仅在该日桶尚无未过期数据时设置，
        避免后续写入刷新 TTL 导致消息永不过期。
        """
        now = self._now()
        expire_at = now + self.config.group_insight_retention_days * 86400
        sql = """
            INSERT INTO messages (group_id, date, user_id, time, expire_at, data)
            VALUES (
                :group_id, :date, :user_id, :time,
                COALESCE(
                    (SELECT expire_at FROM messages
                      WHERE group_id = :group_id AND date = :date AND expire_at > :now
                      LIMIT 1),
                    :expire_at
                ),
                :data
            )
        """
        await self._execute(
            sql,
            {
                "group_id": _to_int(group_id, 0),
                "date": self._today(),
                "user_id": _to_int(message.get("user_id"), 0),
                "time": _to_int(message.get("time"), now),
                "now": now,
                "expire_at": expire_at,
                "data": json.dumps(message, ensure_ascii=False),
            },
        )

    async def get_messages(
        self,
        group_id: int,
        days: int = 1,
        target_date: str | date | None = None,
    ) -> list[dict[str, Any]]:
        """按 ``target_date``（默认今天）为基准向前取 ``days`` 天的消息，时间正序。"""
        dates = self._date_range(target_date, days)
        if not dates:
            return []
        placeholders = ",".join("?" for _ in dates)
        sql = (
            f"SELECT data FROM messages WHERE group_id = ? AND date IN ({placeholders})"
            " AND expire_at > ? ORDER BY time ASC, id ASC"
        )
        rows = await self._query(sql, (_to_int(group_id, 0), *dates, self._now()))
        messages = []
        for row in rows:
            data = self._loads(row["data"])
            if isinstance(data, dict):
                messages.append(data)
        return messages

    async def get_message_count(self, group_id: int, date_str: str | date | None = None) -> int:
        """统计某日桶内未过期的消息数量。"""
        row = await self._query_one(
            "SELECT COUNT(*) AS c FROM messages WHERE group_id = ? AND date = ? AND expire_at > ?",
            (_to_int(group_id, 0), self._parse_date(date_str).isoformat() if date_str else self._today(), self._now()),
        )
        return int(row["c"]) if row else 0

    async def get_recent_user_messages(
        self,
        group_id: int,
        user_id: int | str,
        count: int = 1,
        before_time: int | None = None,
        days: int | None = None,
    ) -> list[dict[str, Any]]:
        """取指定用户最近的消息（时间倒序）。

        - ``days`` 为 ``None`` 时最多返回 ``count`` 条（查询窗口为保留天数）；
        - 指定 ``days`` 时返回该天数内的全部消息，忽略 ``count``；
        - ``before_time`` 为秒级时间戳，仅取严格早于该时间的消息。
        """
        try:
            target_user_id = int(user_id)
        except (TypeError, ValueError):
            return []

        use_count_limit = days is None
        query_days = days or self.config.group_insight_retention_days
        dates = self._date_range(None, query_days)
        if not dates:
            return []

        placeholders = ",".join("?" for _ in dates)
        sql = (
            f"SELECT data FROM messages WHERE group_id = ? AND user_id = ?"
            f" AND date IN ({placeholders}) AND expire_at > ?"
        )
        params: list[Any] = [_to_int(group_id, 0), target_user_id, *dates, self._now()]
        if before_time is not None:
            sql += " AND time < ?"
            params.append(int(before_time))
        sql += " ORDER BY time DESC, id DESC"
        if use_count_limit:
            sql += " LIMIT ?"
            params.append(max(int(count), 0))

        messages = []
        for row in await self._query(sql, params):
            data = self._loads(row["data"])
            if not isinstance(data, dict):
                continue
            messages.append(
                {
                    "message": data.get("message", ""),
                    "time": data.get("time", 0),
                    "images": data.get("images") or [],
                    "faces": data.get("faces") or {},
                    "links": data.get("links") or [],
                    "videos": data.get("videos") or [],
                }
            )
        return messages

    # -------------------------------------------------------------- 报告存储

    async def save_report(
        self,
        group_id: int,
        date_str: str,
        report: dict[str, Any],
        retention_days: int = 0,
    ) -> None:
        """保存群聊报告（``retention_days=0`` 表示永久保存）。"""
        now_ms = self._now_ms()
        analysis_data = json.dumps(
            {
                "stats": report.get("stats") or {},
                "topics": report.get("topics") or [],
                "goldenQuotes": report.get("goldenQuotes") or [],
                "userTitles": report.get("userTitles") or [],
            },
            ensure_ascii=False,
        )
        expire_at = self._now() + retention_days * 86400 if retention_days > 0 else None
        await self._execute(
            """
            INSERT OR REPLACE INTO reports
                (group_id, date, analysis_data, message_count, token_usage,
                 last_update_time, saved_at, expire_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _to_int(group_id, 0),
                str(date_str),
                analysis_data,
                int(report.get("messageCount") or 0),
                json.dumps(report.get("tokenUsage") or {}, ensure_ascii=False),
                now_ms,
                now_ms,
                expire_at,
            ),
        )

    async def get_report(self, group_id: int, date_str: str | date | None = None) -> dict[str, Any] | None:
        """读取群聊报告，不存在或已过期返回 ``None``。"""
        date_value = self._parse_date(date_str)
        row = await self._query_one(
            "SELECT * FROM reports WHERE group_id = ? AND date = ? AND (expire_at IS NULL OR expire_at > ?)",
            (_to_int(group_id, 0), date_value.isoformat() if date_value else self._today(), self._now()),
        )
        if row is None:
            return None
        analysis = self._loads(row["analysis_data"])
        if not isinstance(analysis, dict):
            logger.warning(f"报告数据损坏: {group_id}:{row['date']}")
            return None
        return {
            "stats": analysis.get("stats") or {},
            "topics": analysis.get("topics") or [],
            "goldenQuotes": analysis.get("goldenQuotes") or [],
            "userTitles": analysis.get("userTitles") or [],
            "messageCount": int(row["message_count"] or 0),
            "tokenUsage": self._loads(row["token_usage"]) or {},
            "lastUpdateTime": int(row["last_update_time"] or 0),
            "savedAt": int(row["saved_at"] or 0),
            "date": row["date"],
        }

    # -------------------------------------------------------------- 批次缓存

    async def save_batch(self, group_id: int, date_str: str, batch_index: int, data: dict[str, Any]) -> None:
        """保存批次分析缓存（TTL 86400s）。"""
        payload = dict(data)
        payload.setdefault("batchIndex", _to_int(batch_index, 0))
        await self._execute(
            "INSERT OR REPLACE INTO batches (group_id, date, batch_index, data, expire_at) VALUES (?, ?, ?, ?, ?)",
            (
                _to_int(group_id, 0),
                str(date_str),
                _to_int(batch_index, 0),
                json.dumps(payload, ensure_ascii=False),
                self._now() + _BATCH_TTL,
            ),
        )

    async def get_batch(self, group_id: int, date_str: str, batch_index: int) -> dict[str, Any] | None:
        """读取批次缓存。

        返回 ``None`` 表示"需要分析"（不存在、已过期或数据损坏，后者按原作同样走重试路径）。
        """
        row = await self._query_one(
            "SELECT data FROM batches WHERE group_id = ? AND date = ? AND batch_index = ? AND expire_at > ?",
            (_to_int(group_id, 0), str(date_str), _to_int(batch_index, 0), self._now()),
        )
        if row is None:
            return None
        data = self._loads(row["data"])
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------ 冷却

    async def set_cooldown(
        self,
        group_id: int,
        generated_by: str = "user",
        message_count: int = 0,
        date_str: str | date | None = None,
    ) -> None:
        """设置生成冷却标记（TTL 86400s，跨日自动清理）。"""
        date_value = self._parse_date(date_str)
        await self._execute(
            """
            INSERT OR REPLACE INTO cooldowns
                (group_id, date, generated_at, generated_by, message_count, expire_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _to_int(group_id, 0),
                date_value.isoformat() if date_value else self._today(),
                self._now_ms(),
                str(generated_by),
                int(message_count),
                self._now() + _COOLDOWN_TTL,
            ),
        )

    async def check_cooldown(
        self,
        group_id: int,
        ignore_cooldown: bool = False,
        cooldown_minutes: int | None = None,
        date_str: str | date | None = None,
    ) -> dict[str, Any]:
        """检查冷却状态。

        返回 ``{"inCooldown": bool, "remainingMinutes": int, "lastGenerated": dict | None}``；
        存储异常时视为不在冷却。
        """
        if ignore_cooldown:
            return _no_cooldown()

        minutes = cooldown_minutes if cooldown_minutes is not None else self.config.group_insight_cooldown_minutes
        date_value = self._parse_date(date_str)
        row = await self._query_one(
            "SELECT * FROM cooldowns WHERE group_id = ? AND date = ? AND expire_at > ?",
            (_to_int(group_id, 0), date_value.isoformat() if date_value else self._today(), self._now()),
        )
        if row is None or not row["generated_at"]:
            return _no_cooldown()

        elapsed_minutes = (self._now_ms() - int(row["generated_at"])) // 1000 // 60
        remaining_minutes = int(minutes) - elapsed_minutes
        if remaining_minutes > 0:
            return {
                "inCooldown": True,
                "remainingMinutes": remaining_minutes,
                "lastGenerated": {
                    "timestamp": int(row["generated_at"]),
                    "generatedBy": row["generated_by"] or "user",
                    "messageCount": int(row["message_count"] or 0),
                    "elapsedMinutes": elapsed_minutes,
                },
            }
        return _no_cooldown()

    # -------------------------------------------------------------- @ 记录

    async def save_at_record(
        self,
        group_id: int,
        target_user_id: int | str,
        at_data: dict[str, Any],
    ) -> str | None:
        """保存一条 @ 记录。

        ``target_user_id`` 为被 @ 的用户；``at_data`` 中的 ``user_id`` 为发起者。
        ``expire_at = time + at_retention_hours * 3600``，已过期时静默不保存并返回 ``None``。
        """
        target_id = _to_int(target_user_id)
        if target_id is None:
            return None

        at_time = int(at_data.get("time") or self._now())
        expire_at = at_time + self.config.group_insight_at_retention_hours * 3600
        if expire_at <= self._now():
            logger.debug(f"艾特记录已过期，跳过保存: {group_id}_{target_user_id}_{at_time}")
            return None

        record_id = f"{group_id}_{target_id}_{at_time}"
        payload = {
            "user_id": _to_int(at_data.get("user_id"), 0),
            "nickname": at_data.get("nickname") or "",
            "message": at_data.get("message") or "",
            "images": at_data.get("images") or [],
            "faces": at_data.get("faces") or {},
            "time": at_time,
            "messageId": at_data.get("messageId") or "",
            "contextMessages": at_data.get("contextMessages") or [],
            "endTime": datetime.fromtimestamp(expire_at).strftime("%Y-%m-%d %H:%M:%S"),
            "nextMessages": [],
        }
        affected = await self._execute(
            """
            INSERT OR REPLACE INTO at_records
                (record_id, group_id, target_user_id, sender_id, time, expire_at, data)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                _to_int(group_id, 0),
                target_id,
                payload["user_id"],
                at_time,
                expire_at,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        if not affected:
            return None
        logger.debug(f"保存艾特记录成功: {record_id}")
        return record_id

    async def get_at_records(self, group_id: int, target_user_id: int | str) -> list[dict[str, Any]]:
        """获取被 @ 用户的记录（时间正序），无记录返回空列表。"""
        target_id = _to_int(target_user_id)
        if target_id is None:
            return []

        now = self._now()
        cutoff = now - self.config.group_insight_at_retention_hours * 3600
        rows = await self._query(
            "SELECT record_id, expire_at, data FROM at_records"
            " WHERE group_id = ? AND target_user_id = ? AND time >= ?"
            " ORDER BY time ASC, rowid ASC",
            (_to_int(group_id, 0), target_id, cutoff),
        )
        records: list[dict[str, Any]] = []
        expired_ids: list[str] = []
        for row in rows:
            data = self._loads(row["data"]) if int(row["expire_at"]) > now else None
            if not isinstance(data, dict):
                expired_ids.append(row["record_id"])
                continue
            data.setdefault("nextMessages", [])
            records.append(data)
        if expired_ids:
            placeholders = ",".join("?" for _ in expired_ids)
            await self._execute(f"DELETE FROM at_records WHERE record_id IN ({placeholders})", expired_ids)
            logger.debug(f"清理过期艾特记录: {len(expired_ids)} 条")
        return records

    async def update_at_record_next_message(self, record_id: str | None, next_messages: list[dict[str, Any]]) -> bool:
        """为 @ 记录补全"下一条消息"，记录不存在或已过期返回 ``False``。"""
        if not record_id:
            return False
        row = await self._query_one(
            "SELECT data FROM at_records WHERE record_id = ? AND expire_at > ?",
            (str(record_id), self._now()),
        )
        if row is None:
            logger.debug(f"@ 记录已过期，跳过更新: {record_id}")
            return False
        data = self._loads(row["data"])
        if not isinstance(data, dict):
            return False
        data["nextMessages"] = next_messages
        affected = await self._execute(
            "UPDATE at_records SET data = ? WHERE record_id = ?",
            (json.dumps(data, ensure_ascii=False), str(record_id)),
        )
        if affected:
            logger.debug(f"更新 @ 记录成功: {record_id}, 添加 {len(next_messages)} 条后续消息")
        return affected > 0

    async def clear_at_records(self, group_id: int, target_user_id: int | str) -> int:
        """清除被 @ 用户的全部记录，返回删除条数。"""
        target_id = _to_int(target_user_id)
        if target_id is None:
            return 0
        return await self._execute(
            "DELETE FROM at_records WHERE group_id = ? AND target_user_id = ?",
            (_to_int(group_id, 0), target_id),
        )

    async def clear_all_at_records(self) -> int:
        """清除全部 @ 记录，返回删除条数。"""
        deleted = await self._execute("DELETE FROM at_records")
        logger.info(f"清除所有艾特记录: {deleted} 条")
        return deleted

    # -------------------------------------------------------------- @ pending

    async def save_pending_at(
        self,
        group_id: int,
        user_id: int | str,
        record_id: str | None,
        expire_time: int,
    ) -> None:
        """记录一条待补全的 @ 记录（``expire_time = 消息时间 + next_message_timeout``）。"""
        group_value = _to_int(group_id)
        user_value = _to_int(user_id)
        if group_value is None or user_value is None:
            return
        await self._execute(
            "INSERT OR REPLACE INTO at_pending (group_id, user_id, record_id, expire_at) VALUES (?, ?, ?, ?)",
            (group_value, user_value, str(record_id) if record_id else "", int(expire_time)),
        )

    async def get_pending_ats(self, group_id: int, user_id: int | str) -> list[str | None]:
        """取未过期的 pending 记录 ID（并清理已过期项）。"""
        group_value = _to_int(group_id)
        user_value = _to_int(user_id)
        if group_value is None or user_value is None:
            return []

        now = self._now()
        rows = await self._query(
            "SELECT record_id FROM at_pending WHERE group_id = ? AND user_id = ? AND expire_at >= ?"
            " ORDER BY expire_at ASC",
            (group_value, user_value, now),
        )
        await self._execute(
            "DELETE FROM at_pending WHERE group_id = ? AND user_id = ? AND expire_at < ?",
            (group_value, user_value, now),
        )
        return [row["record_id"] or None for row in rows]

    async def pop_pending_ats(self, group_id: int, user_id: int | str) -> list[str | None]:
        """取走未过期的 pending 记录 ID（取走即删除）。"""
        group_value = _to_int(group_id)
        user_value = _to_int(user_id)
        if group_value is None or user_value is None:
            return []

        pending = await self.get_pending_ats(group_value, user_value)
        if pending:
            await self._execute(
                "DELETE FROM at_pending WHERE group_id = ? AND user_id = ? AND expire_at >= ?",
                (group_value, user_value, self._now()),
            )
        return pending

    async def remove_pending_at(self, group_id: int, user_id: int | str, record_id: str | None) -> None:
        """移除单条 pending 记录。"""
        group_value = _to_int(group_id)
        user_value = _to_int(user_id)
        if group_value is None or user_value is None:
            return
        await self._execute(
            "DELETE FROM at_pending WHERE group_id = ? AND user_id = ? AND record_id = ?",
            (group_value, user_value, str(record_id) if record_id else ""),
        )

    # ------------------------------------------------------------------ 清理

    async def purge_expired(self) -> int:
        """清理所有已过期数据，返回删除的总条数。"""
        now = self._now()
        deleted = 0
        deleted += await self._execute("DELETE FROM messages WHERE expire_at <= ?", (now,))
        deleted += await self._execute("DELETE FROM batches WHERE expire_at <= ?", (now,))
        deleted += await self._execute("DELETE FROM cooldowns WHERE expire_at <= ?", (now,))
        deleted += await self._execute("DELETE FROM at_records WHERE expire_at <= ?", (now,))
        deleted += await self._execute("DELETE FROM at_pending WHERE expire_at < ?", (now,))
        deleted += await self._execute("DELETE FROM reports WHERE expire_at IS NOT NULL AND expire_at <= ?", (now,))
        return deleted

    # ---------------------------------------------------------------- 生成锁

    async def acquire_generating_lock(self, group_id: int, date_str: str, ttl: int = 300) -> bool:
        """获取报告生成锁（进程内，TTL 默认 300s，复刻原作 SET NX EX）。"""
        now = self._now()
        self._purge_generating_locks(now)
        key = f"{group_id}:{date_str}"
        if key in self._generating_locks:
            return False
        self._generating_locks[key] = (now, int(ttl))
        logger.debug(f"获取生成锁成功: 群 {group_id}, 日期 {date_str}")
        return True

    async def release_generating_lock(self, group_id: int, date_str: str) -> None:
        """释放报告生成锁。"""
        self._generating_locks.pop(f"{group_id}:{date_str}", None)
        logger.debug(f"释放生成锁: 群 {group_id}, 日期 {date_str}")

    def _purge_generating_locks(self, now: int) -> None:
        for key, (acquired_at, ttl) in list(self._generating_locks.items()):
            if now - acquired_at >= ttl:
                del self._generating_locks[key]


_storage: Storage | None = None
_lifecycle_registered = False


def get_storage() -> Storage:
    """获取存储层单例。"""
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage


def set_storage(storage: Storage | None) -> None:
    """替换存储层单例（测试或自定义场景使用）。"""
    global _storage
    _storage = storage


async def init_default_storage() -> None:
    """初始化默认存储（driver startup 钩子，幂等）。"""
    await get_storage().init()


async def close_default_storage() -> None:
    """关闭默认存储（driver shutdown 钩子）。"""
    await get_storage().close()


def register_lifecycle() -> bool:
    """把存储初始化挂到 driver 的 startup / shutdown 上，返回是否本次注册成功。

    幂等；NoneBot 未初始化（例如直接导入模块）时返回 ``False``。
    """
    global _lifecycle_registered
    if _lifecycle_registered:
        return False

    try:
        from nonebot import get_driver

        driver = get_driver()
    except Exception:
        return False

    driver.on_startup(init_default_storage)
    driver.on_shutdown(close_default_storage)
    _lifecycle_registered = True
    return True


register_lifecycle()
