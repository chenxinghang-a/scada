"""TDengine 实时桥接器的「写失败」回退路径测试（审计 #6：数据库不可用时行为）。

背景（2026-10-13 排查发现）：
`智能层/tsdb_adapter.py::RealtimeDataBridge._flush_buffer` 在 TDengine 写失败时，
如果没有可用的离线缓存，就把整批记录**放回内存缓冲**。原实现没有上限 ——
TDengine 长时间不可用（或断线）时内存会无限增长，最终进程被 OOM 杀掉，
**内存里的记录全都没了**，比「丢最旧」更糟。

同时核实出一件更隐蔽的事：`offline_buffer` 这个「断线缓存」钩子
**在本仓库没有实现**（`timeseries/offline_buffer.py` 不存在；
README 目录树里那条在 round 202 按「文档腐烂」清理了，调用点却留了下来），
生产入口 `run.py` 也没注入 → 运行时恒为 None → 走的就是上面那条无上限路径。
单测用 MagicMock 注入它，于是「看起来有断线缓存」。

本文件钉死两件事：
1. 内存回退**必须有上限**，溢出丢最旧并计数 + 告警（与采集队列同策略）；
2. 「断线缓存没有实现」这个事实必须**写在代码里**，且一旦实现就要改口径（反向守卫）。
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
TSDB_ADAPTER = BACKEND_ROOT / '智能层' / 'tsdb_adapter.py'
OFFLINE_BUFFER_IMPL = BACKEND_ROOT / 'timeseries' / 'offline_buffer.py'


def _bridge(*, fail=True, offline_buffer=None, max_buffer_size=None):
    from 智能层.tsdb_adapter import RealtimeDataBridge

    td = MagicMock()
    if fail:
        td.write_telemetry_batch.side_effect = RuntimeError('TDengine down')
    return RealtimeDataBridge(td, offline_buffer=offline_buffer,
                              max_buffer_size=max_buffer_size)


# ---------------------------------------------------------------------------
# 1. 内存回退必须有上限
# ---------------------------------------------------------------------------

def test_memory_fallback_is_bounded():
    """写失败回灌内存时，缓冲不得无限增长（无上限 = OOM = 全丢）"""
    b = _bridge(max_buffer_size=5)
    for i in range(12):
        b.feed('dev', 'r', float(i))
        b._flush_buffer()

    assert len(b._buffer) <= 5, f"内存回退缓冲涨到 {len(b._buffer)} 条 —— 上限没生效"
    assert b.stats['dropped_buffer_full'] == 7


def test_fallback_keeps_newest_on_overflow():
    """溢出丢**最旧**（与采集队列 _enqueue_drop_oldest 同策略）：留下的应是新数据"""
    b = _bridge(max_buffer_size=5)
    for i in range(12):
        b.feed('dev', 'r', float(i))
        b._flush_buffer()

    kept = sorted(r.value for r in b._buffer)
    assert len(kept) == 5
    assert kept[-1] == 11.0, '最新一条没留下'
    assert kept[0] == 7.0, f'丢的不是最旧：留下 {kept}'


def test_overflow_is_counted_in_stats():
    """溢出计数必须能从 get_stats() 读出来（只算不报等于没算）"""
    b = _bridge(max_buffer_size=3)
    for i in range(6):
        b.feed('dev', 'r', float(i))
        b._flush_buffer()

    assert b.get_stats()['dropped_buffer_full'] == 3


def test_overflow_is_logged_at_error(caplog):
    """溢出必须 ERROR 告警（数据在丢，不能静默）"""
    import logging
    b = _bridge(max_buffer_size=3)
    with caplog.at_level(logging.ERROR):
        for i in range(6):
            b.feed('dev', 'r', float(i))
            b._flush_buffer()

    hits = [r for r in caplog.records if '内存回退缓冲已达上限' in r.getMessage()]
    assert hits, '溢出没有任何 ERROR 日志'
    assert all(r.levelno >= logging.ERROR for r in hits)


def test_no_overflow_when_writes_succeed():
    """反向对照：写入正常时不得产生任何丢弃"""
    b = _bridge(fail=False, max_buffer_size=3)
    for i in range(20):
        b.feed('dev', 'r', float(i))
        b._flush_buffer()

    assert b.stats['dropped_buffer_full'] == 0
    assert len(b._buffer) == 0
    assert b.stats['records_written'] == 20


# ---------------------------------------------------------------------------
# 2. 离线缓存钩子（可选注入）
# ---------------------------------------------------------------------------

def test_offline_buffer_hook_is_used_when_injected():
    """注入离线缓存时，写失败应转入它，而不是回灌内存"""
    buf = MagicMock()
    b = _bridge(offline_buffer=buf)

    b.feed('dev', 'r', 1.0)
    b._flush_buffer()

    buf.buffer_records.assert_called_once()
    assert len(b._buffer) == 0, '已有离线缓存却又回灌了内存'
    assert b.stats['dropped_buffer_full'] == 0


def test_offline_buffer_failure_falls_back_to_memory():
    """离线缓存自身写失败 → 退回内存（且同样受上限约束）"""
    buf = MagicMock()
    buf.buffer_records.side_effect = RuntimeError('offline buffer disk full')
    b = _bridge(offline_buffer=buf, max_buffer_size=10)

    b.feed('dev', 'r', 1.0)
    b._flush_buffer()

    assert len(b._buffer) == 1, '离线缓存失败后既没落盘也没回内存 —— 数据丢了'


# ---------------------------------------------------------------------------
# 3. 反向守卫：幽灵钩子的口径必须与事实一致
# ---------------------------------------------------------------------------

def test_phantom_offline_buffer_is_documented_or_implemented():
    """「断线缓存」要么真有实现，要么注释必须如实说明它不存在。

    这条是**双向**的：
    * 实现不存在时 → 注释必须写明（否则读者以为断线缓存真的在跑）；
    * 实现存在时 → 注释里那句「本仓库没有该实现」必须删掉，并去 `run.py` 注入它。
    """
    src = TSDB_ADAPTER.read_text(encoding='utf-8')
    marker = '本仓库没有该实现'

    if OFFLINE_BUFFER_IMPL.exists():
        assert marker not in src, (
            'timeseries/offline_buffer.py 已存在 —— 请删掉 tsdb_adapter 里'
            '「本仓库没有该实现」的说明，并在 run.py 里把它注入 RealtimeDataBridge，'
            '否则断线缓存依然不会生效'
        )
    else:
        assert marker in src, (
            'offline_buffer 的实现不存在，但 tsdb_adapter 的注释没说明这一点 —— '
            '读者（和单测的 MagicMock）会以为「断线缓存」在跑'
        )


def test_run_py_does_not_inject_offline_buffer_yet():
    """记录当前事实：run.py 没有注入离线缓存（所以生产走内存回退）。

    若将来接上了，这条会红 —— 那时应当一并更新上面的口径守卫与 DEPLOYMENT 文档。
    """
    run_src = (BACKEND_ROOT / 'run.py').read_text(encoding='utf-8')
    if OFFLINE_BUFFER_IMPL.exists():
        pytest.skip('offline_buffer 已有实现，本事实记录不再适用')
    assert 'offline_buffer=' not in run_src, (
        'run.py 已开始注入 offline_buffer —— 请确认实现存在并更新相关口径/文档'
    )
