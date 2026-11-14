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
    "delivery/service.py": 95.0,  # 实测 99.0
    "delivery/channels.py": 95.0,  # 实测 100.0
    "controllers/register.py": 93.0,  # 实测 96.1
    "controllers/trace.py": 83.0,  # 实测 86.5
    "controllers/admin.py": 71.0,  # 实测 73.8
    "controllers/checkin.py": 65.0,  # 实测 68.0
    "controllers/random.py": 77.0,  # 实测 80.0
    "services/checkin_service.py": 95.0,  # 实测 100.0
    "services/trace_service.py": 83.0,  # 实测 86.1
    "services/random_service.py": 70.0,  # 实测 73.6
    "services/original_service.py": 85.0,  # 实测 89.9
    "search/vector_engine.py": 89.0,  # 实测 92.9
    "search/hash_engine.py": 90.0,  # 实测 94.5
    "search/auto_engine.py": 95.0,  # 实测 100.0
    "search/embedding_client.py": 83.0,  # 实测 86.3
    "search/qdrant_client.py": 84.0,  # 实测 87.7
    "search/errors.py": 95.0,  # 实测 100.0
    "storage/backend_utils.py": 92.0,  # 实测 95.5
    "storage/cloudflare_backend.py": 73.0,  # 实测 77.1
    "storage/http_backend.py": 72.0,  # 实测 75.5
    "storage/local_backend.py": 95.0,  # 实测 100.0
    "storage/modes.py": 95.0,  # 实测 100.0
    "storage/object_backend.py": 68.0,  # 实测 71.6
    "storage/urls.py": 80.0,  # 实测 83.3
    "image_bed.py": 74.0,  # 实测 77.7
    "vector_search.py": 78.0,  # 实测 81.5
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
