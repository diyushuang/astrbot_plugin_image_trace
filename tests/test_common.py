from urllib.parse import urlparse

from astrbot_plugin_image_trace.common import is_qq_image_bed_host, sanitize_error_text
from astrbot_plugin_image_trace.url_guard import prepare_headers


def test_qq_image_bed_host_matches_exact_and_real_subdomains():
    assert is_qq_image_bed_host("multimedia.nt.qq.com.cn")
    assert is_qq_image_bed_host("gchat.qpic.cn")
    assert is_qq_image_bed_host("MEDIA.GCHAT.QPIC.CN")
    assert is_qq_image_bed_host("multimedia.nt.qq.com.cn.")


def test_qq_image_bed_host_rejects_forged_suffixes_and_other_hosts():
    assert not is_qq_image_bed_host("eviltqpic.cn")
    assert not is_qq_image_bed_host("evil.nt.qq.com.cn.example.com")
    assert not is_qq_image_bed_host("qq.com.cn")
    assert not is_qq_image_bed_host("")


def test_referer_is_reevaluated_for_each_redirect_hop():
    def prepare(url, _method, _cross_origin):
        headers = {}
        if is_qq_image_bed_host(urlparse(url).hostname or ""):
            headers["Referer"] = "https://gchat.qpic.cn/"
        return {"headers": headers}

    qq_request = prepare_headers(
        prepare, "https://multimedia.nt.qq.com.cn/image", "GET", cross_origin=False
    )
    external_redirect = prepare_headers(
        prepare, "https://example.com/image", "GET", cross_origin=True
    )
    assert qq_request["headers"]["Referer"] == "https://gchat.qpic.cn/"
    assert "Referer" not in external_redirect["headers"]


def test_sanitize_error_text_hides_urls_and_ipv4():
    text = "请求失败: http://10.0.0.5:7658/random?token=secret 172.20.0.1:8080"
    safe = sanitize_error_text(text, limit=80)

    assert "10.0.0.5" not in safe
    assert "172.20.0.1" not in safe
    assert "secret" not in safe
    assert "<地址已隐藏>" in safe


def test_sanitize_error_text_truncates_and_defaults():
    assert sanitize_error_text("") == "未知错误（详情见机器人日志）"
    assert sanitize_error_text("x" * 300, limit=20).endswith("…")
