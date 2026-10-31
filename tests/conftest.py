"""测试期最小 astrbot 桩与共享 fixture。

本仓库未安装 astrbot，既有测试也从不 import main.py，导致命令处理器层
（用户真实可达入口）从未被运行时验证。这里在 collection 之前把最小 astrbot
包注入 sys.modules，使 `import astrbot_plugin_image_trace.main` 可行，从而让
测试能真实驱动命令处理器（`async for ... in plugin.<命令方法>(event)`）。

桩只覆盖 main.py 实际用到的符号；不修改任何生产代码。为避免 ruff E402
（模块级 import 必须在文件顶部），main 的导入放在 make_plugin fixture 内部。
"""

from __future__ import annotations

import io
import logging
import socket
import sys
import types
from urllib.parse import urlparse

import pytest
from aiohttp import web

from astrbot_plugin_image_trace import url_guard

# ----------------------------------------------------------------------
# 1) 最小 astrbot 桩（模块级注入，先于任何 main 导入执行）
# ----------------------------------------------------------------------


def _install(name: str, **attrs):
    """注册一个模块到 sys.modules，并挂到父模块的同名属性上。"""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    parent, _, child = name.rpartition(".")
    if parent and parent in sys.modules:
        setattr(sys.modules[parent], child, module)
    return module


class _Image:
    """Image 组件替身：构造即把参数存到自身属性，便于断言回传内容。"""

    def __init__(self, *, url=None, file=None, path=None):
        self.url = url
        self.file = file
        self.path = path

    @classmethod
    def fromURL(cls, url):
        return cls(url=url)

    @classmethod
    def fromBytes(cls, data):
        return cls(file=data)

    @classmethod
    def fromFileSystem(cls, path):
        return cls(path=path)

    async def convert_to_file_path(self):
        return self.path or self.file or self.url


class _Reply:
    def __init__(self, chain=None):
        self.chain = chain or []


class _Plain:
    def __init__(self, text=""):
        self.text = text


class _At:
    def __init__(self, qq=None):
        self.qq = qq


class _Video:
    def __init__(self, *, url=None):
        self.url = url

    @classmethod
    def fromURL(cls, url):
        return cls(url=url)


def _command(name, alias=None):
    """模拟 filter.command：装饰后原样返回函数，并记录命令名/别名便于断言。"""
    normalized_alias = set(alias or ())

    def decorator(func):
        func.command_name = name
        func.command_alias = normalized_alias
        return func

    return decorator


def _llm_tool(name):
    def decorator(func):
        func.llm_tool_name = name
        return func

    return decorator


def _permission_type(permission):
    def decorator(func):
        func.permission_required = permission
        return func

    return decorator


class _PermissionType:
    ADMIN = "admin"


class _Filter:
    PermissionType = _PermissionType
    command = staticmethod(_command)
    llm_tool = staticmethod(_llm_tool)
    permission_type = staticmethod(_permission_type)


class _AstrMessageEvent:
    pass


class _Context:
    def __init__(self):
        self.config = {}

    def get_config(self):
        return self.config

    def get_using_provider(self):
        return None

    async def get_using_provider_async(self):
        return None


class _Star:
    def __init__(self, context):
        self.context = context


class _AstrBotConfig(dict):
    def save_config(self):
        return None


def _register(*_args, **_kwargs):
    def decorator(cls):
        return cls

    return decorator


# 插件数据目录由 fixture 注入；__init__ 会用它落库/建临时目录
_DATA_DIR = {"path": None}


def _get_astrbot_data_path():
    return _DATA_DIR["path"]


_install("astrbot")
_install("astrbot.api", logger=logging.getLogger("astrbot"), AstrBotConfig=_AstrBotConfig)
_install("astrbot.api.event", AstrMessageEvent=_AstrMessageEvent, filter=_Filter)
_install(
    "astrbot.api.message_components",
    Image=_Image,
    Reply=_Reply,
    Plain=_Plain,
    At=_At,
    Video=_Video,
)
_install("astrbot.api.star", Context=_Context, Star=_Star, register=_register)
_install("astrbot.core")
_install("astrbot.core.utils")
_install("astrbot.core.utils.astrbot_path", get_astrbot_data_path=_get_astrbot_data_path)


# ----------------------------------------------------------------------
# 2) 事件 / 结果 / 配置替身
# ----------------------------------------------------------------------


class FakeResult:
    """plain_result / chain_result 的返回值：可直接断言 kind / text / chain。"""

    def __init__(self, kind, text=None, chain=None):
        self.kind = kind
        self.text = text
        self.chain = chain


class FakeEvent:
    """AstrMessageEvent 替身：只实现插件真正调用的方法。"""

    def __init__(
        self,
        message_str="",
        *,
        origin="session-1",
        platform="telegram",
        sender_id="10001",
        group_id=None,
        bot=None,
        self_id="9999",
        chain=None,
    ):
        self.message_str = message_str
        self.unified_msg_origin = origin
        self.platform = platform
        self.sender_id = sender_id
        self.group_id = group_id
        self.bot = bot
        self.stopped = False
        self.sent = []
        # message_obj.message 供 _extract_images 遍历
        self.message_obj = types.SimpleNamespace(self_id=self_id, message=list(chain or []))

    def get_platform_name(self):
        return self.platform

    def get_sender_id(self):
        return self.sender_id

    def get_group_id(self):
        return self.group_id

    def get_sender_name(self):
        return "测试用户"

    def stop_event(self):
        self.stopped = True

    def is_admin(self):
        return True

    def plain_result(self, text):
        return FakeResult("plain", text=text)

    def chain_result(self, chain):
        return FakeResult("chain", chain=list(chain))

    async def send(self, result):
        """AstrBot 的主动发送接口；/溯源重扫 用它汇报开始与进度。"""
        self.sent.append(result)


class FakeConfig(dict):
    """AstrBotConfig 替身：dict 语义 + save_config() 无操作。"""

    def save_config(self):
        return None


# ----------------------------------------------------------------------
# 3) 共享 fixture
# ----------------------------------------------------------------------


@pytest.fixture
def make_event():
    """返回 FakeEvent 类本身，测试里 make_event("/随机图", origin="g1") 构造事件。"""
    return FakeEvent


@pytest.fixture
def data_dir(tmp_path):
    """把插件数据目录指向 tmp_path，隔离每个用例的库/临时文件。"""
    _DATA_DIR["path"] = str(tmp_path)
    return tmp_path


@pytest.fixture
async def make_plugin(data_dir):
    """返回工厂 make_plugin(**config) -> ImageTracePlugin；用例结束统一关会话。"""
    from astrbot_plugin_image_trace.main import ImageTracePlugin

    created = []

    def factory(**overrides):
        plugin = ImageTracePlugin(_Context(), FakeConfig(overrides))
        created.append(plugin)
        return plugin

    yield factory
    for plugin in created:
        if plugin._http is not None:
            await plugin._http.close()
    # 内网直连白名单是模块级状态（生产由 terminate 清理），测试插件不走
    # terminate，这里显式复位，避免泄漏到其它测试文件
    url_guard.set_allowed_private_hosts(())


def _jpeg_bytes() -> bytes:
    from PIL import Image as PILImage

    buffer = io.BytesIO()
    PILImage.effect_noise((64, 64), 32).convert("RGB").save(buffer, format="JPEG")
    return buffer.getvalue()


class MockImgBed:
    """本地 ImgBed 替身的句柄：暴露 base_url 与已收到的 /random 查询参数。"""

    def __init__(self, base_url, state):
        self.base_url = base_url
        self._state = state

    @property
    def random_queries(self):
        return self._state["random_queries"]


@pytest.fixture
async def imgbed(monkeypatch):
    """在本地起一个假 ImgBed：/random 返回 JSON 直链，/file/ 返回真实 JPEG。

    同时把 url_guard.resolve_public_addrs 打桩为“照常 getaddrinfo 但跳过公网
    IP 白名单”，否则指向 127.0.0.1 的请求会被 GuardedHttpClient 拒绝。
    """

    def local_resolve(url, allowed_private_hosts=None):
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        addrs = [
            url_guard.ResolveResult(
                hostname=host,
                host=str(sockaddr[0]),
                port=int(sockaddr[1]) if len(sockaddr) > 1 else port,
                family=family,
                proto=proto,
                flags=0,
            )
            for family, _type, proto, _canon, sockaddr in infos
        ]
        return host, addrs

    monkeypatch.setattr(url_guard, "resolve_public_addrs", local_resolve)

    state = {"random_queries": []}
    image_bytes = _jpeg_bytes()

    async def random_handler(request):
        state["random_queries"].append(dict(request.query))
        name = "v.mp4" if request.query.get("content") == "video" else "x.jpg"
        return web.json_response({"url": f"/file/{name}"})

    async def file_handler(_request):
        return web.Response(body=image_bytes, content_type="image/jpeg")

    app = web.Application()
    app.router.add_get("/random", random_handler)
    app.router.add_get("/file/{name}", file_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield MockImgBed(f"http://127.0.0.1:{port}", state)
    finally:
        await runner.cleanup()
