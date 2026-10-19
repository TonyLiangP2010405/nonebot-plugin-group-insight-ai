"""词云生成（对应原作 ``utils/textProcessor.js`` + ``services/wordCloudGenerator.js``）。

- :class:`TextProcessor`：jieba 分词 + 词性标注、四层过滤、词频 / TF-IDF 权重。
  词典文件位于插件数据目录（``Storage.data_dir``）下的 ``userdict.txt`` 与 ``stopwords.txt``，
  缺失时静默降级（仅 debug 日志）。
- :func:`generate_wordcloud_image` / :func:`generate_personal_wordcloud_image`：渲染词云图片，
  失败或词表为空时返回 ``None``（调用方给出提示文案）。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any

from nonebot.log import logger

from .config import Config, get_config
from .render.templates import html_to_image, render_template_html
from .storage import get_storage

STOPWORDS_FILENAME = "stopwords.txt"
USERDICT_FILENAME = "userdict.txt"

CHINESE_PATTERN = re.compile(r"[\u4e00-\u9fa5]")
AT_PATTERN = re.compile(r"@[^\s]+")
BRACKET_PATTERN = re.compile(r"\[.*?\]")
COMMAND_PREFIX_PATTERN = re.compile(r"^[#/]")
WHITESPACE_PATTERN = re.compile(r"\s+")

# 词性过滤档位（jieba 词性标注：u 助词 / y 语气词 / d 副词 / c 连词 / r 代词 /
# q 量词 / t 时间词 / f 方位词 / m 数词）
POS_FILTER_RULES: dict[str, frozenset[str]] = {
    "loose": frozenset({"u", "y"}),
    "standard": frozenset({"u", "y", "d", "c", "r"}),
    "strict": frozenset({"u", "y", "d", "c", "r", "q", "t", "f", "m"}),
}

# 词云画布尺寸之外的留白：body 20px * 2 + .wordcloud-container 40px * 2
WORDCLOUD_VIEWPORT_PADDING_X = 120
WORDCLOUD_VIEWPORT_PADDING_Y = 200
# 截图前额外等待（canvas 同步绘制，留一点时间给字体加载）
WORDCLOUD_SCREENSHOT_WAIT = 300

_jieba_available: bool | None = None


def is_wordcloud_available() -> bool:
    """词云功能是否就绪（jieba 可用）。

    命令层在不可用时回复「词云功能未就绪」（对应原作 ``getWordCloudGenerator()`` 为空）。
    结果缓存，避免每次命令都重新探测。
    """
    global _jieba_available
    if _jieba_available is None:
        try:
            import jieba  # noqa: F401
            import jieba.posseg  # noqa: F401
        except Exception as e:
            logger.warning(f"词云功能未就绪（jieba 不可用）: {e}")
            _jieba_available = False
        else:
            _jieba_available = True
    return _jieba_available


class TextProcessor:
    """分词与词频 / TF-IDF 统计。"""

    def __init__(self, config: Config | None = None, data_dir: str | Path | None = None) -> None:
        self.config = config or get_config()
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self.initialized = False
        self.stopwords: set[str] = set()
        self._jieba: Any = None
        self._posseg: Any = None

    @property
    def data_dir(self) -> Path:
        """词典文件所在目录（默认插件数据目录）。"""
        if self._data_dir is None:
            self._data_dir = get_storage().data_dir
        return self._data_dir

    # ------------------------------------------------------------------ 初始化

    def init(self) -> None:
        """加载 jieba 与词典（幂等）；jieba 不可用时降级为简单分词。"""
        if self.initialized:
            return

        try:
            import jieba
            import jieba.posseg as posseg

            self._jieba = jieba
            self._posseg = posseg
            self.initialized = True
            logger.debug("文本处理器初始化成功 (jieba + 词性标注)")
        except Exception as e:
            logger.opt(exception=e).error(f"文本处理器初始化失败: {e}")
            self.initialized = False
            return

        # 词典缺失 / 数据目录不可用时静默降级，不影响分词
        try:
            self.load_user_dict()
            self.load_stopwords()
        except Exception as e:
            logger.opt(exception=e).warning(f"加载词云词典失败: {e}")

    def load_user_dict(self) -> int:
        """加载自定义词典（过滤空行与 ``#`` 注释行），返回加载的词数。"""
        try:
            path = self.data_dir / USERDICT_FILENAME
            if not path.exists():
                logger.debug(f"自定义词典文件不存在: {path}")
                return 0

            lines = [
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            if not lines:
                return 0

            self._jieba.load_userdict(StringIO("\n".join(lines)))
            logger.info(f"已加载自定义词典 ({len(lines)} 词)")
            return len(lines)
        except Exception as e:
            logger.opt(exception=e).warning(f"加载自定义词典失败: {e}")
            return 0

    def load_stopwords(self) -> int:
        """加载过滤词（每行一词，``#`` 注释），返回加载的词数。"""
        try:
            path = self.data_dir / STOPWORDS_FILENAME
            if not path.exists():
                logger.debug(f"过滤词文件不存在: {path}")
                return 0

            words = {
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            }
            self.stopwords = words
            logger.info(f"已加载过滤词 ({len(words)} 词)")
            return len(words)
        except Exception as e:
            logger.opt(exception=e).warning(f"加载过滤词失败: {e}")
            return 0

    # -------------------------------------------------------------------- 分词

    def get_filter_pos_list(self) -> frozenset[str]:
        """按 ``wordcloud_filter_strength`` 取要过滤的词性集合（未知档位按 standard）。"""
        strength = self.config.group_insight_wordcloud_filter_strength or "standard"
        return POS_FILTER_RULES.get(strength, POS_FILTER_RULES["standard"])

    def clean_text(self, text: str | None) -> str:
        """清理消息文本：去 @、去 ``[...]``、去行首命令前缀、合并空白。"""
        if not text:
            return ""

        cleaned = AT_PATTERN.sub("", text)
        cleaned = BRACKET_PATTERN.sub("", cleaned)
        cleaned = COMMAND_PREFIX_PATTERN.sub("", cleaned)
        cleaned = WHITESPACE_PATTERN.sub(" ", cleaned)
        return cleaned.strip()

    def cut(self, text: str, min_length: int = 2) -> list[str]:
        """分词 + 四层过滤（长度 / 停用词 / 必须含中文 / 词性档位）。"""
        if not self.initialized or self._posseg is None:
            logger.warning("分词器未初始化，使用简单分词")
            return self.simple_cut(text, min_length)

        try:
            filter_pos = self.get_filter_pos_list()
            words: list[str] = []
            for pair in self._posseg.cut(text, HMM=True):
                word, tag = pair.word, pair.flag
                if len(word) < min_length:
                    continue
                if word in self.stopwords:
                    continue
                if not CHINESE_PATTERN.search(word):
                    continue
                if tag in filter_pos:
                    continue
                words.append(word)
            return words
        except Exception as e:
            logger.opt(exception=e).error(f"分词失败: {e}")
            return self.simple_cut(text, min_length)

    def simple_cut(self, text: str, min_length: int = 2) -> list[str]:
        """降级分词：按非中文字符切段，仅保留长度达标的纯中文片段。"""
        words: list[str] = []
        current_word = ""

        for char in text:
            if CHINESE_PATTERN.match(char):
                current_word += char
            else:
                if len(current_word) >= min_length and current_word not in self.stopwords:
                    words.append(current_word)
                current_word = ""

        if len(current_word) >= min_length and current_word not in self.stopwords:
            words.append(current_word)

        return words

    # ------------------------------------------------------------------ 词频

    def count_words(self, words: list[str], min_frequency: int = 2) -> list[dict[str, Any]]:
        """统计词频，过滤低频词，按词频降序返回 ``[{word, count}]``。"""
        word_count: dict[str, int] = {}
        for word in words:
            word_count[word] = word_count.get(word, 0) + 1

        result = [{"word": word, "count": count} for word, count in word_count.items() if count >= min_frequency]
        result.sort(key=lambda item: item["count"], reverse=True)
        return result

    def process_messages(
        self,
        messages: list[dict[str, Any]],
        options: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """处理消息列表：返回词频榜或 TF-IDF 关键词榜。"""
        options = options or {}
        min_length = options.get("minLength", 2)
        min_frequency = options.get("minFrequency", 2)
        max_words = options.get("maxWords", 100)
        extract_method = (
            options.get("extractMethod") or self.config.group_insight_wordcloud_extract_method or "frequency"
        )

        self.init()

        if extract_method == "tfidf":
            return self.process_messages_tfidf(messages, {"minLength": min_length, "maxWords": max_words})

        all_words: list[str] = []
        for msg in messages:
            cleaned_text = self.clean_text(msg.get("message") or msg.get("msg") or "")
            if not cleaned_text:
                continue
            all_words.extend(self.cut(cleaned_text, min_length))

        return self.count_words(all_words, min_frequency)[:max_words]

    def process_messages_tfidf(
        self,
        messages: list[dict[str, Any]],
        options: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """以用户为文档计算 TF-IDF，返回 ``[{word, weight}]``（权重归一化到 0-1）。"""
        options = options or {}
        min_length = options.get("minLength", 2)
        max_words = options.get("maxWords", 100)

        self.init()

        user_docs: dict[Any, list[str]] = {}
        for msg in messages:
            sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
            user_id = msg.get("user_id") or sender.get("user_id") or "unknown"
            cleaned_text = self.clean_text(msg.get("message") or msg.get("msg") or "")
            if not cleaned_text:
                continue
            words = self.cut(cleaned_text, min_length)
            if not words:
                continue
            user_docs.setdefault(user_id, []).extend(words)

        docs_words = [words for words in user_docs.values() if words]
        if not docs_words:
            return []

        keywords = self.calculate_tfidf(docs_words, max_words)
        logger.debug(f"TF-IDF 提取完成，用户数: {len(docs_words)}，关键词数: {len(keywords)}")
        return keywords

    def calculate_tfidf(self, docs_words: list[list[str]], top_k: int = 100) -> list[dict[str, Any]]:
        """TF-IDF：``idf = log((N+1)/(df+1)) + 1``，按文档长度归一化 TF 后累加。"""
        if not docs_words:
            return []

        doc_count = len(docs_words)

        word_doc_freq: dict[str, int] = {}
        for words in docs_words:
            for word in set(words):
                word_doc_freq[word] = word_doc_freq.get(word, 0) + 1

        word_tfidf: dict[str, float] = {}
        for words in docs_words:
            if not words:
                continue

            tf: dict[str, int] = {}
            for word in words:
                tf[word] = tf.get(word, 0) + 1

            doc_length = len(words)
            for word, freq in tf.items():
                df = word_doc_freq[word]
                idf = math.log((doc_count + 1) / (df + 1)) + 1
                word_tfidf[word] = word_tfidf.get(word, 0) + (freq / doc_length) * idf

        sorted_words = sorted(word_tfidf.items(), key=lambda item: item[1], reverse=True)[:top_k]
        max_weight = sorted_words[0][1] if sorted_words else 1

        return [{"word": word, "weight": weight / max_weight} for word, weight in sorted_words]


def get_time_range_text(days: int) -> str:
    """天数 → 模板展示文案。"""
    if days == 1:
        return "当天"
    if days == 3:
        return "近三天"
    if days == 7:
        return "近七天"
    return f"近{days}天"


def build_word_list(word_data: list[dict[str, Any]], extract_method: str) -> list[list[Any]]:
    """把词频 / TF-IDF 结果转换为 wordcloud2.js 的 ``[word, weight]`` 列表（权重 1-10）。"""
    if not word_data:
        return []

    if extract_method == "tfidf":
        # TF-IDF 权重 0-1 → 1-10
        return [[item["word"], 1 + item["weight"] * 9] for item in word_data]

    frequencies = [item["count"] for item in word_data]
    max_freq = max(frequencies)
    min_freq = min(frequencies)
    freq_range = max_freq - min_freq

    word_list: list[list[Any]] = []
    for item in word_data:
        if freq_range == 0:
            normalized_weight = 5
        else:
            log_freq = math.log(item["count"])
            log_min = math.log(min_freq)
            log_max = math.log(max_freq)
            normalized_weight = 1 + ((log_freq - log_min) / (log_max - log_min)) * 9
        word_list.append([item["word"], normalized_weight])

    return word_list


def build_wordcloud_template_data(
    *,
    group_name: str,
    days: int,
    message_count: int,
    word_list: list[list[Any]],
    user_name: str | None = None,
    config: Config | None = None,
    create_time: datetime | None = None,
) -> dict[str, Any]:
    """组装词云模板数据（字段见 PORT_SPEC §6）。"""
    config = config or get_config()

    return {
        "groupName": group_name,
        "timeRange": get_time_range_text(days),
        "messageCount": message_count,
        "createTime": (create_time or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
        "wordListJson": json.dumps(word_list, ensure_ascii=False),
        "width": config.group_insight_wordcloud_width,
        "height": config.group_insight_wordcloud_height,
        "backgroundColor": config.group_insight_wordcloud_background_color,
        "userName": user_name or "",
        "isPersonal": bool(user_name),
    }


async def generate_wordcloud_image(
    messages: list[dict[str, Any]],
    *,
    group_name: str,
    days: int,
    config: Config | None = None,
) -> bytes | None:
    """生成群聊词云图片；词表为空或渲染失败返回 ``None``。"""
    return await _generate(
        messages,
        group_name=group_name,
        days=days,
        user_name=None,
        config=config,
    )


async def generate_personal_wordcloud_image(
    messages: list[dict[str, Any]],
    *,
    group_name: str,
    user_name: str,
    days: int,
    config: Config | None = None,
) -> bytes | None:
    """生成个人词云图片（标题展示 ``{userName} 的词云``）；失败返回 ``None``。"""
    return await _generate(
        messages,
        group_name=group_name,
        days=days,
        user_name=user_name,
        config=config,
    )


async def _generate(
    messages: list[dict[str, Any]],
    *,
    group_name: str,
    days: int,
    user_name: str | None,
    config: Config | None,
) -> bytes | None:
    try:
        config = config or get_config()
        extract_method = config.group_insight_wordcloud_extract_method or "frequency"

        logger.info(f"开始生成词云，消息数: {len(messages)}，提取方式: {extract_method}")

        # 数据目录在事件循环线程内解析（Storage 懒加载含 asyncio.Lock，py3.9 下不能在线程池中构造）
        processor = TextProcessor(config, data_dir=get_storage().data_dir)
        options = {
            "minLength": config.group_insight_wordcloud_min_length,
            "minFrequency": config.group_insight_wordcloud_min_frequency,
            "maxWords": config.group_insight_wordcloud_max_words,
            "extractMethod": extract_method,
        }
        # jieba 是同步库，分词放到线程池，避免阻塞事件循环
        word_data = await asyncio.to_thread(processor.process_messages, messages, options)

        if not word_data:
            logger.warning("没有足够的词汇生成词云")
            return None

        logger.info(f"统计到 {len(word_data)} 个词汇")
        if extract_method != "tfidf":
            frequencies = [item["count"] for item in word_data]
            logger.info(f"频率范围: {min(frequencies)} - {max(frequencies)}")

        word_list = build_word_list(word_data, extract_method)
        template_data = build_wordcloud_template_data(
            group_name=group_name,
            days=days,
            message_count=len(messages),
            word_list=word_list,
            user_name=user_name,
            config=config,
        )

        html = render_template_html("wordcloud", template_data)

        width = config.group_insight_wordcloud_width
        height = config.group_insight_wordcloud_height
        image = await html_to_image(
            html,
            viewport={
                "width": width + WORDCLOUD_VIEWPORT_PADDING_X,
                "height": height + WORDCLOUD_VIEWPORT_PADDING_Y,
            },
            img_type=config.group_insight_render_img_type or "png",
            quality=config.group_insight_render_quality,
            wait=WORDCLOUD_SCREENSHOT_WAIT,
        )

        logger.info("词云生成成功")
        return image
    except Exception as e:
        logger.opt(exception=e).error(f"词云生成失败: {e}")
        return None
