"""图片回传与向量命中去重的纯函数工具。"""

from __future__ import annotations

import io
import math
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

try:
    from PIL import Image as PILImage
    from PIL import ImageOps as PILImageOps
except ImportError:  # Pillow 缺失时本地压缩自动降级为原字节
    PILImage = None
    PILImageOps = None


DELIVERY_MODES = frozenset({"scaled-url", "original-url", "local-compress"})
IMGBED_MANAGED_QUERY_KEYS = frozenset({"width", "height", "fit", "fallback"})
NAPCAT_PARSEABLE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
)
NAPCAT_PARSEABLE_FORMATS = frozenset({"JPEG", "PNG", "GIF", "WEBP", "BMP", "TIFF"})
COMPRESS_MIN_BYTES = 200 * 1024


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """读取并限制整数配置，非法值回退默认值。"""
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    return max(minimum, min(maximum, result))


def delivery_settings(config: Any) -> tuple[str, int, int]:
    """归一化图片回传配置，返回 (mode, max_side, quality)。"""
    raw = config if isinstance(config, dict) else {}
    mode = str(raw.get("mode") or "scaled-url").strip().lower()
    if mode not in DELIVERY_MODES:
        mode = "scaled-url"
    max_side = bounded_int(raw.get("max_side"), 1920, 1, 4096)
    quality = bounded_int(raw.get("quality"), 85, 1, 100)
    return mode, max_side, quality


def is_cloudflare_imgbed_url(url: str) -> bool:
    """按 CloudFlare-ImgBed 标准 /file/ 路径识别可缩放直链。"""
    try:
        parsed = urlsplit(str(url or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        return unquote(parsed.path).startswith("/file/")
    except Exception:
        return False


def build_scaled_url(url: str, max_side: int) -> str:
    """构造 ImgBed 等比缩放 URL；非 ImgBed URL 原样返回。"""
    if not is_cloudflare_imgbed_url(url):
        return url
    side = bounded_int(max_side, 1920, 1, 4096)
    parsed = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in IMGBED_MANAGED_QUERY_KEYS
    ]
    query.extend([("width", str(side)), ("height", str(side)), ("fallback", "original")])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def is_napcat_parseable_url(url: str) -> bool:
    """判断 URL 扩展名对应的格式能否被 QQ 协议端解析出宽高。"""
    try:
        path = unquote(urlsplit(str(url or "")).path).lower()
    except Exception:
        return False
    return any(path.endswith(extension) for extension in NAPCAT_PARSEABLE_EXTENSIONS)


def sniff_image_format(data: bytes) -> str | None:
    """按文件头识别图片格式，用于本地回退日志与压缩决策。"""
    if len(data) < 12:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if data.startswith(b"BM"):
        return "BMP"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "TIFF"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand.startswith(b"av"):
            return "AVIF"
        if brand in (b"heic", b"heix", b"hevc", b"mif1", b"msf1"):
            return "HEIF"
    stripped = data.lstrip(b" \t\r\n")
    if stripped.startswith((b"<svg", b"<?xml")):
        return "SVG"
    return None


def prepare_image_bytes(data: bytes, max_side: int, quality: int) -> tuple[bytes, bool] | None:
    """压缩静态图；返回 (字节, 是否压缩)，失败返回 None。"""
    if PILImage is None:
        return None
    try:
        with PILImage.open(io.BytesIO(data)) as image:
            if getattr(image, "is_animated", False):
                return data, False
            original_format = (image.format or "").upper()
            if len(data) <= COMPRESS_MIN_BYTES and original_format in NAPCAT_PARSEABLE_FORMATS:
                return data, False
            image = PILImageOps.exif_transpose(image)
            side = bounded_int(max_side, 1920, 1, 4096)
            width, height = image.size
            if max(width, height) > side:
                scale = side / max(width, height)
                image = image.resize((round(width * scale), round(height * scale)), PILImage.LANCZOS)
            if image.mode in ("RGBA", "LA", "P"):
                rgba = image.convert("RGBA")
                background = PILImage.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=bounded_int(quality, 85, 1, 100), optimize=True)
            compressed = output.getvalue()
    except Exception:
        return None
    if len(compressed) >= len(data) and original_format in NAPCAT_PARSEABLE_FORMATS:
        return data, False
    return compressed, True


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """计算两个向量的余弦相似度；维度不一致或零向量返回 0。"""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def _normalized_url(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower() if parsed.hostname else ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port is not None and port != (443 if scheme == "https" else 80):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((scheme, netloc, parsed.path or "/", query, ""))


def _exact_hit_key(hit: dict) -> tuple[str, str] | None:
    payload = hit.get("payload") or {}
    src = str(payload.get("src") or "").strip()
    if src:
        return ("src", src)
    image_url = _normalized_url(str(payload.get("image_url") or ""))
    if image_url:
        return ("url", image_url)
    return None


def deduplicate_vector_hits(hits: list[dict], threshold: float = 0.995) -> list[dict]:
    """合并相同 URL/src 与向量近似相同的命中，保留相似度最高者。"""
    ordered = sorted(hits, key=lambda hit: float(hit.get("score") or 0.0), reverse=True)
    result: list[dict] = []
    exact_index: dict[tuple[str, str], int] = {}
    for hit in ordered:
        exact_key = _exact_hit_key(hit)
        if exact_key is not None and exact_key in exact_index:
            representative = result[exact_index[exact_key]]
            representative["duplicate_count"] = int(representative.get("duplicate_count") or 1) + 1
            representative.setdefault("duplicates", []).append(hit)
            continue

        merged = False
        vector = hit.get("vector")
        if isinstance(vector, list) and vector:
            for representative in result:
                representative_vector = representative.get("vector")
                if not isinstance(representative_vector, list) or not representative_vector:
                    continue
                if cosine_similarity(vector, representative_vector) >= threshold:
                    representative["duplicate_count"] = (
                        int(representative.get("duplicate_count") or 1) + 1
                    )
                    representative.setdefault("duplicates", []).append(hit)
                    merged = True
                    break
        if merged:
            continue

        item = dict(hit)
        item["duplicate_count"] = 1
        item["duplicates"] = []
        if exact_key is not None:
            exact_index[exact_key] = len(result)
        result.append(item)
    return result
