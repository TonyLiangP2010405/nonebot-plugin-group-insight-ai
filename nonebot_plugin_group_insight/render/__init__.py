"""渲染层：Jinja2 模板环境、报告长图渲染与文本兜底。"""

from .report import build_template_data, render_report, send_text_summary
from .templates import TEMPLATES_DIR, html_to_image, render_template_html

__all__ = [
    "TEMPLATES_DIR",
    "build_template_data",
    "html_to_image",
    "render_report",
    "render_template_html",
    "send_text_summary",
]
