"""media / storage 层可脱离 AstrBot 独立加载的冒烟用例。

做法：清空本次要验证的包模块后，在屏蔽所有 ``astrbot.*`` 导入的前提下按包
路径重新导入目标模块。这样既能证明这两层没有硬依赖 AstrBot，也不再需要各
模块维护「包加载 / 平铺加载」两套 import 回退。
"""

from __future__ import annotations

import builtins
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "astrbot_plugin_image_trace"


def _import_without_astrbot(module_name: str):
    """在 astrbot 不可导入的条件下重新导入目标包模块。"""
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == PACKAGE or name.startswith(PACKAGE + ".")
    }
    for name in saved:
        sys.modules.pop(name, None)

    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name.split(".")[0] == "astrbot":
            raise ImportError("astrbot 在冒烟用例中被故意设为不可用")
        return real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = guarded
    try:
        return importlib.import_module(module_name)
    finally:
        builtins.__import__ = real_import
        for name in list(sys.modules):
            if (name == PACKAGE or name.startswith(PACKAGE + ".")) and name not in saved:
                sys.modules.pop(name, None)
        sys.modules.update(saved)


def test_media_image_source_imports_without_astrbot() -> None:
    module = _import_without_astrbot(f"{PACKAGE}.media.image_source")
    assert module.ImageSource.__module__ == f"{PACKAGE}.media.image_source"
    assert module.ResolvedImage.__module__ == f"{PACKAGE}.media.image_source"


def test_storage_protocol_imports_without_astrbot() -> None:
    module = _import_without_astrbot(f"{PACKAGE}.storage.protocol")
    result = module.StoreResult(ok=True, url="https://example.com/a.jpg")
    assert result.ok is True
    assert result.url == "https://example.com/a.jpg"
    assert module.ImageStore.__module__ == f"{PACKAGE}.storage.protocol"
