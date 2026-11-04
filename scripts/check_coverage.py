"""覆盖率门禁：只卡纯规则模块，入口层（main）不设硬门槛。

阈值取实测值略低一档，避免刚引入时偶然波动就变红；实测值与阈值都记录在这里，
新增/重构这些模块时同步调整。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 模块（相对包根目录的 posix 路径）→ 最低覆盖率（%）
THRESHOLDS = {
    "image_delivery.py": 90.0,  # 实测 91.52
    "features.py": 88.0,  # 实测 89.71
    "weibo_posts.py": 83.0,  # 实测 85.31
    "delivery/planner.py": 92.0,  # 实测 93.75
}
CONFIG_PACKAGE = "config/"  # 整包聚合，实测 83.47
CONFIG_THRESHOLD = 82.0


def _relative(path: str) -> str:
    return Path(path.replace("\\", "/")).as_posix().split("astrbot_plugin_image_trace/")[-1]


def _aggregate(items: list[dict]) -> tuple[float, int, int]:
    covered = sum(item["covered_lines"] for item in items)
    total = sum(item["num_statements"] for item in items)
    percent = (covered / total * 100) if total else 100.0
    return percent, covered, total


def main(report_path: str = "coverage.json") -> int:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    files = {_relative(path): item["summary"] for path, item in report["files"].items()}
    failures: list[str] = []

    for module, minimum in THRESHOLDS.items():
        summary = files.get(module)
        if summary is None:
            failures.append(f"{module}: 未采集到覆盖率数据")
            continue
        percent = summary["percent_covered"]
        status = "OK" if percent >= minimum else "FAIL"
        print(f"{status:4} {module:28} {percent:6.2f}%  (门槛 {minimum:.0f}%)")
        if percent < minimum:
            failures.append(f"{module}: {percent:.2f}% < {minimum:.0f}%")

    config_items = [item for name, item in files.items() if name.startswith(CONFIG_PACKAGE)]
    percent, covered, total = _aggregate(config_items)
    status = "OK" if percent >= CONFIG_THRESHOLD else "FAIL"
    print(f"{status:4} {'config/（整包）':28} {percent:6.2f}%  (门槛 {CONFIG_THRESHOLD:.0f}%)")
    if percent < CONFIG_THRESHOLD:
        failures.append(f"config/ 整包: {percent:.2f}% < {CONFIG_THRESHOLD:.0f}%")
    print(f"     config/ 汇总: {covered}/{total}")

    if failures:
        print("\n覆盖率门禁未通过：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\n覆盖率门禁通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "coverage.json"))
