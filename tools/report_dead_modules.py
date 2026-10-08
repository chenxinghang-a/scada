#!/usr/bin/env python3
"""死模块清单（P2-1 决策用）：生产不可达的模块 + 分类 + 测试覆盖情况。

用法
----
    python tools/report_dead_modules.py [--json]

为什么要有这个工具（而不是一份一次性报告）
------------------------------------------
「哪些模块生产不可达」会随接线 / 删除而变化，写死在报告里会**过期**。
本工具复用 `tools/wiring_analysis.py`（与守卫**同一套分析**），随时可重跑。

分类口径（对删除决策意义完全不同）
--------------------------------
  A. **有活替代**：它的公开 API（顶层类/函数名）在某个**可达**模块里也定义了
     → 能力仍然存在，它只是重复实现。删掉不影响功能。
  B. **能力孤本**：公开 API 名字在任何可达模块里都找不到
     → 删掉可能真的丢能力，**必须人确认**。

⚠️ 名字比对是**保守**判据：`masking_rule_engine` 的能力在 `tools/data_masking.py`
里有另一套实现（不同类名），按本判据会落进 B —— 这是**安全方向**的误判
（宁可让人多看一眼，也不误报"可安全删除"）。
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from wiring_analysis import analyse  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    report = analyse(ROOT)
    dup_rows, orphan_rows = report.classify()

    if "--json" in argv:
        print(json.dumps({
            "modules_total": len(report.path_of),
            "unreachable": len(report.unreachable),
            "undeclared": report.undeclared,
            "unreachable_lines": report.line_count,
            "has_live_duplicate": [
                {"module": m, "lines": n, "same_named_api": sorted(d)} for m, n, d in dup_rows
            ],
            "capability_orphan": [
                {"module": m, "lines": n, "api_sample": a} for m, n, a in orphan_rows
            ],
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"生产模块 {len(report.path_of)} 个；**不可达 {len(report.unreachable)} 个**"
          f"（合计 {report.line_count} 行）")
    print(f"其中**未声明**（未声明的死代码）{len(report.undeclared)} 个")
    if report.undeclared:
        for m in report.undeclared:
            print(f"    ⚠️ {m}")

    print(f"\n=== A. 有活替代（公开 API 在活模块里也有）—— 删除不影响功能：{len(dup_rows)} 个 ===")
    for m, lines, dup in dup_rows:
        print(f"  {m:<44} {lines:>5} 行  同名 API: {sorted(dup)[:3]}")

    print(f"\n=== B. 能力孤本（API 名字在任何活模块里都没有）—— **删前必须人确认**：{len(orphan_rows)} 个 ===")
    for m, lines, names in orphan_rows:
        print(f"  {m:<44} {lines:>5} 行  API 样本: {names}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
