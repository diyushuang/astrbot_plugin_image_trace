import pytest

from astrbot_plugin_image_trace import random_media
from astrbot_plugin_image_trace.random_media import (
    CONTENT_TYPES,
    RandomMediaClient,
    RandomMediaError,
    build_random_api_url,
    extract_directory,
    media_filename,
    media_kind,
    parse_random_response,
    resolve_media_url,
    swap_url_origin,
)


class FakeResponse:
    """aiohttp 响应的最小替身：只保留 fetch 用到的属性。"""

    def __init__(self, *, status=200, body="", headers=None, url="https://img.example.com/random"):
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None


class FakeHttpClient:
    """GuardedHttpClient 替身：按顺序弹出预设响应并记录请求参数。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def request(self, method, url, *, prepare=None, timeout=None):
        self.calls.append({"method": method, "url": url, "prepare": prepare, "timeout": timeout})
        return self._responses.pop(0)


class FakeHttpGetter:
    def __init__(self, client):
        self._client = client
        self.called = 0

    async def __call__(self):
        self.called += 1
        return self._client


def _settings(**overrides):
    """模拟 main._random_settings() 归一化后的配置 dict。"""
    settings = {
        "base_url": "https://img.example.com",
        "api_endpoint": "/random",
        "api_token": "",
        "default_dir": "",
        "timeout": 5.0,
        "retry_count": 0,
    }
    settings.update(overrides)
    return settings


def test_build_random_api_url_includes_directory_and_content():
    url = build_random_api_url(
        "https://img.example.com/", "/random", directory="风景/2026", content_type="IMAGE"
    )
    assert url == (
        "https://img.example.com/random?type=url&form=json"
        "&dir=%E9%A3%8E%E6%99%AF%2F2026&content=image"
    )


def test_build_random_api_url_without_directory_or_content():
    url = build_random_api_url(
        "https://img.example.com", "/random", directory="", content_type=None
    )
    assert url == "https://img.example.com/random?type=url&form=json"


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "ftp://img.example.com",
        "https://user:pass@img.example.com",
        "https://img.example.com?a=1",
        "https://img.example.com#frag",
    ],
)
def test_build_random_api_url_rejects_invalid_domain(base_url):
    with pytest.raises(RandomMediaError):
        build_random_api_url(base_url, "/random", directory="", content_type="image")


def test_build_random_api_url_rejects_absolute_endpoint():
    with pytest.raises(RandomMediaError):
        build_random_api_url(
            "https://img.example.com",
            "https://other.example/random",
            directory="",
            content_type=None,
        )


def test_build_random_api_url_rejects_endpoint_query_or_fragment():
    """endpoint 自身带 ?/# 会与我们拼的查询串叠加成非法 URL。"""
    for endpoint in ("/random?x=1", "/random#frag"):
        with pytest.raises(RandomMediaError):
            build_random_api_url(
                "https://img.example.com",
                endpoint,
                directory="",
                content_type="image",
            )


def test_build_random_api_url_rejects_unknown_content_type():
    with pytest.raises(RandomMediaError):
        build_random_api_url(
            "https://img.example.com", "/random", directory="", content_type="audio"
        )


def test_parse_random_response_json_shapes_and_plain_text():
    assert (
        parse_random_response('{"url":"/file/a.jpg"}', "https://s/random", "application/json")
        == "https://s/file/a.jpg"
    )
    assert (
        parse_random_response(
            '{"data":{"url":"https://cdn/x.png"}}', "https://s/random", "application/json"
        )
        == "https://cdn/x.png"
    )
    assert (
        parse_random_response("/file/t.jpg", "https://s/random", "text/plain")
        == "https://s/file/t.jpg"
    )


def test_parse_random_response_direct_media_uses_response_url():
    assert (
        parse_random_response("", "https://s/file/v.mp4", "video/mp4; charset=utf-8")
        == "https://s/file/v.mp4"
    )
    assert parse_random_response("ignored", "https://s/file/p.jpg", "image/jpeg") == (
        "https://s/file/p.jpg"
    )


def test_parse_random_response_rejects_unparsable_or_unsafe_targets():
    assert parse_random_response("", "https://s/random", "application/json") is None
    assert parse_random_response('{"data":{}}', "https://s/random", "application/json") is None
    assert parse_random_response("file:///etc/passwd", "https://s/random", "text/plain") is None
    assert parse_random_response("https://u:p@h/a.jpg", "https://s/random", "text/plain") is None


def test_resolve_media_url_handles_relative_and_rejects_bad_scheme():
    assert resolve_media_url("a/b.jpg", "https://s/random") == "https://s/a/b.jpg"
    assert resolve_media_url(123, "https://s/random") is None
    assert resolve_media_url("   ", "https://s/random") is None
    assert resolve_media_url("data:image/png;base64,AAA", "https://s/random") is None


def test_parse_random_response_rejects_error_text_and_html():
    """图床出错时同样回 200 + 纯文本，错误页不能经 urljoin 变成"合法"直链。

    没有这道闸时 "server is busy" 会变成 https://s/server is busy，被当作
    图片直链发进群：用户看到破图，日志里也没有可诊断的失败。
    """
    assert parse_random_response("server is busy", "https://s/random", "text/plain") is None
    assert (
        parse_random_response(
            "<!DOCTYPE html><html>500</html>", "https://s/random", "text/plain"
        )
        is None
    )
    assert parse_random_response("busy", "https://s/random", "text/plain") is None
    assert parse_random_response("No such file or directory", "https://s/random", "text/plain") is None
    assert (
        parse_random_response('{"detail":"not found"}', "https://s/random", "application/json")
        is None
    )
    assert parse_random_response("/file/a.jpg extra", "https://s/random", "text/plain") is None


def test_parse_random_response_accepts_documented_reference_shapes():
    """三种合法形态都要放行：完整链接、站点路径（含无扩展名的床内文件）、床内相对路径。"""
    cases = {
        "/file/ok.jpg": "https://s/file/ok.jpg",
        "file/ok.jpg": "https://s/file/ok.jpg",
        "file/x.mp4": "https://s/file/x.mp4",
        "https://cdn.example.com/a/b.webp": "https://cdn.example.com/a/b.webp",
        "http://s/file/a.PNG": "http://s/file/a.PNG",
        "/file/noext": "https://s/file/noext",
    }
    for body, expected in cases.items():
        assert parse_random_response(body, "https://s/random", "text/plain") == expected
    assert (
        parse_random_response(
            '{"url":"/file/ok.jpg?width=640&fallback=original"}',
            "https://s/random",
            "application/json",
        )
        == "https://s/file/ok.jpg?width=640&fallback=original"
    )


def test_media_filename_and_media_kind():
    assert media_filename("https://s/file/a%20b.jpg") == "a b.jpg"
    assert media_filename("https://s/file/noext") is None
    assert media_kind("https://s/file/x.JPG") == "image"
    assert media_kind("https://s/file/x.mp4") == "video"
    assert media_kind("https://s/file/x", "video") == "video"
    assert media_kind("https://s/file/x") is None


def test_is_thumbnail_url_matches_only_whole_path_segment():
    """判据是路径里存在 `thumbnails` 整段，不能被子串碰瓷。

    误判方向也要防：`mythumbnails/`、`thumbnails-old/` 这类目录名若被判成
    缩略图，会让正常的原图直查被改走反查通路，白白多一次 Qdrant 请求。
    """
    assert random_media.is_thumbnail_url("http://s/file/thumbnails/1_x.jpg")
    assert random_media.is_thumbnail_url("http://s/file/thumbnails/abc/1_x.jpg")
    # 编码过的中文目录不影响判定（路径先 unquote）
    assert random_media.is_thumbnail_url("http://s/file/thumbnails/%E4%B8%AD/1_x.jpg")
    assert not random_media.is_thumbnail_url("http://s/file/%E4%B8%AD/1_x.jpg")
    assert not random_media.is_thumbnail_url("http://s/file/mythumbnails/1_x.jpg")
    assert not random_media.is_thumbnail_url("http://s/file/thumbnails-old/1_x.jpg")
    assert not random_media.is_thumbnail_url("")
    assert not random_media.is_thumbnail_url(None)


def test_strip_thumbnail_prefix_only_removes_leading_timestamp():
    """只剥起始处的 `数字_` 前缀——这是图床改名的确定形态，其余一律不动。

    实测依据：图床 DB 里 23,932 条缩略图的 id 末段与 file_name 列逐条比对，
    改名规则 = 前置 `{毫秒时间戳}_` + 把 `@` 换成 `_`。
    `@`→`_` 不可逆，故清洗**只做去前缀**这一步，不尝试还原 `@`（那是猜）。
    """
    assert (
        random_media.strip_thumbnail_prefix("1789658616018_【微博_赵今麦】20200228-01.jpg")
        == "【微博_赵今麦】20200228-01.jpg"
    )
    # 非前缀形态的数字开头不得被误删（中间/末尾的数字属于文件名本身）
    assert (
        random_media.strip_thumbnail_prefix("20200228-01：剧照.jpg") == "20200228-01：剧照.jpg"
    )
    assert random_media.strip_thumbnail_prefix("a_1789658616018_x.jpg") == "a_1789658616018_x.jpg"
    # 位数不足（像普通编号）不剥
    assert random_media.strip_thumbnail_prefix("123_x.jpg") == "123_x.jpg"
    assert random_media.strip_thumbnail_prefix("") == ""


def test_clean_display_name_accepts_urls_and_bare_names():
    """配文/展示用名统一走这里：去前缀，但不动其余内容。

    普通原图名（无前缀）必须原样返回——清洗对它是空操作，因此可以无条件调用。
    """
    assert (
        random_media.clean_display_name("http://s/file/thumbnails/1789658616018_x.jpg")
        == "x.jpg"
    )
    assert random_media.clean_display_name("http://s/file/x.jpg") == "x.jpg"
    assert random_media.clean_display_name("x.jpg") == "x.jpg"
    assert random_media.clean_display_name("1789658616018_x.jpg") == "x.jpg"
    assert random_media.clean_display_name("") is None
    assert random_media.clean_display_name(None) is None


def test_clean_display_name_strips_decoration_prefix_from_own_caption():
    """配文前缀必须被剥掉——这是 1.6.1 修线上 404 的核心。

    触发链（线上实测）：
        发图时配文是 `🖼️ {name}（原图）` → 用户引用那条消息发 /原图
        → 引用段里取到的名字**自带 `🖼️ ` 前缀**
        → 拼成 `/file/🖼️%20【抖音…】` → 404
        → 提示文案又把带前缀的名字复述一遍，用户看不出问题在哪儿

    报错回显的直链里带着 `%F0%9F%96%BC%EF%B8%8F%20`（= `🖼️ `），即本用例第一行。
    不剥就是死循环：名字带前缀 → 查不到 → 提示里再带一次前缀。
    """
    assert (
        random_media.clean_display_name("🖼️ 【抖音@赵今麦工作室】20251224-06：花絮【live】.jpeg")
        == "【抖音@赵今麦工作室】20251224-06：花絮【live】.jpeg"
    )
    assert random_media.clean_display_name("🎬 视频.mp4") == "视频.mp4"
    # 多图回传的序号前缀（main._block_caption 拼的是 `{n}. {caption}`）
    assert random_media.clean_display_name("1. 图.jpg") == "图.jpg"
    assert random_media.clean_display_name("12. 图.jpg") == "图.jpg"
    # 前缀叠加：`1. 🖼️ 名字` 与 `🖼️ 1. 名字` 都要剥干净（循环剥的意义）
    assert random_media.clean_display_name("1. 🖼️ 图.jpg") == "图.jpg"
    assert random_media.clean_display_name("🖼️ 1. 图.jpg") == "图.jpg"


def test_clean_display_name_strips_decoration_and_timestamp_together():
    """装饰前缀与时间戳前缀同时存在时必须两层都剥（顺序：先装饰后时间戳）。

    装饰前缀挡在最前面，不先剥掉它，时间戳就露不出来——这正是旧版只做
    `strip_thumbnail_prefix` 时的失效路径。
    """
    assert (
        random_media.clean_display_name("🖼️ 1789658616018_【微博_赵】a.jpg") == "【微博_赵】a.jpg"
    )
    assert (
        random_media.clean_display_name("1. 🖼️ 1789658616018_【微博_赵】a.jpg")
        == "【微博_赵】a.jpg"
    )


def test_clean_display_name_keeps_legitimate_names_intact():
    """安全闸：不得把合法文件名削坏，也不得剥成残骸。

    两类必须守住：
      1. 文件名主体的中文标点（`【`/`】`）与内部数字编号，一个字都不能动。
         早期用 Unicode 区间匹配图标时把 `【`(U+3010) 一起吃掉了，
         `【微博@X】a.jpg` 被削成 `微博@X】a.jpg` —— 比不剥更糟。
      2. 剥完只剩扩展名的残骸要拒绝：`🖼️.jpg` 的图标本身就是文件名主体。
    """
    assert random_media.clean_display_name("【微博@X】a.jpg") == "【微博@X】a.jpg"
    assert (
        random_media.clean_display_name("20251224-06：花絮【live】.jpeg")
        == "20251224-06：花絮【live】.jpeg"
    )
    # `1.2米` 的 `1.` 后不是空白 ⇒ 不是序号前缀，归属文件名本身
    assert random_media.clean_display_name("1.2米海报.jpg") == "1.2米海报.jpg"
    # 剥完只剩 `.jpg`（无主名）⇒ 拒绝这一刀，原样保留
    assert random_media.clean_display_name("🖼️.jpg") == "🖼️.jpg"
    # 时间戳在装饰之后（不是最前）⇒ 不属前缀形态，只剥装饰
    assert random_media.clean_display_name("1789658616018_🖼️ 图.jpg") == "🖼️ 图.jpg"


def test_content_types_constant_matches_imgbed_contract():
    assert frozenset({"image", "video"}) == CONTENT_TYPES


def test_extract_directory_command_and_natural_language():
    assert extract_directory("/随机图 风景") == ("风景", "image")
    assert extract_directory("/随机视频") == ("", "video")
    assert extract_directory("来张随机图 风景") == ("风景", "image")
    assert extract_directory("随机影片：2026") == ("2026", "video")
    assert extract_directory("普通聊天") == (None, None)
    assert extract_directory("") == (None, None)


async def test_fetch_returns_media_url_and_builds_request(monkeypatch):
    client = FakeHttpClient(
        [
            FakeResponse(
                status=200,
                body='{"url":"/file/rand.jpg"}',
                headers={"Content-Type": "application/json"},
                url="https://img.example.com/random",
            )
        ]
    )
    getter = FakeHttpGetter(client)

    async def fake_read(_resp, _limit=None):
        return _resp._body

    monkeypatch.setattr(random_media, "read_limited_text", fake_read)
    media = RandomMediaClient(_settings(api_token="tok"), getter)

    url = await media.fetch("风景", "image")

    assert url == "https://img.example.com/file/rand.jpg"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == (
        "https://img.example.com/random?type=url&form=json&dir=%E9%A3%8E%E6%99%AF&content=image"
    )
    # Token 以 Bearer 附加；跨域跳转时不附带
    assert call["prepare"]("https://img.example.com/random", "GET", False) == {
        "headers": {"Authorization": "Bearer tok"}
    }
    assert call["prepare"]("https://img.example.com/random", "GET", True) == {"headers": {}}


async def test_fetch_retries_then_succeeds(monkeypatch):
    client = FakeHttpClient(
        [
            FakeResponse(status=503, url="https://img.example.com/random"),
            FakeResponse(
                status=200,
                body="https://img.example.com/file/ok.png",
                headers={"Content-Type": "text/plain"},
                url="https://img.example.com/random",
            ),
        ]
    )
    getter = FakeHttpGetter(client)

    async def fake_read(_resp, _limit=None):
        return _resp._body

    class FakeAsyncio:
        TimeoutError = TimeoutError

        @staticmethod
        async def sleep(_seconds):
            return None

    monkeypatch.setattr(random_media, "read_limited_text", fake_read)
    monkeypatch.setattr(random_media, "asyncio", FakeAsyncio)
    media = RandomMediaClient(_settings(retry_count=1), getter)

    assert await media.fetch(None, "image") == "https://img.example.com/file/ok.png"
    assert len(client.calls) == 2


async def test_fetch_raises_without_retry_on_403():
    client = FakeHttpClient([FakeResponse(status=403, url="https://img.example.com/random")])
    getter = FakeHttpGetter(client)
    media = RandomMediaClient(_settings(retry_count=3), getter)
    with pytest.raises(RandomMediaError, match="403"):
        await media.fetch(None, "image")
    assert len(client.calls) == 1


async def test_fetch_requires_https_when_token_set():
    client = FakeHttpClient([])
    getter = FakeHttpGetter(client)
    media = RandomMediaClient(
        _settings(base_url="http://img.example.com", api_token="tok"), getter
    )
    with pytest.raises(RandomMediaError, match="https"):
        await media.fetch(None, "image")
    assert getter.called == 0


async def test_fetch_raises_after_exhausting_retries():
    client = FakeHttpClient([FakeResponse(status=500, url="https://img.example.com/random")])
    getter = FakeHttpGetter(client)
    media = RandomMediaClient(_settings(retry_count=0), getter)
    with pytest.raises(RandomMediaError, match="500"):
        await media.fetch(None, "image")


async def test_fetch_reports_missing_base_url():
    media = RandomMediaClient(_settings(base_url=""), FakeHttpGetter(FakeHttpClient([])))
    with pytest.raises(RandomMediaError, match="base_url"):
        await media.fetch(None, "image")


# ---------------------------------------------------------------------------
# 内网直连（lan_base_url）
# ---------------------------------------------------------------------------


def test_swap_url_origin_rewrites_only_known_hosts():
    """内网直连改写：只动「本图床」的直链，第三方主机与非 URL 原样保留。"""
    hosts = ("img.example.com", "172.20.0.1")
    lan = "http://172.20.0.1:7658"
    # 公网域名直链 → 内网源
    assert (
        swap_url_origin("http://img.example.com/file/a.jpg", lan, hosts)
        == "http://172.20.0.1:7658/file/a.jpg"
    )
    # 已是内网源的直链：改写幂等
    assert (
        swap_url_origin("http://172.20.0.1:7658/file/a.jpg", lan, hosts)
        == "http://172.20.0.1:7658/file/a.jpg"
    )
    # 路径与查询串保留
    assert (
        swap_url_origin("https://img.example.com/file/a.jpg?x=1", lan, hosts)
        == "http://172.20.0.1:7658/file/a.jpg?x=1"
    )
    # 第三方主机不改写
    assert (
        swap_url_origin("https://cdn.other.net/file/a.jpg", lan, hosts)
        == "https://cdn.other.net/file/a.jpg"
    )
    # 主机大小写不敏感
    assert (
        swap_url_origin("http://IMG.example.com/file/a.jpg", lan, hosts)
        == "http://172.20.0.1:7658/file/a.jpg"
    )
    # 非 URL / origin 非法 / 未配置内网（空 origin）：原样返回
    assert swap_url_origin("not a url", lan, hosts) == "not a url"
    assert (
        swap_url_origin("http://img.example.com/a.jpg", "", hosts)
        == "http://img.example.com/a.jpg"
    )


async def test_fetch_prefers_lan_base_url(monkeypatch):
    """配置 lan_base_url 时接口地址走内网源，公网 base_url 不再用于拼接口。"""
    client = FakeHttpClient(
        [
            FakeResponse(
                status=200,
                body='{"url":"http://172.20.0.1:7658/file/rand.jpg"}',
                headers={"Content-Type": "application/json"},
                url="http://172.20.0.1:7658/random",
            )
        ]
    )
    getter = FakeHttpGetter(client)

    async def fake_read(_resp, _limit=None):
        return _resp._body

    monkeypatch.setattr(random_media, "read_limited_text", fake_read)
    media = RandomMediaClient(
        _settings(
            base_url="https://img.example.com",
            lan_base_url="http://172.20.0.1:7658",
        ),
        getter,
    )

    assert await media.fetch(None, "image") == "http://172.20.0.1:7658/file/rand.jpg"
    assert client.calls[0]["url"].startswith("http://172.20.0.1:7658/random?")


async def test_fetch_allows_http_token_over_lan_base(monkeypatch):
    """内网直连是管理员声明的可信路径：http + Token 不再强制 https。"""
    client = FakeHttpClient(
        [
            FakeResponse(
                status=200,
                body="http://172.20.0.1:7658/file/a.jpg",
                headers={"Content-Type": "text/plain"},
                url="http://172.20.0.1:7658/random",
            )
        ]
    )
    getter = FakeHttpGetter(client)

    async def fake_read(_resp, _limit=None):
        return _resp._body

    monkeypatch.setattr(random_media, "read_limited_text", fake_read)
    media = RandomMediaClient(
        _settings(lan_base_url="http://172.20.0.1:7658", api_token="tok"), getter
    )

    assert await media.fetch(None, "image") == "http://172.20.0.1:7658/file/a.jpg"
    # Token 仍以 Bearer 附带在内网请求上
    assert client.calls[0]["prepare"]("http://172.20.0.1:7658/random", "GET", False) == {
        "headers": {"Authorization": "Bearer tok"}
    }
