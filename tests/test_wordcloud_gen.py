"""词云测试：分词过滤、词频 / TF-IDF 权重、词典加载与渲染参数（截图被 monkeypatch）。"""

from __future__ import annotations

import json
import math

from nonebot_plugin_group_insight import wordcloud_gen
from nonebot_plugin_group_insight.config import Config
from nonebot_plugin_group_insight.wordcloud_gen import (
    WORDCLOUD_VIEWPORT_PADDING_X,
    WORDCLOUD_VIEWPORT_PADDING_Y,
    TextProcessor,
    build_word_list,
    build_wordcloud_template_data,
    generate_personal_wordcloud_image,
    generate_wordcloud_image,
    get_time_range_text,
)

DAY = 1_700_000_000


def make_message(user_id: int, text: str, offset: int = 0) -> dict:
    return {
        "user_id": user_id,
        "nickname": f"用户{user_id}",
        "message": text,
        "time": DAY + offset,
        "hour": 10,
        "length": len(text),
        "hasReply": False,
    }


def processor(tmp_path, **config_overrides) -> TextProcessor:
    config = Config()
    for key, value in config_overrides.items():
        setattr(config, key, value)
    instance = TextProcessor(config, data_dir=tmp_path)
    instance.init()
    return instance


# ------------------------------------------------------------------ 文本清洗


def test_clean_text_removes_mentions_brackets_and_command_prefix():
    instance = TextProcessor(Config(), data_dir="/nonexistent")

    assert instance.clean_text("@小明 [图片] 今天 天气不错") == "今天 天气不错"
    assert instance.clean_text("#群聊报告 今天 天气不错") == "群聊报告 今天 天气不错"
    assert instance.clean_text("/词云  三天") == "词云 三天"
    assert instance.clean_text("多   个\t空白\n换行") == "多 个 空白 换行"
    assert instance.clean_text("") == ""
    assert instance.clean_text(None) == ""
    # 行首前缀只在字符串真正以 # / 开头时删除（原作正则无 m 标志）
    assert instance.clean_text("  #前缀") == "#前缀"
    # 方括号只删同行的内容（原作正则无 dotall），换行随后被折叠为空格
    assert instance.clean_text("[跨\n行]文本") == "[跨 行]文本"


def test_simple_cut_fallback(tmp_path):
    instance = TextProcessor(Config(), data_dir=tmp_path)
    instance.stopwords = {"世界"}

    assert instance.simple_cut("今天天气不错hello世界", min_length=2) == ["今天天气不错"]
    assert instance.simple_cut("今天天气不错hello世界", min_length=7) == []
    assert instance.initialized is False
    # 未初始化时 cut 自动降级
    assert instance.cut("今天天气不错", 2) == ["今天天气不错"]


# -------------------------------------------------------------- 分词与过滤


def test_cut_four_layer_filtering(tmp_path):
    instance = processor(tmp_path)

    # 我们(r)/很(d) 被词性过滤；的/好 长度不足被过滤
    assert instance.cut("我们的项目进展很好") == ["项目", "进展"]
    assert instance.cut("hello world 你好世界") == ["你好", "世界"]  # 英文词无中文被过滤
    assert instance.cut("好", min_length=2) == []
    assert instance.cut("好", min_length=1) == ["好"]


def test_cut_filters_stopwords(tmp_path):
    (tmp_path / "stopwords.txt").write_text("# 注释行\n项目\n\n", encoding="utf-8")
    instance = processor(tmp_path)

    assert instance.stopwords == {"项目"}
    assert "项目" not in instance.cut("我们的项目进展很好")


def test_filter_strength_levels(tmp_path):
    loose = processor(tmp_path, group_insight_wordcloud_filter_strength="loose")
    standard = processor(tmp_path)
    strict = processor(tmp_path, group_insight_wordcloud_filter_strength="strict")
    unknown = processor(tmp_path, group_insight_wordcloud_filter_strength="unknown")

    assert loose.get_filter_pos_list() == frozenset({"u", "y"})
    assert standard.get_filter_pos_list() == frozenset({"u", "y", "d", "c", "r"})
    assert strict.get_filter_pos_list() == frozenset({"u", "y", "d", "c", "r", "q", "t", "f", "m"})
    assert unknown.get_filter_pos_list() == frozenset({"u", "y", "d", "c", "r"})

    assert "我们" in loose.cut("我们的项目进展很好")  # r 未被 loose 过滤
    assert "我们" not in standard.cut("我们的项目进展很好")
    assert "一起" in standard.cut("一起来看电影吧")  # m 只被 strict 过滤
    assert "一起" not in strict.cut("一起来看电影吧")
    assert "个" not in strict.cut("这个 个 的 了 吗")


def test_user_dict_merges_custom_words(tmp_path):
    (tmp_path / "userdict.txt").write_text("# 自定义词典\n紫色心情\n", encoding="utf-8")
    instance = processor(tmp_path)

    assert instance.initialized is True
    assert "紫色心情" in instance.cut("群友们都说紫色心情很好用")
    assert instance.load_user_dict() == 1


def test_missing_dict_files_degrade_silently(tmp_path):
    instance = TextProcessor(Config(), data_dir=tmp_path)
    instance.init()

    assert instance.initialized is True
    assert instance.load_user_dict() == 0
    assert instance.load_stopwords() == 0
    assert instance.stopwords == set()


# ---------------------------------------------------------------- 词频统计


def test_count_words_filters_and_sorts():
    instance = TextProcessor(Config(), data_dir="/nonexistent")
    words = ["甲", "乙", "乙", "丙", "丙", "丙"]

    assert instance.count_words(words, 2) == [{"word": "丙", "count": 3}, {"word": "乙", "count": 2}]
    assert instance.count_words(words, 1)[0] == {"word": "丙", "count": 3}
    assert instance.count_words([], 2) == []


def test_process_messages_frequency_mode(tmp_path):
    instance = processor(tmp_path)
    messages = [
        make_message(1001, "项目进展顺利"),
        make_message(1001, "项目进展很快"),
        make_message(1002, "项目进展"),
    ]

    word_data = instance.process_messages(messages, {"minLength": 2, "minFrequency": 2, "maxWords": 100})

    assert word_data[0] == {"word": "项目", "count": 3}
    assert {item["word"] for item in word_data} == {"项目", "进展"}

    limited = instance.process_messages(messages, {"minLength": 2, "minFrequency": 2, "maxWords": 1})
    assert limited == [{"word": "项目", "count": 3}]

    assert instance.process_messages(messages, {"minLength": 2, "minFrequency": 9}) == []


def test_process_messages_skips_empty_and_placeholder_messages(tmp_path):
    instance = processor(tmp_path)
    messages = [
        make_message(1001, ""),
        make_message(1001, "[图片]"),
        make_message(1001, "@某人"),
        make_message(1002, "项目进展项目进展"),
    ]

    assert instance.process_messages(messages, {"minFrequency": 2}) == [
        {"word": "项目", "count": 2},
        {"word": "进展", "count": 2},
    ]


# ------------------------------------------------------------------ TF-IDF


def test_calculate_tfidf_matches_formula():
    instance = TextProcessor(Config(), data_dir="/nonexistent")
    docs = [["甲", "甲", "乙"], ["甲", "丙"]]

    result = instance.calculate_tfidf(docs, top_k=10)
    weights = {item["word"]: item["weight"] for item in result}

    # 甲 出现在 2 篇文档：idf = log(3/3) + 1 = 1；两篇 tf 分别为 2/3 与 1/2
    alpha = (2 / 3) * (math.log(3 / 3) + 1) + (1 / 2) * (math.log(3 / 3) + 1)
    # 乙 / 丙 各出现在 1 篇文档：idf = log(3/2) + 1
    beta = math.log(3 / 2) + 1
    expected = {
        "甲": alpha,
        "乙": (1 / 3) * beta,
        "丙": (1 / 2) * beta,
    }
    assert result[0]["word"] == "甲"
    assert weights["甲"] == 1.0  # 最高分归一化为 1
    for word, score in expected.items():
        assert math.isclose(weights[word], score / alpha, rel_tol=1e-9)


def test_calculate_tfidf_empty_and_top_k():
    instance = TextProcessor(Config(), data_dir="/nonexistent")

    assert instance.calculate_tfidf([], 10) == []
    result = instance.calculate_tfidf([["甲", "乙", "丙"]], top_k=2)
    assert len(result) == 2


def test_process_messages_tfidf_ignores_min_frequency(tmp_path):
    instance = processor(tmp_path)
    messages = [
        make_message(1001, "项目进展顺利"),
        make_message(1002, "项目进展顺利"),
        make_message(1003, "天气不错"),
    ]

    word_data = instance.process_messages(messages, {"minFrequency": 9, "extractMethod": "tfidf"})

    words = [item["word"] for item in word_data]
    assert "天气" in words  # TF-IDF 模式忽略 minFrequency
    assert word_data[0]["weight"] == 1.0
    assert all(0 <= item["weight"] <= 1 for item in word_data)


def test_process_messages_tfidf_groups_by_user(tmp_path):
    instance = processor(tmp_path)
    # 以用户为文档：同一用户刷词不提升权重（文档内 TF 归一化）
    messages = [make_message(1001, "项目项目项目项目")] * 4 + [make_message(1002, "项目天气")]

    result = instance.process_messages_tfidf(messages, {"maxWords": 10})
    weights = {item["word"]: item["weight"] for item in result}

    assert set(weights) == {"项目", "天气"}
    assert weights["项目"] == 1.0
    assert weights["天气"] < weights["项目"]


# ------------------------------------------------------------------ 权重换算


def test_build_word_list_frequency_scaling():
    # freqRange == 0 → 权重恒为 5
    assert build_word_list([{"word": "甲", "count": 3}, {"word": "乙", "count": 3}], "frequency") == [
        ["甲", 5],
        ["乙", 5],
    ]

    word_list = build_word_list([{"word": "甲", "count": 8}, {"word": "乙", "count": 2}], "frequency")
    assert word_list[0] == ["甲", 10.0]
    assert word_list[1] == ["乙", 1.0]

    # 对数缩放：中间值落在 1-10 之间
    middle = build_word_list([{"word": "甲", "count": 8}, {"word": "乙", "count": 4}, {"word": "丙", "count": 2}], "frequency")
    assert 1 < middle[1][1] < 10
    assert build_word_list([], "frequency") == []


def test_build_word_list_tfidf_scaling():
    assert build_word_list([{"word": "甲", "weight": 1.0}, {"word": "乙", "weight": 0.5}], "tfidf") == [
        ["甲", 10.0],
        ["乙", 5.5],
    ]


def test_get_time_range_text():
    assert get_time_range_text(1) == "当天"
    assert get_time_range_text(3) == "近三天"
    assert get_time_range_text(7) == "近七天"
    assert get_time_range_text(30) == "近30天"


def test_build_wordcloud_template_data():
    data = build_wordcloud_template_data(
        group_name="测试群",
        days=3,
        message_count=12,
        word_list=[["甲", 10.0]],
        user_name="小明",
        config=Config(),
    )

    assert data["groupName"] == "测试群"
    assert data["timeRange"] == "近三天"
    assert data["messageCount"] == 12
    assert data["width"] == 1200 and data["height"] == 800
    assert data["backgroundColor"] == "#ffffff"
    assert data["userName"] == "小明" and data["isPersonal"] is True
    assert json.loads(data["wordListJson"]) == [["甲", 10.0]]


# ---------------------------------------------------------------- 图片生成


async def test_generate_wordcloud_image_renders_with_config(monkeypatch, tmp_path):
    calls: list[dict] = []

    async def fake_html_to_image(html: str, **kwargs):
        calls.append({"html": html, **kwargs})
        return b"wordcloud-bytes"

    monkeypatch.setattr(wordcloud_gen, "html_to_image", fake_html_to_image)

    config = Config()
    messages = [make_message(1001, "项目进展顺利"), make_message(1002, "项目进展很快")]

    image = await generate_wordcloud_image(messages, group_name="测试群", days=3, config=config)

    assert image == b"wordcloud-bytes"
    assert len(calls) == 1
    assert calls[0]["viewport"] == {
        "width": 1200 + WORDCLOUD_VIEWPORT_PADDING_X,
        "height": 800 + WORDCLOUD_VIEWPORT_PADDING_Y,
    }
    assert calls[0]["img_type"] == "png"
    assert "群聊词云" in calls[0]["html"]
    assert "近三天" in calls[0]["html"]
    assert "消息数: 2" in calls[0]["html"]
    # 两个词频相同 → freqRange == 0 → 权重恒为 5（整数，与原作 JSON 一致）
    assert 'const wordList = [["项目", 5], ["进展", 5]];' in calls[0]["html"]


async def test_generate_personal_wordcloud_image_marks_personal(monkeypatch, tmp_path):
    calls: list[dict] = []

    async def fake_html_to_image(html: str, **kwargs):
        calls.append({"html": html, **kwargs})
        return b"personal-bytes"

    monkeypatch.setattr(wordcloud_gen, "html_to_image", fake_html_to_image)

    messages = [make_message(1001, "项目进展顺利"), make_message(1001, "项目进展顺利")]
    image = await generate_personal_wordcloud_image(
        messages, group_name="测试群", user_name="小明", days=1, config=Config()
    )

    assert image == b"personal-bytes"
    assert "小明 的词云" in calls[0]["html"]
    assert "当天" in calls[0]["html"]


async def test_generate_wordcloud_image_uses_tfidf_config(monkeypatch, tmp_path):
    calls: list[dict] = []

    async def fake_html_to_image(html: str, **kwargs):
        calls.append({"html": html, **kwargs})
        return b"tfidf-bytes"

    monkeypatch.setattr(wordcloud_gen, "html_to_image", fake_html_to_image)

    config = Config()
    config.group_insight_wordcloud_extract_method = "tfidf"
    messages = [make_message(1001, "项目进展顺利"), make_message(1002, "天气不错")]

    assert await generate_wordcloud_image(messages, group_name="群", days=1, config=config) == b"tfidf-bytes"
    # 两篇文档各贡献一半词汇 → 权重最高的词归一化为 10
    assert 'const wordList = [["天气", 10.0], ["不错", 10.0]' in calls[0]["html"]


async def test_generate_wordcloud_image_returns_none_for_empty_word_list(monkeypatch):
    async def fake_html_to_image(html: str, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("空词表不应渲染")

    monkeypatch.setattr(wordcloud_gen, "html_to_image", fake_html_to_image)

    messages = [make_message(1001, "hello world"), make_message(1002, "[图片]")]
    assert await generate_wordcloud_image(messages, group_name="群", days=1, config=Config()) is None


async def test_generate_wordcloud_image_returns_none_on_render_error(monkeypatch):
    async def boom(html: str, **kwargs):
        raise RuntimeError("截图失败")

    monkeypatch.setattr(wordcloud_gen, "html_to_image", boom)

    messages = [make_message(1001, "项目进展顺利"), make_message(1002, "项目进展顺利")]
    assert await generate_wordcloud_image(messages, group_name="群", days=1, config=Config()) is None
