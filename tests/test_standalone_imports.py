"""media / storage 层可脱离 AstrBot 独立加载的冒烟用例。

做法：按文件路径把目标模块当成顶层模块加载（`__package__` 为空，包内相对
import 失败后会走各模块自带的保底分支），并在加载期间拦截所有 `astrbot.*`
导入。这样能证明这两层没有硬依赖 AstrBot，后续抽库时可以直接搬走。
"""

from __future__ import annotations

import builtins
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_standalone(relative_path: str, module_name: str):
    """以顶层模块方式加载 rel_path，期间 astrbot 不可导入。"""
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    before = set(sys.modules)
    saved = sys.modules.get(module_name)
    sys.modules[module_name] = module
    inserted = str(ROOT) not in sys.path
    if inserted:
        sys.path.insert(0, str(ROOT))
    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name.split(".")[0] == "astrbot":
            raise ImportError("astrbot 在冒烟用例中被故意设为不可用")
        return real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = guarded
    try:
        spec.loader.exec_module(module)
    finally:
        builtins.__import__ = real_import
        if inserted:
            sys.path.remove(str(ROOT))
        # 清掉本次加载引入的仓库内顶层模块，避免污染其它用例
        for name in set(sys.modules) - before:
            loaded = sys.modules.get(name)
            file_path = getattr(loaded, "__file__", None)
            if file_path and Path(file_path).resolve().parent == ROOT:
                sys.modules.pop(name, None)
        if saved is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = saved
    return module


def test_media_image_source_imports_without_astrbot() -> None:
    module = _load_standalone("media/image_source.py", "_standalone_image_source")
    assert module.ImageSource.__module__ == "_standalone_image_source"
    assert module.ResolvedImage.__module__ == "_standalone_image_source"


def test_storage_protocol_imports_without_astrbot() -> None:
    module = _load_standalone("storage/protocol.py", "_standalone_storage_protocol")
    result = module.StoreResult(ok=True, url="https://example.com/a.jpg")
    assert result.ok is True
    assert result.url == "https://example.com/a.jpg"
    assert module.ImageStore.__module__ == "_standalone_storage_protocol"
