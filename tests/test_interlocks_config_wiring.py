"""
安全联锁的工艺阈值配置与接线守卫（round 247）

背景
----
round 245 修掉了「NaN 解除联锁」，但当时如实记录了一个更根本的事实：
仓库里**没有任何联锁配置** —— `配置/interlocks.yaml` 不存在、
`配置/devices.yaml` 没有 `interlocks` 段、`run.py` 构造
`DeviceControlSafety` 时没传 `config`。三个加载来源全空，
**生产环境联锁规则是 0 条**，只有启动日志一句 WARNING。
那轮的用例靠 `config={'interlocks': [...]}` 注入规则，
验证的是「配了规则之后行为对不对」，不代表线上真的配了。

本轮补上 `配置/interlocks.yaml`（工艺阈值）并验证接线真的通。
`DeviceControlSafety._load_interlock_rules()` 的第一来源就是这个文件
（`INTERLOCK_CONFIG_PATH = '配置/interlocks.yaml'`），所以**建文件即接线**，
不需要改 run.py。本测试把这件事钉死：

1. 文件必须存在且格式合法（`{interlocks: [...]}`）；
2. 每条规则的 `device_id` / `register` 必须真实存在于
   `配置/devices.yaml` —— 写错不报错，只会让规则永不触发；
3. **不注入任何规则**，用真实构造路径加载，规则数必须 > 0；
4. 至少一条启用的规则能端到端触发 `block_start` 保护
   （超温 → 联锁状态 True → `validate_write` 拒绝写入）。

第 3、4 条是核心：没有它们，「配置写了但加载路径坏了」
（比如路径常量被改、yaml 结构变了）会静默退回 0 条规则，
而 round 245 那批注入式用例照样全绿。
"""
import pathlib

import pytest
import yaml

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
INTERLOCKS_PATH = PROJECT_ROOT / '配置' / 'interlocks.yaml'
DEVICES_PATH = PROJECT_ROOT / '配置' / 'devices.yaml'


def _load_yaml(path):
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def _device_registers():
    """{device_id: set(register_name)}，口径与 DeviceControlSafety 一致。"""
    devices = _load_yaml(DEVICES_PATH).get('devices') or []
    result = {}
    for d in devices:
        if not isinstance(d, dict):
            continue
        did = d.get('id') or d.get('device_id')
        if not did:
            continue
        result[did] = {r.get('name') for r in (d.get('registers') or [])
                       if isinstance(r, dict) and r.get('name')}
    return result


def _real_rules():
    data = _load_yaml(INTERLOCKS_PATH)
    rules = data.get('interlocks') if isinstance(data, dict) else data
    assert isinstance(rules, list) and rules, \
        '配置/interlocks.yaml 应为 {interlocks: [...]} 且至少一条规则'
    return rules


class TestConfigFile:
    def test_file_exists(self):
        assert INTERLOCKS_PATH.exists(), (
            '配置/interlocks.yaml 不存在 —— 生产环境将没有任何联锁保护'
            '（round 245 记录过这个状态，别再退回去）')

    def test_every_rule_has_required_fields(self):
        for rule in _real_rules():
            rid = rule.get('id')
            assert rid, f'规则缺 id: {rule!r}'
            assert 'condition' in rule, f'{rid} 缺 condition'
            assert 'action' in rule, f'{rid} 缺 action'
            cond = rule['condition']
            assert cond.get('type') == 'threshold', f'{rid} 只支持 threshold 条件'
            for field in ('device_id', 'register', 'operator', 'value'):
                assert cond.get(field) is not None, f'{rid} 的 condition 缺 {field}'
            assert cond['operator'] in ('>', '<', '>=', '<='), \
                f'{rid} 的 operator 非法: {cond["operator"]}'

    def test_rule_ids_unique(self):
        ids = [r.get('id') for r in _real_rules()]
        assert len(ids) == len(set(ids)), f'联锁 id 重复: {ids}'

    def test_devices_and_registers_exist(self):
        """写错设备/寄存器名不会报错，只会让规则永不触发 —— 必须逐条核对。"""
        known = _device_registers()
        for rule in _real_rules():
            rid = rule['id']
            cond = rule['condition']
            did, reg = cond['device_id'], cond['register']
            assert did in known, \
                f'{rid} 引用了设备清单里不存在的设备: {did}'
            assert reg in known[did], \
                f'{rid} 引用了 {did} 上不存在的寄存器: {reg}（可用: {sorted(known[did])}）'

    def test_action_device_matches_condition_device(self):
        """block_start 保护的是条件所在设备自己；指向别的设备属于可疑配置。"""
        for rule in _real_rules():
            cond_dev = rule['condition'].get('device_id')
            action = rule['action']
            if action.get('type') == 'block_start':
                assert action.get('device_id') == cond_dev, (
                    f'{rule["id"]} 的 action 设备 {action.get("device_id")} '
                    f'与条件设备 {cond_dev} 不一致')


class TestRealLoading:
    def test_loads_rules_without_injection(self):
        """不注入任何规则，走真实文件加载路径，必须加载到 >0 条。"""
        from unittest.mock import MagicMock
        from 智能层.device_control import DeviceControlSafety

        dcm = DeviceControlSafety(MagicMock(), MagicMock(), MagicMock())
        loaded = dcm._interlock_rules
        assert loaded, (
            'DeviceControlSafety 没有从 配置/interlocks.yaml 加载到任何规则 '
            '—— 检查 INTERLOCK_CONFIG_PATH 与 yaml 结构')

    def test_enabled_rule_blocks_write_end_to_end(self):
        """端到端：超温 → 联锁触发 → validate_write 拒绝写入。"""
        from unittest.mock import MagicMock
        from 智能层.device_control import DeviceControlSafety

        dcm = DeviceControlSafety(MagicMock(), MagicMock(), MagicMock())
        enabled = [r for r in dcm._interlock_rules.values() if r.get('enabled')]
        assert enabled, '配置文件里没有任何 enabled=true 的联锁规则'

        rule = enabled[0]
        cond = rule['condition']
        did, reg = cond['device_id'], cond['register']
        # 取一个必然满足条件的值（> 阈值就填阈值的 10 倍，< 就填 0）
        hot = cond['value'] * 10 if cond['operator'].startswith('>') else 0.0

        dcm.check_interlocks(did, reg, hot)
        assert dcm._interlock_states.get(rule['id']) is True, \
            f'{rule["id"]} 在超限值 {hot} 下没有触发'

        # 联锁生效时，写操作必须被拒（block_start 是写入准入依据）
        result = dcm.validate_write(did, 0, 1, 'test')
        assert result.get('allowed') is False, (
            f'联锁 {rule["id"]} 已触发，但 validate_write 仍然放行 '
            f'—— block_start 保护没接上')

    def test_static_guard_run_py_constructs_safety(self):
        """run.py 必须真的构造 DeviceControlSafety（否则加载根本不会发生）。"""
        source = (PROJECT_ROOT / 'run.py').read_text(encoding='utf-8')
        assert 'DeviceControlSafety(' in source, \
            'run.py 里没有构造 DeviceControlSafety'
