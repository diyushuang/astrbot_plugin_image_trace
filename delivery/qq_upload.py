"""QQ 官方机器人上传通道：URL 转存 / 分片上传与接管编排。

QQ 官方上传通道的实现主体；`main` 保留同名薄委托，因此测试对
``plugin._qq_upload_block`` / ``plugin._qq_block_size`` 等方法的 monkeypatch
继续生效。模块函数统一接收 host（插件实例），通过 host 上的公开成员访问
配置、图片下载与会话能力，不反向 import main。
"""

from __future__ import annotations

import asyncio
import itertools
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..common import read_bytes, sanitize_error_text
from ..image_delivery import (
    QQ_UPLOAD_FATAL_KINDS,
    CompressionEvidence,
    build_scaled_url,
    choose_qq_upload_channel,
    classify_qq_upload_error,
    is_public_http_url,
    qq_upload_failure_hint,
    qq_upload_safe_image,
    sniff_image_format,
)
from ..random_media import swap_url_origin

# QQ 开放平台富媒体 file_type：1=图片、2=视频、3=语音、4=文件
QQ_IMAGE_FILE_TYPE = 1

# 官方文档：每个 msg_id 最多被动回复 5 次。一次事件里若回传超过 5 张图，
# 超出的部分必然发不出去，还会连记一串失败日志；这里提前截断并把说明并进
# 首图正文，至少让用户知道还有图没发。
QQ_OFFICIAL_MAX_MEDIA_PER_REPLY = 5

# 图片白名单格式 → 用于分片上传文件名的扩展名。QQ 的分片上传要求 file_name，
# 且格式不受支持会直接回 850019；文件名与真实内容保持一致最稳妥。
_QQ_FORMAT_SUFFIXES = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "GIF": ".gif",
    "WEBP": ".webp",
    "BMP": ".bmp",
}
_JPEG_SUFFIXES = frozenset({".jpg", ".jpeg"})

# 被动回复的 (msg_id, msg_seq) 组合重复会被平台判失败；AstrBot 自身发消息
# 默认用 1，因此插件从 2 开始单调递增，并在 2~9999 之间循环。
_QQ_MSG_SEQ = itertools.count(2)


def cap_qq_media_blocks(
    blocks: list[dict], limit: int = QQ_OFFICIAL_MAX_MEDIA_PER_REPLY
) -> tuple[list[dict], str]:
    """把一次投递的图片数截到官方被动回复上限，返回 (保留的 blocks, 提示语)。

    QQ 文档：被动回复每个 msg_id 最多 5 次。多图命中超过 5 张时，第 6 张起
    必然失败；与其把它们发失败，不如保留前 5 张并在标题里说明。提示语由
    调用方并进 header（首图正文），不能写进 block["text"]——向量回传路径
    随后会用 `_mark_compressed_captions` 重写配文，会把提示覆盖掉。
    纯文本 block 原样保留；不改动调用方传入的 block，避免污染历史记录。
    """
    image_indexes = [
        index for index, block in enumerate(blocks) if block.get("url") or block.get("path")
    ]
    if len(image_indexes) <= max(1, limit):
        return blocks, ""
    keep = set(image_indexes[:limit])
    kept = [
        block
        for index, block in enumerate(blocks)
        if not ((block.get("url") or block.get("path")) and index not in keep)
    ]
    dropped = len(image_indexes) - limit
    logger.info(
        f"qq_official 单次图片数超过官方被动回复上限，已截断 "
        f"{len(image_indexes)}→{limit} 张"
    )
    return kept, f"（QQ 官方单次最多回传 {limit} 张，另有 {dropped} 张未发送）"


def next_qq_msg_seq() -> int:
    """取下一个可用 msg_seq（2~9999 循环）。

    官方文档只要求同一 msg_id 下不重复，进程内单调递增足以避免插件自己
    连发多张图时撞号，也避开 AstrBot 适配器默认使用的 1。
    """
    value = next(_QQ_MSG_SEQ)
    return 2 + (value - 2) % 9998


def _name_with_suffix(name: str, detected: str) -> str:
    """按嗅探出的真实格式修正文件名扩展名（仅图片白名单格式）。"""
    suffix = _QQ_FORMAT_SUFFIXES.get(detected)
    if not suffix:
        return name
    stem, current = os.path.splitext(name)
    current = current.lower()
    if current == suffix or (detected == "JPEG" and current in _JPEG_SUFFIXES):
        return name
    return f"{stem or 'image'}{suffix}"


def _read_file_head(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(16)
    except OSError:
        return b""


async def qq_safe_local_image(
    host, path: str, name: str, size_limit: int
) -> tuple[str, str, str]:
    """确保本地图片是 QQ 富媒体白名单格式；必要时转码为 JPEG。

    返回 (上传路径, 文件名, 需清理的临时文件)。白名单格式（jpg/png/gif/
    webp/bmp）原样返回；HEIC/AVIF/TIFF/SVG 等可解码图片转成 JPEG，避免
    分片上传因 850019「不支持的文件格式」失败；解码失败时仍交原文件，
    让接口返回真实错误，而不是插件提前把图吞掉。
    """
    head = await asyncio.to_thread(_read_file_head, path)
    detected = sniff_image_format(head)
    if qq_upload_safe_image(head):
        return path, _name_with_suffix(name, detected), ""
    try:
        oversized = os.path.getsize(path) > size_limit
    except OSError:
        return path, name, ""
    if oversized:
        # 转码大文件要整份读进内存；超限时交给接口报错，不在本地冒险
        return path, _name_with_suffix(name, detected), ""
    data = await asyncio.to_thread(read_bytes, path)
    converted = await asyncio.to_thread(host._write_transcoded_jpeg, data)
    if not converted:
        return path, _name_with_suffix(name, detected), ""
    logger.info(
        f"qq_official 分片上传前已把 {detected or '未知格式'} 转码为 JPEG: {converted}"
    )
    return converted, os.path.basename(converted), converted


def qq_send_target(event: AstrMessageEvent) -> tuple[str, str] | None:
    """取 (会话类型, 目标 openid)：group→group_openid、c2c→user_openid。

    botpy 的群消息自带 group_openid、C2C 消息在 author.user_openid 上。
    都拿不到时退化为「session_id + 消息类型」；连类型都判不出就返回 None，
    让调用方交回原回传逻辑——猜错会话类型会把图发到错误的聊天里，比不发
    严重得多，所以这里宁可放弃接管。
    """
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    group_openid = str(getattr(raw, "group_openid", "") or "").strip()
    if group_openid:
        return ("group", group_openid)
    author = getattr(raw, "author", None)
    user_openid = str(getattr(author, "user_openid", "") or "").strip()
    if not user_openid:
        user_openid = str(getattr(raw, "user_openid", "") or "").strip()
    if user_openid:
        return ("c2c", user_openid)
    try:
        session_id = str(event.get_session_id() or "").strip()
    except Exception:
        session_id = ""
    if not session_id:
        return None
    try:
        message_type = str(event.get_message_type() or "").upper()
    except Exception:
        message_type = ""
    if "GROUP" in message_type:
        return ("group", session_id)
    if any(hint in message_type for hint in ("FRIEND", "C2C", "PRIVATE")):
        return ("c2c", session_id)
    return None


def qq_target_kwargs(target: tuple[str, str]) -> dict:
    """把 (类型, openid) 转成适配器上传方法的收件人关键字参数。"""
    kind, session_id = target
    return {"group_openid": session_id} if kind == "group" else {"openid": session_id}


def public_media_url(host, url: str, settings: dict) -> str | None:
    """把图床直链换成「外部服务器下载得到」的公网地址；换不出来返回 None。

    两处共用：qq_official 的视频要交给腾讯拉流、图片要交给腾讯按 URL 转存。
    内网直连（lan_base_url）只是图床与 AstrBot 之间的便利通道，对外正源是
    base_url，故命中内网主机时按 base_url 换源（只换协议+主机端口，路径与
    查询串原样保留）。

    判定不能只看 IP 段：曾遇到把云厂商分配的公网段 IP 当作内网直连地址的
    部署，句法检查会把它当公网地址放行。因此以配置为准：

    1. 命中内网直连主机（lan_base_url）→ 按 base_url 换回公网源；
    2. 换完仍落在内网主机上（说明 base_url 本身就配成了内网）→ 不可用；
    3. 与 base_url 同主机 → 视为公网直链（配置已声明它是公网站点）；
    4. 其余第三方主机 → 退化为句法判断（公网域名/公网 IP 放行）。
    """
    if not url:
        return None
    base = str(settings.get("base_url") or "").strip()
    lan_hosts = host._lan_url_hosts(settings)
    candidate = swap_url_origin(url, base, lan_hosts) if lan_hosts else url
    try:
        hostname = (urlsplit(candidate).hostname or "").lower()
    except ValueError:
        return None
    # 只与「内网地址的主机」比，不能与整个 _lan_url_hosts 比——后者里头还
    # 包含公网 base 的主机（换源后正是要落到它上面）
    lan_host = ""
    lan_base = str(settings.get("lan_base_url") or "").strip()
    if lan_base:
        try:
            lan_host = (urlsplit(lan_base).hostname or "").lower()
        except ValueError:
            lan_host = ""
    if lan_host and hostname == lan_host:
        # 换源后仍是内网主机：腾讯服务器访问不到，别发出一次必败的上传
        return None
    base_host = ""
    if base:
        try:
            base_host = (urlsplit(base).hostname or "").lower()
        except ValueError:
            base_host = ""
    if base_host and hostname == base_host:
        return candidate
    return candidate if is_public_http_url(candidate) else None


async def block_size(host, block: dict) -> int | None:
    """该图体积：优先用命中条目自带的大小，未知时 HEAD 探一次。

    探不出来返回 None（=未知），调用方据此避开「未知却当成小图直传」这个
    5MB 陷阱；探测只读响应头，不产生下载流量。
    """
    known = host._known_bytes(block)
    if known:
        return known
    url = str(block.get("url") or "")
    if not url:
        return None
    exists, length = await host._probe_url(url)
    return length if exists is not False else None


async def upload_takeover(
    host,
    event: AstrMessageEvent,
    header: str,
    blocks: list[dict],
    *,
    qq_config: tuple[str, int, int, str, int, str],
    random_settings: dict,
    mark_compressed: bool = False,
) -> bool:
    """qqofficial 上传通道接管：返回 True 表示本批已由本方法处理完毕。

    只在通道不是 direct 时才真正接管；一旦接管，图片由插件自己上传并发送
    （post_group_message / post_c2c_message + msg_type=7），所以成功返回后
    调用方必须标记已发送并 stop_event，否则 respond stage 会照原路（base64
    直传）再发一次，那正是「上传文件大小超过限制」的来源。

    固定 url / url-scaled 通道是例外：某张图没有可交给腾讯的公网直链
    （只剩本地转码 path，或图床只有内网源）时，该图降级为分片上传——
    URL 上传没有入参，照原通道发出去必然失败，白记一轮 WARN。

    auto 模式下 URL 上传失败（腾讯侧拉不到图）会按官方建议改走分片重试：
    先把图下载到本地再分片，既不依赖图床公网可达，也能保住原图。额度用完 /
    被禁言 / 发送超频这类「换通道也会失败」的错误直接回一条说明，不再做无谓
    回退。

    失败语义两档（delivery.qq_on_failure）：
    - fallback（默认）：一张都还没发出去时返回 False，交回原有的本地压缩 /
      缩略图降级链；只要已经发过就绝不再交回，避免同一张图发两遍。
    - notify：不交回，改回一条文字说明失败原因。

    发送（post_media）阶段的超时/未知错误按「已送达」处理：请求可能已经到达
    QQ，只是回执丢了，重发会让用户收到重复图片。
    """
    mode, direct_max, url_max, auto_prefer, max_upload, on_failure = qq_config
    if mode == "native":
        # 通道关闭：交给 image_delivery 的原生回传逻辑（含压缩）
        return False
    target = host._qq_send_target(event)
    bot = getattr(event, "bot", None)
    if target is None or getattr(bot, "api", None) is None:
        logger.debug("qq_official 上传通道：会话标识或 bot 未就绪，交回原回传逻辑")
        return False
    images = [block for block in blocks if block.get("url") or block.get("path")]
    if not images:
        return False

    plan: list[tuple[dict, str, str | None]] = []
    degraded = 0
    for block in images:
        # 固定通道（native/bytes/url/url-scaled）不依赖体积，省掉一次 HEAD 探测
        size = None if mode != "auto" else await host._qq_block_size(block)
        url = str(block.get("url") or "")
        public_url = host._public_media_url(url, random_settings) if url else None
        url_usable = public_url is not None
        if mode == "auto" and url_usable and host._qq_url_cooling():
            # 刚撞过「腾讯拉不到公网直链」：冷却期内 auto 直接分片，别再白等
            url_usable = False
        channel = choose_qq_upload_channel(
            mode=mode,
            size_bytes=size,
            direct_max_bytes=direct_max,
            url_max_bytes=url_max,
            url_usable=url_usable,
            auto_prefer=auto_prefer,
        )
        if channel == "native":
            # 混批里只要有一张判定该直传，就整批交回原逻辑：拆成两次投递会
            # 绕开「一次事件只投递一批图」的去重盾，用户可能收到重复图片
            logger.debug("qq_official 上传通道：该批含直传图，交回原回传逻辑")
            return False
        if channel in {"url", "url-scaled"} and public_url is None:
            # 钉死直链通道，但这张图没有可交给腾讯的公网直链：只剩本地
            # 转码 path（格式体检把假格式换成 JPEG 时会丢掉 url），或图床
            # 只有内网源（base_url 未配/本身是内网）。URL 上传没有入参，
            # 发出去必然失败——降级到分片，图源本机就有，不依赖图床公网可达
            degraded += 1
            channel = "bytes"
        plan.append((block, channel, public_url))

    if degraded:
        logger.info(
            f"qq_official 直链通道没有可用的公网直链（本地转码图或内网图床），"
            f"本批 {degraded} 张改走分片上传"
        )

    if mark_compressed:
        # 本通道发的是原图（URL 原样 / 分片原始字节），配文按「原图」口径写，
        # 不能沿用压缩证据——文字说已压缩而用户收到原图是这个仓库的老毛病
        host._mark_compressed_captions(
            blocks, [CompressionEvidence.ORIGINAL] * len(images)
        )

    prefix = [header] if header else []
    sent_any = False
    for index, (block, channel, public_url) in enumerate(plan):
        own_text = str(block.get("text") or "").strip()
        parts = (*prefix, own_text) if index == 0 else (own_text,)
        content = "\n".join(part for part in parts if part)

        # 上传与发送分开捕获：上传失败时图片一定还没发出去，可以安全换通道；
        # 发送失败则可能已被 QQ 接收，必须按「结果未知」处理，不能一律重发。
        try:
            media = await host._qq_upload_block(
                event, block, channel, public_url, target, max_upload
            )
        except Exception as exc:
            upload_kind = classify_qq_upload_error(exc)
            logger.warning(
                f"qq_official {channel} 通道上传失败"
                f"（第 {index + 1}/{len(plan)} 张，{upload_kind}）: "
                f"{type(exc).__name__}: {exc}"
            )
            if channel in {"url", "url-scaled"} and upload_kind in {
                "url_unreachable",
                "timeout",
                "unknown",
            }:
                # 公网直链拿得到、腾讯却拉不到：记冷却，auto 后续直接走 bytes
                host._note_qq_url_unsupported(exc)
            if (
                mode == "auto"
                and channel in {"url", "url-scaled"}
                and upload_kind in {"url_unreachable", "timeout", "unknown"}
            ):
                # 官方文档明确建议：URL 拉取失败时先下载到本地，再分片上传。
                # 分片不依赖图床公网可达，也能保住原图，比直接压缩降级更好。
                logger.info("qq_official URL 上传不可用，按官方建议改走分片重试")
                try:
                    media = await host._qq_upload_block(
                        event, block, "bytes", None, target, max_upload
                    )
                except Exception as retry_exc:
                    upload_kind = classify_qq_upload_error(retry_exc)
                    logger.warning(
                        f"qq_official 分片重试失败（{upload_kind}）: "
                        f"{type(retry_exc).__name__}: {retry_exc}"
                    )
                    exc = retry_exc
                else:
                    upload_kind = ""
            if upload_kind:
                if upload_kind in QQ_UPLOAD_FATAL_KINDS:
                    # 额度用完 / 禁言 / 超频：换任何通道都会失败，直接说明原因
                    await host._qq_notify(event, target, _failure_text(exc, upload_kind))
                    return True
                if not sent_any and on_failure == "fallback":
                    logger.warning(
                        "qq_official 上传通道不可用，交回原回传逻辑（本地压缩/缩略图）"
                    )
                    return False
                await host._qq_notify(event, target, _failure_text(exc, upload_kind))
                return True

        try:
            await host._qq_post_media(event, target, content, media)
        except Exception as exc:
            send_kind = classify_qq_upload_error(exc)
            logger.warning(
                f"qq_official 媒体消息发送失败（{send_kind}，"
                f"第 {index + 1}/{len(plan)} 张）: {type(exc).__name__}: {exc}"
            )
            if channel in {"url", "url-scaled"} and send_kind in {
                "url_unreachable",
                "timeout",
                "unknown",
            }:
                # 发送阶段的 304082/304083 同样是「腾讯拉不到媒体」，后续 auto
                # 应直接走分片，别再反复上传一个发不出去的直链
                host._note_qq_url_unsupported(exc)
            if (
                mode == "auto"
                and channel in {"url", "url-scaled"}
                and send_kind == "url_unreachable"
                and not sent_any
            ):
                # 媒体已上传但腾讯拉不到：改分片重传再发一次。此时还没有任何
                # 图片送达，重试不会产生重复消息。
                logger.info("qq_official 媒体拉取失败，改走分片重试")
                try:
                    media = await host._qq_upload_block(
                        event, block, "bytes", None, target, max_upload
                    )
                    await host._qq_post_media(event, target, content, media)
                except Exception as retry_exc:
                    send_kind = classify_qq_upload_error(retry_exc)
                    logger.warning(
                        f"qq_official 分片重试发送失败（{send_kind}）: "
                        f"{type(retry_exc).__name__}: {retry_exc}"
                    )
                    exc = retry_exc
                else:
                    sent_any = True
                    continue
            if send_kind in {"unknown", "timeout"}:
                # 请求可能已经送达，只是没拿到回执；按不变量「UNKNOWN 视为
                # 已送达」，绝不回退重发，否则用户会收到重复图片。
                logger.warning("qq_official 媒体消息结果未知，按已送达处理，不再重发")
                sent_any = True
                continue
            if send_kind in QQ_UPLOAD_FATAL_KINDS:
                await host._qq_notify(event, target, _failure_text(exc, send_kind))
                return True
            if not sent_any and on_failure == "fallback":
                logger.warning(
                    "qq_official 媒体消息明确失败，交回原回传逻辑（本地压缩/缩略图）"
                )
                return False
            await host._qq_notify(event, target, _failure_text(exc, send_kind))
            return True
        sent_any = True
    return True


def _failure_text(exc: BaseException, kind: str) -> str:
    """失败说明：优先给可操作的短句，未收录的类别退回脱敏后的异常摘要。"""
    hint = qq_upload_failure_hint(kind)
    if hint:
        return f"⚠️ 图片发送失败：{hint}"
    detail = sanitize_error_text(f"{type(exc).__name__}: {exc}")
    return f"⚠️ 图片发送失败：{detail}"


async def upload_block(
    host,
    event: AstrMessageEvent,
    block: dict,
    channel: str,
    public_url: str | None,
    target: tuple[str, str],
    max_upload: int,
    *,
    delivery_config: tuple[str, int, int, str],
):
    """按通道上传一张图，返回 QQ 侧 Media。

    url / url-scaled 共用同一条 URL 上传实现，区别只在交给腾讯的是原图
    直链还是图床缩放直链；bytes 即分片上传（官方术语 chunked upload），
    需要先把图落到本地。
    """
    if channel in {"url", "url-scaled"}:
        if not public_url:
            raise RuntimeError("没有公网直链，QQ 无法按 URL 拉取该图")
        upload_url = public_url
        if channel == "url-scaled":
            _mode, max_side, quality, style = delivery_config
            upload_url = (
                build_scaled_url(public_url, max_side, quality=quality, style=style)
                or public_url
            )
        return await host._qq_upload_via_url(event, upload_url, target)
    path = str(block.get("path") or "")
    temp = ""
    converted = ""
    try:
        if not path:
            temp = await host.image_source.download(
                str(block.get("url") or ""), size_limit=max_upload
            )
            path = temp
        name = (
            str(block.get("file_name") or "").strip()
            or os.path.basename(path)
            or "image.jpg"
        )
        # 分片上传要求 file_name，且 QQ 只接受富媒体白名单格式；本地图库里的
        # HEIC/TIFF 与「扩展名 .jpg、实为 WEBP/HEIC」都会在这里被纠正。
        path, name, converted = await qq_safe_local_image(
            host, path, name, max_upload
        )
        return await host._qq_upload_via_chunked(event, path, name, target)
    finally:
        # 分片通道只为这一次上传落盘，发完即删；临时目录另有 TTL 清理兜底
        host._remove_quiet(temp)
        host._remove_quiet(converted)


async def upload_via_url(host, event: AstrMessageEvent, url: str, target: tuple[str, str]):
    """URL 上传：只传 `url` 字段，由腾讯侧下载转存（图片软限 20MB）。

    借适配器自身的通用媒体上传方法（它对「非本地路径」正是走 url 分支），
    避免在插件里复刻 QQ 接口字段与错误码；AstrBot 发主动消息时也是用同样的
    伪事件（只带 bot 属性）调用它。
    """
    try:
        from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
            QQOfficialMessageEvent,
        )
    except ImportError as exc:  # 版本差异：模块或方法被改名
        raise RuntimeError(
            f"当前 AstrBot 版本缺少 qqofficial 媒体上传方法: {exc}"
        ) from exc
    helper = SimpleNamespace(bot=getattr(event, "bot", None))
    return await QQOfficialMessageEvent.upload_group_and_c2c_media(
        helper,
        url,
        QQ_IMAGE_FILE_TYPE,
        srv_send_msg=False,
        **host._qq_target_kwargs(target),
    )


async def upload_via_chunked(
    host, event: AstrMessageEvent, path: str, name: str, target: tuple[str, str]
):
    """分片上传：prepare → 并发 PUT 分片 → 逐片确认 → 合并（硬限 200MB）。

    直接实例化适配器的分片器，而不是走 upload_group_and_c2c_media：后者只在
    「本地文件 > 10MB」时才分片，5~10MB 仍会落回 base64 分支（照样撞 5MB）。
    """
    try:
        from astrbot.core.platform.sources.qqofficial.qqofficial_chunked_upload import (
            QQOfficialChunkedUploader,
        )
    except ImportError as exc:
        raise RuntimeError(
            f"当前 AstrBot 版本缺少 qqofficial 分片上传器: {exc}"
        ) from exc
    uploader = QQOfficialChunkedUploader(event.bot.api._http)
    kind, session_id = target
    if kind == "group":
        return await uploader.upload_group(
            file_path=Path(path),
            file_type=QQ_IMAGE_FILE_TYPE,
            file_name=name,
            group_openid=session_id,
            srv_send_msg=False,
        )
    return await uploader.upload_c2c(
        file_path=Path(path),
        file_type=QQ_IMAGE_FILE_TYPE,
        file_name=name,
        user_openid=session_id,
        srv_send_msg=False,
    )


async def notify(
    host, event: AstrMessageEvent, target: tuple[str, str], text: str
) -> None:
    """失败说明用纯文本回一条；这条发不出去也不影响主流程。"""
    try:
        api = event.bot.api
        message_id = str(
            getattr(getattr(event, "message_obj", None), "message_id", "") or ""
        ).strip()
        payload = {
            "msg_type": 0,
            "content": text,
            "msg_id": message_id or None,
            "msg_seq": next_qq_msg_seq(),
        }
        kind, session_id = target
        if kind == "group":
            await api.post_group_message(group_openid=session_id, **payload)
        else:
            await api.post_c2c_message(openid=session_id, **payload)
    except Exception as exc:
        logger.debug(f"qq_official 失败说明发送失败（忽略）: {exc}")
