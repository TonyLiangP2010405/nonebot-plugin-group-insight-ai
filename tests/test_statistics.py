"""统计服务测试：基础/用户/小时统计字段、夜间区间与活跃度图表数据。"""

from __future__ import annotations

from datetime import datetime

from nonebot_plugin_group_insight.analysis.statistics import ActivityVisualizer, StatisticsService
from nonebot_plugin_group_insight.config import Config

DAY = datetime(2023, 11, 14)


def at(hour: int, minute: int = 0) -> int:
    return int(DAY.replace(hour=hour, minute=minute).timestamp())


def msg(user_id: int, nickname: str, text: str, hour: int, **extra) -> dict:
    record = {
        "user_id": user_id,
        "nickname": nickname,
        "message": text,
        "time": at(hour),
        "hour": hour,
        "length": len(text),
        "hasReply": False,
    }
    record.update(extra)
    return record


def sample_messages() -> list[dict]:
    return [
        msg(
            1001,
            "甲",
            "abc",
            2,
            hasReply=True,
            faces={"face": [1, 2], "mface": [3], "emoji": [1, 2], "total": 5},
            links=[{"source": "A"}],
            videos=[{"url": "v"}],
        ),
        msg(
            1001,
            "甲",
            "defg",
            10,
            faces={"face": [], "mface": [], "emoji": [], "total": 1},
            links=[{"source": "A"}, {"source": "B"}],
        ),
        msg(1002, "乙", "hi", 10, videos=[{"url": "v2"}]),
        msg(1003, "丙", "j", 14),
    ]


def test_basic_stats_fields():
    stats = StatisticsService(Config()).analyze(sample_messages())

    assert stats["basic"] == {
        "totalMessages": 4,
        "totalUsers": 3,
        "totalChars": 10,
        "totalEmojis": 6,
        "totalReplies": 1,
        "replyRatio": 0.25,
        "avgCharsPerMsg": "2.5",
        "dateRange": {"start": "2023/11/14", "end": "2023/11/14"},
    }
    assert stats["emoji"] == {"face": 2, "mface": 1, "emoji": 3, "total": 6}
    assert stats["links"] == {"total": 3, "bySource": {"A": 2, "B": 1}}
    assert stats["videos"] == 2


def test_hourly_stats_and_activity_levels():
    stats = StatisticsService(Config()).analyze(sample_messages())
    hourly = stats["hourly"]

    assert hourly["hourlyCount"][2] == 1
    assert hourly["hourlyCount"][10] == 2
    assert hourly["hourlyCount"][14] == 1
    assert sum(hourly["hourlyCount"]) == 4
    assert hourly["peakHour"] == 10
    assert hourly["peakCount"] == 2
    assert hourly["peakPeriod"] == "10:00-11:00"

    assert hourly["hourlyActivity"][10] == "high"
    assert hourly["hourlyActivity"][2] == "medium"
    assert hourly["hourlyActivity"][14] == "medium"
    assert hourly["hourlyActivity"][0] == "none"
    assert hourly["hourlyActivity"][11] == "none"


def test_user_stats_fields():
    stats = StatisticsService(Config()).analyze(sample_messages())
    users = {user["user_id"]: user for user in stats["users"]}

    assert [user["user_id"] for user in stats["users"]] == [1001, 1002, 1003]

    first = users[1001]
    assert first["nickname"] == "甲"
    assert first["messageCount"] == 2
    assert first["charCount"] == 7
    assert first["emojiCount"] == 6
    assert first["replyCount"] == 1
    assert first["nightCount"] == 1
    assert first["linkCount"] == 3
    assert first["videoCount"] == 1
    assert first["hourlyDistribution"][2] == 1
    assert first["hourlyDistribution"][10] == 1
    assert first["avgLength"] == "3.5"
    assert first["emojiRatio"] == "3.00"
    assert first["replyRatio"] == "0.50"
    assert first["nightRatio"] == "0.50"
    assert first["shareRatio"] == "2.00"
    assert first["mostActiveHour"] == 2  # 并列时取首个最大值

    second = users[1002]
    assert second["avgLength"] == "2.0"
    assert second["emojiRatio"] == "0.00"
    assert second["nightRatio"] == "0.00"
    assert second["shareRatio"] == "1.00"
    assert second["mostActiveHour"] == 10

    assert users[1003]["avgLength"] == "1.0"
    assert users[1003]["mostActiveHour"] == 14


def test_top_users_are_ranked_by_message_count():
    stats = StatisticsService(Config()).analyze(sample_messages())

    assert [(user["user_id"], user["rank"], user["messageCount"]) for user in stats["topUsers"]] == [
        (1001, 1, 2),
        (1002, 2, 1),
        (1003, 3, 1),
    ]


def test_hour_falls_back_to_timestamp():
    stats = StatisticsService(Config()).analyze(
        [{"user_id": 1, "nickname": "甲", "message": "hi", "time": at(7), "length": 2}]
    )

    assert stats["hourly"]["hourlyCount"][7] == 1
    assert stats["users"][0]["mostActiveHour"] == 7


def test_night_window_is_left_closed_right_open():
    service = StatisticsService(Config(group_insight_night_start_hour=1, group_insight_night_end_hour=3))

    assert service.is_night_hour(1) is True
    assert service.is_night_hour(2) is True
    assert service.is_night_hour(3) is False
    assert service.is_night_hour(0) is False


def test_empty_stats():
    stats = StatisticsService(Config()).analyze([])

    assert stats["basic"] == {
        "totalMessages": 0,
        "totalUsers": 0,
        "totalChars": 0,
        "totalEmojis": 0,
        "totalReplies": 0,
        "replyRatio": 0,
        "avgCharsPerMsg": 0,
        "dateRange": {"start": "", "end": ""},
    }
    assert stats["users"] == []
    assert stats["hourly"] == {
        "hourlyCount": [0] * 24,
        "hourlyActivity": ["none"] * 24,
        "peakHour": 0,
        "peakCount": 0,
        "peakPeriod": "00:00-01:00",
    }
    assert stats["emoji"] == {"face": 0, "mface": 0, "emoji": 0, "total": 0}
    assert stats["links"] == {"total": 0, "bySource": {}}
    assert stats["videos"] == 0
    assert stats["topUsers"] == []


def test_prepare_chart_data():
    hourly = StatisticsService(Config()).analyze(sample_messages())["hourly"]
    chart = ActivityVisualizer(Config()).prepare_chart_data(hourly)

    assert chart["peakHour"] == 10
    assert chart["peakCount"] == 2
    assert chart["peakNextHour"] == 11
    assert len(chart["hours"]) == 24

    peak = chart["hours"][10]
    assert peak == {
        "hour": 10,
        "count": 2,
        "heightPercent": 100.0,
        "activityLevel": "high",
        "activityRatio": 1.0,
        "isPeak": True,
    }
    assert chart["hours"][2]["heightPercent"] == 50.0
    assert chart["hours"][2]["activityLevel"] == "medium"
    assert chart["hours"][2]["isPeak"] is False
    assert chart["hours"][0] == {
        "hour": 0,
        "count": 0,
        "heightPercent": 0,
        "activityLevel": "none",
        "activityRatio": 0,
        "isPeak": False,
    }


def test_prepare_chart_data_without_hourly_stats():
    visualizer = ActivityVisualizer(Config())

    assert visualizer.prepare_chart_data(None) is None
    assert visualizer.prepare_chart_data({}) is None
    assert visualizer.prepare_chart_data({"hourlyCount": []}) is None
