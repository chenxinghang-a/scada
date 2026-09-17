# -*- coding: utf-8 -*-
"""`tools/` 下 5 个运维脚本的「静默失效」修复回归测试（round 162）。

背景
----
`tools/` 此前被静默异常守卫**整目录排除**，结果 11 处 `except Exception: pass`
藏在里面躲过了守卫。它们的共同危害不是"崩掉"，而是**报告出一个看起来正常的结论**：

- `security_scan.py`（7 处）：某个文件读不了就静默跳过，扫描器照常输出
  「安全分数 100 / 发现问题 0」→ 调用方看到"安全"，实际是"根本没扫"。
- `deploy.py`：磁盘空间检查失败时那一项**直接从报告里消失** →
  部署前检查报告"一切正常"，而实际上**根本没检查过磁盘**。
- `diagnostics.py`：读不了的日志被静默跳过 → `recent_errors` **偏小** →
  诊断结论给出 "ok"，而真相是"这几个日志文件压根没读到"。
- `performance_baseline.py`：DB 采样失败时 `db_query_time_ms` 保持 0，
  而 `if s.get('db_query_time_ms')` 会把 0 当成"没这个维度"过滤掉 →
  **数据库性能维度从基线里整段消失**，调用方完全不知道。
- `auto_metrics.py`：`except Exception: continue` 把 IOError 这类"整份文件读不下去"
  的错误也当成"这行格式不对"吞掉 → 指标条数莫名其妙变少，没有任何提示。

这些测试锁死"失败必须可见"这个契约：断言**返回结构里带着失败信息**，
而不只是"没抛异常"。
"""

import builtins
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ===========================================================================
# security_scan.py
# ===========================================================================

@pytest.fixture
def fake_project():
    """在**路径不含 'test' 字样**的临时目录下造一个最小假项目。

    为什么必须绕这一圈（踩过）：`security_scan.py` 的每个扫描方法开头都有
    `if 'test' in str(py_file).lower(): continue` —— 这是个基于**整条路径字符串**
    的过滤。而 pytest 的 `tmp_path` 形如
    `...\\pytest-of-cxx\\pytest-123\\test_security_scan_unreadable_0\\`，
    路径里必然含 'test'，于是**所有文件都被跳过**，测试会"假绿"
    （第一次跑就是这么翻车的：scan_error_count 恒为 0）。
    用 `tempfile.mkdtemp(prefix='scanprobe_')` 拿到干净路径。
    """
    base = Path(tempfile.mkdtemp(prefix='scanprobe_'))
    proj = base / 'proj'
    (proj / 'sub').mkdir(parents=True)
    # 每种被扫描的文件类型各一份「好文件」，测试里再把对应的换成读不了的
    (proj / 'sub' / 'ok_module.py').write_text('x = 1\n', encoding='utf-8')
    (proj / 'ok_page.vue').write_text('<template><p>x</p></template>\n', encoding='utf-8')
    (proj / 'ok_conf.yaml').write_text('host: localhost\n', encoding='utf-8')
    (proj / '.gitignore').write_text('# empty\n', encoding='utf-8')
    (proj / 'requirements.txt').write_text('flask==3.0.0\n', encoding='utf-8')

    # 双保险：路径里真的不能有 'test'，否则下面的断言全是假的
    assert 'test' not in str(proj).lower(), f'临时路径含 test，测试会假绿: {proj}'

    yield proj
    shutil.rmtree(base, ignore_errors=True)


def test_security_scan_clean_run_reports_zero_scan_errors(fake_project):
    """反向验证：一切正常时 scan_error_count 必须是 0。

    没有这条，"scan_error_count 永远 > 0"这种假实现也能让下面的测试通过。
    """
    from tools.security_scan import SecurityScanner

    scanner = SecurityScanner()
    scanner.project_root = fake_project

    report = scanner.run_full_scan()

    assert report['summary']['scan_error_count'] == 0, report['scan_errors']
    assert report['scan_errors'] == []


def test_security_scan_unreadable_file_is_surfaced(fake_project, monkeypatch):
    """读不了的文件必须进报告，而不是被静默跳过。

    这是本文件最核心的一条：修复前扫描器会照常输出「发现问题 0」，
    调用方无从知道它根本没扫这个文件。
    """
    from tools.security_scan import SecurityScanner, format_report

    proj = fake_project
    bad = proj / 'sub' / 'bad_module.py'
    bad.write_text('y = 2\n', encoding='utf-8')

    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self.name == 'bad_module.py':
            raise PermissionError('模拟：文件被占用 / 无读权限')
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', fake_read_text)

    scanner = SecurityScanner()
    scanner.project_root = proj
    report = scanner.run_full_scan()

    # 1) 失败被计数
    assert report['summary']['scan_error_count'] >= 1, '读不了的文件没有被计数'
    # 2) 失败详情带着文件名与原因
    targets = [e['target'] for e in report['scan_errors']]
    assert any('bad_module.py' in t for t in targets), report['scan_errors']
    assert all(e['error'] for e in report['scan_errors']), '错误原因不能为空'
    # 3) 人类可读的报告里有显著警告，且说明了「0 问题」不可采信
    text = format_report(report)
    assert '不完整' in text
    assert 'bad_module.py' in text
    assert 'PermissionError' in text


def test_security_scan_every_stage_surfaces_read_failure(fake_project, monkeypatch):
    """**每一个**扫描阶段的读失败都必须留痕 —— 逐个阶段锁死。

    为什么单独有这条：上面那条只断言「有 >=1 条记录」，而 `bad_module.py` 会被
    4 个阶段先后读到，所以哪怕其中 3 个阶段还原成 `except: pass`，剩下 1 个
    也会把断言撑绿 —— 变异验证就是这么抓到这条测试是假绿的。
    这里逐个阶段点名，任何一处漏记都会被抓住。
    """
    from tools.security_scan import SecurityScanner

    proj = fake_project
    # 每个阶段一个"读不了"的目标：.py 覆盖 4 个阶段，.vue / .yaml / requirements 各 1 个
    (proj / 'sub' / 'bad_module.py').write_text('y = 2\n', encoding='utf-8')
    (proj / 'bad_page.vue').write_text('<template><p>y</p></template>\n', encoding='utf-8')
    (proj / 'bad_conf.yaml').write_text('host: localhost\n', encoding='utf-8')

    unreadable = {'bad_module.py', 'bad_page.vue', 'bad_conf.yaml', 'requirements.txt'}
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self.name in unreadable:
            raise PermissionError('模拟：文件被占用 / 无读权限')
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', fake_read_text)

    scanner = SecurityScanner()
    scanner.project_root = proj
    report = scanner.run_full_scan()

    stages = {e['stage'] for e in report['scan_errors']}
    expected = {
        'hardcoded_credentials', 'sql_injection', 'xss_risks',
        'permissions', 'dependencies', 'config_security', 'logging_security',
    }
    assert expected <= stages, f'这些阶段读失败后没有留痕: {sorted(expected - stages)}'
    assert all(e['error'].startswith('PermissionError') for e in report['scan_errors']), \
        report['scan_errors']


def test_security_scan_gitignore_read_failure_is_surfaced(fake_project, monkeypatch):
    """`.gitignore` 读不了时也要留痕。

    修复前 `_is_gitignored` 是 `except Exception: return False` ——
    这会让本该被忽略的敏感文件被报成"未忽略"（误报），
    而调用方不知道这条结论来自一次失败的读取。

    注意必须**真的放一个敏感文件**（这里用 `.env`）才能走到 `_is_gitignored`：
    它是从 `_check_sensitive_files` 调进来的，没有敏感文件就永远不会被调用
    —— 第一次跑就是因为假项目里没有敏感文件而失败的。
    """
    from tools.security_scan import SecurityScanner

    proj = fake_project
    (proj / '.env').write_text('SECRET=1\n', encoding='utf-8')

    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self.name == '.gitignore':
            raise OSError('模拟：.gitignore 读不了')
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', fake_read_text)

    scanner = SecurityScanner()
    scanner.project_root = proj
    report = scanner.run_full_scan()

    stages = [e['stage'] for e in report['scan_errors']]
    assert 'gitignore_check' in stages, report['scan_errors']


# ===========================================================================
# deploy.py
# ===========================================================================

def test_deploy_disk_check_failure_does_not_vanish(tmp_path, monkeypatch):
    """磁盘空间检查失败时，那一项必须作为 error 留在报告里。

    修复前是 `except Exception: pass` —— 这一项**直接从 checks 里消失**，
    部署前检查于是报告"一切正常"，而实际上根本没检查过磁盘
    （磁盘满是部署最常见的翻车原因）。
    """
    import shutil

    import tools.deploy as deploy_mod

    monkeypatch.setattr(deploy_mod, 'project_root', tmp_path)

    def boom(_path):
        raise OSError('模拟：disk_usage 失败')

    monkeypatch.setattr(shutil, 'disk_usage', boom)

    checks = deploy_mod.Deployer().check_environment()

    disk_entries = [c for c in checks['checks'] if c['name'] == '磁盘空间']
    assert len(disk_entries) == 1, (
        f'磁盘空间检查项消失了 —— checks 里只有 {[c["name"] for c in checks["checks"]]}'
    )
    assert disk_entries[0]['status'] == 'error'
    assert 'disk_usage' in disk_entries[0]['message'] or 'OSError' in disk_entries[0]['message']


# ===========================================================================
# diagnostics.py
# ===========================================================================

def test_diagnostics_unreadable_log_downgrades_status(tmp_path, monkeypatch):
    """有日志读不了时，结论不能是 ok —— 必须降级并说明原因。

    修复前读不了的日志被静默跳过 → recent_errors 偏小 → 结论 "ok"，
    而真相是"少读了几百行 ERROR"。
    """
    import tools.diagnostics as diag_mod

    log_dir = tmp_path / 'logs'
    log_dir.mkdir()
    (log_dir / 'good.log').write_text('INFO ok\nERROR boom\n', encoding='utf-8')
    (log_dir / 'bad.log').write_text('ERROR x\n', encoding='utf-8')

    real_open = builtins.open

    def fake_open(file, *args, **kwargs):
        if str(file).endswith('bad.log'):
            raise PermissionError('模拟：日志被其他进程占用')
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', fake_open)

    d = diag_mod.SystemDiagnostics()
    d.log_dir = log_dir
    result = d._check_logs()

    assert result['status'] == 'warning', result
    assert 'unreadable_logs' in result, result
    assert any('bad.log' in x for x in result['unreadable_logs'])
    assert '计数不完整' in result.get('message', '')


# ===========================================================================
# performance_baseline.py
# ===========================================================================

def test_performance_baseline_db_failure_is_marked_not_zero(tmp_path, monkeypatch):
    """DB 采样失败时必须显式置 None + 记原因，不能留一个 0 冒充"0ms 极快"。

    修复前 db_query_time_ms 保持 0，而 `if s.get('db_query_time_ms')` 会把 0
    当成"没有这个维度"过滤掉 → 数据库性能维度从基线里整段消失。
    """
    import tools.performance_baseline as perf_mod

    monkeypatch.setattr(perf_mod, 'project_root', tmp_path)

    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    # 一个不是 sqlite 库的文件 → connect 能过，execute 会抛 sqlite3.DatabaseError
    (data_dir / 'scada.db').write_text('这不是一个 sqlite 数据库', encoding='utf-8')

    pb = perf_mod.PerformanceBaseline(baseline_file=str(tmp_path / 'bl.json'))
    sample = pb._take_sample()

    assert sample['db_query_time_ms'] is None, (
        f'DB 采样失败却留下了 db_query_time_ms={sample["db_query_time_ms"]!r}'
    )
    assert 'db_query_error' in sample, sample
    assert 'DatabaseError' in sample['db_query_error'] or 'sqlite3' in sample['db_query_error']


def _stub_baseline(tmp_path, monkeypatch, sample: dict):
    """造一个 PerformanceBaseline，采样结果固定为 `sample`，且不真的 sleep / 落盘。

    注意 `duration_minutes=0` 是不行的：`num_samples = 0*60//10 = 0`，
    后面 `sum([])/len([])` 会抛 ZeroDivisionError（见
    `test_performance_baseline_rejects_zero_duration`）。
    这里用 1 分钟 = 6 次采样，并把 sleep 打桩掉，所以不耗时。
    """
    import tools.performance_baseline as perf_mod

    monkeypatch.setattr(perf_mod, 'project_root', tmp_path)
    monkeypatch.setattr(perf_mod.time, 'sleep', lambda _seconds: None)

    pb = perf_mod.PerformanceBaseline(baseline_file=str(tmp_path / 'bl.json'))
    monkeypatch.setattr(pb, '_take_sample', lambda: dict(sample))
    monkeypatch.setattr(pb, '_save_baseline', lambda: None)
    return pb


def test_performance_baseline_skips_db_dimension_when_sample_failed(tmp_path, monkeypatch):
    """采样全失败时，基线里不该出现 database 维度（而不是出现一个假的 avg_ms=0）。"""
    pb = _stub_baseline(tmp_path, monkeypatch, {
        'timestamp': 0, 'cpu_percent': 1, 'memory_percent': 1,
        'thread_count': 1, 'db_query_time_ms': None,
        'db_query_error': 'sqlite3.DatabaseError: file is not a database',
    })

    baseline = pb.establish_baseline(duration_minutes=1)
    assert 'database' not in baseline['metrics'], baseline['metrics']
    # 但 cpu / memory / threads 这些采样成功的维度必须还在（别把整份基线搞空）
    assert set(baseline['metrics']) == {'cpu', 'memory', 'threads'}, baseline['metrics']


def test_performance_baseline_includes_db_dimension_when_sampling_works(tmp_path, monkeypatch):
    """反向验证：采样成功时 database 维度必须出现。

    没有这条，一个"永远不输出 database 维度"的假实现也能让上面那条通过。
    """
    pb = _stub_baseline(tmp_path, monkeypatch, {
        'timestamp': 0, 'cpu_percent': 1, 'memory_percent': 1,
        'thread_count': 1, 'db_query_time_ms': 3.5,
    })

    baseline = pb.establish_baseline(duration_minutes=1)
    assert 'database' in baseline['metrics'], baseline['metrics']
    assert baseline['metrics']['database']['avg_ms'] == 3.5


def test_performance_baseline_rejects_zero_duration(tmp_path, monkeypatch):
    """`duration_minutes=0` 必须给出明确报错，而不是在算平均值时 ZeroDivisionError。

    这条是本轮写测试时顺手撞出来的真 bug：`num_samples` 为 0 →
    `samples` 为空 → `sum([])/len([])` 抛 ZeroDivisionError，
    堆栈指向 `tools/performance_baseline.py` 的算术行，看不出是"时长参数不对"。
    """
    import tools.performance_baseline as perf_mod

    monkeypatch.setattr(perf_mod, 'project_root', tmp_path)
    pb = perf_mod.PerformanceBaseline(baseline_file=str(tmp_path / 'bl.json'))

    with pytest.raises(ValueError, match='duration_minutes'):
        pb.establish_baseline(duration_minutes=0)


# ===========================================================================
# auto_metrics.py
# ===========================================================================

def test_auto_metrics_skips_corrupt_lines_but_keeps_valid(tmp_path, monkeypatch, capsys):
    """损坏的记录跳过并计数，正常记录照常返回。

    修复前是 `except Exception: continue` —— 连 IOError / MemoryError 这类
    "整份文件都读不下去"的错误也被当成"这行格式不对"吞掉，
    表现为指标条数莫名其妙变少，没有任何提示。
    """
    import tools.auto_metrics as metrics_mod

    monkeypatch.setattr(metrics_mod, 'project_root', tmp_path)
    mc = metrics_mod.MetricsCollector()
    mc.metrics_file.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now().isoformat()
    lines = [
        json.dumps({'timestamp': now, 'value': 1}),
        '{ 这不是合法 JSON',                       # JSONDecodeError
        json.dumps({'no_timestamp': True}),       # KeyError
        json.dumps({'timestamp': '不是时间'}),     # ValueError (fromisoformat)
        '',                                        # 空行
        json.dumps({'timestamp': now, 'value': 2}),
    ]
    mc.metrics_file.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    result = mc.load_metrics(hours=24)

    assert len(result) == 2, f'应只返回 2 条有效记录，实际 {len(result)}'
    assert {r['value'] for r in result} == {1, 2}

    err = capsys.readouterr().err
    assert '跳过 3 条损坏记录' in err, f'没有提示跳过了几条，stderr={err!r}'


def test_auto_metrics_does_not_swallow_resource_errors(tmp_path, monkeypatch):
    """超出「单条记录坏了」范围的错误必须往上抛，不能被当成"这行格式不对"。

    用一段深层嵌套的 JSON 触发 `RecursionError` —— 这是 `json.loads` 在损坏 /
    恶意数据上的真实行为，而 `RecursionError` **不属于**
    `(JSONDecodeError, KeyError, ValueError, TypeError)`。
    宽 except 会把它算成一条"损坏记录"继续跑，调用方以为只是脏数据，
    实际是撞到了资源级错误。

    踩过的坑：第一版用坏字节触发 `UnicodeDecodeError`，但解码是在
    `for line in f:` 那一行发生的 —— **位置在 try 块外面**，
    无论 except 写多宽都抓不到，测试恒绿（变异验证抓出来的假绿）。
    所以必须让异常**发生在 try 内部**。
    """
    import tools.auto_metrics as metrics_mod

    monkeypatch.setattr(metrics_mod, 'project_root', tmp_path)
    mc = metrics_mod.MetricsCollector()
    mc.metrics_file.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now().isoformat()
    bomb = '[' * 200000 + ']' * 200000          # json.loads → RecursionError
    mc.metrics_file.write_text(
        json.dumps({'timestamp': now, 'value': 1}) + '\n' + bomb + '\n',
        encoding='utf-8',
    )

    with pytest.raises(RecursionError):
        mc.load_metrics(hours=24)
