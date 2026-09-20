"""
智能层横切回归测试（2026-09 审计修复项）

覆盖无法归入单模块测试文件的问题：
1. 五个"零引用模块"的处置状态：必须显式标注"未接线"，且标注必须与事实一致
   （run.py 里确实没有引用）—— 防止"代码在库里，却让人以为它在工作"。
2. 这些未接线模块本身仍须可导入、可实例化（未接线 ≠ 代码坏死）。
3. 费率/碳因子的单一数据源（跨 energy_manager 与 energy_optimizer）。
"""

import importlib
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INTELLIGENCE_DIR = PROJECT_ROOT / '智能层'
RUN_PY = PROJECT_ROOT / 'run.py'

# 模块名 -> 主类名（2026-09 审计确认在 run.py 中零引用）
UNWIRED_MODULES = {
    'alarm_intelligence': 'SmartAlarmManager',
    'data_quality': 'DataQualityMonitor',
    'energy_optimizer': 'EnergyOptimizer',
    'fault_prediction': 'FaultPredictionEngine',
    'production_analyzer': 'ProductionAnalyzer',
}


def _module_docstring(module_name: str) -> str:
    """读取模块级 docstring（文件头第一个三引号块）"""
    text = (INTELLIGENCE_DIR / f'{module_name}.py').read_text(encoding='utf-8')
    match = re.match(r'\s*(?:#.*\n)*\s*"""(.*?)"""', text, re.DOTALL)
    assert match, f'{module_name}.py 缺少模块级 docstring'
    return match.group(1)


# ============================================================
# 未接线状态标注
# ============================================================

@pytest.mark.parametrize('module_name', sorted(UNWIRED_MODULES))
def test_unwired_module_is_documented_as_unwired(module_name):
    """零引用模块的文件头必须明确标注"未接线"，不得默认它已在工作"""
    doc = _module_docstring(module_name)
    assert '未接线' in doc, f'{module_name}.py 未标注未接线状态'
    assert 'run.py' in doc, f'{module_name}.py 未说明与 run.py 的关系'


@pytest.mark.parametrize('module_name', sorted(UNWIRED_MODULES))
def test_unwired_claim_is_factually_true(module_name):
    """"未接线"必须与事实一致：run.py 中不得出现该模块

    若有人真的把它接进 run.py，本用例会失败 —— 此时应同步删除文件头的
    "未接线"标注（把文档与事实一起更新），而不是放任标注过期。
    """
    run_src = RUN_PY.read_text(encoding='utf-8')
    assert module_name not in run_src, (
        f'{module_name} 已出现在 run.py 中，请更新 {module_name}.py 文件头的"未接线"标注'
    )


@pytest.mark.parametrize('module_name,class_name', sorted(UNWIRED_MODULES.items()))
def test_unwired_module_is_importable_and_instantiable(module_name, class_name):
    """未接线 ≠ 坏死：模块必须可导入，主类必须可实例化（均支持 config=None）"""
    module = importlib.import_module(f'智能层.{module_name}')
    cls = getattr(module, class_name)
    instance = cls()
    assert instance is not None


def test_energy_optimizer_uses_canonical_rates():
    """energy_optimizer 不得自带第二套费率/碳因子（必须与 energy_manager 同源）"""
    from 智能层.energy_manager import DEFAULT_CONFIG
    from 智能层.energy_optimizer import EnergyAnalyzer

    analyzer = EnergyAnalyzer()
    assert analyzer.tariff == DEFAULT_CONFIG['tariff']
    assert analyzer.carbon_factor == DEFAULT_CONFIG['carbon_factor']


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
