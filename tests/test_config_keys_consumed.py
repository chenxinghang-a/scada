# -*- coding: utf-8 -*-
"""配置键守卫：`配置/*.yaml` 里声明的每个键，代码里必须**真的有人按名字读**。

## 为什么需要这条闸门

`配置/system.yaml` 里曾经躺着这样一段：

    database:
      compression:
        enabled: true
        interval_hours: 24
        method: average

全仓库没有任何压缩实现。运维看到 `enabled: true, interval_hours: 24`，
会认为"库每天压缩一次"，实际**从不压缩** —— 增长只能靠 `retention`
的按时间窗口删除兜底，采集频率调高一倍磁盘就多吃一倍。
这类"死配置"不产生任何报错，只是让配置表说谎。

同类历史（都是本项目真实发生过的）：
* `collection.default_interval` 硬编码 5，配置也是 5 → 旋钮是死的没人发现；
* `collection.max_concurrent` 代码硬编码 20、配置写 10 → 两边对不上；
* `export:` 段（directory/formats/max_rows）零读取点 → round 257 删除；
* `retry.interval_seconds` / `timeout` → round 210 删除。

## 判定口径

1. 从 `配置/*.yaml` 递归收集**叶子键名**（列表元素也展开）；
2. 从**生产代码**（全仓库 `*.py`，排除 `tests/`、`.venv`、`dist`、
   `.pytest_tmp*`）+ `static/**/*.js` + `模板/*.html` 收集所有字符串字面量；
3. 叶子键名**不在**字面量集合里 → 可疑，必须落进 `DOCUMENTATION_ONLY`
   并写明为什么它不需要读取点；
4. 允许清单**不许腐烂**：写了某个前缀却一条都没命中 → 红
   （否则清单会变成"什么都往里塞"的垃圾桶，闸门失效）。

## 三个已知盲区（写在这里，避免下次误判）

* **`scada-app` 是独立仓库**：前端若消费某个字段，这里看不到。
  相关键以"由前端消费"登记，并在 scada-app 侧自查。
* **动态拼 key**：`config.get(f'{proto}_timeout')` 这种拼出来的键
  在字面量里只有 `_timeout` 片段。当前代码里没有这种写法（已人工确认），
  将来引入时需要在 `DOCUMENTATION_ONLY` 登记。
* **键名过于通用**（`enabled` / `type` / `value`）几乎必然命中，
  所以本闸门只保证"完全没有读取点"的键不出现，不保证"读的就是这个键"。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO / '配置'

SKIP_DIR_NAMES = {'.venv', 'dist', 'build', '__pycache__', 'node_modules',
                  '.git', 'legacy', 'tests'}

#: 允许「只在配置里存在、代码不读」的键（前缀匹配），必须写明原因。
DOCUMENTATION_ONLY: dict[str, str] = {
    'alarm_output.relay_output.do_mapping.':
        '继电器板物理接线图（solenoid_valve/spare 等备用通道）。代码只用'
        '红/黄/绿/蜂鸣器四路标准通道，其余键是给现场接线与备件核对看的；'
        '要启用备用通道得先有对应的业务语义（哪条报警驱动哪一路）。',
    'alarm_output.station_output.do_mapping.':
        '站级继电器板（A/B 站各自的红绿灯与蜂鸣器）接线图。'
        '站级分区播报尚未实现 —— 当前是全场单一输出，'
        '`station_output` 整段没有装配点。留作现场接线依据，不参与运行。',
    'notification.sms.':
        '短信通道**未实现**（`notification.py:send_sms` 是明确报错的占位实现，'
        '已接进报警链路，启用会产生响亮的失败）。这些键是"实现它需要什么"的'
        '规格清单，`enabled: false` 时无消费方是**设计如此**。',
    'notification.sound.':
        '报警音效由前端播放（WebSocket 收到 alarm 事件时），后端不播放音频文件。'
        '本段只为兼容前端可能仍在发送的字段（round 250 已核实零服务端消费方）。',
    'devices[].firmware':
        '设备台账元数据（固件版本），供现场核对与巡检报告引用，不参与采集逻辑。',
    'fault_presets.yaml:severity_levels.':
        '纯文档：严重度分级说明文字，给人看的，没有代码消费方。',
    'fault_presets.yaml:fault_types.':
        '纯文档：故障类型的典型持续时间与恢复方式描述（运维手册素材）。'
        '真正的注入逻辑读的是 `scenarios.*`。',
    'simulation_presets.yaml:categories[].icon':
        '前端字段，由独立仓库 `scada-app` 消费（本仓库的前端只做静态渲染）。'
        '此处的字面量扫描看不到那个仓库。',
    'presets[].simulation_params.base_values.':
        '**以寄存器/变量名为 key 的数据映射表**，不是开关。消费方式是'
        '`base_values.get(var_name)`（core/simulation_initializer.py:360）与'
        '`for name, value in base_values.items()`'
        '（采集层/device_behavior_simulator.py:321）—— 键名来自设备清单，'
        '代码里当然不会出现字面量。',
    'presets[].simulation_params.noise_levels.':
        '同 base_values：按变量名查表的噪声幅度映射表'
        '（core/simulation_initializer.py:361）。',
    'system.yaml:system.author':
        '台账元数据（配置作者），供交接与审计署名，无代码消费方。',
}


def _iter_source_files() -> list[Path]:
    out = []
    for p in REPO.rglob('*.py'):
        if any(part in SKIP_DIR_NAMES or part.startswith('.pytest_tmp')
               for part in p.parts):
            continue
        out.append(p)
    return out


def _collect_literals() -> set[str]:
    literals: set[str] = set()
    for p in _iter_source_files():
        try:
            tree = ast.parse(p.read_text(encoding='utf-8', errors='ignore'))
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)
                # 兼容 `config.get('a.b.c')` 这种点分写法
                literals.update(node.value.split('.'))
    for pattern in ('static/**/*.js', '模板/*.html'):
        for p in REPO.glob(pattern):
            try:
                txt = p.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            for m in re.finditer(r"['\"]([A-Za-z_][A-Za-z0-9_]{1,40})['\"]", txt):
                literals.add(m.group(1))
    return literals


def _walk_yaml(node, path: str, out: list[tuple[str, str, str]]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            k = str(k)
            child = f'{path}.{k}' if path else k
            out.append((child, k, type(v).__name__))
            _walk_yaml(v, child, out)
    elif isinstance(node, list):
        for v in node:
            _walk_yaml(v, f'{path}[]', out)


def _declared_leaves() -> list[tuple[str, str, str]]:
    """返回 [(file, 路径, 键名)]，只含标量叶子。"""
    leaves = []
    for f in sorted(CONFIG_DIR.glob('*.yaml')):
        try:
            data = yaml.safe_load(f.read_text(encoding='utf-8'))
        except Exception as e:                       # noqa: BLE001
            pytest.fail(f'{f.name} 解析失败，守卫无法判断：{e}')
        rows: list[tuple[str, str, str]] = []
        _walk_yaml(data, '', rows)
        for path, leaf, typ in rows:
            if typ in ('dict', 'list'):
                continue
            leaves.append((f.name, path, leaf))
    return leaves


def _dead_keys() -> list[tuple[str, str, str]]:
    literals = _collect_literals()
    return [(f, path, leaf) for f, path, leaf in _declared_leaves()
            if leaf not in literals]


def _split_entry(entry: str) -> tuple[str | None, str]:
    """允许清单条目 → (文件名 | None, 路径前缀)。

    支持两种写法：
      * `system.yaml:system.author` —— 只对某个 yaml 生效（同名键在别的文件里
        可能是活的）；
      * `notification.sms.` —— 对任何文件生效（键名足够独特时用）。
    """
    if ':' in entry:
        fname, prefix = entry.split(':', 1)
        if fname.endswith('.yaml'):
            return fname, prefix
    return None, entry


def _is_allowed(filename: str, path: str) -> bool:
    for entry in DOCUMENTATION_ONLY:
        fname, prefix = _split_entry(entry)
        if fname is not None and fname != filename:
            continue
        if path == prefix or path.startswith(prefix):
            return True
    return False


# ---------------------------------------------------------------------------
# 守卫
# ---------------------------------------------------------------------------

def test_no_undocumented_dead_config_keys():
    """配置里声明的键，代码必须有人按名字读（或登记为纯文档）。"""
    dead = _dead_keys()
    unclassified = [
        f'{f}  {path}'
        for f, path, _leaf in dead
        if not _is_allowed(f, path)
    ]
    assert not unclassified, (
        '以下配置键在代码里**没有任何读取点** —— 运维改了它不会产生任何效果，'
        '配置表在说谎。要么接上消费方，要么删掉这段配置，'
        '要么在 DOCUMENTATION_ONLY 里写明为什么它不需要读取点：\n  '
        + '\n  '.join(unclassified)
    )


def test_documentation_only_allowlist_is_not_stale():
    """允许清单不许腐烂：登记的前缀必须还能命中真实的死键。"""
    dead = _dead_keys()
    stale = []
    for entry in DOCUMENTATION_ONLY:
        fname, prefix = _split_entry(entry)
        hit = any((fname is None or f == fname)
                  and (path == prefix or path.startswith(prefix))
                  for f, path, _ in dead)
        if not hit:
            stale.append(entry)
    assert not stale, (
        'DOCUMENTATION_ONLY 里这些前缀已经一条都命中不到了（键被删或已接上消费方），'
        '请从清单里移除，否则清单会退化成垃圾桶：\n  ' + '\n  '.join(stale)
    )


# ---------------------------------------------------------------------------
# 元守卫：防「扫描器自己坏了 → 恒绿」
# ---------------------------------------------------------------------------

def test_scanner_actually_scans_the_repo():
    """扫描器必须真的扫到了东西。

    这条是给自己兜底的：第一版扫描器漏了 `tools/` 目录，把
    `writable_hold_seconds`（仿真器的写保持时长，真的在读）误报成死配置；
    另一版把「跳过兄弟仓库」写成 `parent.parent/.git` 存在就跳过，
    结果把**本仓库所有二级目录**都跳过了，扫到的文件数掉到个位数。
    没有这条元守卫，扫描器坏掉的表现是"全部通过"。
    """
    files = _iter_source_files()
    assert len(files) > 100, (
        f'只扫到 {len(files)} 个 py 文件 —— 排除规则把本仓库自己也排掉了？')

    leaves = _declared_leaves()
    assert len(leaves) > 200, (
        f'只解析出 {len(leaves)} 个配置叶子键 —— yaml 没读到或目录路径错了？')

    # 必须扫到 tools/ 与 gateway/ 这类"容易被排除规则误伤"的目录
    scanned = {p.relative_to(REPO).parts[0] for p in files}
    for must in ('tools', 'gateway', '采集层'):
        assert must in scanned, f'扫描范围漏了 {must}/'


def test_known_dead_key_would_be_caught(tmp_path):
    """反向对照：判定逻辑必须真的能抓到死键，且读得到"易被排除规则误伤"的目录。"""
    literals = _collect_literals()
    assert 'writable_hold_seconds' in literals, (
        '扫描器读不到 tools/modbus_simulator.py 里的字面量 —— '
        '这正是第一版漏掉 tools/ 时的症状（把真在读的配置误报成死键）')
    assert 'max_concurrent' in literals, (
        '扫描器读不到 采集层/ 下的字面量 —— 二级目录被排除规则误伤')

    # 造一个绝对不可能被读到的键名，确认「不在字面量集合」这条路是通的
    ghost = 'this_key_should_never_be_read_9f3a'
    assert ghost not in literals
    assert not _is_allowed('system.yaml', ghost), (
        '允许清单匹配过宽 —— 任意键都能被放行，闸门等于不存在')
