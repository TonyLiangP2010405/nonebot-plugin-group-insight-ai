"""Jinja2 模板环境与 htmlrender 截图封装。

模板位于 ``resources/templates/{default,scrapbook,wordcloud}/``，CSS/JS 由模板用
``{% include %}`` 内联（截图不依赖相对路径解析）。

环境开启 ``autoescape``（对齐 art-template 的 ``{{ }}`` 转义语义，原样注入用 ``| safe``）
与 ``StrictUndefined``（字段缺失直接报错，便于在测试中抓漏）。

:func:`html_to_image` 按接口可用性适配三代 nonebot-plugin-htmlrender：
``html_to_pic``（0.5–0.7）/ ``render_html``（0.8.x）/ ``get_new_page``（更旧）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import jinja2
from nonebot.log import logger

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

    按**接口可用性**（而非版本号）分层适配三代 API：

    1. ``html_to_pic``（0.5–0.7）：直接调用，参数一一对应；
    2. ``render_html``（0.8.x）：整页/固定视口由 ``height`` 表达，见 :func:`_render_html_08x`；
    3. ``get_new_page``（更旧）：手动开页面截图，见 :func:`_html_to_image_fallback`。
    """
    if img_type != "jpeg":
        # Playwright 不接受 png 截图携带 quality（原作 puppeteer 直接忽略该参数）
        quality = None

    # 必须在 import htmlrender 之前：NoneBot 拒绝"先 import 再 load"，而 0.8.x 的
    # Playwright 启动依赖 htmlrender 的插件身份（localstore 探测调用方插件）
    _ensure_htmlrender_plugin_loaded()

    try:
        from nonebot_plugin_htmlrender import html_to_pic
    except ImportError:
        html_to_pic = None

    if html_to_pic is not None:
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

    try:
        from nonebot_plugin_htmlrender import render_html
    except ImportError:
        render_html = None

    if render_html is not None:
        return await _render_html_08x(
            render_html,
            html,
            viewport=viewport,
            img_type=img_type,
            quality=quality,
            wait=wait,
            full_page=full_page,
        )

    return await _html_to_image_fallback(
        html,
        viewport=viewport,
        img_type=img_type,
        quality=quality,
        wait=wait,
        full_page=full_page,
    )


async def _render_html_08x(
    render_html: Any,
    html: str,
    *,
    viewport: dict[str, int],
    img_type: str,
    quality: int | None,
    wait: int,
    full_page: bool,
) -> bytes:
    """nonebot-plugin-htmlrender 0.8.x（``render_html`` → ``RenderedImage``）。

    参数映射（0.8.x 只在 ``height=None`` 时整页截图，见 playwright provider 的
    ``full_page=options.height is None``）：

    - ``width`` ← ``viewport['width']``；
    - ``height=None`` ← ``full_page=True``（内部视口高度 10 + 整页截图，与旧版
      ``html_to_pic`` 默认一致）；``full_page=False`` 时才传 ``viewport['height']``
      固定视口——传高度会同时关掉整页截图，故不能两者兼得；
    - ``image_format`` ← ``img_type``；``quality`` 仅 jpeg 合法（``RasterOptions`` 校验
      拒绝 png + quality），非 jpeg 时上游已置空；
    - ``wait`` 无对应参数：0.8.x 的 ``wait_before_screenshot`` 被 provider 硬编码为 0，
      ``ContentConfig.additional_wait`` 也无法从公开 API 注入。本插件模板都是同步渲染
      （词云 canvas 由 wordcloud2.js 同步绘制、报告为静态 HTML/CSS），截图前等待不影响
      结果，仅记 debug 日志；
    - 不传 ``base_url``：模板 CSS/JS 已内联、无相对引用，外网头像与字体是绝对 URL。
    """
    if wait:
        logger.debug(f"htmlrender 0.8.x 无截图等待参数，忽略 wait={wait}ms")

    try:
        image = await render_html(
            html,
            width=viewport["width"],
            height=None if full_page else viewport["height"],
            image_format=img_type,
            quality=quality,
        )
    except Exception as error:
        _log_08x_setup_hint(error)
        raise

    # RenderedImage 实现 __bytes__（返回 .data）
    return bytes(image)


# 0.8.x 的两个必配项（0.7 及以前不需要），命中时给出可操作的提示
_08X_SETUP_HINTS: tuple[tuple[str, str], ...] = (
    (
        "Cannot detect caller plugin",
        "htmlrender 0.8.x 需作为 NoneBot 插件加载（nb plugin install nonebot-plugin-htmlrender，"
        "或加入 [tool.nonebot].plugins），否则 Playwright 启动时 localstore 无法识别调用方插件",
    ),
    (
        "not available in this composition",
        "htmlrender 0.8.x 需在 .env 配置渲染 provider（RENDER__PROVIDER=playwright），"
        "并安装 playwright 与浏览器（python -m playwright install chromium）",
    ),
)


def _ensure_htmlrender_plugin_loaded() -> None:
    """尽力把 htmlrender 注册为 NoneBot 插件（0.8.x 的浏览器启动依赖插件身份）。

    0.8.x 的 Playwright provider 启动时用 ``nonebot_plugin_localstore`` 探测调用方插件；
    htmlrender 仅被 import、未作为插件加载时探测失败（``Cannot detect caller plugin``）。
    这里在 import htmlrender **之前**补一次 ``require``（NoneBot 拒绝"先 import 再 load"，
    因此顺序不能反）；任何异常只记 debug（例如用户配置仍是 0.7 的旧键），真正的失败仍由
    渲染异常上报。
    """
    try:
        from nonebot import require
        from nonebot.plugin import get_plugin

        if get_plugin("nonebot_plugin_htmlrender") is not None:
            return
        if "nonebot_plugin_htmlrender" in sys.modules:
            # 已被别人 import 的模块无法补注册为插件，只能跳过（由调用方提示配置）
            logger.debug("nonebot_plugin_htmlrender 已被导入，无法补注册为插件")
            return

        require("nonebot_plugin_htmlrender")
        logger.debug("已按 NoneBot 插件方式加载 nonebot_plugin_htmlrender")
    except Exception as e:
        logger.debug(f"无法自动加载 nonebot_plugin_htmlrender 插件: {e}")


def _log_08x_setup_hint(error: BaseException) -> None:
    """把 0.8.x 的常见配置错误翻译成可操作提示（不改变异常传播）。"""
    message = str(error)
    for marker, hint in _08X_SETUP_HINTS:
        if marker in message:
            logger.warning(f"htmlrender 0.8.x 配置提示: {hint}")
            return


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
