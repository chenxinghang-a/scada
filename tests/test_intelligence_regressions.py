"""
智能层横切回归测试（2026-09 审计修复项）

覆盖无法归入单模块测试文件的问题：
1. 已接线模块必须仍然出现在 run.py 中（防误删接线）。
2. 振动模块不得再出现"默认 100Hz"式的假定采样率。
3. SPC 判异结果的写入必须唯一入口且在锁内。

⚠️ 2026-10-08（round 188 · D4 死代码清理）删掉的部分
------------------------------------------------------
原先这里还有 3 组用例（共 15 例），覆盖「五个零引用模块的处置状态」：
`test_unwired_module_is_documented_as_unwired` /
`test_unwired_claim_is_factually_true` /
`test_unwired_module_is_importable_and_instantiable`，
针对 `智能层` 下五个**生产不可达**模块
（alarm_intelligence / data_quality / energy_optimizer / fault_prediction / production_analyzer）。

那五个模块已按 D4 决策**备份后删除**
（备份：`C:\\Users\\cxx\\scada-dead-modules-backup-20261008\\`，含 MANIFEST.json 与 git blob）。
模块不存在了，「它有没有被标注为未接线」这个问题本身也就不存在 ——
**这是本轮唯一一类"删除测试"，删的是只对已删代码有意义的断言，不是覆盖。**

「未接线」这条约定本身的机械守卫仍保留在
`tests/test_wiring_declaration.py`（它用合成小仓库自测，不依赖仓库里恰好有死模块）。
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INTELLIGENCE_DIR = PROJECT_ROOT / '智能层'
RUN_PY = PROJECT_ROOT / 'run.py'


def test_wired_modules_are_still_wired():
    """反向守卫：已接线的模块必须仍然出现在 run.py 中（防止误删接线）"""
    run_src = RUN_PY.read_text(encoding='utf-8')
    for module_name in ('energy_manager', 'edge_decision', 'device_control',
                        'spc_analyzer', 'vibration_analyzer',
                        'predictive_maintenance', 'oee_calculator'):
        assert module_name in run_src, f'{module_name} 的接线疑似被移除'


# ============================================================
# 振动/SPC 修复的"跨模块"不变量
# ============================================================

def test_vibration_module_has_no_assumed_sample_rate():
    """振动模块源码中不得再出现"默认 100Hz"式的假定采样率"""
    src = (INTELLIGENCE_DIR / 'vibration_analyzer.py').read_text(encoding='utf-8')
    assert "get('sample_rate', 100)" not in src
    assert "self._sample_rate = self.config.get('sample_rate', 100)" not in src


def test_spc_violation_write_is_under_lock():
    """SPC 判异结果只能经 _record_violations 写入，且调用点必须在锁内。

    历史缺陷：判异结果在**锁外** `extend`，且每轮重扫整个窗口 → 同一违规被反复
    计入。此用例用 AST 固化"唯一写入口 + 调用点在 with self._lock 内"这两条约束。
    """
    import ast

    src = (INTELLIGENCE_DIR / 'spc_analyzer.py').read_text(encoding='utf-8')
    tree = ast.parse(src)

    functions = [
        (node.name, node.lineno, node.end_lineno or node.lineno, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    ]

    def enclosing_function(lineno):
        candidates = [f for f in functions if f[1] <= lineno <= f[2]]
        if not candidates:
            return None
        return min(candidates, key=lambda f: f[2] - f[1])

    # 1) self.violations[...].extend(...) 只允许出现在 _record_violations 内
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'extend'):
            segment = ast.get_source_segment(src, node) or ''
            if 'self.violations' in segment:
                owner = enclosing_function(node.lineno)
                assert owner and owner[0] == '_record_violations', \
                    f'判异结果在 {owner[0] if owner else "?"} 中裸 extend（应只在 _record_violations 内写入）'

    # 2) _record_violations 的每个调用点都必须**直接位于** `with self._lock:` 块内
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    call_sites = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == '_record_violations'
    ]
    assert call_sites, '未找到 _record_violations 调用点（实现已变更，请同步本用例）'

    def is_inside_lock(call_node) -> bool:
        node = parents.get(call_node)
        while node is not None:
            if isinstance(node, ast.With):
                for item in node.items:
                    ctx = item.context_expr
                    if (isinstance(ctx, ast.Attribute) and ctx.attr == '_lock'
                            and isinstance(ctx.value, ast.Name) and ctx.value.id == 'self'):
                        return True
                return False        # 最近一层 with 不是 self._lock
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return False
            node = parents.get(node)
        return False

    for node in call_sites:
        assert is_inside_lock(node), \
            f'第 {node.lineno} 行的 _record_violations 调用不在 with self._lock 块内（锁外写入）'
