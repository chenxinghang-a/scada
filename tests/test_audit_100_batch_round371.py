# -*- coding: utf-8 -*-
"""100 Bug 清单批量修复的回归用例（BUG-022 ~ BUG-100）。

只挑「纯函数 / 单对象」能直接断言的行为，避免把 Flask/线程环境搬进来：

* BUG-036：归档二次聚合必须累加桶内 count、取桶内真实极值；
* BUG-071：Prometheus 必须导出 high/medium/low 三级活动报警；
* BUG-072：诊断包配置必须递归脱敏；
* BUG-077：联锁规则不再依赖 `current_value`；`writable` 字段参与可写判定；
* BUG-084：S7 布尔量按位读写（读不把整字节当值，写不清掉同字节其它位）。
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from 存储层.data_archive import DataArchive
from core import write_safety
from core.write_safety import WriteSafetyValidator
from core.ops_tools import _redact


# ---------------------------------------------------------------------------
# BUG-036：归档的样本数与极值
# ---------------------------------------------------------------------------

def test_archive_statistical_uses_bucket_count_and_extremes():
    archiver = DataArchive(MagicMock())
    buckets = {
        0: [
            # 1 分钟预聚合桶：均值 95，桶内真实极值 100/90，样本 60 个
            {'value': 95.0, 'max_value': 100.0, 'min_value': 90.0,
             'count': 60, 'unit': '°C'},
            {'value': 96.0, 'max_value': 99.0, 'min_value': 91.0,
             'count': 30, 'unit': '°C'},
        ],
    }

    out = archiver._compress_statistical(buckets)

    assert len(out) == 1
    point = out[0]
    assert point['max'] == 100.0, '对均值求 max 会把尖峰削平'
    assert point['min'] == 90.0
    assert point['count'] == 90, 'count 必须是样本数（60+30），不是桶数 2'


# ---------------------------------------------------------------------------
# BUG-071：告警级别不能漏导
# ---------------------------------------------------------------------------

def test_metrics_export_includes_high_medium_low():
    from core.metrics import MetricsCollector, ALARMS_ACTIVE

    manager = MagicMock()
    manager.get_active_alarms.return_value = [
        {'alarm_level': 'high'}, {'alarm_level': 'high'},
        {'alarm_level': 'medium'}, {'alarm_level': 'low'},
    ]
    # 不能只看四个"老"级别：high/medium/low 才是本项目规则默认会产出的等级
    MetricsCollector().update_alarm_metrics(manager)

    assert ALARMS_ACTIVE.labels(level='high')._value.get() == 2
    assert ALARMS_ACTIVE.labels(level='medium')._value.get() == 1
    assert ALARMS_ACTIVE.labels(level='low')._value.get() == 1


# ---------------------------------------------------------------------------
# BUG-072：诊断包脱敏
# ---------------------------------------------------------------------------

def test_diagnostic_config_is_redacted():
    raw = {
        'alarms': {'smtp': {'password': 'p@ss', 'host': 'mail'}},
        'devices': {'plc': {'auth_token': 'tok', 'protocol': 'modbus'}},
        'nested': [{'secret_key': 'sk', 'keep': 1}],
    }

    clean = _redact(raw)

    assert clean['alarms']['smtp']['password'] == '***'
    assert clean['alarms']['smtp']['host'] == 'mail'
    assert clean['devices']['plc']['auth_token'] == '***'
    assert clean['devices']['plc']['protocol'] == 'modbus'
    assert clean['nested'][0] == {'secret_key': '***', 'keep': 1}


# ---------------------------------------------------------------------------
# BUG-077：联锁与 writable
# ---------------------------------------------------------------------------

DEVICE = {
    'id': 'd1', 'name': '注塑机', 'protocol': 'modbus_tcp',
    'registers': [
        {'address': 1, 'name': 'temperature', 'unit': '°C',
         'data_type': 'int16', 'access': 'rw'},
        # 只写了 writable，没有 access
        {'address': 2, 'name': 'mode', 'unit': '',
         'data_type': 'uint16', 'writable': True},
    ],
}


def test_interlock_applies_without_current_value(monkeypatch):
    """联锁规则只看 new_val 时，不能因为没传 current_value 就整段跳过。"""
    monkeypatch.setitem(
        write_safety._INTERLOCK_RULES, 'temperature',
        {'condition': lambda current, new: new <= 50.0,
         'message': '温度设定值不得超过 50°C'})

    validator = WriteSafetyValidator(DEVICE)
    allowed, reason = validator.validate_write(1, 60.0)   # 量程内、但违反联锁

    assert allowed is False
    assert '联锁' in reason


def test_writable_field_grants_write():
    """`access` 缺省但 `writable: true` 的寄存器必须可写。"""
    validator = WriteSafetyValidator(DEVICE)
    allowed, reason = validator.validate_write(2, 1)

    assert allowed is True, f'writable 字段被忽略: {reason}'


# ---------------------------------------------------------------------------
# BUG-084：S7 布尔位
# ---------------------------------------------------------------------------

class _FakeS7Client:
    def __init__(self, byte_value: int):
        self.byte_value = byte_value
        self.written = None

    def db_read(self, db_number, offset, size):
        return bytearray([self.byte_value])

    def db_write(self, db_number, offset, data):
        self.written = bytes(data)


def _bare_gateway():
    from gateway.s7_gateway import S7Gateway

    gw = S7Gateway.__new__(S7Gateway)     # 绕开 PLC 连接，只测编解码
    gw.logger = logging.getLogger('s7-test')
    return gw


def test_s7_bool_read_uses_bit_offset():
    gw = _bare_gateway()
    client = _FakeS7Client(0x80)          # 只有 bit7 为 1

    assert gw._read_register(client, 1, 0, 'bool', bit_offset=7) == 1.0
    assert gw._read_register(client, 1, 0, 'bool', bit_offset=0) == 0.0


def test_s7_bool_write_preserves_other_bits():
    gw = _bare_gateway()
    client = _FakeS7Client(0b1010_1010)
    gw._clients = {'d1': client}

    assert gw.write_register('d1', 1, 0, 1, 'bool', bit_offset=0) is True
    assert client.written == bytes([0b1010_1011]), '写一个布尔位把同字节其它位清零了'


@pytest.mark.parametrize('value, expected', [(0, 0b1010_1010), (1, 0b1010_1011)])
def test_s7_bool_write_roundtrip(value, expected):
    gw = _bare_gateway()
    client = _FakeS7Client(0b1010_1010)
    gw._clients = {'d1': client}

    gw.write_register('d1', 1, 0, value, 'bool', bit_offset=0)
    assert client.written == bytes([expected])
