"""模板移植测试：Jinja2 渲染、字段注入、转义语义与条件分支。

不依赖浏览器：用 ``StrictUndefined`` 环境渲染 HTML 字符串后直接断言（未定义变量会抛异常）。
"""

from __future__ import annotations

from datetime import datetime

from nonebot_plugin_group_insight.analysis.statistics import StatisticsService
from nonebot_plugin_group_insight.config import Config
from nonebot_plugin_group_insight.render.report import build_template_data
from nonebot_plugin_group_insight.render.templates import render_template_html
from nonebot_plugin_group_insight.wordcloud_gen import build_wordcloud_template_data

DAY = datetime(2023, 11, 14)

USER_CAPSULE = (
    '<span class="user-capsule"><img class="user-capsule-avatar" '
    'src="https://q1.qlogo.cn/g?b=qq&nk=1001&s=100" alt="小明">'
    '<span class="user-capsule-name">小明</span></span>'
)


def at(hour: int, minute: int = 0) -> int:
    return int(DAY.replace(hour=hour, minute=minute).timestamp())


def message(user_id: int, nickname: str, text: str, hour: int, **extra) -> dict:
    record = {
        "user_id": user_id,
        "nickname": nickname,
        "message": text,
        "time": at(hour),
        "hour": hour,
        "length": len(text),
        "hasReply": False,
        "faces": {"face": [], "mface": [], "emoji": [], "total": 0},
        "links": [],
        "videos": [],
    }
    record.update(extra)
    return record


def sample_messages() -> list[dict]:
    messages = [
        message(1001, "小明", "今天天气真不错", 9, hasReply=True, links=[{"source": "B站"}]),
        message(1001, "小明", "要不要出去玩", 9),
        message(1002, "小红", "好呀好呀", 10, faces={"face": [], "mface": [], "emoji": [], "total": 2}),
        message(1003, "小刚", "哈哈哈哈笑死", 23, videos=[{"url": "v"}]),
    ]
    return messages


def sample_analysis(**overrides) -> dict:
    analysis = {
        "stats": StatisticsService(Config()).analyze(sample_messages()),
        "topics": [
            {
                "topic": "周末出行",
                "detail": f"大家约着出去玩 {USER_CAPSULE}",
                "contributors": [
                    {"user_id": "1001", "nickname": "小明"},
                    {"user_id": None, "nickname": "路人甲"},
                ],
            },
            {"topic": "天气", "detail": "聊了聊天气", "contributors": []},
        ],
        "goldenQuotes": [
            {
                "quote": "哈哈哈哈笑死",
                "reason": "确实好笑",
                "sender": {"user_id": "1003", "nickname": "小刚"},
            },
            {"quote": "好呀好呀", "reason": "敷衍", "sender": {"user_id": None, "nickname": "小红"}},
        ],
        "userTitles": [
            {"user_id": "1001", "user": "小明", "title": "话痨王", "mbti": "ENFP", "reason": "话最多"},
            {"user_id": None, "user": "小红", "title": "夜猫子", "mbti": "INFP", "reason": "半夜活跃"},
        ],
        "tokenUsage": {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46},
        "messageCount": 4,
    }
    analysis.update(overrides)
    return analysis


def report_data(**overrides) -> dict:
    data = build_template_data(sample_analysis(), group_name="测试群", date_label="今天", query_date="2023-11-14")
    data.update(overrides)
    return data


# ------------------------------------------------------------------ 语法转换


def test_default_template_has_no_art_template_syntax():
    html = render_template_html("default", report_data())

    for token in ("{{each", "{{/each", "{{if", "{{/if", "{{@", "{{else", "{{", "{%"):
        assert token not in html


def test_scrapbook_template_has_no_art_template_syntax():
    html = render_template_html("scrapbook", report_data())

    for token in ("{{each", "{{/each", "{{if", "{{/if", "{{@", "{{else", "{{", "{%"):
        assert token not in html


def test_wordcloud_template_has_no_art_template_syntax():
    data = build_wordcloud_template_data(group_name="测试群", days=3, message_count=7, word_list=[["你好", 5.0]])
    html = render_template_html("wordcloud", data)

    for token in ("{{each", "{{/each", "{{if", "{{/if", "{{@", "{{", "{%"):
        assert token not in html
    assert "WordCloud(canvas" in html
    assert "<script src=" not in html  # wordcloud2.js 已内联，不依赖相对路径


def test_css_is_inlined():
    for name in ("default", "scrapbook"):
        html = render_template_html(name, report_data())
        assert "<style>" in html
        assert 'href="style.css"' not in html
        assert ".container" in html

    wordcloud_html = render_template_html(
        "wordcloud",
        build_wordcloud_template_data(group_name="群", days=1, message_count=1, word_list=[["词", 5.0]]),
    )
    assert ".wordcloud-container" in wordcloud_html


# ------------------------------------------------------------------ 字段注入


def test_default_template_renders_all_sections():
    html = render_template_html("default", report_data())

    assert "群聊分析报告" in html
    assert "消息总数" in html and ">4</div>" in html  # totalMessages
    assert "分析时段: 2023/11/14" in html
    assert "最活跃时段: 09:00-10:00" in html
    assert "峰值: 9:00-10:00" in html

    # 24 小时活跃度：24 根柱子
    assert html.count('class="activity-hour ') == 24
    assert html.count('class="activity-bar activity-') == 24
    assert 'class="activity-hour peak"' in html

    # 话题 + 参与者胶囊
    assert "1. 周末出行" in html and "2. 天气" in html
    assert USER_CAPSULE in html
    assert '<span class="user-capsule-name">小明</span>' in html
    assert '<span>路人甲</span>' in html  # 无 user_id 的贡献者走纯文本分支
    collapsed = " ".join(html.split())
    assert "、 <span>路人甲</span>" in collapsed  # 贡献者之间的顿号分隔

    # 群友称号 / 金句
    assert "群友称号" in html and "话痨王" in html and "ENFP" in html
    assert "群圣经" in html and "哈哈哈哈笑死" in html and "小刚" in html
    assert 'src="https://q1.qlogo.cn/g?b=qq&nk=1003&s=100"' in html

    # Token 与生成时间
    assert "Prompt 12 + Completion 34 = Total 46" in html
    assert "报告生成时间: " in html


def test_default_template_escapes_plain_values_but_keeps_detail_html():
    analysis = sample_analysis(
        topics=[{"topic": "<b>加粗</b>", "detail": "<b>原文注入</b>", "contributors": []}],
        goldenQuotes=[{"quote": "<i>金句</i>", "reason": "r", "sender": {"user_id": None, "nickname": "n"}}],
    )
    html = render_template_html("default", build_template_data(analysis))

    assert "&lt;b&gt;加粗&lt;/b&gt;" in html
    assert "<b>加粗</b>" not in html
    assert "&lt;i&gt;金句&lt;/i&gt;" in html
    assert "<b>原文注入</b>" in html


def test_default_template_hides_optional_sections_and_stats_cards():
    analysis = sample_analysis(
        topics=[],
        goldenQuotes=[],
        userTitles=[],
        tokenUsage=None,
    )
    data = build_template_data(analysis)
    data["totalLinks"] = 0
    data["totalVideos"] = 0
    html = render_template_html("default", data)

    assert '<i class="fas fa-fire"></i> 热门话题' not in html
    assert '<i class="fas fa-gem"></i> 群圣经' not in html
    assert '<i class="fas fa-trophy"></i> 群友称号' not in html
    assert "分享链接" not in html
    assert "视频数量" not in html
    assert "Token 使用" not in html
    # 活跃度图表仍在（受 enableActivityChart 控制）
    assert "24小时活跃度分布" in html


def test_default_template_activity_chart_disabled_hides_section():
    config = Config()
    config.group_insight_activity_enabled = False
    data = build_template_data(sample_analysis(), config=config)
    html = render_template_html("default", data)

    assert data["activityChart"] is None
    assert data["enableActivityChart"] is False
    # 原作：关闭时两个分支都不渲染
    assert "24小时活跃度分布" not in html
    assert "暂无数据" not in html


def test_default_template_activity_empty_branch():
    """``{{else enableActivityChart}}`` 分支的模板层验证。

    原作 ``enableActivityChart`` 为 ``enabled && chart !== null``，因此该分支在真实数据下不可达
    （``build_template_data`` 同样不会产出 ``enableActivityChart=True, activityChart=None``）。
    """
    data = report_data()
    data["enableActivityChart"] = True
    data["activityChart"] = None
    html = render_template_html("default", data)

    assert "暂无数据" in html
    assert 'class="activity-bars"' not in html


def test_build_template_data_marks_chart_disabled_when_hourly_missing():
    data = build_template_data(sample_analysis(stats={"basic": {"totalMessages": 3}}))

    assert data["activityChart"] is None
    assert data["enableActivityChart"] is False


def test_scrapbook_template_renders_all_sections():
    html = render_template_html("scrapbook", report_data())

    assert "五彩斑斓的一天" in html
    assert ">4</div>" in html  # 消息数邮票
    assert "09:00-10:00" in html
    assert "2023/11/14" in html

    # 手绘条形图：24 行，小时补零，颜色分档
    assert html.count('<div class="hand-chart-row">') == 24
    assert ">00:00</div>" in html and ">23:00</div>" in html
    assert ">09:00</div>" in html
    assert "var(--color-purple)" in html  # count == 0 的柱子
    assert "var(--accent-orange)" in html  # heightPercent >= 70

    # 话题编号补零 + 参与者行内联
    assert "#01" in html and "#02" in html
    assert "🙋‍♀️ 参与者: 小明、路人甲" in html
    assert USER_CAPSULE in html

    # 画像与金句
    assert "话痨王" in html and "ENFP" in html
    assert "哈哈哈哈笑死" in html and "— 小刚" in html
    assert "AI 锐评" in html
    assert "Prompt 12 + Completion 34 = Total 46" in html


def test_scrapbook_topic_index_beyond_nine_uses_raw_number():
    topics = [{"topic": f"话题{i}", "detail": "d", "contributors": []} for i in range(12)]
    html = render_template_html("scrapbook", build_template_data(sample_analysis(topics=topics)))

    assert "#09" in html and "#10" in html and "#12" in html


def test_wordcloud_template_personal_and_group_title():
    word_list = [["你好", 10.0], ["世界", 1.0]]
    personal = render_template_html(
        "wordcloud",
        build_wordcloud_template_data(
            group_name="测试群", days=7, message_count=30, word_list=word_list, user_name="小明"
        ),
    )
    assert "小明 的词云" in personal
    assert "近七天" in personal
    assert "消息数: 30" in personal
    assert 'const wordList = [["你好", 10.0], ["世界", 1.0]];' in personal
    assert "canvas.width = 1200;" in personal and "canvas.height = 800;" in personal
    assert "backgroundColor: '#ffffff'" in personal

    group = render_template_html(
        "wordcloud",
        build_wordcloud_template_data(group_name="测试群", days=1, message_count=3, word_list=word_list),
    )
    assert "群聊词云" in group
    assert "当天" in group
    assert "的词云" not in group
