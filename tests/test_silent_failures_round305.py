# -*- coding: utf-8 -*-
"""round 305：把轮询型全表统计压下来 + 死区必须落盘。

两个缺陷：
· `/api/system/status` 每次请求 6 条 `COUNT(*)` 全表扫描（`history_data` 约 200 万行），
  而它被前端 3~5s 轮询 —— 统计查询和实时写入抢同一条读连接；
· `set_deadband()` 只改内存不落盘 —— 下一次文件重载就把 API 设的值静默清掉。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import yaml


def _mgr():
    from 报警层.alarm_manager import AlarmManager
    return AlarmManager(MagicMock())


def _rule(rid, name, threshold=10, **extra):
    return {'id': rid, 'name': name, 'device_id': 'd1', 'register_name': 't',
            'condition': 'gt', 'threshold': threshold, 'level': 'warning', **extra}


def _write(path, rules):
    path.write_text(yaml.safe_dump({'alarm_rules': rules}, allow_unicode=True),
                    encoding='utf-8')


# ---------------------------------------------------------------------------
# 1. 数据库统计的 TTL 缓存
# ---------------------------------------------------------------------------

def _db(tmp_path):
    from 存储层.database import Database
    return Database(str(tmp_path / 'scada.db'))


def test_stats_cached_within_ttl(tmp_path):
    """TTL 内重复调用不许再扫全表。"""
    db = _db(tmp_path)
    calls = []
    real = db._collect_database_stats
    db._collect_database_stats = lambda: (calls.append(1), real())[1]

    first = db.get_database_stats()
    second = db.get_database_stats()

    assert len(calls) == 1, f'TTL 内又扫了 {len(calls)} 遍全表'
    assert first == second


def test_stats_force_bypasses_cache(tmp_path):
    """force=True 必须重新统计（刚清理完、想立刻看结果）。"""
    db = _db(tmp_path)
    calls = []
    real = db._collect_database_stats
    db._collect_database_stats = lambda: (calls.append(1), real())[1]

    db.get_database_stats()
    db.get_database_stats(force=True)

    assert len(calls) == 2, 'force=True 没有绕过缓存'


def test_stats_recomputed_after_ttl(tmp_path):
    """缓存过期后必须重算。"""
    db = _db(tmp_path)
    calls = []
    real = db._collect_database_stats
    db._collect_database_stats = lambda: (calls.append(1), real())[1]

    db.get_database_stats()
    db._dbstats_cache_at -= 3600        # 人为让它过期
    db.get_database_stats()

    assert len(calls) == 2


def test_stats_cache_not_written_on_failure(tmp_path):
    """统计抛异常时不许留半份缓存（否则"行数归零"的假象会活 30 秒）。"""
    db = _db(tmp_path)

    def boom():
        raise RuntimeError('db locked')

    db._collect_database_stats = boom
    try:
        db.get_database_stats(force=True)
    except RuntimeError:
        pass

    assert db._dbstats_cache is None, '失败的统计结果被写进了缓存'


# ---------------------------------------------------------------------------
# 2. 死区必须落盘
# ---------------------------------------------------------------------------

def test_set_deadband_persists_across_reload(tmp_path):
    cfg = tmp_path / 'alarms.yaml'
    _write(cfg, [_rule('r1', 'A')])

    mgr = _mgr()
    mgr.config_path = str(cfg)
    try:
        mgr.load_config()
        assert mgr.set_deadband('r1', 2.5) is True

        saved = yaml.safe_load(cfg.read_text(encoding='utf-8')) or {}
        assert saved['alarm_rules'][0].get('deadband') == 2.5, (
            '死区没有写进配置文件 —— 下一次重载就会消失')

        # 重载后仍在（这正是原来会丢的场景）
        mgr.load_config()
        assert mgr.get_deadbands().get('r1') == 2.5, '重载后死区丢了'
    finally:
        mgr.stop()


def test_set_deadband_rolls_back_on_save_failure(tmp_path, monkeypatch):
    cfg = tmp_path / 'alarms.yaml'
    _write(cfg, [_rule('r1', 'A')])

    mgr = _mgr()
    mgr.config_path = str(cfg)
    try:
        mgr.load_config()
        monkeypatch.setattr(mgr, '_save_config', lambda: False)

        assert mgr.set_deadband('r1', 3.0) is False, '落盘失败却回了成功'
        assert 'r1' not in mgr.get_deadbands(), '落盘失败没有回滚内存'
        assert 'deadband' not in mgr.rules['r1'], '规则本体里还留着死区'
    finally:
        mgr.stop()


def test_set_deadband_rejects_unknown_rule(tmp_path):
    cfg = tmp_path / 'alarms.yaml'
    _write(cfg, [_rule('r1', 'A')])

    mgr = _mgr()
    mgr.config_path = str(cfg)
    try:
        mgr.load_config()
        assert mgr.set_deadband('nope', 1.0) is False, (
            '给不存在的规则设死区居然返回成功')
    finally:
        mgr.stop()
