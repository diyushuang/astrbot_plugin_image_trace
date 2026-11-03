"""守护：除 main.py 与测试外，任何模块都不得绕过 config 门面直接读私有配置。

Step 1 已把配置读取/迁移收口到 `config/`，并把 `main` 上的同名方法降级为薄委托。
后续批次（ImageSource / SearchEngine / DeliveryService …）如果把 `self._cfg(...)`
或 `plugin._checkin_settings()` 这类调用写回新模块，就会重新长出一条绕过门面的
依赖，同时把默认值再分散一份。这里用 AST 静态检查挡住两类回归：

1. 访问 main 的私有配置方法（`._cfg`、`._bucket_config` 等）；
2. 新模块反向 import `main`（`from main import x` 与 `import main` 两种写法都拦）。

同一文件里自己定义的同类方法（例如 `ImageBedClient._bool_cfg` 只是本地真值解析）
不算绕过，避免误报。测试里对 `main._LAYOUT_VERSION` 等兼容符号的引用不在扫描范围。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# main 上已收口为薄委托的配置方法：新模块只允许调 config.loader / config.migrations
PRIVATE_CONFIG_METHODS = {
    "_cfg",
    "_int_cfg",
    "_float_cfg",
    "_bool_cfg",
    "_dict_cfg",
    "_napcat_delivery_cfg",
    "_qq_delivery_cfg",
    "_delivery_config",
    "_bucket_config",
    "_effective_bed_cfg",
    "_random_settings",
    "_checkin_settings",
    "_verify_scaled",
    "_onebot_image_timeout",
    "_encode_options",
    "_qq_upload_config",
    "_scan_dirs",
    "_first_value",
    "_engine_choice",
    "_vector_index_on_register",
}

# 目录名 -> 不参与检查（测试自身要构造插件、也可能引用兼容常量）
SKIP_DIRS = {"tests", "__pycache__", ".git", ".github", ".venv"}


def _plugin_sources() -> list[Path]:
    sources = []
    for path in ROOT.rglob("*.py"):
        parts = path.relative_to(ROOT).parts
        if parts[0] in SKIP_DIRS:
            continue
        if path.name == "main.py":
            continue
        sources.append(path)
    return sources


class _FacadeBypassVisitor(ast.NodeVisitor):
    """记录「访问 main 私有配置方法」与「反向 import main」的行号。"""

    def __init__(self, rel: Path) -> None:
        self.rel = rel
        self.violations: list[str] = []
        self._class_stack: list[str] = []
        self._local_methods: dict[str, set[str]] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._local_methods[node.name] = {
            item.name for item in node.body if isinstance(item, ast.FunctionDef)
        }
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in PRIVATE_CONFIG_METHODS and not self._is_local_shadow(node):
            self.violations.append(f"{self.rel}:{node.lineno} 直接访问 .{node.attr}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.module.split(".")[-1] == "main":
            self.violations.append(f"{self.rel}:{node.lineno} 反向 import {node.module}")
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.split(".")[-1] == "main":
                self.violations.append(f"{self.rel}:{node.lineno} 反向 import {alias.name}")
        self.generic_visit(node)

    def _is_local_shadow(self, node: ast.Attribute) -> bool:
        """self._x / cls._x 且当前类自己定义了 _x：属于本文件内部方法，不是 main 的。"""
        if not isinstance(node.value, ast.Name) or node.value.id not in {"self", "cls"}:
            return False
        if not self._class_stack:
            return False
        return node.attr in self._local_methods.get(self._class_stack[-1], set())


def _find_violations() -> list[str]:
    violations = []
    for path in _plugin_sources():
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        visitor = _FacadeBypassVisitor(rel)
        visitor.visit(tree)
        violations.extend(visitor.violations)
    return violations


def test_services_do_not_bypass_config_facade():
    violations = _find_violations()
    assert not violations, "配置读取必须走 config 门面，发现绕过调用：\n" + "\n".join(violations)


def test_main_keeps_config_delegate_methods():
    """薄委托仍在：旧测试与外部引用依赖这些方法名，Step 1–2 期间不能删。"""
    from astrbot_plugin_image_trace.main import ImageTracePlugin

    missing = sorted(
        name for name in PRIVATE_CONFIG_METHODS if not hasattr(ImageTracePlugin, name)
    )
    assert not missing, f"main 缺少配置薄委托方法: {missing}"


def test_guard_detects_both_import_forms() -> None:
    """守护规则自检：`from main import x` 与 `import main` 都必须被拦。"""
    visitor = _FacadeBypassVisitor(Path("demo/new_module.py"))
    visitor.visit(ast.parse("from main import foo\nimport main\nimport pkg.main as m\n"))
    assert len(visitor.violations) == 3
    assert any("import main" in item for item in visitor.violations)
