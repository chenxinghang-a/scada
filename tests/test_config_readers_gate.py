# -*- coding: utf-8 -*-
"""把 `tools/scan_config_readers.py` 从「报告」升级成**闸门**（round 218）。

问题
----
这个扫描器此前只是打印一份清单，**没有任何测试会因为它而失败**。
于是「配置项算了没人读」这件事只能靠人偶尔跑一次、看一眼 ——
和本项目 2026-10-08 给 API 契约测试做的升级（round 193）是同一个教训：
**一份没人看的清单不构成闸门**。

而且清单本身还不准：别名 `WebConfig = FlaskConfig` 没被解析，
`FlaskConfig.HOST/PORT/REAL_PORT` 被误报成「无人读」（56 项里 7 项是假的）。
混着假阳性的清单，人的处理方式会退化成「大概看看」，真问题照样淹掉。

本文件做两件事
--------------
1. **双向闸门**：
   - 出现**新的**未读配置项（不在 allowlist 里）→ 失败，必须「接线」或
     「显式登记 + 写清为什么不接」；
   - allowlist 里某一项**已经被接上**（变回 read）→ 也失败，要求从 allowlist 删掉。
     只做单向的话，allowlist 会腐烂成「历史垃圾场」，谁也不知道哪条还成立。

2. **口径透明**：每个类都写清「真正的配置源在哪」。这不是注释洁癖 ——
   运维看到 `config.py` 里的 `AlarmOutputConfig.DO_MAPPING` 会以为改它有用，
   而真源其实是 `配置/alarms.yaml`。把真源写在 allowlist 里，读的人一眼能到对的地方。

⚠️ 这不是「允许死配置存在」的背书，只是把已知债务**钉住规模**：
新增一处都过不去。要清债就按类接线或删类，然后同步改这里的 allowlist。
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools import scan_config_readers as scr  # noqa: E402


# ---------------------------------------------------------------------------
# 每个类的「真源在哪」——读的人据此去对的地方改
# ---------------------------------------------------------------------------
CLASS_REASONS = {
    "AlarmConfig": (
        "真源是 `配置/alarms.yaml`（alarm_output/alarm_rules/dedup/notification 四段）。"
        "CHECK_INTERVAL 无主：升级检查间隔在 `报警层/alarm_escalation.py` 自己的 "
        "config.get('check_interval', 30)，洪水检查在 alarm_manager 里硬编码 15s。"
    ),
    "ModbusConfig": (
        "真源是 `配置/devices*.yaml` 的每设备字段 + `采集层/modbus_client.py` 的重试/退避实现。"
    ),
    "DatabaseConfig": (
        "DB_PATH 由 `paths.py` 决定（打包/模拟模式各不同）；"
        "RETENTION_DAYS / COMPRESSION_INTERVAL 的实际执行在 `core/ops_tools.py`"
        "（默认 90 天），本类不参与。"
    ),
    "ExportConfig": "EXPORT_DIR 由 `paths.py` 决定；FORMATS 由导出接口按需决定。",
    "AuthConfig": (
        "_DEV_DEFAULT_ADMIN_PW 是 config.py 内部用于「弱口令检测」的私有常量，"
        "在类体里自用，不对外。"
    ),
}

# 当前已知的未读配置（2026-10-20 基线，round 218 修完 8 项后的余量）
KNOWN_UNREAD = {
    # round 231 之后剩下的未读项。四个「整类死配置」
    # （AlarmOutputConfig / BroadcastConfig / MQTTConfig / TDengineConfig）
    # 与 FlaskConfig 的三个无主旋钮都已**接线**，因此从清单里删除
    # —— 清单只增不减就失去意义（本文件有双向断言盯着）。
    "AlarmConfig": {
        "CHECK_INTERVAL", "EMAIL_ENABLED", "RETENTION_DAYS",
        "SMTP_PASSWORD", "SMTP_PORT", "SMTP_SERVER", "SMTP_USERNAME",
    },
    "AuthConfig": {"_DEV_DEFAULT_ADMIN_PW"},
    "DatabaseConfig": {"COMPRESSION_INTERVAL", "DB_PATH", "RETENTION_DAYS"},
    "ExportConfig": {"EXPORT_DIR", "FORMATS"},
    "ModbusConfig": {"CONNECTION_TIMEOUT", "DEFAULT_INTERVAL", "MAX_RETRIES", "RETRY_INTERVAL"},
}


def _actual_unread() -> dict[str, set[str]]:
    """跑真实扫描，返回 {类: {生产代码不读的字段}}。"""
    classes = scr.collect_config_classes()
    resolve = {name: name for name in classes}
    resolve.update(scr.collect_class_aliases(classes))

    prod_roots = [REPO / d for d in scr.PROD_DIRS if (REPO / d).exists()]
    prod_roots += [REPO / f for f in scr.PROD_FILES if (REPO / f).exists()]
    prod_readers, dynamic = scr.scan_readers(resolve, prod_roots)

    assert not dynamic, (
        f"出现动态访问（getattr/__dict__），扫描可能漏报，闸门不可信: {dynamic}"
    )

    out: dict[str, set[str]] = {}
    for cls, attrs in classes.items():
        for attr in attrs:
            if not prod_readers.get((cls, attr)):
                out.setdefault(cls, set()).add(attr)
    return out


def test_no_new_decorative_config():
    """新增的「算了没人读」配置项必须接线，或显式登记到 allowlist。"""
    actual = _actual_unread()
    new_items = []
    for cls, attrs in actual.items():
        known = KNOWN_UNREAD.get(cls, set())
        for attr in sorted(attrs - known):
            new_items.append(f"{cls}.{attr}")

    assert not new_items, (
        "发现新的装饰性配置（定义了但生产代码从不读取）：\n  "
        + "\n  ".join(new_items)
        + "\n\n处置二选一：\n"
        "  a) 把它接到消费方（推荐 —— 否则运维改了不生效）；\n"
        "  b) 若确认无用，删掉它；\n"
        "  c) 若确实要先留着，加进本文件 KNOWN_UNREAD 并在 CLASS_REASONS 写清真源。"
    )


def test_allowlist_is_not_stale():
    """allowlist 里已被接上的项必须删掉 —— 否则清单会腐烂成垃圾场。"""
    actual = _actual_unread()
    stale = []
    for cls, attrs in KNOWN_UNREAD.items():
        now_read = attrs - actual.get(cls, set())
        for attr in sorted(now_read):
            stale.append(f"{cls}.{attr}")

    assert not stale, (
        "这些配置项**已经**被生产代码读取了，但仍挂在 KNOWN_UNREAD 里：\n  "
        + "\n  ".join(stale)
        + "\n\n请从 KNOWN_UNREAD 删除对应条目（这个方向同样重要："
          "allowlist 只增不减就失去意义）。"
    )


def test_every_allowlisted_class_has_reason():
    """每个被登记的类都要写清「真源在哪」，否则读的人不知道该去哪儿改。"""
    missing = sorted(set(KNOWN_UNREAD) - set(CLASS_REASONS))
    assert not missing, f"这些类没有写 CLASS_REASONS: {missing}"


def test_scanner_is_not_blind_to_aliases():
    """别名解析必须生效 —— 这是本闸门可信的前提（见 test_scan_config_readers_aliases）。"""
    classes = scr.collect_config_classes()
    resolve = {name: name for name in classes}
    resolve.update(scr.collect_class_aliases(classes))
    readers, _ = scr.scan_readers(resolve, [REPO / "run.py"])
    assert readers.get(("FlaskConfig", "HOST")), (
        "run.py 通过 WebConfig.HOST 读配置，但扫描器没解析到 —— "
        "闸门会把一堆已接线的项误报成未读"
    )
