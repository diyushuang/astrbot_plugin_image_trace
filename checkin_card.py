"""签到卡片：语句库（内置 + 可选在线接口）与拍立得风图片合成。

为什么用 Pillow 而不是 AstrBot 的 HTML 文转图：本插件已依赖 Pillow，PIL 渲染
一张 720px 卡片实测 9~25ms，且不依赖 Playwright / t2i 服务是否可用；签到是
高频小功能，不值得为版式引入一条更重、更不确定的渲染链路。

语句接口刻意做成「URL + JSON 字段路径 + 可选请求头」三件套，而不是写死
一言：同类服务（一言、今日诗词、纯文本接口、带 token 的聚合 API）返回结构
各不相同，写死任何一家都会让用户换服务时被迫改代码。任何在线失败都回退本地
语句库——签到不能因为第三方接口抖动而中断。
"""

from __future__ import annotations

import io
import json
import os
import random
import re
from functools import lru_cache
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("checkin_card")

try:
    from .url_guard import read_limited_text
except ImportError:  # 兼容插件以独立模块方式加载
    try:
        from url_guard import read_limited_text  # type: ignore[no-redef]
    except ImportError:  # 本地自测环境没有 url_guard 时退化为普通读取
        async def read_limited_text(resp, limit: int) -> str:  # type: ignore[no-redef]
            data = await resp.content.read(limit + 1)
            return bytes(data[:limit]).decode("utf-8", errors="replace")

# 内置语句库：首次运行会写入 data 目录的 quotes.txt，用户可直接增删。
# 全部为原创短句，不引用受版权保护的歌词/诗句。
DEFAULT_QUOTES: tuple[str, ...] = (
    "今天的每一步，都算数。",
    "把喜欢的事做久一点，光就会慢慢亮起来。",
    "慢一点也没关系，方向对了就好。",
    "你不必追赶谁，按自己的节奏走就好。",
    "生活不会一直顺，但你可以一直往前走。",
    "心里有光，脚下的路就不会太暗。",
    "先照顾好自己，再去拥抱世界。",
    "允许自己偶尔停顿，风景也在路上。",
    "把平凡的日子，过成自己喜欢的样子。",
    "今天也要好好吃饭，好好生活。",
    "认真生活的人，运气都不会太差。",
    "风吹过的地方，都会留下痕迹。",
    "别急，好的事情值得等待。",
    "你比想象中更坚韧。",
    "把烦恼交给时间，把期待留给明天。",
    "世界很大，先去看看再说。",
    "每一次坚持，都是在给未来铺路。",
    "温柔待人，也温柔待己。",
    "把今天过好，就是最好的准备。",
    "心有热爱，日子就不会荒芜。",
    "走慢一点，才看得见沿途的花。",
    "愿你眼里有光，心里有海。",
    "不慌不忙，自有答案。",
    "把期待放低一点，快乐就会多一点。",
    "生活是旷野，不是轨道。",
    "与其焦虑，不如动手做点什么。",
    "你走过的路，都会变成礼物。",
    "今日份的好运，已经在路上了。",
    "允许一切发生，也允许自己慢慢来。",
    "把心里的褶皱，交给阳光熨平。",
    "只要还在走，就永远不会晚。",
    "认真吃饭，按时睡觉，其余交给时间。",
    "你不需要完美，只需要真实。",
    "每一次抬头，都能看见新的天空。",
    "生活偶尔苦，但你可以很甜。",
    "把日子过成诗，不必等风来。",
    "你的努力，时间都记得。",
    "先成为自己的光，再去照亮别人。",
    "慢慢走，别回头。",
    "愿今天顺利，愿你被温柔以待。",
    "心里装着春天，四季都不会太冷。",
    "把小事做好，日子就会发光。",
    "世界会奖励认真的人。",
    "别让明天的忧虑，偷走今天的快乐。",
    "你已经在路上了，这就很好。",
    "今天的天气，适合重新出发。",
    "有期待的日子，总不会太差。",
    "生活给的糖，要记得接住。",
    "把疲惫放下，把好梦带走。",
    "愿你今天的努力，明天都有回音。",
    "山高路远，但你可以一步一步走。",
    "不必最厉害，只要一直在变好。",
    "和自己和解，是今天的头等大事。",
    "风会记得每一朵花的香。",
    "愿你被生活温柔相待，也温柔待人。",
    "把心放宽，路就宽了。",
    "今天的你，比昨天更值得被喜欢。",
    "日子是自己的，喜欢最重要。",
    "愿所有努力，都不被辜负。",
    "明天见，今天的你辛苦了。",
)

# 中文字体候选：只列确定含 CJK 字形的常见字体。不把 DejaVu 这类无中文字形
# 的字体放进来——它能让 truetype 成功，却在卡片上渲染成一排方框。
FONT_CANDIDATES: tuple[str, ...] = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
)

# 参与合成的源图最长边：签到发的是缩略图，正常 720px；万一替身查不到退回
# 原图（可能 4K），也要把画布控制在可控体积内。
POSTER_MAX_SIDE = 1440
# 防解压炸弹：与 features.py 的 _MAX_LOAD_PIXELS 同一量级
_MAX_LOAD_PIXELS = 50_000_000

QUOTE_MAX_CHARS = 120
MAX_RESPONSE_BYTES = 64 * 1024

# 在线语句的常见字段。显式路径取不到时按此顺序自动探测；顶层是数组时对首个
# 元素再探一遍。兼容一言（hitokoto）、今日诗词（data.content）、聚合 API
# （data.hitokoto）与常见自定义字段。
QUOTE_FIELD_CANDIDATES: tuple[str, ...] = (
    "hitokoto",
    "sentence",
    "content",
    "text",
    "word",
    "msg",
    "data.hitokoto",
    "data.sentence",
    "data.content",
    "data.text",
    "data.word",
    "data",
    "result",
)

_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAG_RE = re.compile(r"<[^>]{0,200}>")
_MD_MARKS_RE = re.compile(r"[*_`~]+")
_EDGE_STRIP = " \t\r\n-—·…"

try:  # Pillow >= 9.1
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # 旧版本兜底
    _RESAMPLE = Image.LANCZOS

_BG_COLOR = (248, 247, 245)
_FRAME_COLOR = (210, 206, 200)
_QUOTE_COLOR = (60, 58, 55)
_FOOTER_COLOR = (150, 146, 140)


def _sanitize(value) -> str:
    """清洗一条语句：去链接语法/控制字符/HTML 标签/Markdown 标记并限长。"""
    text = str(value or "")
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _CONTROL_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    text = _MD_MARKS_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(_EDGE_STRIP)
    if len(text) > QUOTE_MAX_CHARS:
        text = text[:QUOTE_MAX_CHARS].rstrip("，。；、,.;:：") + "…"
    return text


def _lookup_path(payload, path: str):
    """按点号路径取值；路径中允许数字下标（如 data.0.content）。"""
    current = payload
    for key in str(path or "").split("."):
        if not key:
            continue
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list) and key.isdigit():
            index = int(key)
            current = current[index] if 0 <= index < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


def _coerce_text(value) -> str:
    """把 JSON 值转成候选文本；数组（如今日诗词诗句）用逗号连接。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() for item in value if isinstance(item, (str, int, float))]
        return "，".join(part for part in parts if part)
    return ""


def _first_line(raw: str) -> str:
    for line in str(raw or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def extract_quote(payload_text: str, json_path: str = "") -> str | None:
    """从接口响应中提取一句可用语句；无法识别时返回 None。

    顺序：指定路径 → 常见字段自动探测 → 数组首元素 → 纯文本首行。
    每一步都过 _sanitize，保证返回的文本能直接画进卡片。
    """
    raw = str(payload_text or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _sanitize(_first_line(raw)) or None

    candidate = _coerce_text(_lookup_path(payload, json_path)) if json_path else ""
    if not candidate:
        for path in QUOTE_FIELD_CANDIDATES:
            candidate = _coerce_text(_lookup_path(payload, path))
            if candidate:
                break
    if not candidate and isinstance(payload, list):
        for item in payload[:5]:
            if isinstance(item, dict):
                for path in QUOTE_FIELD_CANDIDATES:
                    candidate = _coerce_text(_lookup_path(item, path))
                    if candidate:
                        break
            elif isinstance(item, str):
                candidate = item.strip()
            if candidate:
                break
    if not candidate and isinstance(payload, str):
        candidate = payload

    text = _sanitize(candidate)
    return text or None


def _split_quotes(raw: str) -> tuple[str, ...]:
    """把多行文本拆成语句：跳过空行与 `#` 开头的注释行。"""
    return tuple(
        line.strip()
        for line in str(raw or "").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _load_quotes(file_path: str) -> tuple[str, ...]:
    """读取本地语句库；缺失时用内置语句初始化，任何失败都回退内置元组。

    文件存在但为空（用户删光了）时不写回默认——尊重「就是不想用内置句」的
    可能意图，由调用方继续使用内置兜底，避免每轮启动反复改写用户文件。
    """
    path = str(file_path or "").strip()
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as handle:
                lines = _split_quotes(handle.read())
            if lines:
                return lines
        except OSError as exc:
            logger.warning(f"读取签到语句库失败，使用内置语句: {exc}")
        return DEFAULT_QUOTES

    if path:
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(DEFAULT_QUOTES) + "\n")
        except OSError as exc:
            logger.warning(f"初始化签到语句库失败，使用内置语句: {exc}")
    return DEFAULT_QUOTES


class QuoteLibrary:
    """语句来源门面：本地语句库 +（可选）任意在线接口。"""

    def __init__(
        self,
        quotes_file: str,
        *,
        local_text: str = "",
        source: str = "builtin",
        api_url: str = "",
        json_path: str = "",
        headers: dict | None = None,
        timeout: float = 5.0,
        http_getter=None,
    ) -> None:
        # 配置面板里的多行文本框优先于 quotes.txt：粘贴即导入，留空才读文件
        self._quotes = _split_quotes(local_text) or _load_quotes(quotes_file)
        self._source = str(source or "").strip().lower()
        self._api_url = str(api_url or "").strip()
        self._json_path = str(json_path or "").strip()
        self._headers = {
            str(key): str(value) for key, value in (headers or {}).items() if str(key).strip()
        }
        self._timeout = max(1.0, float(timeout or 5.0))
        self._http_getter = http_getter
        self._last = ""
        parsed = urlsplit(self._api_url) if self._api_url else None
        self._api_ready = bool(
            parsed and parsed.scheme in {"http", "https"} and parsed.netloc
        )
        if self._api_url and not self._api_ready:
            logger.warning(f"签到语句接口地址不是有效的 http(s) URL，已回退本地语句库: {self._api_url}")

    def _pick_local(self) -> str:
        pool = self._quotes or DEFAULT_QUOTES
        index = random.randrange(len(pool))
        if pool[index] == self._last and len(pool) > 1:
            # 只避开「上一句」，不引入更复杂的去重窗口：诗句池本身足够大，
            # 连抽两次同一句才是用户能明显感知的重复。
            index = (index + 1) % len(pool)
        return pool[index]

    def _prepare(self, _url: str, _method: str, cross_origin: bool) -> dict:
        """自定义请求头只在同源请求里发送；跨域重定向会把它交给第三方。"""
        if cross_origin:
            return {"headers": {}}
        return {"headers": dict(self._headers)}

    async def _fetch_remote(self) -> str | None:
        if not self._api_ready or self._http_getter is None:
            return None
        try:
            client = await self._http_getter()
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with await client.request(
                "GET", self._api_url, prepare=self._prepare, timeout=timeout
            ) as resp:
                if not 200 <= resp.status < 300:
                    logger.debug(f"签到语句接口返回 HTTP {resp.status}，回退本地语句库")
                    return None
                body = await read_limited_text(resp, MAX_RESPONSE_BYTES)
        except Exception as exc:
            logger.debug(f"签到语句接口请求失败，回退本地语句库: {exc}")
            return None
        return extract_quote(body, self._json_path)

    async def pick(self) -> str:
        """取一条语句。

        builtin 只用本地；api 在线优先、失败回退本地；mixed 每次随机决定
        本地或在线（在线失败同样回退本地）。
        """
        use_remote = self._source == "api" or (
            self._source == "mixed" and random.random() < 0.5
        )
        if use_remote and self._api_ready:
            remote = await self._fetch_remote()
            if remote:
                # 在线接口连续返回同一句时，用本地池换一句
                text = self._pick_local() if remote == self._last else remote
                self._last = text
                return text
        text = self._pick_local()
        self._last = text
        return text


@lru_cache(maxsize=8)
def resolve_font_path(explicit: str = "") -> str | None:
    """返回可用的中文字体路径；找不到返回 None（调用方降级为轻量模式）。

    不返回 ImageFont.load_default()：它不含 CJK 字形，画出来的中文全是方框，
    比明确降级更糟。验证方式是真的用该文件构造一次 FreeTypeFont，避免把
    「路径存在但文件损坏/非字体」的候选当成可用字体。
    """
    candidates: list[str] = []
    target = str(explicit or "").strip()
    if target:
        candidates.append(target)
    candidates.extend(FONT_CANDIDATES)
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            ImageFont.truetype(path, 24)
            return path
        except Exception as exc:
            logger.debug(f"字体不可用 {path}: {exc}")
    logger.warning(
        "未找到可用的中文字体，签到将使用「缩略图 + 文字」模式；"
        "可在插件配置 checkin.font_path 指定 .ttf/.ttc/.otf 字体文件"
    )
    return None


def wrap_text(text: str, font, max_width: int, max_lines: int = 3) -> list[str]:
    """按像素宽度逐字折行；超过 max_lines 时末行加省略号并停止。"""
    body = str(text or "").strip()
    if not body:
        return []
    width = max(1, int(max_width))
    lines: list[str] = []
    current = ""
    for char in body:
        if font.getlength(current + char) <= width:
            current += char
            continue
        if len(lines) + 1 < max_lines:
            lines.append(current)
            current = char
        else:
            lines.append((current.rstrip() or body[:1]) + "…")
            return lines
    if current:
        lines.append(current)
    return lines


def compose_poster(
    image_bytes: bytes,
    *,
    quote: str,
    footer: str,
    font_path: str = "",
    border_px: int = 0,
) -> bytes | None:
    """把缩略图合成「白边 + 下方留白写句」的卡片 JPEG；失败返回 None。

    画布宽度跟随图片，边框与字号按宽度比例自适应，因此 720px 缩略图与
    1440px 原图都能得到相近的视觉比例。留白区高度由实际折行数决定，
    1 行与 3 行语句各自居中，不会出现固定高度导致的空白过多。
    """
    resolved_font = resolve_font_path(font_path)
    if resolved_font is None:
        return None
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            if source.width * source.height > _MAX_LOAD_PIXELS:
                logger.warning(f"签到卡片源图像素过大，跳过合成: {source.size}")
                return None
            source.seek(0)
            image = ImageOps.exif_transpose(source)
            image.load()
            if max(image.size) > POSTER_MAX_SIDE:
                scale = POSTER_MAX_SIDE / max(image.size)
                image = image.resize(
                    (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                    _RESAMPLE,
                )
            if image.mode in ("RGBA", "LA", "P"):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            else:
                image = image.copy()
    except Exception as exc:
        logger.warning(f"签到卡片解码源图失败，改用轻量模式: {exc}")
        return None

    try:
        width, height = image.size
        border = int(border_px) if 0 < int(border_px) <= 200 else max(12, round(width * 0.045))
        font_size = min(48, max(18, round(width * 0.036)))
        footer_size = max(14, round(font_size * 0.72))
        font = ImageFont.truetype(resolved_font, font_size)
        footer_font = ImageFont.truetype(resolved_font, footer_size)

        text_width = max(120, width - border * 2)
        lines = wrap_text(quote, font, text_width, max_lines=3)
        if not lines:
            lines = ["今天的每一步，都算数。"]
        line_height = font_size + round(font_size * 0.45)
        gap_quote_footer = max(8, round(font_size * 0.5))
        canvas_height = height + border * 3 + line_height * len(lines) + gap_quote_footer + footer_size
        canvas = Image.new("RGB", (width + border * 2, canvas_height), _BG_COLOR)
        canvas.paste(image, (border, border))
        drawer = ImageDraw.Draw(canvas)
        drawer.rectangle(
            [border - 1, border - 1, border + width, border + height],
            outline=_FRAME_COLOR,
        )

        top = height + border * 2
        for line in lines:
            line_width = drawer.textlength(line, font=font)
            drawer.text(
                (max(0, (canvas.width - line_width) // 2), top),
                line,
                fill=_QUOTE_COLOR,
                font=font,
            )
            top += line_height
        top += gap_quote_footer
        footer_width = drawer.textlength(footer, font=footer_font)
        drawer.text(
            (max(0, (canvas.width - footer_width) // 2), top),
            footer,
            fill=_FOOTER_COLOR,
            font=footer_font,
        )

        output = io.BytesIO()
        canvas.save(output, format="JPEG", quality=88, optimize=True)
        return output.getvalue()
    except Exception as exc:
        logger.warning(f"签到卡片合成失败，改用轻量模式: {exc}")
        return None
