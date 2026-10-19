"""统计分析服务（对应原作 ``services/StatisticsService.js`` 与 ``services/ActivityVisualizer.js``）。

单次遍历消息列表，产出基础统计、用户统计、小时分布、表情/链接/视频统计与排行。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..config import Config, get_config


class StatisticsService:
    """从消息列表计算统计指标。"""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or get_config()
        self.night_start_hour = self.config.group_insight_night_start_hour
        self.night_end_hour = self.config.group_insight_night_end_hour

    def analyze(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        if not messages:
            return self.get_empty_stats()

        user_map: dict[Any, dict[str, Any]] = {}
        hourly_count = [0] * 24
        emoji_stats = {"face": 0, "mface": 0, "emoji": 0, "total": 0}
        link_stats: dict[str, int] = {}
        total_videos = 0

        total_chars = 0
        total_emojis = 0
        total_replies = 0
        timestamps: list[float] = []

        for msg in messages:
            user_id = msg.get("user_id")
            hour = msg.get("hour")
            if hour is None:
                hour = datetime.fromtimestamp(int(msg.get("time") or 0)).hour
            msg_length = msg.get("length") or len(msg.get("message") or "")

            total_chars += msg_length
            timestamps.append((msg.get("time") or 0) * 1000)

            if msg.get("hasReply"):
                total_replies += 1

            faces = msg.get("faces")
            if isinstance(faces, dict) and faces.get("total") is not None:
                total_emojis += faces.get("total") or 0
                emoji_stats["face"] += len(faces.get("face") or [])
                emoji_stats["mface"] += len(faces.get("mface") or [])
                if isinstance(faces.get("emoji"), list):
                    emoji_stats["emoji"] += sum(faces["emoji"])
                emoji_stats["total"] += faces.get("total") or 0

            links = msg.get("links") or []
            if links:
                for link in links:
                    source = (link.get("source") if isinstance(link, dict) else None) or "未知来源"
                    link_stats[source] = link_stats.get(source, 0) + 1

            videos = msg.get("videos") or []
            if videos:
                total_videos += len(videos)

            hourly_count[hour] += 1

            if user_id not in user_map:
                user_map[user_id] = {
                    "user_id": user_id,
                    "nickname": msg.get("nickname"),
                    "messageCount": 0,
                    "charCount": 0,
                    "emojiCount": 0,
                    "replyCount": 0,
                    "nightCount": 0,
                    "linkCount": 0,
                    "videoCount": 0,
                    "hourlyDistribution": [0] * 24,
                }

            user_stat = user_map[user_id]
            user_stat["messageCount"] += 1
            user_stat["charCount"] += msg_length
            user_stat["hourlyDistribution"][hour] += 1

            if isinstance(faces, dict) and faces.get("total") is not None:
                user_stat["emojiCount"] += faces.get("total") or 0

            if msg.get("hasReply"):
                user_stat["replyCount"] += 1

            if links:
                user_stat["linkCount"] += len(links)

            if videos:
                user_stat["videoCount"] += len(videos)

            if self.is_night_hour(hour):
                user_stat["nightCount"] += 1

        min_time = min(timestamps)
        max_time = max(timestamps)
        basic_stats = {
            "totalMessages": len(messages),
            "totalUsers": len(user_map),
            "totalChars": total_chars,
            "totalEmojis": total_emojis,
            "totalReplies": total_replies,
            "replyRatio": (total_replies / len(messages)) if messages else 0,
            "avgCharsPerMsg": f"{total_chars / len(messages):.1f}" if messages else 0,
            "dateRange": {
                "start": self._format_date(min_time),
                "end": self._format_date(max_time),
            },
        }

        user_stats = []
        for user_stat in user_map.values():
            message_count = user_stat["messageCount"]
            user_stat["avgLength"] = f"{user_stat['charCount'] / message_count:.1f}" if message_count > 0 else 0
            user_stat["emojiRatio"] = f"{user_stat['emojiCount'] / message_count:.2f}" if message_count > 0 else 0
            user_stat["replyRatio"] = f"{user_stat['replyCount'] / message_count:.2f}" if message_count > 0 else 0
            user_stat["nightRatio"] = f"{user_stat['nightCount'] / message_count:.2f}" if message_count > 0 else 0
            total_shares = user_stat["linkCount"] + user_stat["videoCount"]
            user_stat["shareRatio"] = f"{total_shares / message_count:.2f}" if message_count > 0 else 0
            user_stat["mostActiveHour"] = self.find_peak_hour(user_stat["hourlyDistribution"])
            user_stats.append(user_stat)

        peak_hour = self.find_peak_hour(hourly_count)
        peak_count = hourly_count[peak_hour]
        hourly_activity = [
            self._activity_level(count, peak_count) for count in hourly_count
        ]

        hourly_stats = {
            "hourlyCount": hourly_count,
            "hourlyActivity": hourly_activity,
            "peakHour": peak_hour,
            "peakCount": peak_count,
            "peakPeriod": self.get_hour_range(peak_hour),
        }

        return {
            "basic": basic_stats,
            "users": user_stats,
            "hourly": hourly_stats,
            "emoji": emoji_stats,
            "links": {"total": sum(link_stats.values()), "bySource": link_stats},
            "videos": total_videos,
            "topUsers": self.rank_users(user_stats),
        }

    def rank_users(self, user_stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ranked = sorted(user_stats, key=lambda user: user["messageCount"], reverse=True)
        return [{**user, "rank": index + 1} for index, user in enumerate(ranked)]

    def is_night_hour(self, hour: int) -> bool:
        return self.night_start_hour <= hour < self.night_end_hour

    def find_peak_hour(self, hourly_count: list[int]) -> int:
        max_count = 0
        peak_hour = 0

        for index, count in enumerate(hourly_count):
            if count > max_count:
                max_count = count
                peak_hour = index

        return peak_hour

    def get_hour_range(self, hour: int) -> str:
        next_hour = (hour + 1) % 24
        return f"{hour:02d}:00-{next_hour:02d}:00"

    def get_empty_stats(self) -> dict[str, Any]:
        return {
            "basic": {
                "totalMessages": 0,
                "totalUsers": 0,
                "totalChars": 0,
                "totalEmojis": 0,
                "totalReplies": 0,
                "replyRatio": 0,
                "avgCharsPerMsg": 0,
                "dateRange": {"start": "", "end": ""},
            },
            "users": [],
            "hourly": {
                "hourlyCount": [0] * 24,
                "hourlyActivity": ["none"] * 24,
                "peakHour": 0,
                "peakCount": 0,
                "peakPeriod": "00:00-01:00",
            },
            "emoji": {"face": 0, "mface": 0, "emoji": 0, "total": 0},
            "links": {"total": 0, "bySource": {}},
            "videos": 0,
            "topUsers": [],
        }

    @staticmethod
    def _activity_level(count: int, peak_count: int) -> str:
        if count == 0:
            return "none"
        ratio = count / peak_count
        if ratio >= 0.7:
            return "high"
        if ratio >= 0.4:
            return "medium"
        return "low"

    @staticmethod
    def _format_date(timestamp_ms: float) -> str:
        """``toLocaleDateString('zh-CN')`` 的等价形式：``2023/11/14``（不补零）。"""
        moment = datetime.fromtimestamp(timestamp_ms / 1000)
        return f"{moment.year}/{moment.month}/{moment.day}"


class ActivityVisualizer:
    """活跃度图表数据准备（对应原作 ``services/ActivityVisualizer.js``）。"""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or get_config()

    def prepare_chart_data(self, hourly_stats: dict[str, Any] | None) -> dict[str, Any] | None:
        if not hourly_stats or not hourly_stats.get("hourlyCount"):
            return None

        hourly_count = hourly_stats["hourlyCount"]
        peak_hour = hourly_stats.get("peakHour", 0)
        peak_count = hourly_stats.get("peakCount", 0)

        hours = []
        for hour, count in enumerate(hourly_count):
            height_percent = (count / peak_count) * 100 if peak_count > 0 else 0
            activity_ratio = count / peak_count if peak_count > 0 else 0

            activity_level = "none"
            if count > 0:
                if activity_ratio >= 0.7:
                    activity_level = "high"
                elif activity_ratio >= 0.4:
                    activity_level = "medium"
                else:
                    activity_level = "low"

            hours.append(
                {
                    "hour": hour,
                    "count": count,
                    "heightPercent": height_percent,
                    "activityLevel": activity_level,
                    "activityRatio": activity_ratio,
                    "isPeak": hour == peak_hour,
                }
            )

        return {
            "hours": hours,
            "peakHour": peak_hour,
            "peakCount": peak_count,
            "peakNextHour": (peak_hour + 1) % 24,
        }
