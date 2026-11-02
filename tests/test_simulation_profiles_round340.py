"""模拟参数配置化（round 340）。

为什么需要这个文件
------------------
改造前，模拟器是**两张硬编码表**：

1. `_generate_output_data` 里约 90 项「参数名 → 数值」的字面量字典，
   系数写死在表达式里（`mold_temperature = _current_temp * 0.8`）。
2. `_configure_process_model` 里 8 个 `elif '锅炉' in description` 式的分支。

后果（改造前实测，`配置/devices_simulated.yaml` 30 台设备）：

    点位总数 229，模拟器拿不到值 **136 个（59%）**

也就是说模拟模式下**一半以上的点位根本没有数据**，客户端拿到 `None`
后按 25.0 兜底 —— 界面上看起来「有数」，实际是假的。
凡是名字不在那 90 项里的设备（压缩机、CEMS、火灾报警、UPS、焊机、
仓储、机器人关节…）全部落在这个坑里。

本文件锁住改造后的三条性质：

1. **配置能造出新点位**：`sim.source` 声明的点位必须有值。
2. **配置能改量纲**：gain/offset/min/max/decimals/dtype 生效。
3. **配置不会造出假数据**：源不可用或 source 非法时**不输出该键**，
   而不是输出 0 或 25.0 —— 后者会让「没配好」看起来像「设备读到 0」。
"""

import copy
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from 采集层.device_behavior_simulator import (  # noqa: E402
    DeviceBehaviorSimulator,
    DeviceState,
)


def _cfg(registers, **extra):
    """构造一台最小设备配置。"""
    base = {
        'id': 'probe_dev_01',
        'name': '探针设备',
        'description': '测试用',
        'registers': registers,
    }
    base.update(extra)
    return base


def _run(config, times=2):
    sim = DeviceBehaviorSimulator(config['id'], config)
    sim.force_state(DeviceState.RUNNING)
    out = {}
    for _ in range(times):
        out = sim.update(1.0)
    return sim, out


class TestConfiguredPoints:
    """配置声明的点位必须有值，且量纲按配置换算。"""

    def test_unknown_param_name_gets_value(self):
        """内置表里没有的名字，只要写了 sim 段就必须有值。

        这是改造的核心：`oil_pressure` 这种名字原来永远不会出现在输出里。
        """
        cfg = _cfg([{
            'address': 0, 'name': 'oil_pressure', 'data_type': 'float32',
            'sim': {'source': 'pressure', 'gain': 2.0, 'decimals': 3},
        }])
        _, out = _run(cfg)
        assert 'oil_pressure' in out, f'配置声明了却没输出: {sorted(out)}'
        assert isinstance(out['oil_pressure'], float)

    def test_gain_and_offset_applied(self):
        """gain/offset 必须真的参与换算（否则量纲还是错的）。"""
        cfg = _cfg([
            {'address': 0, 'name': 'p_raw', 'sim': {'source': 'pressure', 'decimals': 4}},
            {'address': 2, 'name': 'p_scaled',
             'sim': {'source': 'pressure', 'gain': 1000.0, 'offset': 5.0, 'decimals': 4}},
        ])
        _, out = _run(cfg)
        # p_scaled = p_raw * 1000 + 5（同一周期同一个 pressure 源）
        assert abs(out['p_scaled'] - (out['p_raw'] * 1000 + 5)) < 0.01, out

    def test_min_max_clamp(self):
        """钳位生效：基变量远超上限时仍被压回 max。"""
        cfg = _cfg([{
            'address': 0, 'name': 'pct',
            'sim': {'source': 'pressure', 'gain': 10000.0, 'min': 0, 'max': 100, 'decimals': 1},
        }])
        _, out = _run(cfg)
        assert 0 <= out['pct'] <= 100, out

    def test_dtype_int_rounds(self):
        """dtype: int 必须取整（状态位/计数不能是 3.7）。"""
        cfg = _cfg([{
            'address': 0, 'name': 'alarm_bits',
            'sim': {'source': 'temperature', 'dtype': 'int'},
        }])
        _, out = _run(cfg)
        assert isinstance(out['alarm_bits'], int), type(out['alarm_bits'])

    def test_constant_source(self):
        """常量源：消防/报警类点位正常态固定。"""
        cfg = _cfg([{
            'address': 0, 'name': 'fire_door',
            'sim': {'source': 'constant', 'value': 1, 'dtype': 'int'},
        }])
        _, out = _run(cfg)
        assert out['fire_door'] == 1

    def test_random_source_within_range(self):
        """区间随机源：AGV 台数这类离散量。"""
        cfg = _cfg([{
            'address': 0, 'name': 'agv_count',
            'sim': {'source': 'random', 'min': 3, 'max': 5, 'dtype': 'int'},
        }])
        sim = DeviceBehaviorSimulator(cfg['id'], cfg)
        sim.force_state(DeviceState.RUNNING)
        seen = {sim.update(1.0)['agv_count'] for _ in range(20)}
        assert seen <= {3, 4, 5}, seen
        assert len(seen) > 1, f'20 次采样只有一个值，随机没生效: {seen}'


class TestCoupledChain:
    """点位之间可以串链，表达真实物理耦合。"""

    def test_point_derived_from_another_point(self):
        """`出水 = 进水 + 5`：source 指向本设备的另一个派生点位。

        没有这条能力，就只能让所有点位从同一个基变量硬套系数，
        结果是「进水温度」和「出水温度」只是两个不同倍数的同一条曲线，
        温差恒定、看不出机组负荷 —— 一眼就是假的。
        """
        cfg = _cfg([
            {'address': 0, 'name': 'cw_in',
             'sim': {'source': 'temperature', 'gain': 0.3, 'noise': 0.0, 'decimals': 4}},
            {'address': 2, 'name': 'cw_out',
             'sim': {'source': 'cw_in', 'offset': 5.0, 'noise': 0.0, 'decimals': 4}},
        ])
        _, out = _run(cfg)
        assert abs((out['cw_out'] - out['cw_in']) - 5.0) < 1e-6, out


class TestNoFakeData:
    """宁可缺键，不可造 0。"""

    def test_illegal_source_is_skipped_not_zero(self):
        """source 拼错 → 构建期就拒绝该点位，不输出任何键。

        注意这条测的是**构建期**拒绝（`_build_sim_specs` 里 source 不在
        白名单、也不在本设备声明点位中）。运行期「源本周期不可用」的
        分支由下一个用例覆盖 —— 两者是不同的代码路径，别混为一谈。
        """
        cfg = _cfg([{
            'address': 0, 'name': 'typo_point',
            'sim': {'source': 'temprature'},   # 故意拼错
        }])
        _, out = _run(cfg)
        assert 'typo_point' not in out, (
            '非法 source 不应该产出任何值（哪怕是 0）')

    def test_dependent_point_skipped_when_source_absent(self):
        """运行期源缺失 → 依赖它的点位也要跳过，不能补 0 或只留 offset。

        构造：`b` 的 source 非法（构建期被拒），但 `b` 仍进了「已声明点位」
        集合，于是 `a`（source: b）能通过构建期校验，运行时却拿不到 b。
        此时若补 0，`a` 会变成 `0 * gain + offset = 5` ——
        一个**看起来完全正常**的数字，而实际什么都没测到。
        """
        cfg = _cfg([
            {'address': 0, 'name': 'b', 'sim': {'source': 'no_such_source'}},
            {'address': 2, 'name': 'a', 'sim': {'source': 'b', 'offset': 5.0}},
        ])
        _, out = _run(cfg)
        assert 'a' not in out, (
            f'源缺失时不应输出依赖点位，实际 a={out.get("a")!r}')

    def test_missing_register_without_sim_still_absent(self):
        """没写 sim 段、名字又不在内置表 → 仍然缺失（不假装有数据）。

        这是改造前的真实状态：136/229 个点位属于这一类。
        这里断言「机制不会凭空补」，补数据必须靠配置显式声明。
        """
        cfg = _cfg([{'address': 0, 'name': 'never_heard_of_it'}])
        _, out = _run(cfg)
        assert 'never_heard_of_it' not in out


class TestProcessModelConfig:
    """设备级过程模型可配，不再需要为每种设备加一个关键词分支。"""

    def test_process_model_overrides_keyword_default(self):
        """配置里的 base_temperature 必须覆盖关键词分支的默认值。"""
        cfg = _cfg([], simulation={
            'process_model': {'base_temperature': 88.0, 'base_pressure': 0.75},
        })
        sim = DeviceBehaviorSimulator(cfg['id'], cfg)
        assert sim.process_model.base_temperature == pytest.approx(88.0)
        assert sim.process_model.base_pressure == pytest.approx(0.75)

    def test_unknown_process_model_field_is_ignored(self):
        """未知字段只告警不抛异常（一台设备配错不该让采集进程起不来）。"""
        cfg = _cfg([], simulation={'process_model': {'no_such_field': 1.0}})
        sim = DeviceBehaviorSimulator(cfg['id'], cfg)   # 不应抛异常
        assert not hasattr(sim.process_model, 'no_such_field')

    def test_monitoring_declared_explicitly(self):
        """`simulation.monitoring` 显式声明优先于关键词猜测。"""
        # 名字里没有任何「监测/传感器」关键词，靠关键词会被判成工作设备
        cfg = _cfg([], name='激光测厚仪', description='在线测厚',
                   simulation={'monitoring': True})
        sim = DeviceBehaviorSimulator(cfg['id'], cfg)
        assert sim.is_monitoring_device is True

    def test_monitoring_false_beats_keyword(self):
        """反过来：名字里带「监测」但显式声明 False，也要听配置的。"""
        cfg = _cfg([], name='监测工位电机', description='产线监测工位',
                   simulation={'monitoring': False})
        sim = DeviceBehaviorSimulator(cfg['id'], cfg)
        assert sim.is_monitoring_device is False


class TestProfileFile:
    """`配置/simulation_profiles.yaml` 的加载与合并规则。"""

    @pytest.fixture
    def isolated_profiles(self, tmp_path, monkeypatch):
        """把 profile 路径指向临时文件，避免依赖仓库里的真实档。"""
        from 采集层 import simulation_profiles as mod

        path = tmp_path / 'simulation_profiles.yaml'
        monkeypatch.setattr(mod, '_PROFILE_PATH', path)
        monkeypatch.setattr(mod, '_cache', None)
        monkeypatch.setattr(mod, '_cache_mtime', -1.0)
        return path, mod

    def test_profile_fills_missing_point(self, isolated_profiles):
        """profile 能给设备补点位规则。"""
        path, mod = isolated_profiles
        path.write_text(yaml.safe_dump({'profiles': {
            'dev_x': {'points': {'oil_temp': {'source': 'temperature', 'offset': 8.0}}},
        }}, allow_unicode=True), encoding='utf-8')

        cfg = {'id': 'dev_x', 'name': 'x',
               'registers': [{'address': 0, 'name': 'oil_temp'}]}
        assert mod.apply_profile(cfg) is True
        assert cfg['registers'][0]['sim']['source'] == 'temperature'
        assert cfg['registers'][0]['sim']['offset'] == 8.0

    def test_inline_sim_wins_over_profile(self, isolated_profiles):
        """设备配置内联的 sim 优先于 profile（单台特调不用改共享档）。"""
        path, mod = isolated_profiles
        path.write_text(yaml.safe_dump({'profiles': {
            'dev_y': {'points': {'t': {'source': 'temperature', 'gain': 99.0}}},
        }}, allow_unicode=True), encoding='utf-8')

        cfg = {'id': 'dev_y', 'name': 'y', 'registers': [
            {'address': 0, 'name': 't', 'sim': {'source': 'pressure', 'gain': 2.0}},
        ]}
        mod.apply_profile(cfg)
        assert cfg['registers'][0]['sim']['source'] == 'pressure'
        assert cfg['registers'][0]['sim']['gain'] == 2.0

    def test_missing_profile_file_is_not_fatal(self, isolated_profiles):
        """没有 profile 文件是正常情况（真实部署不需要它）。"""
        _, mod = isolated_profiles
        cfg = {'id': 'dev_z', 'name': 'z', 'registers': []}
        assert mod.apply_profile(cfg) is False

    def test_broken_profile_file_is_not_fatal(self, isolated_profiles):
        """profile 写坏了只告警，设备配置原样返回。"""
        path, mod = isolated_profiles
        path.write_text('profiles: [unclosed', encoding='utf-8')
        cfg = {'id': 'dev_w', 'name': 'w', 'registers': []}
        assert mod.apply_profile(cfg) is False


class TestRealConfigCoverage:
    """真实配置的覆盖率守卫 —— 这是改造要解决的核心指标。"""

    def test_all_declared_points_have_values(self):
        """`devices_simulated.yaml` 里每个点位都必须能取到值。

        改造前 229 个点位缺 136 个（59%）。这个用例就是防止回退：
        新增设备/点位时忘了写 sim 规则，这里立刻红。
        """
        root = Path(__file__).resolve().parent.parent
        cfg_path = root / '配置' / 'devices_simulated.yaml'
        if not cfg_path.exists():
            pytest.skip('没有模拟设备配置')

        devices = yaml.safe_load(cfg_path.read_text(encoding='utf-8'))['devices']
        missing = []
        total = 0
        for dev in devices:
            names = [p['name'] for sec in ('registers', 'nodes', 'topics', 'endpoints')
                     for p in (dev.get(sec) or []) if p.get('name')]
            if not names:
                continue
            total += len(names)
            sim = DeviceBehaviorSimulator(dev['id'], copy.deepcopy(dev))
            sim.force_state(DeviceState.RUNNING)
            out = sim.update(1.0)
            lack = [n for n in names if n not in out]
            if lack:
                missing.append((dev['id'], lack[:5]))

        assert not missing, (
            f'{total} 个点位中有 {len(missing)} 台设备缺值（前 5 台）: {missing}')


class TestByteOrderParity:
    """两套模拟客户端的字节序必须一致（成对路径等价）。

    改造前：`EnhancedSimulatedModbusClient` 读 `config['byte_order']` 支持四种，
    而 `SimulatedModbusClient` 三处 `struct.pack('>f')` **硬编码 ABCD**。
    同一份配置走两条路会给出不同的寄存器内容 —— 配成 CDAB 的设备
    在基础版下读到的是字节乱序的浮点数（典型值 1.6e-38 / -2.3e18）。
    """

    ORDERS = ('ABCD', 'BADC', 'CDAB', 'DCBA')
    VALUES = (7.0, -12.5, 1234.5678, 0.0)

    def test_encode_decode_roundtrip_all_orders(self):
        """四种字节序下，编码 → 解码必须还原原值。"""
        from 采集层.base_client import ByteOrderCapableDecoder
        from 采集层.simulated_client import encode_float32_registers

        class _Decoder(ByteOrderCapableDecoder):
            def __init__(self, order):
                self.config = {'byte_order': order}

        for order in self.ORDERS:
            for value in self.VALUES:
                regs = encode_float32_registers(value, order)
                assert len(regs) == 2, (order, regs)
                assert all(0 <= r <= 0xFFFF for r in regs), (order, regs)
                got = _Decoder(order).decode_float32(regs)
                assert abs(got - value) < 1e-3, (
                    f'{order} 编解码不互逆: {value} → {regs} → {got}')

    def test_client_own_decode_matches(self):
        """客户端自己的 decode_float32 也要按 byte_order 解。"""
        from 采集层.simulated_client import (
            SimulatedModbusClient,
            encode_float32_registers,
        )

        for order in self.ORDERS:
            client = SimulatedModbusClient({'id': 't', 'name': 't', 'registers': []})
            client.byte_order = order
            regs = encode_float32_registers(3.14, order)
            assert abs(client.decode_float32(regs) - 3.14) < 1e-4, order

    def test_no_bare_struct_pack_in_simulated_client(self):
        """结构守卫：simulated_client 里不许再出现裸 `struct.pack('>f'`。

        helper 函数 `encode_float32_registers` 内部那一次是**唯一允许**的，
        其余位置必须走 helper —— 否则又会变成「一处支持字节序、另一处不支持」。
        """
        import inspect
        from 采集层 import simulated_client as mod

        src = inspect.getsource(mod)
        # 去掉注释与 docstring 里的示例，避免误判
        code_lines = []
        in_doc = False
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.count('"""') == 1:
                in_doc = not in_doc
                continue
            if in_doc or stripped.startswith('#'):
                continue
            code_lines.append(line)
        code = '\n'.join(code_lines)

        helper_start = code.find('def encode_float32_registers')
        helper_end = code.find('\ndef ', helper_start + 10)
        outside = code[:helper_start] + code[helper_end:]

        assert "struct.pack('>f'" not in outside, (
            "发现硬编码大端的 float32 编码，应改用 encode_float32_registers()")
