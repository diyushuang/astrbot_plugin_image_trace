"""构建发布用 zip 包。

白名单单点维护在这里：模块清单由「根目录 *.py + RUNTIME_PACKAGES 下的 *.py」
自动生成，避免发布包漏掉新增子包；数据文件单独列一份。
在插件仓库根目录执行：python scripts/build_release.py
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_image_trace"
PLUGIN_DIR = Path(__file__).resolve().parent.parent
# 运行时代码包：新增子包时只改这里
RUNTIME_PACKAGES = ("config", "controllers", "delivery", "media", "search", "services", "storage")
# 非代码运行所需文件：新增/删除时只改这里
DATA_FILES = (
    "CHANGELOG.md",
    "LICENSE",
    "_conf_schema.json",
    "logo.png",
    "metadata.yaml",
    "requirements.txt",
    "README.md",
)


def module_manifest() -> list[str]:
    """返回运行时代码清单（相对插件根目录的 posix 路径），自动包含各子包。"""
    files = [p.name for p in PLUGIN_DIR.glob("*.py")]
    for package in RUNTIME_PACKAGES:
        root = PLUGIN_DIR / package
        files.extend(
            p.relative_to(PLUGIN_DIR).as_posix()
            for p in root.rglob("*.py")
            if "__pycache__" not in p.parts
        )
    return sorted(files)


def release_manifest() -> list[str]:
    """发布包完整文件清单：代码清单 + 存在的数据文件。"""
    missing = [name for name in DATA_FILES if not (PLUGIN_DIR / name).is_file()]
    if missing:
        raise SystemExit(f"缺少发布所需文件: {', '.join(missing)}")
    return sorted(module_manifest() + list(DATA_FILES))


def build(dist_dir: Path) -> Path:
    files = release_manifest()
    dist_dir.mkdir(parents=True, exist_ok=True)
    archive = dist_dir / f"{PLUGIN_NAME}.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for relative in files:
            zf.write(PLUGIN_DIR / relative, f"{PLUGIN_NAME}/{relative}")
    py_count = sum(1 for name in files if name.endswith(".py"))
    print(f"已生成 {archive}")
    print(f"文件总数 {len(files)}，其中 .py {py_count}")
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 astrbot_plugin_image_trace 发布包")
    parser.add_argument("--dist", type=Path, default=PLUGIN_DIR / "dist", help="输出目录")
    parser.add_argument(
        "--staging",
        action="store_true",
        help="同时把解压后的目录放到 dist/staging 下，便于 import/pytest 验证",
    )
    args = parser.parse_args()
    archive = build(args.dist)
    if args.staging:
        staging = args.dist / "staging"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(staging)
        print(f"已解压到 {staging / PLUGIN_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
