"""架构守护：服务层与控制器层不得反向依赖 main。"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
LAYERS = ("services", "controllers")


def _imports_main(tree: ast.AST) -> list[str]:
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "main" or alias.name.endswith(".main"):
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "main" or module.endswith(".main"):
                offenders.append(module)
    return offenders


@pytest.mark.parametrize("layer", LAYERS)
def test_layer_never_imports_main(layer: str) -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted((ROOT / layer).glob("*.py")):
        found = _imports_main(ast.parse(path.read_text(encoding="utf-8")))
        if found:
            offenders[path.name] = found
    assert offenders == {}, f"{layer}/ 出现了对 main 的反向 import: {offenders}"
