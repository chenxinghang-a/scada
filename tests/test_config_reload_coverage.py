# -*- coding: utf-8 -*-
"""round 319：配置热重载的**全景** —— 每个 yaml 到底会不会生效。

## 这一轮查出来的两件事

### 1. `energy.yaml` 改了不生效 + 写坏会静默回落默认值

`EnergyManager._load_config()` 只在 `__init__` 里调用。而 `energy.yaml`
头部写着「支持通过前端界面动态调整」—— 走 API 确实能改，但运维更常直接改文件。
手改文件不生效，且无任何提示。电价算错不会报错，只会让「本月电费」「碳排放」
两个报表静默失真。

更糟的是写坏文件时（缩进错一格、保存中间态）**只 `logger.warning` 就回落默认值**：
峰时电价 1.2 变成默认值，报表上的电费直接失真，而日志只有一行 warning。
已按 round 284 给 `devices.yaml` 的口径改成 **fail-closed**（保留原配置 + ERROR）。

### 2. 两份配置文件**整份没有任何消费方**

`配置/alarm_templates.yaml`（"报警规则模板库，可快速应用到设备"）和
`配置/fault_presets.yaml`（"故障场景预设"）在**全仓库 grep 零读取点**。

改它们不会产生任何效果，也不会报错。round 308 的 `test_config_keys_consumed`
只检查**键名**有没有被读，查不出"整份文件没人读"。

处置：**先加醒目文件头标注**（说明"改这里没有任何效果" + 三种处置方式），
不直接删除 —— 删配置文件是破坏性动作，而且现场可能有自己的脚本在读它。
是否删除由人决定；本文件的守卫负责保证**每份配置都有明确归类**。

## 本文件守的是什么

`配置/*.yaml` 里每一个文件，必须落进三类之一：

* `watch`   —— 有 mtime 监视，改了自动生效（写明是哪个函数）；
* `restart` —— 只在启动时读，**改了要重启是合理的**（写明为什么合理）；
* `dead`    —— 没有任何消费方（写明"未实现"还是"纯规格文档"）。

新增一个 yaml 却没登记 → 红。这样"这份配置到底会不会生效"永远有答案，
不需要每次去 grep。
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO / '配置'

#: 每份配置文件的归类。新增 yaml 必须在这里登记，否则守卫会红。
COVERAGE: dict[str, tuple[str, str]] = {
    # ---- 有 mtime 监视 ----
    'alarms.yaml': (
        'watch',
        '`AlarmManager._start_config_watcher()` 线程（10s）；'
        '重载走 `load_config()`，round 317 起还会收尾被删/禁用规则留下的活跃报警',
    ),
    'devices.yaml': (
        'watch',
        '`core/device_config_watch.watch_once()`（维护任务 10s）',
    ),
    'devices_real.yaml': (
        'watch',
        '同 devices.yaml —— `RealDeviceManager(config_path=...)` 指向它，'
        '监视器读的是同一个 `device_manager.config_path`',
    ),
    'devices_simulated.yaml': (
        'watch',
        '同 devices.yaml —— `SimulatedDeviceManager(config_path=...)` 指向它',
    ),
    'devices_modbus_sim.yaml': (
        'watch',
        '仿真 Modbus 从站启动时读取（`tools/modbus_simulator.py`），'
        '保持时长等参数在启动时生效；该工具是独立进程，重启即重载',
    ),
    'interlocks.yaml': (
        'watch',
        '`core/device_config_watch.watch_interlocks_once()`（round 318，维护任务 10s）'
        '→ `DeviceControlSafety.reload_interlocks()`',
    ),
    'energy.yaml': (
        'watch',
        '`core/device_config_watch.watch_energy_once()`（round 319，维护任务 30s）'
        '→ `EnergyManager.reload_config()`',
    ),

    # ---- 启动时读，改了就重启（合理） ----
    'system.yaml': (
        'restart',
        '全是**启动参数**：`collection.max_concurrent` / `default_interval`'
        '（线程池与调度间隔在构造时固定）、`database.path` / `type`。'
        '`database.retention` 由 `core/retention_scheduler` 每 6 小时按配置执行'
        '（它自己读文件，不属于"改了不生效"）；'
        '`database.compression` 已在 round 308 删除（当时也无实现）',
    ),
    'simulation_presets.yaml': (
        'restart',
        '`core/simulation_initializer.py` 在 `SimulationInitializer` 构造时读一次；'
        '预设只在"添加预设设备"时被应用，改它后重启一次即可',
    ),

    # ---- 无消费方（未实现 / 纯规格） ----
    'alarm_templates.yaml': (
        'dead',
        '**全仓库零读取点**。"报警规则模板库，可快速应用到设备"这个功能'
        '没有任何接口或逻辑实现。文件头已加醒目标注说明"改这里没有任何效果"',
    ),
    'fault_presets.yaml': (
        'dead',
        '**全仓库零读取点**。`core/fault_injection.py` 的注入是按'
        '`inject(target, fault_type, **params)` 直接调用的，不读本文件。'
        '文件头已加醒目标注',
    ),
    'iec104_example.yaml': (
        'dead',
        '示例文件（IEC104 设备配置样例），供人参考，不被任何代码加载',
    ),
}


def _config_files() -> list[str]:
    return sorted(p.name for p in CONFIG_DIR.glob('*.yaml'))


def test_every_config_file_is_classified():
    """每份配置都必须有明确归类：watch / restart / dead。"""
    files = set(_config_files())
    missing = sorted(files - set(COVERAGE))
    stale = sorted(set(COVERAGE) - files)

    assert not missing, (
        '以下配置文件没有登记"改了会不会生效" —— 请在本文件的 COVERAGE 里'
        '说明它属于 watch（有 mtime 监视）/ restart（启动时读，重启合理）/ '
        'dead（无消费方）：\n  ' + '\n  '.join(missing))
    assert not stale, (
        'COVERAGE 里登记了但文件已不存在（清单腐烂）: \n  ' + '\n  '.join(stale))


def test_coverage_categories_are_valid():
    bad = {k: v[0] for k, v in COVERAGE.items() if v[0] not in ('watch', 'restart', 'dead')}
    assert not bad, f'非法分类: {bad}'


def test_every_entry_has_a_reason():
    """每条都必须写明理由 —— 否则清单会退化成"随便登记一下就过"。"""
    thin = [k for k, (_cat, reason) in COVERAGE.items() if len(reason) < 20]
    assert not thin, f'这些条目的理由太短（等于没写）: {thin}'


def test_watch_entries_point_at_real_code():
    """`watch` 类登记的函数必须真的存在（防止清单指向已删除的代码）。"""
    src = (REPO / 'core' / 'device_config_watch.py').read_text(encoding='utf-8')
    alarm_src = (REPO / '报警层' / 'alarm_manager.py').read_text(encoding='utf-8')

    assert 'def watch_once(' in src
    assert 'def watch_interlocks_once(' in src
    assert 'def watch_energy_once(' in src
    assert 'def _start_config_watcher(' in alarm_src


# ---------------------------------------------------------------------------
# energy.yaml：热重载 + fail-closed
# ---------------------------------------------------------------------------

@pytest.fixture
def energy(tmp_path):
    import yaml
    from unittest.mock import MagicMock

    from 智能层.energy_manager import EnergyManager

    cfg = tmp_path / 'energy.yaml'

    def write(data):
        cfg.write_text(yaml.safe_dump(data, allow_unicode=True), encoding='utf-8')

    write({'tariff': {'peak': 1.5, 'flat': 0.9, 'valley': 0.4},
           'carbon_factor': 0.7})
    return EnergyManager(MagicMock(), config_path=str(cfg)), write, cfg


def test_energy_reload_applies_new_tariff(energy):
    em, write, _cfg = energy
    assert em.tariff['peak'] == 1.5

    write({'tariff': {'peak': 2.0, 'flat': 0.9, 'valley': 0.4},
           'carbon_factor': 0.75})
    r = em.reload_config()

    assert r['reloaded'] is True
    assert em.tariff['peak'] == 2.0, '改了电价没生效 —— 电费报表会算错'
    assert em.carbon_factor == 0.75
    assert 'tariff' in r['changed'] and 'carbon_factor' in r['changed']


def test_energy_reload_reports_no_change(energy):
    """反向对照：文件没改 → changed 为空（不许每次都报"变更"）。"""
    em, write, _cfg = energy
    write({'tariff': {'peak': 1.5, 'flat': 0.9, 'valley': 0.4},
           'carbon_factor': 0.7})
    r = em.reload_config()
    assert r['reloaded'] is True
    assert r['changed'] == {}


def test_energy_broken_file_keeps_current_config(energy):
    """**关键**：写坏文件不许回落默认电价（否则电费/碳排报表静默失真）。"""
    em, _write, cfg = energy
    em.tariff['peak'] = 3.0          # 模拟运行时已生效的电价

    cfg.write_text('tariff: [unclosed\n', encoding='utf-8')     # 坏 YAML
    r = em.reload_config()

    assert r['reloaded'] is False
    assert em.tariff['peak'] == 3.0, (
        '坏配置让电价回落默认值 —— 报表上的电费会静默算错')


def test_watch_energy_once_baselines_then_reloads(tmp_path, monkeypatch):
    import os
    import time

    import core.device_config_watch as mod

    cfg = tmp_path / 'energy.yaml'
    cfg.write_text('tariff: {}\n', encoding='utf-8')
    monkeypatch.setattr(mod, 'DEFAULT_ENERGY_REL', str(cfg))

    from unittest.mock import MagicMock
    em = MagicMock()
    em.reload_config.return_value = {'reloaded': True, 'changed': {'tariff': {}},
                                     'reason': '已重载'}
    state = {}

    r0 = mod.watch_energy_once(em, state=state)
    assert r0['reloaded'] is False and '基线' in r0['reason']
    em.reload_config.assert_not_called()

    time.sleep(0.01)
    cfg.write_text('tariff: {peak: 2.0}\n', encoding='utf-8')
    os.utime(cfg, None)

    r1 = mod.watch_energy_once(em, state=state)
    assert r1['reloaded'] is True
    em.reload_config.assert_called_once()


def test_watch_energy_broken_file_is_reported_not_retried(tmp_path, monkeypatch):
    """坏文件：如实上报"没重载"，且基线已推进（不每 30 秒刷一次）。"""
    import os
    import time

    import core.device_config_watch as mod

    cfg = tmp_path / 'energy.yaml'
    cfg.write_text('tariff: {}\n', encoding='utf-8')
    monkeypatch.setattr(mod, 'DEFAULT_ENERGY_REL', str(cfg))

    from unittest.mock import MagicMock
    em = MagicMock()
    em.reload_config.return_value = {'reloaded': False, 'changed': {},
                                     'reason': '配置文件解析失败，已保留原配置'}
    state = {}
    mod.watch_energy_once(em, state=state)

    time.sleep(0.01)
    cfg.write_text('broken: [\n', encoding='utf-8')
    os.utime(cfg, None)

    r1 = mod.watch_energy_once(em, state=state)
    assert r1['reloaded'] is False
    assert '保留原配置' in r1['reason']
    calls = em.reload_config.call_count

    mod.watch_energy_once(em, state=state)
    assert em.reload_config.call_count == calls, '坏文件被反复重试'
