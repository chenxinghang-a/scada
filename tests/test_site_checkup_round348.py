"""现场体检工具 `tools/site_checkup.py`（round 348）。

## 为什么给一个「工具」写测试

这个工具的输出会被用来**决定能不能上现场**。它自己错了比没有更糟：
一个漏报的体检报告会让运维以为「都检查过了」。

而且它守的正是前几轮踩过的坑：
* `${VAR}` 没注入 → 42 台一台都连不上
* `access: rw` 但没 `write_limits` → 所有写入被拒
* Modbus 没声明 `byte_order` → 32 位值读成 1.6e-38
* 坏 YAML → 0 台设备静默启动

每条都要有对应的用例，否则工具退化成「打印一堆 ✅」。
"""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.site_checkup import BLOCK, OK, WARN, Report, check_config  # noqa: E402


def _write(tmp_path, devices, name='devices.yaml') -> Path:
    p = tmp_path / name
    p.write_text(yaml.dump({'devices': devices}, allow_unicode=True), encoding='utf-8')
    return p


def _levels(rep, item_kw=None):
    rows = rep.rows if item_kw is None else [r for r in rep.rows if item_kw in r[2]]
    return [r[1] for r in rows]


class TestConfigChecks:
    def test_healthy_config_has_no_block(self, tmp_path, monkeypatch):
        monkeypatch.setenv('DEV_HOST_A', '127.0.0.1')
        cfg = _write(tmp_path, [{
            'id': 'a', 'name': 'A', 'protocol': 'modbus_tcp',
            'host': '${DEV_HOST_A}', 'port': 502, 'byte_order': 'ABCD',
            'enabled': True,
            'registers': [{'address': 0, 'name': 't', 'data_type': 'uint16'}],
        }])
        rep = Report()
        check_config(cfg, rep)
        assert not rep.blocked(), [r[2:] for r in rep.blocked()]

    def test_missing_env_var_is_block(self, tmp_path, monkeypatch):
        """没注入的 `${VAR}` 必须阻断 —— 客户端会拿占位符当主机名去连。"""
        monkeypatch.delenv('DEV_HOST_MISSING', raising=False)
        cfg = _write(tmp_path, [{
            'id': 'a', 'name': 'A', 'protocol': 'modbus_tcp',
            'host': '${DEV_HOST_MISSING}', 'port': 502, 'byte_order': 'ABCD',
        }])
        rep = Report()
        check_config(cfg, rep)
        assert BLOCK in _levels(rep, '环境变量'), '未注入的环境变量没有阻断'

    def test_rw_without_write_limits_is_block(self, tmp_path, monkeypatch):
        """`access: rw` 但没量程 → 写入一律被拒（round 348 真踩到的坑）。"""
        monkeypatch.setenv('DEV_HOST_B', '127.0.0.1')
        cfg = _write(tmp_path, [{
            'id': 'b', 'name': 'B', 'protocol': 'modbus_tcp',
            'host': '${DEV_HOST_B}', 'port': 502, 'byte_order': 'ABCD',
            'registers': [
                {'address': 0, 'name': 'cmd_start', 'data_type': 'uint16', 'access': 'rw'},
            ],
        }])
        rep = Report()
        check_config(cfg, rep)
        assert BLOCK in _levels(rep, 'write_limits'), (
            'rw 点位没量程却没阻断 —— 现场会发现「所有写入被拒」而不知道原因')

    def test_rw_with_write_limits_passes(self, tmp_path, monkeypatch):
        monkeypatch.setenv('DEV_HOST_C', '127.0.0.1')
        cfg = _write(tmp_path, [{
            'id': 'c', 'name': 'C', 'protocol': 'modbus_tcp',
            'host': '${DEV_HOST_C}', 'port': 502, 'byte_order': 'ABCD',
            'write_limits': {'cmd_start': [0, 1]},
            'registers': [
                {'address': 0, 'name': 'cmd_start', 'data_type': 'uint16', 'access': 'rw'},
            ],
        }])
        rep = Report()
        check_config(cfg, rep)
        assert OK in _levels(rep, 'write_limits')

    def test_missing_byte_order_is_warn(self, tmp_path, monkeypatch):
        """没声明字序只是警告 —— 默认 ABCD 对多数设备是对的，但要说出来。"""
        monkeypatch.setenv('DEV_HOST_D', '127.0.0.1')
        cfg = _write(tmp_path, [{
            'id': 'd', 'name': 'D', 'protocol': 'modbus_tcp',
            'host': '${DEV_HOST_D}', 'port': 502,
            'registers': [{'address': 0, 'name': 't', 'data_type': 'uint16'}],
        }])
        rep = Report()
        check_config(cfg, rep)
        assert WARN in _levels(rep, 'byte_order')

    def test_bad_yaml_is_block(self, tmp_path):
        """坏 YAML 必须阻断 —— 否则进程会以 0 台设备「正常」启动。"""
        p = tmp_path / 'bad.yaml'
        p.write_text('devices:\n  - id: x\n   name: broken\n', encoding='utf-8')
        rep = Report()
        assert check_config(p, rep) is None
        assert BLOCK in _levels(rep, '可解析')

    def test_devices_not_list_is_block(self, tmp_path):
        p = tmp_path / 'weird.yaml'
        p.write_text(yaml.dump({'devices': {'a': 1}}), encoding='utf-8')
        rep = Report()
        check_config(p, rep)
        assert BLOCK in _levels(rep, '列表')

    def test_empty_devices_is_block(self, tmp_path):
        p = tmp_path / 'empty.yaml'
        p.write_text(yaml.dump({'devices': []}), encoding='utf-8')
        rep = Report()
        check_config(p, rep)
        assert BLOCK in _levels(rep, '非空')

    def test_duplicate_id_is_block(self, tmp_path, monkeypatch):
        monkeypatch.setenv('DEV_HOST_E', '127.0.0.1')
        cfg = _write(tmp_path, [
            {'id': 'dup', 'name': 'A', 'protocol': 'modbus_tcp',
             'host': '${DEV_HOST_E}', 'port': 502, 'byte_order': 'ABCD'},
            {'id': 'dup', 'name': 'B', 'protocol': 'modbus_tcp',
             'host': '${DEV_HOST_E}', 'port': 502, 'byte_order': 'ABCD'},
        ])
        rep = Report()
        check_config(cfg, rep)
        assert BLOCK in _levels(rep, 'id 唯一')

    def test_all_disabled_is_block(self, tmp_path, monkeypatch):
        monkeypatch.setenv('DEV_HOST_F', '127.0.0.1')
        cfg = _write(tmp_path, [{
            'id': 'f', 'name': 'F', 'protocol': 'modbus_tcp',
            'host': '${DEV_HOST_F}', 'port': 502, 'byte_order': 'ABCD',
            'enabled': False,
        }])
        rep = Report()
        check_config(cfg, rep)
        assert BLOCK in _levels(rep, '至少一台设备启用')

    def test_float_without_unit_is_warn(self, tmp_path, monkeypatch):
        monkeypatch.setenv('DEV_HOST_G', '127.0.0.1')
        cfg = _write(tmp_path, [{
            'id': 'g', 'name': 'G', 'protocol': 'modbus_tcp',
            'host': '${DEV_HOST_G}', 'port': 502, 'byte_order': 'ABCD',
            'registers': [{'address': 0, 'name': 'temperature', 'data_type': 'float32'}],
        }])
        rep = Report()
        check_config(cfg, rep)
        assert WARN in _levels(rep, '单位')

    def test_dimensionless_float_does_not_warn(self, tmp_path, monkeypatch):
        """功率因数 / pH 本来就无量纲，不该报「缺单位」。"""
        monkeypatch.setenv('DEV_HOST_H', '127.0.0.1')
        cfg = _write(tmp_path, [{
            'id': 'h', 'name': 'H', 'protocol': 'modbus_tcp',
            'host': '${DEV_HOST_H}', 'port': 502, 'byte_order': 'ABCD',
            'registers': [
                {'address': 0, 'name': 'power_factor', 'data_type': 'float32'},
                {'address': 2, 'name': 'ph_value', 'data_type': 'float32'},
            ],
        }])
        rep = Report()
        check_config(cfg, rep)
        assert OK in _levels(rep, '单位')


class TestRealConfig:
    """仓库里真实清单的自检（防止 round 348 的 write_limits 问题再出现）。"""

    def test_real_config_has_write_limits_for_rw(self):
        p = Path(__file__).resolve().parent.parent / '配置' / 'devices_real.yaml'
        if not p.exists():
            pytest.skip('没有 devices_real.yaml')
        cfg = yaml.safe_load(p.read_text(encoding='utf-8'))
        bad = []
        for d in (cfg.get('devices') or []):
            rw = [r['name'] for r in (d.get('registers') or []) if r.get('access') == 'rw']
            if not rw:
                continue
            limits = d.get('write_limits') or {}
            missing = [n for n in rw if n not in limits]
            if missing:
                bad.append(f"{d['id']}: {missing[:3]}")
        assert not bad, f'真实清单里有 rw 点位缺 write_limits（写入会被拒）：{bad[:5]}'
