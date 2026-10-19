"""命令层公共工具：命令正则、日期 / 天数解析与群信息查询。"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from conftest import FakeBot, make_event
from nonebot.adapters.onebot.v11 import Message

from nonebot_plugin_group_insight.commands.common import (
    AT_ME_PATTERN,
    CLEAR_ALL_AT_PATTERN,
    CLEAR_AT_PATTERN,
    FORCE_REPORT_PATTERN,
    GROUP_REPORT_PATTERN,
    PERSONAL_WORDCLOUD_PATTERN,
    WORDCLOUD_PATTERN,
    date_label_for,
    days_label,
    fetch_group_info,
    is_group_event,
    parse_days,
    resolve_query_date,
)

TODAY = date.today()


# ------------------------------------------------------------------ 命令正则


@pytest.mark.parametrize(
    ("pattern", "text"),
    [
        (GROUP_REPORT_PATTERN, "#群聊报告"),
        (GROUP_REPORT_PATTERN, "#群聊总结"),
        (GROUP_REPORT_PATTERN, "#群聊报告 123456789"),
        (GROUP_REPORT_PATTERN, "#群聊报告 昨天"),
        (GROUP_REPORT_PATTERN, "#群聊总结 123456789 昨天"),
        (GROUP_REPORT_PATTERN, "#群聊报告 昨天 123456789"),
        (GROUP_REPORT_PATTERN, "#群聊报告 2024-11-01"),
        (FORCE_REPORT_PATTERN, "#强制生成报告 123456789 前天"),
        (WORDCLOUD_PATTERN, "#词云"),
        (WORDCLOUD_PATTERN, "#群聊词云"),
        (WORDCLOUD_PATTERN, "#词云 三天"),
        (PERSONAL_WORDCLOUD_PATTERN, "#个人词云"),
        (PERSONAL_WORDCLOUD_PATTERN, "#个人词云七天"),
        (AT_ME_PATTERN, "谁艾特我"),
        (AT_ME_PATTERN, "谁@我"),
        (AT_ME_PATTERN, "哪个小可爱at她"),
        (CLEAR_AT_PATTERN, "#清除艾特数据"),
        (CLEAR_AT_PATTERN, "清除at数据"),
        (CLEAR_ALL_AT_PATTERN, "#清除全部艾特数据"),
        (CLEAR_ALL_AT_PATTERN, "清除全部at数据"),
    ],
)
def test_pattern_matches(pattern: str, text: str):
    assert re.search(pattern, text)


@pytest.mark.parametrize(
    ("pattern", "text"),
    [
        # 锚定全匹配：前缀 / 后缀多余内容不触发
        (GROUP_REPORT_PATTERN, "看看#群聊报告"),
        (GROUP_REPORT_PATTERN, "#群聊报告 今天 好耶"),
        (GROUP_REPORT_PATTERN, "#群聊报告 abc"),
        (GROUP_REPORT_PATTERN, "#群聊报告 12345 6789"),
        (FORCE_REPORT_PATTERN, "#群聊报告"),
        (WORDCLOUD_PATTERN, "#词云 五天"),
        (WORDCLOUD_PATTERN, "#词云五"),
        (AT_ME_PATTERN, "谁艾特你"),
        # 清除全部不会被清除规则命中（互斥）
        (CLEAR_AT_PATTERN, "#清除全部艾特数据"),
    ],
)
def test_pattern_does_not_match(pattern: str, text: str):
    assert not re.search(pattern, text)


def test_report_pattern_extracts_args():
    matched = re.search(GROUP_REPORT_PATTERN, "#群聊报告 昨天 123456789")
    assert matched is not None
    assert (matched.group("arg1"), matched.group("arg2")) == ("昨天", "123456789")

    matched = re.search(GROUP_REPORT_PATTERN, "#群聊报告")
    assert matched is not None
    assert (matched.group("arg1"), matched.group("arg2")) == (None, None)


def test_is_group_event():
    assert is_group_event(make_event(message=Message("#词云"))) is True


# ------------------------------------------------------------------ 天数 / 日期


@pytest.mark.parametrize(
    ("message", "days"),
    [("#词云", 1), ("#词云 当天", 1), ("#词云 三天", 3), ("#个人词云七天", 7)],
)
def test_parse_days(message: str, days: int):
    assert parse_days(message) == days


def test_days_label():
    assert days_label(1) == "当天"
    assert days_label(3) == "三天"
    assert days_label(7) == "七天"


def test_resolve_query_date_keywords():
    assert resolve_query_date(None) == (TODAY.isoformat(), "今天", True)
    assert resolve_query_date("今天") == (TODAY.isoformat(), "今天", True)
    assert resolve_query_date("昨天") == ((TODAY - timedelta(days=1)).isoformat(), "昨天", False)
    assert resolve_query_date("前天") == ((TODAY - timedelta(days=2)).isoformat(), "前天", False)


def test_resolve_query_date_explicit():
    query_date, label, is_today = resolve_query_date("2024-11-01")
    assert query_date == "2024-11-01"
    assert label == "2024年11月01日"
    assert is_today is False

    # 具体日期即今天时仍按 isToday 处理，但标签保持 YYYY年MM月DD日（与原作一致）
    assert resolve_query_date(TODAY.isoformat()) == (
        TODAY.isoformat(),
        f"{TODAY.year}年{TODAY.month:02d}月{TODAY.day:02d}日",
        True,
    )


@pytest.mark.parametrize("value", ["2024-13-01", "2024-02-30", "0000-01-01"])
def test_resolve_query_date_invalid(value: str):
    with pytest.raises(ValueError):
        resolve_query_date(value)


def test_date_label_for():
    assert date_label_for(TODAY.isoformat()) == "今天"
    assert date_label_for((TODAY - timedelta(days=1)).isoformat()) == "昨天"
    assert date_label_for((TODAY - timedelta(days=2)).isoformat()) == "前天"
    assert date_label_for("2024-11-01") == "2024年11月01日"


# ------------------------------------------------------------------ 群信息


class RaisingBot(FakeBot):
    async def get_group_info(self, group_id: int):
        raise RuntimeError("群不存在")


async def test_fetch_group_info_success():
    class InfoBot(FakeBot):
        async def get_group_info(self, group_id: int):
            return {"group_id": group_id, "group_name": "测试群"}

    assert await fetch_group_info(InfoBot(), 2002) == (True, "测试群")


async def test_fetch_group_info_fallbacks():
    # API 抛异常 / 返回空 / 没有 Bot 实例 / 没有群名 → 均降级为 群{id}
    assert await fetch_group_info(RaisingBot(), 2002) == (False, "群2002")
    assert await fetch_group_info(FakeBot(), 2002) == (False, "群2002")  # FakeBot 无 get_group_info
    assert await fetch_group_info(None, 2002) == (False, "群2002")

    class NoNameBot(FakeBot):
        async def get_group_info(self, group_id: int):
            return {"group_id": group_id}

    assert await fetch_group_info(NoNameBot(), 2002) == (True, "群2002")
