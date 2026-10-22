"""
死代码删除了就不许悄悄复活（round 247）

背景
----
round 247 删掉了三个「实现了但从来没接进生产」的模块：

| 模块 | 死法 |
|---|---|
| `core/api_cache.py` | 只有自己的 `cleanup_expired()` 被维护任务调用 |
| `core/cache_tier.py` | 同上（`tiered_cache.cleanup()`） |
| `报警层/alarm_rules.py` | 只在测试里实例化；在用的 `AlarmManager._check_condition` 是另一套更完整的实现（带死区/状态感知），它连 `__init__.py` 的导出都是假的「已支持」 |

删除时同步清掉了：维护任务注册、健康接口的 `api_cache` 段、
`报警层/__init__.py` 的导出、以及测试里针对这些模块的用例。

为什么需要这个守卫：删掉的文件**不会自己回来**，但「照着提交历史粘回来」
「新同学不知道它为什么没了」都会让它复活，而且复活时往往只粘实现、
不粘删除理由 —— 于是又变成一套没人调用的代码，下次审查再删一遍。
本文件把「这三个模块不存在、且没有任何生产代码引用它们」钉死。

如果未来确实需要其中某项能力（比如真要做 API 缓存），正确做法是
**带着真实消费方一起加回来**，并同步更新本文件与
`core/maintenance.py` 头部的说明，而不是先粘一套没人用的实现。
"""

import pathlib
import re

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: round 247 删除的模块（相对路径）
DELETED_MODULES = [
    'core/api_cache.py',
    'core/cache_tier.py',
    '报警层/alarm_rules.py',
]

#: 生产代码里不允许出现的 import 形态
FORBIDDEN_IMPORTS = [
    r'from\s+core\.api_cache\s+import',
    r'from\s+core\.cache_tier\s+import',
    r'from\s+core\s+import\s+api_cache',
    r'from\s+报警层\.alarm_rules\s+import',
    r'from\s+报警层\s+import\s+alarm_rules',
    r'from\s+\.alarm_rules\s+import',
]

#: 扫描范围：生产代码目录（排除测试、venv、构建产物）
PROD_DIRS = ['core', '报警层', '采集层', '存储层', '展示层', '智能层',
             '用户层', 'timeseries', 'gateway', 'tools']


def _iter_prod_py():
    for d in PROD_DIRS:
        base = PROJECT_ROOT / d
        if not base.is_dir():
            continue
        for p in base.rglob('*.py'):
            s = str(p)
            if '__pycache__' in s or '.pytest_tmp' in s:
                continue
            yield p


class TestDeletedModulesStayDeleted:
    def test_files_do_not_exist(self):
        for rel in DELETED_MODULES:
            assert not (PROJECT_ROOT / rel).exists(), (
                f'{rel} 又出现了 —— round 247 删除它是因为生产代码零引用。'
                f'如确需该能力，请带着真实消费方加回并更新本测试与 '
                f'core/maintenance.py 头部说明')

    def test_no_production_import(self):
        """任何生产 .py 都不许 import 这三个模块（连 try/except 兜底都不行）。"""
        pattern = re.compile('|'.join(FORBIDDEN_IMPORTS))
        hits = []
        for p in _iter_prod_py():
            for i, line in enumerate(p.read_text(encoding='utf-8').splitlines(), 1):
                if pattern.search(line):
                    hits.append(f'{p.relative_to(PROJECT_ROOT)}:{i}: {line.strip()}')
        assert not hits, '生产代码引用了已删除的模块：\n' + '\n'.join(hits)

    def test_maintenance_has_no_cache_tasks(self):
        """维护任务里不许再出现两个缓存清理任务（清一个永远为空的缓存）。"""
        from core.maintenance import DEFAULT_INTERVALS
        for name in ('api_cache_cleanup', 'cache_tier_cleanup'):
            assert name not in DEFAULT_INTERVALS, (
                f'维护任务 {name} 又注册了 —— 它清理的缓存没有任何业务代码写入')
