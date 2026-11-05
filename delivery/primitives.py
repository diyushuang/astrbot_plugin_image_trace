"""R2 发送原语：OneBot 直发、本地压缩、URL 计划、配文与 @ 前缀。

从 main 下沉到这里的是**字节级与消息段级**实现，通道层（channels.py）仍负责
「选哪条通道、失败后怎么回退」。所有原语都接收 `host` 作为依赖门面，调用
host 上保留的同名薄委托（`_load_delivery_bytes` / `_prepare_delivery_blocks` /
`_send_via_onebot` 等），因此既有 monkeypatch 点不受影响。

配置读取不在这里发生：`_delivery_config` / `_encode_options` /
`_onebot_image_timeout` / `_verify_scaled` 的取值由 main 的薄委托读好后注入，
避免绕过 config 门面。
"""

from __future__ import annotations

import asyncio
import base64

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:
    from ..common import COMPRESS_CONCURRENCY
    from ..features import image_bytes_ok
    from ..image_delivery import (
        CompressionEvidence,
        SendOutcome,
        build_original_url,
        build_scaled_url,
        classify_send_error,
        compression_evidence,
        is_scaling_needed,
        prefer_inline_bytes,
        prefer_url_direct,
        prepare_image_bytes,
        send_diagnosis,
        sniff_image_format,
    )
except ImportError:  # 兼容插件以独立模块方式加载
    from common import COMPRESS_CONCURRENCY  # type: ignore[no-redef]
    from features import image_bytes_ok  # type: ignore[no-redef]
    from image_delivery import (  # type: ignore[no-redef]
        CompressionEvidence,
        SendOutcome,
        build_original_url,
        build_scaled_url,
        classify_send_error,
        compression_evidence,
        is_scaling_needed,
        prefer_inline_bytes,
        prefer_url_direct,
        prepare_image_bytes,
        send_diagnosis,
        sniff_image_format,
    )


def mention_prefix(host, event: AstrMessageEvent) -> list:
    """回传链开头的「@发送者」段；仅能渲染原生 At 的平台启用。

    qq_official 等平台没有原生 At 段：适配器会把 At 转成平台标记原文
    （`<qqbot-at-user id="..."/>`）拼进纯文本，而官方群消息的纯文本并不
    渲染该标记（仅 markdown 消息类型生效），用户看到的就是一串“代码”
    糊在文案前。故 @ 前缀只给能原生渲染它的 aiocqhttp，其余平台不加。
    """
    if not host._delivery_target(event).native_mention:
        return []
    try:
        sender = str(event.get_sender_id() or "").strip()
    except Exception:
        return []
    return [Comp.At(qq=sender)] if sender else []


async def send_via_onebot(host, event: AstrMessageEvent, header: str, blocks: list[dict], payloads: list[str | bytes], *, image_timeout: int) -> SendOutcome:
    """通过 OneBot 原生接口直发消息，消息段按「文字集中 → 图片集中」排列。

    文字在前、图片在后是**必须**的，不是排版偏好：QQ 客户端会把
    「text 段 + 紧邻其后的 image 段」合并渲染成一个图文混排卡片，卡片内的
    图片按行内富文本插图处理，**一律裁成正方形缩略图**（无论原图多高）。
    此前按「标题 →（配文 → 图）…」图文交替排列，多图时每一张图都恰好紧跟在
    一段文字后面，于是几张图全是正方形缩略图；只有排在最后、后面没有更多
    文字重新分组的那张偶尔能拿到正确尺寸——线上报的「前三张方、第四张正常」
    就是这个渲染分组的结果。把全部 text 段提到最前、image 段连续排在末尾后，
    QQ 会把连续图片段按普通大图渲染，宽高比得以保留。

    配文因此不再紧贴各自的图片，改为在配文里前置序号（block["prefix"]，如
    「1.」「2.」），与图片的先后顺序一一对应，用户仍能分辨哪句配文属于哪张图。

    payloads 与 blocks 中的有图 block 同序一一对应，元素为 URL 字符串（原样
    作 data.file）或图片字节（编码为 base64:// 段，压缩结果直达 QQ、不经
    协议端二次下载）。调用方（_yield_delivery 的两个直发分支、
    _send_prompt）已保证数量相等，故这里按遍历到的有图 block 依次取用，
    无需再做长度校验。

    image 段带 OneBot 标准的 `timeout` 字段（秒）：URL 段由协议端自行下载，
    大图 + 多图时下载耗时会让 NapCat 的 sendMsg 回调窗口吃紧（实测超时形态
    见 classify_send_error 注释）。显式放宽该窗口可减少「结果未知」。base64
    段不经下载，带该字段无副作用。

    返回三态而非布尔：只有协议端明确回报失败（FAILED）才允许换通道重发。
    超时等 UNKNOWN 情形请求可能已经送达，调用方必须先看返回值再决定是否
    回退，否则同一张图会被发两遍。
    """
    message: list[dict] = []
    sender_id = event.get_sender_id()
    if sender_id:
        message.append({"type": "at", "data": {"qq": str(sender_id)}})

    # 1) 全部文字段集中在最前：标题一段，随后每张图各一段配文。
    #    连续 text 段之间不会再插入 image 段，QQ 不会把它们拆成图文卡片。
    text_lines: list[str] = []
    if header:
        text_lines.append(header)
    for block in blocks:
        text = str(block.get("text") or "").strip()
        if text:
            text_lines.append(text)
    if text_lines:
        # 单段承载全部文字：段数越少，QQ 越不容易按「文字+图」重新分组
        message.append({"type": "text", "data": {"text": "\n".join(text_lines)}})

    # 2) 全部图片段连续排在末尾：连续 image 段按普通大图渲染，保留宽高比。
    pending_payloads = iter(payloads)
    for block in blocks:
        if not (block.get("url") or block.get("path")):
            continue
        payload = next(pending_payloads)
        if isinstance(payload, bytes):
            payload = "base64://" + base64.b64encode(payload).decode()
        message.append(
            {
                "type": "image",
                "data": {"file": payload, "timeout": image_timeout},
            }
        )

    params: dict = {"message": message}
    group_id = event.get_group_id()
    if group_id:
        action = "send_group_msg"
        params["group_id"] = int(group_id) if str(group_id).isdigit() else group_id
    else:
        action = "send_private_msg"
        user_id = sender_id
        params["user_id"] = int(user_id) if str(user_id).isdigit() else user_id
    self_id = getattr(event.message_obj, "self_id", None)
    if self_id:
        params["self_id"] = self_id

    image_count = sum(1 for seg in message if seg["type"] == "image")
    try:
        await event.bot.call_action(action, **params)
    except Exception as exc:
        outcome = classify_send_error(exc)
        # 日志按成因给结论：不再只写「结果未知」，而是讲清「请求已发出、
        # 未拿到回执、因此按不重发处理」以及成因与排查方向。三态判定与
        # 回退决策一字未改，这里只换表达。
        logger.warning(
            f"OneBot 图片直发（{action}，{image_count} 张）：{send_diagnosis(exc)}"
        )
        logger.debug(
            f"OneBot 直发异常原始信息: {type(exc).__name__} -> {outcome.value}; "
            f"retcode={getattr(exc, 'retcode', None)}; echo={getattr(exc, 'echo', None)}; {exc}"
        )
        return outcome
    logger.info(f"OneBot 图片直发成功: {action}（{image_count} 张）")
    return SendOutcome.SENT


def mark_compressed_captions(host, blocks: list[dict], evidences: list[CompressionEvidence]) -> None:
    """为 mark_compressed 路径写入逐图配文。

    evidences 与 blocks 中的有图 block 一一对应（同序于 URL 直传计划或
    _prepare_delivery_blocks 的输出）；无图 block（例如没有可用直链的条目）
    按 UNKNOWN 处理，但它也要拿到配文，才不会在合并消息里消失。仅在
    mark_compressed 为真时调用，默认路径不触碰 block["text"]。
    """
    index = 0
    for block in blocks:
        if block.get("url") or block.get("path"):
            evidence = evidences[index]
            index += 1
        else:
            evidence = CompressionEvidence.UNKNOWN
        block["text"] = host._vector_caption(block, evidence)


async def prepare_delivery_blocks(host, blocks: list[dict], *, compress: bool, delivery_config, encode_options) -> list[tuple[bytes, bool]] | None:
    """准备要直发的字节，逐图压缩在并发上限内并行执行。

    顺序必须与传入的 image_blocks 一致（调用方按下标取用），因此用
    gather 收集结果而不是 as_completed。任一张失败即整体返回 None，
    调用方据此改走 URL 链——与逐张串行时的语义一致。
    """
    mode, max_side, quality = delivery_config
    target_bytes, use_webp = encode_options
    semaphore = asyncio.Semaphore(COMPRESS_CONCURRENCY)

    async def prepare_one(block: dict) -> tuple[bytes, bool] | None:
        async with semaphore:
            data = await host._load_delivery_bytes(block, validate=not compress)
            if data is None:
                return None
            if not compress or mode == "url":
                return data, False
            result = await asyncio.to_thread(
                prepare_image_bytes,
                data,
                max_side,
                quality,
                target_bytes=target_bytes,
                use_webp=use_webp,
            )
            if result is not None:
                return result
            if await asyncio.to_thread(image_bytes_ok, data):
                # 压缩失败但图片本身可用（格式不支持重编码等）：回退原字节
                logger.warning(
                    f"图片本地压缩失败，回退原字节: 格式={sniff_image_format(data) or '未知'}, "
                    f"大小={len(data) / 1024:.0f}KB"
                )
                return data, False
            if sniff_image_format(data) == "AVIF":
                # Pillow<11.3 无 AVIF 解码器：探测、下载后既压不了也验不了，
                # 只能让协议端自己拉 URL。给出可操作的提示而不是笼统的失败。
                logger.warning(
                    f"AVIF 图片当前环境无法解码（Pillow 版本过低），改用 URL 发送: "
                    f"大小={len(data) / 1024:.0f}KB；升级 Pillow>=11.3 可启用 AVIF 本地压缩"
                )
                return None
            logger.warning(
                f"回退图片无法解码，改用 URL 发送: 格式={sniff_image_format(data) or '未知'}, "
                f"大小={len(data) / 1024:.0f}KB"
            )
            return None

    prepared = await asyncio.gather(
        *(prepare_one(block) for block in blocks if block.get("url") or block.get("path"))
    )
    if any(item is None for item in prepared):
        return None
    return list(prepared)


async def local_compress_delivery(host, event: AstrMessageEvent, header: str, blocks: list[dict], image_blocks: list[dict], *, mark_compressed: bool) -> SendOutcome | None:
    """auto / bytes 模式下 OneBot 的混合载荷直发。

    逐图决定 URL 段还是 base64 段：已知小图（≤COMPRESS_MIN_BYTES 且协议端
    能解析宽高）跳过下载、直发原图 URL——本地压缩本就会跳过这类图，下载再
    base64 回发纯属浪费一次往返；其余并发下载+压缩后内联 base64，压缩结果
    不经协议端二次下载，100% 到达 QQ（QQ 协议端会剥离 URL 查询参数，缩放
    URL 直传拿回的常是原图）。未压缩且超内联上限的巨型图（动图等）退回
    URL 段，避免单条消息过大。

    返回 None 表示准备失败（任一张下载/解码失败），调用方整批改走 URL 链；
    否则返回 _send_via_onebot 的三态结果。
    """
    prepare_blocks = [block for block in image_blocks if not prefer_url_direct(block)]
    prepared = await host._prepare_delivery_blocks(prepare_blocks, compress=True)
    if prepared is None:
        return None

    payloads: list[str | bytes] = []
    evidences: list[CompressionEvidence] = []
    pending = iter(prepared)
    for block in image_blocks:
        url = str(block.get("url") or "")
        if prefer_url_direct(block):
            payloads.append(build_original_url(url) or url)
            evidences.append(CompressionEvidence.ORIGINAL)
            continue
        data, compressed = next(pending)
        if prefer_inline_bytes(len(data), compressed, url):
            payloads.append(data)
        else:
            payloads.append(build_original_url(url) or url)
        evidences.append(
            CompressionEvidence.COMPRESSED if compressed else CompressionEvidence.ORIGINAL
        )

    if mark_compressed:
        host._mark_compressed_captions(blocks, evidences)
    elif any(evidence is CompressionEvidence.COMPRESSED for evidence in evidences):
        header += "（已压缩）"
    return await host._send_via_onebot(event, header, blocks, payloads)


async def url_delivery_plan(host, image_blocks: list[dict], max_side: int, mode: str, *, quality: int = 85, scaled_url_style: str = "query", verify_scaled: bool = False) -> tuple[list[str], list[CompressionEvidence]]:
    """决定每张图 URL 直传实际发哪个 URL，并给出压缩证据，两者同序对应。

    不再无条件改写 URL：只有最长边确实超过 max_side（或尺寸未知）时才追加
    图床缩放参数——不超过 max_side 的图，图床按 fallback=original 原样返回，
    追加参数只是白白触发一次处理。开启 verify_scaled 时进一步实测缩放版是否
    真的更小，这也是「已压缩」文案唯一的一手依据；实测不出来就按 UNKNOWN
    处理，配文宁可不写，也不能出现「文字说压缩、收到的是原图」。

    探测成本被压到最低：所有探测并发执行且有总预算，命中条目自带原图体积
    （size_bytes / file_size）时不必再探原图；图床明确拒绝缩放请求后进入
    冷却期，冷却期内直接跳过探测。
    """
    urls: list[str] = []
    sizes: list[tuple] = []
    jobs: list[tuple[str, str, int] | None] = []
    verify = (
        mode == "url-scaled" and verify_scaled and not host._scaled_verify_cooling()
    )
    for block in image_blocks:
        original = build_original_url(str(block.get("url") or ""))
        size = (block.get("width"), block.get("height"))
        sizes.append(size)
        if mode != "url-scaled" or not original:
            urls.append(original or str(block.get("url") or ""))
            jobs.append(None)
            continue
        scaled = build_scaled_url(original, max_side, quality=quality, style=scaled_url_style)
        if is_scaling_needed(size, max_side) is False or scaled == original:
            # 尺寸证明不会被缩放，或这条直链本来就带不了缩放参数
            urls.append(original)
            jobs.append(None)
            continue
        urls.append(scaled)
        jobs.append((original, scaled, host._known_bytes(block)) if verify else None)

    verified = await host._verify_scaled_batch(jobs) if any(jobs) else {}
    evidences = [
        compression_evidence(sizes[index], max_side, verified_smaller=verified.get(index))
        for index in range(len(image_blocks))
    ]
    return urls, evidences
