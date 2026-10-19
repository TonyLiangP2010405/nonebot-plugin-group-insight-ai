"""群聊洞察插件配置。

NoneBot 的 ``.env`` 使用扁平键，字段名前缀 ``group_insight_``，例如::

    GROUP_INSIGHT_RETENTION_DAYS=7
    GROUP_INSIGHT_AI_API_KEY=sk-xxx
    GROUP_INSIGHT_SCHEDULE_WHITELIST=[123456789, 987654321]

所有字段都有默认值，缺少任何配置（包括未填写 API Key）时插件仍可正常导入。
"""

from pydantic import BaseModel, Field


class Config(BaseModel, extra="ignore"):
    """插件配置（对应原作 ``config/default_config.yaml`` 的 ``groupManager`` 节）。"""

    # ---- 保留 ----
    group_insight_retention_days: int = 7
    group_insight_at_retention_hours: int = 24
    group_insight_context_message_count: int = 1
    group_insight_next_message_timeout: int = 300

    # ---- AI ----
    group_insight_ai_api_key: str = ""
    group_insight_ai_model: str = "gpt-4.1"
    group_insight_ai_base_url: str = ""
    group_insight_ai_timeout: int = 60000
    group_insight_ai_max_messages: int = 1000
    group_insight_ai_llm_timeout: float = 100
    group_insight_ai_llm_retries: int = 2
    group_insight_ai_llm_backoff: float = 2
    # OpenCode Go 等服务商要求的会话头（x-opencode-session）；留空时若 base_url
    # 指向 opencode.ai 则按 API Key 自动生成稳定 ID
    group_insight_ai_session_id: str = ""

    # ---- 分析 ----
    group_insight_topic_enabled: bool = True
    group_insight_golden_quote_enabled: bool = True
    group_insight_max_golden_quotes: int = 5
    group_insight_min_quote_length: int = 5
    group_insight_max_quote_length: int = 100
    group_insight_user_title_enabled: bool = True
    group_insight_max_user_titles: int = 9
    group_insight_min_messages_for_title: int = 5
    group_insight_activity_enabled: bool = True
    group_insight_min_messages_threshold: int = 20

    # ---- 统计 ----
    group_insight_night_start_hour: int = 0
    group_insight_night_end_hour: int = 6

    # ---- 词云 ----
    group_insight_wordcloud_max_words: int = 100
    group_insight_wordcloud_min_length: int = 2
    group_insight_wordcloud_min_frequency: int = 2
    group_insight_wordcloud_filter_strength: str = "standard"
    group_insight_wordcloud_extract_method: str = "frequency"
    group_insight_wordcloud_width: int = 1200
    group_insight_wordcloud_height: int = 800
    group_insight_wordcloud_background_color: str = "#ffffff"

    # ---- 收集 ----
    group_insight_collection_enabled: bool = True
    group_insight_collect_images: bool = True
    group_insight_collect_faces: bool = True
    group_insight_collect_links: bool = True
    group_insight_collect_videos: bool = True
    group_insight_max_message_length: int = 500
    group_insight_nickname_mode: str = "nickname"

    # ---- 定时 ----
    group_insight_schedule_enabled: bool = False
    group_insight_schedule_whitelist: list[int] = Field(default_factory=list)
    group_insight_schedule_min_messages: int = 99
    group_insight_schedule_concurrency: int = 3
    group_insight_cooldown_minutes: int = 60
    group_insight_send_mode: str = "disabled"
    group_insight_send_hour: int = 8
    group_insight_send_minute: int = 0

    # ---- 报告渲染 ----
    group_insight_summary_template: str = "default"
    group_insight_render_img_type: str = "png"
    group_insight_render_quality: int = 100


def get_config() -> Config:
    """获取插件配置；NoneBot 未初始化（例如单元测试）时回退到默认配置。"""
    try:
        from nonebot import get_plugin_config
    except Exception:  # pragma: no cover - 仅在无 nonebot 环境下触发
        return Config()

    try:
        return get_plugin_config(Config)
    except Exception:  # pragma: no cover - 未初始化 driver 时回退默认值
        return Config()
