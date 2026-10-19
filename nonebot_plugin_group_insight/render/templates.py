"""Jinja2 模板环境与 htmlrender 截图封装。

模板位于 ``resources/templates/{default,scrapbook,wordcloud}/``，CSS/JS 由模板用
``{% include %}`` 内联（截图不依赖相对路径解析）。

环境开启 ``autoescape``（对齐 art-template 的 ``{{ }}`` 转义语义，原样注入用 ``| safe``）
与 ``StrictUndefined``（字段缺失直接报错，便于在测试中抓漏）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "resources" / "templates"

# 报告模板 → 截图视口宽度（模板容器宽度 + body 左右 padding）
REPORT_TEMPLATE_VIEWPORTS: dict[str, int] = {
    "default": 840,
    "scrapbook": 1040,
}

# 报告模板高度：与 puppeteer 默认视口一致（整页截图不受影响）
REPORT_VIEWPORT_HEIGHT = 600

_env: jinja2.Environment | None = None


def get_template_env() -> jinja2.Environment:
    """获取（缓存的）Jinja2 模板环境。"""
    global _env
    if _env is None:
        _env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=jinja2.select_autoescape(["html"]),
            undefined=jinja2.StrictUndefined,
        )
    return _env


def render_template_html(template_name: str, data: dict[str, Any]) -> str:
    """渲染 ``{template_name}/index.html``，返回 HTML 字符串。"""
    template = get_template_env().get_template(f"{template_name}/index.html")
    return template.render(**data)


async def html_to_image(
    html: str,
    *,
    viewport: dict[str, int],
    img_type: str = "png",
    quality: int | None = None,
    wait: int = 0,
    full_page: bool = True,
) -> bytes:
    """把 HTML 字符串交给 nonebot-plugin-htmlrender 截图。

    htmlrender 在导入时即读取 NoneBot 配置，因此这里懒导入；未安装或未初始化时抛出的异常
    由调用方捕获（渲染失败走文本兜底）。

    旧版 nonebot-plugin-htmlrender 没有 ``html_to_pic``，回退到 ``get_new_page`` 手动截图。
    """
    if img_type != "jpeg":
        # Playwright 不接受 png 截图携带 quality（原作 puppeteer 直接忽略该参数）
        quality = None

    try:
        from nonebot_plugin_htmlrender import html_to_pic
    except ImportError:
        return await _html_to_image_fallback(
            html,
            viewport=viewport,
            img_type=img_type,
            quality=quality,
            wait=wait,
            full_page=full_page,
        )

    return await html_to_pic(
        html=html,
        template_path=f"file://{TEMPLATES_DIR}",
        base_url=f"file://{TEMPLATES_DIR}",
        viewport=viewport,
        wait=wait,
        type=img_type,
        quality=quality,
        full_page=full_page,
    )


async def _html_to_image_fallback(
    html: str,
    *,
    viewport: dict[str, int],
    img_type: str,
    quality: int | None,
    wait: int,
    full_page: bool,
) -> bytes:
    """旧版 htmlrender（无 ``html_to_pic``）的截图实现。"""
    from nonebot_plugin_htmlrender import get_new_page

    async with get_new_page(viewport=viewport) as page:
        await page.set_content(html, wait_until="networkidle")
        if wait:
            await page.wait_for_timeout(wait)
        return await page.screenshot(full_page=full_page, type=img_type, quality=quality)
