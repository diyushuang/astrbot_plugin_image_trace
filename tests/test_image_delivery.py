import asyncio
import io

from PIL import Image

from astrbot_plugin_image_trace.image_delivery import (
    MAX_INLINE_IMAGE_BYTES,
    SEND_REASON_DISCONNECTED,
    SEND_REASON_PROTOCOL_REJECT,
    SEND_REASON_SENT,
    SEND_REASON_TIMEOUT,
    SEND_REASON_UNCLASSIFIED,
    CompressionEvidence,
    SendOutcome,
    build_imgbed_file_url,
    build_original_url,
    build_scaled_url,
    classify_probe_status,
    classify_send_error,
    compression_evidence,
    content_length_from_headers,
    deduplicate_vector_hits,
    delivery_settings,
    is_cloudflare_imgbed_url,
    is_napcat_parseable_url,
    is_scaling_capability_rejection,
    is_scaling_needed,
    prefer_inline_bytes,
    prefer_url_direct,
    prepare_image_bytes,
    qq_upload_safe_image,
    send_diagnosis,
    send_outcome_reason,
    sniff_image_format,
    upgrade_to_https,
)


def test_delivery_settings_are_normalized_and_clamped():
    # 默认 auto：OneBot 下即「下载后压缩直发」——协议端会剥离 URL 查询参数，
    # 图床缩放直传不可靠，故压缩字节是默认路径
    assert delivery_settings(None) == ("auto", 1920, 85, "query")
    assert delivery_settings({"mode": "BOGUS", "scaled_url_style": "BOGUS"}) == (
        "auto",
        1920,
        85,
        "query",
    )
    assert delivery_settings(
        {"mode": "URL-SCALED", "scaled_url_style": "CF-PATH", "max_side": 9999, "quality": 0}
    ) == ("url-scaled", 4096, 1, "cf-path")
    assert delivery_settings({"mode": "BYTES", "max_side": 9999, "quality": 0}) == (
        "bytes",
        4096,
        1,
        "query",
    )


def test_legacy_delivery_modes_map_to_unified_names():
    """统一命名前的旧取值按等价关系映射，升级后无需改配置。"""
    assert delivery_settings({"mode": "local-compress"})[0] == "auto"
    assert delivery_settings({"mode": "scaled-url"})[0] == "url-scaled"
    assert delivery_settings({"mode": "original-url"})[0] == "url"


def test_scaled_url_only_for_cloudflare_imgbed_file_links():
    url = "https://img.example.com/file/a%20b.jpg?x=1&width=10"
    assert build_scaled_url(url, 1280) == (
        "https://img.example.com/file/a%20b.jpg?x=1&width=1280&height=1280&fallback=original"
    )
    assert not is_cloudflare_imgbed_url("https://img.example.com/uploads/a.jpg")
    assert build_scaled_url("https://img.example.com/uploads/a.jpg?x=1", 1280) == (
        "https://img.example.com/uploads/a.jpg?x=1"
    )


def test_scaled_url_keeps_special_chars_quoted_in_every_style():
    """文件名含空格/中文时，重建的 URL 必须保持转义（回归测试）。"""
    url = "https://img.example.com/file/2026/%E9%A3%8E%E6%99%AF%201.jpg?x=1"
    assert build_scaled_url(url, 1280) == (
        "https://img.example.com/file/2026/%E9%A3%8E%E6%99%AF%201.jpg"
        "?x=1&width=1280&height=1280&fallback=original"
    )
    assert (
        build_original_url("https://img.example.com/file/2026/%E9%A3%8E%E6%99%AF%201.jpg?width=1")
        == "https://img.example.com/file/2026/%E9%A3%8E%E6%99%AF%201.jpg"
    )


def test_scaled_url_cf_path_style_builds_and_roundtrips():
    """cf-path 风格：参数嵌在路径里，build_original_url 能还原回 /file/ 直链。"""
    url = "https://img.example.com/file/a%20b.jpg?x=1"
    scaled = build_scaled_url(url, 1280, quality=80, style="cf-path")
    assert scaled == (
        "https://img.example.com"
        "/cdn-cgi/image/width=1280,height=1280,quality=80,fit=scale-down/file/a%20b.jpg?x=1"
    )
    # cf-path 也算 ImgBed 直链（源文件在 /file/ 下），且能还原
    assert is_cloudflare_imgbed_url(scaled)
    assert build_original_url(scaled) == "https://img.example.com/file/a%20b.jpg?x=1"
    # query 风格构建的 URL 也能被 build_original_url 还原（剥离缩放参数）
    assert build_original_url(build_scaled_url(url, 1280)) == url
    # 非 ImgBed URL 两种风格都原样返回
    assert build_scaled_url("https://other.example/a.jpg", 1280, style="cf-path") == (
        "https://other.example/a.jpg"
    )


def test_prefer_url_direct_only_for_known_small_parseable():
    small = {"url": "https://img.example.com/file/a.jpg", "size_bytes": 100 * 1024}
    assert prefer_url_direct(small) is True
    # 体积未知/为零：不跳下载，万一是大图会绕过压缩
    assert prefer_url_direct({"url": "https://img.example.com/file/a.jpg", "size_bytes": 0}) is False
    assert prefer_url_direct({"url": "https://img.example.com/file/a.jpg"}) is False
    # 已知大图：必须下载压缩
    assert (
        prefer_url_direct({"url": "https://img.example.com/file/a.jpg", "size_bytes": 5 * 1024 * 1024})
        is False
    )
    # 协议端解析不了宽高的扩展名：URL 段体验差，走字节
    assert (
        prefer_url_direct({"url": "https://img.example.com/file/a.avif", "size_bytes": 100 * 1024})
        is False
    )
    # 无 URL（本地 path 块）谈不上直传
    assert prefer_url_direct({"size_bytes": 100}) is False


def test_prefer_inline_bytes_decision_matrix():
    url = "https://img.example.com/file/a.jpg"
    # 压缩成功的字节必须内联——local-compress 的意义所在
    assert prefer_inline_bytes(9 * 1024 * 1024, True, url) is True
    # 未压缩但 ≤ 上限：字节已在手，内联省一次协议端公网下载
    assert prefer_inline_bytes(MAX_INLINE_IMAGE_BYTES, False, url) is True
    # 未压缩且超上限（巨型动图）：URL 段，避免撑爆消息
    assert prefer_inline_bytes(MAX_INLINE_IMAGE_BYTES + 1, False, url) is False
    # URL 不可用（无 URL / 解析不了宽高）时无论多大都只能内联
    assert prefer_inline_bytes(MAX_INLINE_IMAGE_BYTES + 1, False, "") is True
    assert prefer_inline_bytes(MAX_INLINE_IMAGE_BYTES + 1, False, "https://img.example.com/file/a.avif") is True


def test_napcat_parseable_url_is_extension_based():
    assert is_napcat_parseable_url("https://img.example.com/file/a.jpg?x=1")
    assert not is_napcat_parseable_url("https://img.example.com/file/a.avif")


def test_prepare_image_scales_and_converts_to_jpeg():
    image = Image.effect_noise((2500, 1000), 64).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    result = prepare_image_bytes(buffer.getvalue(), 1920, 85)

    assert result is not None
    data, compressed = result
    assert compressed
    with Image.open(io.BytesIO(data)) as output:
        assert output.format == "JPEG"
        assert output.size == (1920, 768)


def test_prepare_image_gives_transparent_png_white_background():
    image = Image.effect_noise((1200, 1200), 64).convert("RGBA")
    alpha = Image.new("L", image.size, 255)
    alpha.paste(Image.new("L", (1200, 600), 0), (0, 600))
    image.putalpha(alpha)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    result = prepare_image_bytes(buffer.getvalue(), 1920, 85)
    assert result is not None
    data, compressed = result
    assert compressed
    with Image.open(io.BytesIO(data)) as output:
        assert output.format == "JPEG"
        assert output.getpixel((600, 900)) == (255, 255, 255)


def test_prepare_image_keeps_animated_gif_untouched():
    frames = [Image.new("RGB", (80, 80), (i * 80, 10, 10)) for i in range(3)]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", append_images=frames[1:], duration=100, loop=0)
    data = buffer.getvalue()

    assert prepare_image_bytes(data, 1920, 85) == (data, False)


def _noise_png(size=(2500, 1000)) -> bytes:
    """纯噪声图：体积大，用于验证「不动图、走格式转换」的路径。"""
    buffer = io.BytesIO()
    Image.effect_noise(size, 64).convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def _photo_png(size=(1400, 1000), seed=1234, noise=12) -> bytes:
    """接近实拍的合成图（渐变 + 噪声）。

    纯噪声是压缩算法的最坏情况（WebP 甚至可能比 JPEG 更大），不适合用来断言
    「谁更小」；真实照片的压缩特性要靠这种图才能体现。noise=0 得到平滑渐变，
    用于比较两种编码器的体积优势。
    """
    import numpy as np

    width, height = size
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width]
    base = (xx / width * 200 + yy / height * 55).astype("float32")
    arr = np.stack(
        [
            base + 40 * np.sin(xx / 120.0),
            base * 0.9 + 35 * np.cos(yy / 90.0),
            base * 0.8 + 30 * np.sin((xx + yy) / 200.0),
        ],
        axis=-1,
    )
    if noise:
        arr += rng.normal(0, noise, arr.shape)
    image = Image.fromarray(np.clip(arr, 0, 255).astype("uint8"), "RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_prepare_image_quality_ladder_stops_once_target_is_met():
    """设了目标体积后逐档降质量，达标即停（用同一编码器逐档结果作对照）。"""
    data = _photo_png()
    first = prepare_image_bytes(data, 1920, 85)
    second = prepare_image_bytes(data, 1920, 75)
    assert first is not None and second is not None
    assert len(second[0]) < len(first[0]), "同一张图降质量应更小（对照前提）"

    laddered = prepare_image_bytes(data, 1920, 85, target_bytes=len(second[0]))

    assert laddered is not None
    assert len(laddered[0]) <= len(second[0]), "第二档已达标，不该再多编几次"
    assert laddered[0] == second[0], "达标档的产物应与该质量单独编码一致"


def test_prepare_image_quality_ladder_walks_down_to_the_floor():
    """目标不可达成时走满阶梯（85→75→65），最终产物等于最低那档。"""
    data = _photo_png()
    laddered = prepare_image_bytes(data, 1920, 85, target_bytes=1)

    assert laddered is not None
    assert laddered[0] == prepare_image_bytes(data, 1920, 65)[0]


def test_prepare_image_target_bytes_zero_keeps_single_pass():
    """target_bytes=0（默认）时不启用阶梯：与不带该参数的结果完全一致。"""
    data = _photo_png()
    assert prepare_image_bytes(data, 1920, 85) == prepare_image_bytes(
        data, 1920, 85, target_bytes=0
    )


def test_prepare_image_webp_output_is_smaller_and_decodable():
    """平滑内容下 WebP 明显更小；噪声极多时反而可能更大——体积优势与内容相关。"""
    smooth = _photo_png((1600, 1200), noise=0)
    jpeg = prepare_image_bytes(smooth, 1920, 80)
    webp = prepare_image_bytes(smooth, 1920, 80, use_webp=True)
    assert jpeg is not None and webp is not None
    with Image.open(io.BytesIO(webp[0])) as output:
        assert output.format == "WEBP"
    assert len(webp[0]) < len(jpeg[0]), "平滑内容下 WebP 应比同质量 JPEG 更小"

    # 噪声极多的图两种编码器都不友好，WebP 不保证更小；但产物必须仍可解码
    noisy = _noise_png((1600, 1200))
    noisy_webp = prepare_image_bytes(noisy, 1920, 80, use_webp=True)
    assert noisy_webp is not None
    with Image.open(io.BytesIO(noisy_webp[0])) as output:
        assert output.format in {"WEBP", "PNG"}


def test_prepare_image_webp_still_keeps_animated_gif_untouched():
    """WebP 选项不得改变动图原样返回的既有语义。"""
    frames = [Image.new("RGB", (80, 80), (i * 80, 10, 10)) for i in range(3)]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", append_images=frames[1:], duration=100, loop=0)
    data = buffer.getvalue()

    assert prepare_image_bytes(data, 1920, 85, use_webp=True) == (data, False)


def test_prepare_image_bakes_exif_orientation():
    noise = Image.effect_noise((2000, 1000), 64).convert("RGB")
    green = Image.blend(
        noise.crop((0, 0, 1000, 1000)),
        Image.new("RGB", (1000, 1000), (0, 255, 0)),
        0.6,
    )
    red = Image.blend(
        noise.crop((1000, 0, 2000, 1000)),
        Image.new("RGB", (1000, 1000), (255, 0, 0)),
        0.6,
    )
    image = Image.new("RGB", (2000, 1000))
    image.paste(green, (0, 0))
    image.paste(red, (1000, 0))
    exif = Image.Exif()
    exif[274] = 6
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95, exif=exif)

    result = prepare_image_bytes(buffer.getvalue(), 1920, 85)
    assert result is not None
    data, compressed = result
    assert compressed
    with Image.open(io.BytesIO(data)) as output:
        assert output.size == (960, 1920)
        top = output.getpixel((480, 100))
        bottom = output.getpixel((480, 1800))
        assert top[1] > top[0]
        assert bottom[0] > bottom[1]


def test_deduplicate_vector_hits_by_exact_key_and_similarity():
    hits = [
        {"id": "1", "score": 0.91, "vector": [1.0, 0.0], "payload": {"src": "/file/a"}},
        {"id": "2", "score": 0.90, "vector": [0.0, 1.0], "payload": {"image_url": "https://b/file/x"}},
        {"id": "3", "score": 0.89, "vector": [1.0, 0.0], "payload": {"image_url": "https://a/file/a?x=1"}},
        {"id": "4", "score": 0.88, "vector": [-1.0, 0.0], "payload": {"image_url": "https://a/file/b"}},
    ]
    result = deduplicate_vector_hits(hits, threshold=0.995)

    assert [hit["id"] for hit in result] == ["1", "2", "4"]
    assert [hit["duplicate_count"] for hit in result] == [2, 1, 1]
    assert [hit["id"] for hit in result[0]["duplicates"]] == ["3"]
    assert result[1]["duplicates"] == []


def test_deduplicate_vector_hits_without_vectors_only_uses_exact_keys():
    hits = [
        {"id": "1", "score": 0.91, "payload": {"image_url": "https://A.example/file/a"}},
        {"id": "2", "score": 0.90, "payload": {"image_url": "https://a.example:443/file/a"}},
        {"id": "3", "score": 0.89, "payload": {"image_url": "https://a.example/file/b?a=2&z=1"}},
        {"id": "4", "score": 0.88, "payload": {"image_url": "https://a.example/file/b?z=1&a=2"}},
    ]
    result = deduplicate_vector_hits(hits)
    assert [hit["id"] for hit in result] == ["1", "3"]
    assert result[0]["duplicate_count"] == 2
    assert result[1]["duplicate_count"] == 2


def test_build_original_url_strips_imgbed_processing_params():
    url = "https://img.example.com/file/a%20b.jpg?width=1280&height=1280&fallback=original&v=2"
    assert build_original_url(url) == "https://img.example.com/file/a%20b.jpg?v=2"
    assert (
        build_original_url("https://img.example.com/file/a.jpg")
        == "https://img.example.com/file/a.jpg"
    )


def test_build_original_url_passes_through_non_imgbed_links():
    assert (
        build_original_url("https://img.example.com/uploads/a.jpg?width=1280")
        == "https://img.example.com/uploads/a.jpg?width=1280"
    )
    assert (
        build_original_url("https://cdn.example.com/a.jpg?w=1")
        == "https://cdn.example.com/a.jpg?w=1"
    )


def test_upgrade_to_https_only_for_same_domain_http_links():
    assert (
        upgrade_to_https("http://img.example.com/file/a.jpg?x=1", "https://img.example.com")
        == "https://img.example.com/file/a.jpg?x=1"
    )
    assert (
        upgrade_to_https("http://other.example/file/a.jpg", "https://img.example.com")
        == "http://other.example/file/a.jpg"
    )
    assert (
        upgrade_to_https("http://img.example.com/file/a.jpg", "http://img.example.com")
        == "http://img.example.com/file/a.jpg"
    )
    assert (
        upgrade_to_https("https://img.example.com/file/a.jpg", "https://img.example.com")
        == "https://img.example.com/file/a.jpg"
    )


def test_is_scaling_needed_distinguishes_unknown_from_not_needed():
    assert is_scaling_needed((4000, 3000), 1920) is True
    assert is_scaling_needed((800, 600), 1920) is False
    # 尺寸未知必须是 None（说不准），不能当成 False（确定不用缩放）
    assert is_scaling_needed(None, 1920) is None
    assert is_scaling_needed((None, 600), 1920) is None
    assert is_scaling_needed((0, 0), 1920) is None


def test_compression_evidence_prefers_measurement_over_size_guess():
    """三态证据：以实测为准，没有实测时只能确定「不超限 = 原图」。"""
    assert compression_evidence((4000, 3000), 1920) is CompressionEvidence.UNKNOWN
    assert compression_evidence((800, 600), 1920) is CompressionEvidence.ORIGINAL
    assert compression_evidence(None, 1920) is CompressionEvidence.UNKNOWN

    # 有实测就据实：缩放版真的更小才算压缩，否则就是原图
    assert (
        compression_evidence((4000, 3000), 1920, verified_smaller=True)
        is CompressionEvidence.COMPRESSED
    )
    assert (
        compression_evidence((4000, 3000), 1920, verified_smaller=False)
        is CompressionEvidence.ORIGINAL
    )
    assert (
        compression_evidence((800, 600), 1920, verified_smaller=False)
        is CompressionEvidence.ORIGINAL
    )


class ActionFailed(Exception):
    """类名与 aiocqhttp 的 ActionFailed 完全一致，异常分类正是按类名识别的。"""


class _WithRetcode(Exception):
    def __init__(self):
        super().__init__("bad")
        self.retcode = 1200


def test_classify_send_error_never_retries_on_timeout():
    """超时是「结果未知」：重发等于把同一张图发两遍，必须禁止。"""
    assert classify_send_error(asyncio.TimeoutError()) is SendOutcome.UNKNOWN
    assert classify_send_error(TimeoutError()) is SendOutcome.UNKNOWN
    assert classify_send_error(RuntimeError("unexpected")) is SendOutcome.UNKNOWN


def test_classify_send_error_allows_retry_on_explicit_rejection():
    assert classify_send_error(ActionFailed()) is SendOutcome.FAILED
    assert classify_send_error(_WithRetcode()) is SendOutcome.FAILED
    assert classify_send_error(RuntimeError("WebSocket is not connected")) is SendOutcome.FAILED


class _NapCatSendTimeout(Exception):
    """NapCat 实测形态：sendMsg 等客户端回调超时，被包成带 retcode 的失败。

    这类异常类名像「明确失败」、retcode 非 0 也像「明确失败」，语义却是结果
    未知——分类必须让语义压过类型名与 retcode，否则会触发回退把图发两遍。
    """

    def __init__(self):
        super().__init__("sendMsg timeout")
        self.retcode = 1200
        self.message = (
            "<ActionFailed status='failed', retcode=1200, data=None, message='Timeout: NTEvent "
            "serviceAndMethod:NodeIKernelMsgService/sendMsg "
            "ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate EventRet:\n{}\n', "
            "echo={'seq': 234}, stream='normal-action'>"
        )
        self.echo = {"seq": 234}


def test_send_diagnosis_names_the_cause_instead_of_leaving_a_mystery():
    """日志要给结论与成因，不能只写「结果未知」让人无从下手。

    超时是**预期内**现象（协议端等待客户端上传回调），必须讲清「请求已发出、
    未拿到回执、按不重发处理」以及排查方向，避免每次都被当成故障上报。
    """
    text = send_diagnosis(_NapCatSendTimeout())
    assert "未确认" in text, "要说明「没拿到回执」而不是含糊的未知"
    assert "不重发" in text, "必须讲清处置方式：不重发以免重复发图"
    assert "retcode=1200" in text, "带上原始 retcode 供排查"
    assert "NapCat" in text, "给出可操作的升级/排查方向"
    # 分类结果不能因此改变：仍是 UNKNOWN，绝不触发回退
    assert classify_send_error(_NapCatSendTimeout()) is SendOutcome.UNKNOWN
    assert send_outcome_reason(_NapCatSendTimeout()) == SEND_REASON_TIMEOUT


def test_send_diagnosis_distinguishes_disconnect_from_rejection():
    """「连接没建起来」与「协议端拒绝」排查方向完全不同，日志要分开。"""
    disconnected = send_diagnosis(RuntimeError("WebSocket is not connected"))
    assert "连接不可用" in disconnected
    assert send_outcome_reason(RuntimeError("WebSocket is not connected")) == (
        SEND_REASON_DISCONNECTED
    )

    rejected = send_diagnosis(ActionFailed("unsupported file type: undefined"))
    assert "协议端明确拒绝" in rejected
    assert send_outcome_reason(ActionFailed("unsupported file type")) == (
        SEND_REASON_PROTOCOL_REJECT
    )


def test_send_diagnosis_falls_back_to_conservative_wording():
    """认不出成因时保守处理：说清「不重发」并带上异常类型，不假装看懂了。"""
    exc = RuntimeError("unexpected")
    text = send_diagnosis(exc)
    assert "未确认" in text and "不重发" in text
    assert "RuntimeError" in text
    assert send_outcome_reason(exc) == SEND_REASON_UNCLASSIFIED
    # 无异常时代表成功，不算未确认
    assert send_outcome_reason(None) == SEND_REASON_SENT


def test_build_imgbed_file_url_quotes_names_and_rejects_escapes():
    assert (
        build_imgbed_file_url("https://bed.example.com", "sunset.jpg")
        == "https://bed.example.com/file/sunset.jpg"
    )
    # 图床里可能带上传目录，允许保留 /；中文与空格要按 URL 规则转义
    assert (
        build_imgbed_file_url("https://bed.example.com/", "2026/09/风景 1.jpg")
        == "https://bed.example.com/file/2026/09/%E9%A3%8E%E6%99%AF%201.jpg"
    )
    # 已经转义过的名字不重复转义
    assert (
        build_imgbed_file_url("https://bed.example.com", "a%20b.jpg")
        == "https://bed.example.com/file/a%20b.jpg"
    )

    assert build_imgbed_file_url("https://bed.example.com", "") is None
    assert build_imgbed_file_url("", "a.jpg") is None
    assert build_imgbed_file_url("https://bed.example.com", "/a.jpg") == (
        "https://bed.example.com/file/a.jpg"
    )
    # 越权与穿越：绝对 URL、上级目录、空路径段一律拒绝
    assert build_imgbed_file_url("https://bed.example.com", "https://evil/a.jpg") is None
    assert build_imgbed_file_url("https://bed.example.com", "../../etc/passwd") is None
    assert build_imgbed_file_url("https://bed.example.com", "a//b.jpg") is None
    # 百分号编码的穿越必须在校验前解码，否则 %2e%2e 会绕过字面 ".."
    assert (
        build_imgbed_file_url("https://bed.example.com", "%2e%2e/%2e%2e/etc/passwd")
        is None
    )
    assert build_imgbed_file_url("https://bed.example.com", "%2e%2e%2Fsecret") is None
    assert build_imgbed_file_url("https://bed.example.com", "a/%2e%2e/b.jpg") is None
    assert build_imgbed_file_url("https://bed.example.com", "a\\b.jpg") is None
    # base 不是 http(s) 时拼不出合法 ImgBed 直链
    assert build_imgbed_file_url("ftp://bed.example.com", "a.jpg") is None


def test_probe_helpers_classify_status_and_length():
    assert classify_probe_status(200) is True
    assert classify_probe_status(206) is True
    # 只有 404/410 才算「确定不存在」
    assert classify_probe_status(404) is False
    assert classify_probe_status(410) is False
    # 403（防盗链/访问规则）、429（限流）、405（方法不允许）说明不了存在性，
    # 判成「不存在」会让 /原图 在文件确实存在时误报未找到
    assert classify_probe_status(403) is None
    assert classify_probe_status(429) is None
    assert classify_probe_status(405) is None
    assert classify_probe_status(503) is None
    assert classify_probe_status(None) is None

    assert content_length_from_headers({"Content-Length": "1234"}) == 1234
    assert content_length_from_headers({"content-length": "12"}) == 12
    assert content_length_from_headers({"Content-Range": "bytes 0-0/98765"}) == 98765
    # 拿不到长度必须返回 None，绝不能让调用方把 0 当成长度
    assert content_length_from_headers({}) is None
    assert content_length_from_headers(None) is None
    assert content_length_from_headers({"Content-Range": "bytes 0-0/*"}) is None


def test_scaling_capability_rejection_covers_api_level_refusals():
    """405（缩放请求只接受 GET）、501（未配置图片处理器）、400（参数组合非法）
    都属于「这个部署处理不了缩放」，据此进入冷却期，不再反复探测。"""
    assert is_scaling_capability_rejection(405)
    assert is_scaling_capability_rejection(501)
    assert is_scaling_capability_rejection(400)
    assert not is_scaling_capability_rejection(403)  # 可能只是防盗链
    assert not is_scaling_capability_rejection(404)
    assert not is_scaling_capability_rejection(200)
    assert not is_scaling_capability_rejection(None)


def test_sniff_image_format_recognizes_heif_brands():
    assert sniff_image_format(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40) == "HEIF"
    assert sniff_image_format(b"\x00\x00\x00\x18ftypmif1" + b"\x00" * 40) == "HEIF"
    assert sniff_image_format(b"\x00\x00\x00\x18ftypavif" + b"\x00" * 40) == "AVIF"


def test_qq_upload_safe_image_whitelists_documented_formats_only():
    """官方文档展示口径：jpg/png/gif/webp/bmp 放行（webp 动图免转码），
    HEIF/AVIF 等清单外格式拒绝（会被打回富媒体文件格式不支持）。"""
    assert qq_upload_safe_image(b"\xff\xd8\xff\xe0" + b"\x00" * 12)
    assert qq_upload_safe_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    assert qq_upload_safe_image(b"GIF89a" + b"\x00" * 10)
    assert qq_upload_safe_image(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8)
    assert qq_upload_safe_image(b"BM" + b"\x00" * 14)
    assert not qq_upload_safe_image(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20)
    assert not qq_upload_safe_image(b"\x00\x00\x00\x20ftypavif" + b"\x00" * 20)
    assert not qq_upload_safe_image(b"")
