"""构建发布用 zip 包。

白名单单点维护在这里：模块清单由「根目录 *.py + RUNTIME_PACKAGES 下的 *.py」
自动生成，避免发布包漏掉新增子包；数据文件单独列一份。
在插件仓库根目录执行：python scripts/build_release.py
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import zipfile
from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_image_trace"
PLUGIN_DIR = Path(__file__).resolve().parent.parent
# AstrBot 插件市场单包上限 16MB
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
SEMVER_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
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


def read_version() -> str:
    """读取并校验 metadata.yaml 的 SemVer（不带 v 前缀）。"""
    metadata = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*([^\s#]+)\s*$", metadata, flags=re.MULTILINE)
    if not match:
        raise SystemExit("metadata.yaml 缺少 version 字段")
    version = match.group(1)
    if not SEMVER_RE.fullmatch(version):
        raise SystemExit(f"metadata.yaml 的 version 不是合法 SemVer: {version}")
    return version


def release_manifest() -> list[str]:
    """发布包完整文件清单：代码清单 + 存在的数据文件。"""
    missing = [name for name in DATA_FILES if not (PLUGIN_DIR / name).is_file()]
    if missing:
        raise SystemExit(f"缺少发布所需文件: {', '.join(missing)}")
    return sorted(module_manifest() + list(DATA_FILES))


def check_archive(archive: Path) -> None:
    """校验现有发布包与 manifest、版本、README 是否一致。"""
    version = read_version()
    files = release_manifest()
    print(f"版本 {version}，manifest {len(files)} 项")
    if not archive.is_file():
        print(f"未找到 {archive}，仅校验 manifest（--check 不构建）")
        return
    size = archive.stat().st_size
    if size >= MAX_ARCHIVE_BYTES:
        raise SystemExit(f"发布包 {size} 字节，超过 16MB 上限")
    with zipfile.ZipFile(archive) as zf:
        names = sorted(zf.namelist())
        expected = sorted(f"{PLUGIN_NAME}/{name}" for name in files)
        if names != expected:
            missing = sorted(set(expected) - set(names))
            extra = sorted(set(names) - set(expected))
            raise SystemExit(f"发布包与 manifest 不一致: 缺 {missing}，多 {extra}")
        metadata = zf.read(f"{PLUGIN_NAME}/metadata.yaml").decode("utf-8")
        if not re.search(rf"^version:\s*{re.escape(version)}\s*$", metadata, re.MULTILINE):
            raise SystemExit("发布包 metadata.yaml 版本与仓库不一致")
        readme = zf.read(f"{PLUGIN_NAME}/README.md")
        if readme != (PLUGIN_DIR / "README.md").read_bytes():
            raise SystemExit("发布包 README.md 与仓库不一致")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    print(f"发布包校验通过: {size} 字节, sha256={digest}")


def build(dist_dir: Path) -> Path:
    files = release_manifest()
    version = read_version()
    dist_dir.mkdir(parents=True, exist_ok=True)
    archive = dist_dir / f"{PLUGIN_NAME}.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for relative in files:
            zf.write(PLUGIN_DIR / relative, f"{PLUGIN_NAME}/{relative}")
    size = archive.stat().st_size
    if size >= MAX_ARCHIVE_BYTES:
        archive.unlink(missing_ok=True)
        raise SystemExit(f"发布包 {size} 字节，超过 16MB 上限")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    py_count = sum(1 for name in files if name.endswith(".py"))
    print(f"已生成 {archive}（版本 {version}）")
    print(f"文件总数 {len(files)}，其中 .py {py_count}，{size} 字节")
    print(f"sha256={digest}")
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 astrbot_plugin_image_trace 发布包")
    parser.add_argument("--dist", type=Path, default=PLUGIN_DIR / "dist", help="输出目录")
    parser.add_argument(
        "--staging",
        action="store_true",
        help="同时把解压后的目录放到 dist/staging 下，便于 import/pytest 验证",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验 manifest / 现有发布包（版本、体积、README），不重新构建",
    )
    args = parser.parse_args()
    if args.check:
        check_archive(args.dist / f"{PLUGIN_NAME}.zip")
        return 0
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
