# -*- coding: utf-8 -*-
"""性能历史必须有生产写入方（round 252）。

背景
----
`GET /performance/metrics/history` 与 `/summary` 读
`metrics/metrics.jsonl`，而那个文件**只有 `tools/auto_metrics.py`
（独立 CLI）会写** —— 生产环境没有任何地方调用它：没有 cron、
没有 Windows 计划任务、维护任务里也没有。

净效果：
- `/history` 永远 `samples: 0`；
- `/summary` 永远「无历史数据」；
- 前端 `src/api/performance.ts` 的 getHistory/getSummary 拿到空数组，
  **性能页的历史曲线和摘要面板在生产永远是空的**。

「读取侧完好、写入侧不存在」—— 又一处声明与实现不符（本项目第 N 个）。
本轮把采样接进 `core/maintenance.py` 的维护任务（默认 5 分钟一次），
并加保留策略（jsonl 不裁剪会无界增长）。

另附：修掉一个**测试污染**—— `test_every_maintenance_task_function_runs`
会真实执行每个维护任务，于是每跑一次全量测试就往仓库的
`metrics/metrics.jsonl` 里灌一行采样（本轮实测到：跑完一次全量，
该文件 mtime 就变）。已把该用例隔离到临时目录。
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import paths  # noqa: E402


@pytest.fixture
def metrics_dir(tmp_path, monkeypatch):
    """把指标目录隔离到临时目录（不碰仓库里那份）。"""
    d = tmp_path / 'metrics'
    monkeypatch.setattr(paths, 'METRICS_DIR', d)
    return d


@pytest.fixture
def perf_collector(metrics_dir):
    from 展示层.api.api_performance import PerformanceCollector
    return PerformanceCollector()


# ===========================================================================
# 1. 采样能写、写得进读取侧
# ===========================================================================

class TestSampling:
    def test_append_sample_creates_file(self, perf_collector, metrics_dir):
        lines = perf_collector.append_sample()
        assert lines == 1
        f = metrics_dir / 'metrics.jsonl'
        assert f.exists()
        record = json.loads(f.read_text(encoding='utf-8').splitlines()[0])
        assert 'timestamp' in record, '读取侧按 timestamp 过滤，必须有这个字段'
        assert 'system' in record

    def test_written_record_is_readable_by_history(self, perf_collector):
        """端到端：写进去的必须能被 `get_history` 读出来。"""
        perf_collector.append_sample()
        history = perf_collector.get_history(hours=1)
        assert len(history) == 1, '写进去了但读不出来 —— 两侧格式不一致'
        assert 'system' in history[0]

    def test_repeated_samples_accumulate(self, perf_collector, metrics_dir):
        for _ in range(3):
            perf_collector.append_sample()
        assert len(perf_collector.get_history(hours=1)) == 3

    def test_trim_keeps_tail(self, perf_collector, metrics_dir):
        """行数超限时保留尾部（新）而不是头部（旧）。

        注意语义：文件是**追加写**，最新的一定在末尾。超限时保留末尾 N 行，
        所以裁完首行仍是旧行、末行必须是最新那条 —— 要断言的是
        "新采样没被裁掉" + "总数被压到上限"。
        """
        from datetime import datetime
        f = metrics_dir / 'metrics.jsonl'
        metrics_dir.mkdir(parents=True, exist_ok=True)
        old = json.dumps({'timestamp': '2020-01-01T00:00:00', 'system': {}})
        f.write_text('\n'.join([old] * 10) + '\n', encoding='utf-8')

        lines = perf_collector.append_sample(max_lines=5)
        assert lines == 5, f'应裁剪到 5 行，实际 {lines}'
        content = f.read_text(encoding='utf-8').splitlines()
        assert len(content) == 5
        # 末行必须是刚采的新样本（没被裁掉）
        newest = json.loads(content[-1])
        assert newest['timestamp'][:4] == str(datetime.now().year), (
            '最新采样被裁掉了 —— 保留策略留旧弃新')
        # 裁掉了 6 行旧的（10 旧 + 1 新 = 11 → 留 5）
        assert sum(1 for c in content if '2020-01-01' in c) == 4

    def test_history_filters_by_hours(self, perf_collector, metrics_dir):
        """早于 cutoff 的旧行不该出现在 history 里（保留策略的另一半）。"""
        from datetime import datetime, timedelta
        f = metrics_dir / 'metrics.jsonl'
        metrics_dir.mkdir(parents=True, exist_ok=True)
        old_ts = (datetime.now() - timedelta(hours=48)).isoformat()
        f.write_text(json.dumps({'timestamp': old_ts, 'system': {}}) + '\n',
                     encoding='utf-8')
        perf_collector.append_sample()
        history = perf_collector.get_history(hours=24)
        assert len(history) == 1, '48 小时前的旧行不该出现在 24 小时窗口里'


# ===========================================================================
# 2. 维护任务必须注册并真的采样
# ===========================================================================

class TestMaintenanceWiring:
    def test_task_registered(self):
        from core.maintenance import DEFAULT_INTERVALS, build_maintenance_tasks
        names = {t[0] for t in build_maintenance_tasks()}
        assert 'performance_metrics' in names, (
            'performance_metrics 没注册 —— 性能历史又没有生产写入方了')
        assert DEFAULT_INTERVALS['performance_metrics'] > 0

    def test_task_actually_writes(self, tmp_path, monkeypatch, metrics_dir):
        from core.maintenance import build_maintenance_tasks
        tasks = {t[0]: t[3] for t in build_maintenance_tasks()}
        result = tasks['performance_metrics']()
        assert 'metrics_lines' in result, result
        assert result['metrics_lines'] >= 1
        assert (metrics_dir / 'metrics.jsonl').exists()

    def test_task_failure_is_loud(self, tmp_path, monkeypatch, metrics_dir):
        """采样失败必须抛出来让维护框架记账，不能静默返回。

        真实的 `append_sample` 内部吞异常并返回 -1（它自己会打 ERROR），
        所以这里 patch 成返回 -1，验的是**任务层**有没有把 -1 变成异常 ——
        维护框架只有看到异常才会记失败，静默返回等于"采样坏了也没人知道"。
        """
        from core.maintenance import build_maintenance_tasks

        import 展示层.api.api_performance as ap
        monkeypatch.setattr(ap.PerformanceCollector, 'append_sample',
                            lambda self: -1)
        tasks = {t[0]: t[3] for t in build_maintenance_tasks()}
        with pytest.raises(RuntimeError, match='采样失败'):
            tasks['performance_metrics']()


# ===========================================================================
# 3. 静态守卫：不许退回「只有 CLI 会写」
# ===========================================================================

def test_maintenance_has_metrics_writer():
    """`core/maintenance.py` 必须包含性能采样任务。"""
    source = (PROJECT_ROOT / 'core' / 'maintenance.py').read_text(encoding='utf-8')
    assert 'performance_metrics' in source, (
        '维护任务里没有 performance_metrics —— metrics.jsonl 又没人写了')
    assert 'append_sample' in source, (
        '维护任务没有调用 append_sample')


def test_test_suite_does_not_pollute_real_metrics_file():
    """`test_every_maintenance_task_function_runs` 必须隔离指标目录。

    否则每跑一次全量测试就往仓库的 metrics/metrics.jsonl 灌一行真实采样
    （round 252 实测到过），把"生产没有写入方"这个问题掩盖掉。
    """
    source = (PROJECT_ROOT / 'tests' / 'test_maintenance_scheduler.py').read_text(
        encoding='utf-8')
    assert 'METRICS_DIR' in source, (
        '全任务执行用例没有隔离 METRICS_DIR —— 它会写仓库里真实的 metrics.jsonl')
