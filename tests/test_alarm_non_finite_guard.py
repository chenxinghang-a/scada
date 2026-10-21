"""
报警判定的非有限值防御（round 244）

缺陷
----
`AlarmManager.check_alarm()` 只挡了 `value is None`，没挡 NaN / ±Infinity。
`_check_condition` 里所有比较对 NaN 都返回 False，于是：

    value=120（阈值 100）→ 触发，状态 ACTIVE
    value=NaN            → triggered=False → `del self.alarm_states[...]`
                         → **报警被静默清除**，还连带撤销升级跟踪

运维看到报警消失，会以为设备自己好了 —— 而实际是「读数无效」。
（实测复现：ACTIVE → CLEARED → 再喂 130 又 ACTIVE，中间那段"恢复"是假的。）

NaN 也**不能**按触发处理：传感器故障时会刷出一堆假报警。
正确语义是「本条不更新状态」—— 既不清除、也不触发，留一条 warning。

第二条口子：`采集层/data_collector._decode_register()` 的
`value * scale + offset` **缩放后没有再过一遍非有限值检查**。
`base_client.decode_float32` 只过滤了原始解码值；scale 配置过大或异常数据
会让缩放结果溢出成 ±inf。缓存采集路径（`_collect_from_cache`）早就有这道
检查，Modbus 路径没有 —— 两条采集路径对同一个问题不一致。
"""

import os
import sys
import textwrap
from datetime import datetime
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from 报警层.alarm_manager import AlarmManager  # noqa: E402

NON_FINITE = [float('nan'), float('inf'), float('-inf')]

RULES_YAML = """
alarm_rules:
  - id: hi
    name: 温度过高
    device_id: D1
    register_name: temp
    condition: greater_than
    threshold: 100
    level: critical
    enabled: true
  - id: lo
    name: 压力过低
    device_id: D1
    register_name: press
    condition: less_than
    threshold: 10
    level: warning
    enabled: true
dedup:
  enabled: true
  emit_cooldown_seconds: 60
  acknowledge_suppress_seconds: 600
"""


@pytest.fixture
def am(tmp_path):
    path = tmp_path / "alarms.yaml"
    path.write_text(textwrap.dedent(RULES_YAML), encoding='utf-8')
    manager = AlarmManager(MagicMock(), str(path))
    manager.alarm_output = None
    manager.broadcast_system = None
    yield manager
    try:
        manager.stop()
    except Exception:
        pass


def _active(am, rule_id, reg):
    return (rule_id, 'D1', reg) in am.alarm_states


# ============================================================
# 1. 非有限值不能清除已激活的报警（核心）
# ============================================================

class TestNonFiniteDoesNotClear:
    @pytest.mark.parametrize('bad', NON_FINITE)
    def test_high_rule_survives(self, am, bad):
        am.check_alarm('D1', 'temp', 120.0, datetime.now())
        assert _active(am, 'hi', 'temp')

        am.check_alarm('D1', 'temp', bad, datetime.now())

        assert _active(am, 'hi', 'temp'), \
            f'{bad!r} 把已激活的「温度过高」报警清掉了（虚假恢复）'

    @pytest.mark.parametrize('bad', NON_FINITE)
    def test_low_rule_survives(self, am, bad):
        """`less_than` 规则尤其危险：`inf < 10` 为 False → 直接清除。"""
        am.check_alarm('D1', 'press', 5.0, datetime.now())
        assert _active(am, 'lo', 'press')

        am.check_alarm('D1', 'press', bad, datetime.now())

        assert _active(am, 'lo', 'press'), \
            f'{bad!r} 把已激活的「压力过低」报警清掉了'

    def test_state_intact_after_nan(self, am):
        """NaN 之后正常值必须仍能正常触发/清除，状态机没被搞坏。"""
        am.check_alarm('D1', 'temp', 120.0, datetime.now())
        am.check_alarm('D1', 'temp', float('nan'), datetime.now())
        assert _active(am, 'hi', 'temp')

        # 真恢复正常 → 该清除
        am.check_alarm('D1', 'temp', 80.0, datetime.now())
        assert not _active(am, 'hi', 'temp'), '正常值反而清不掉报警了'


# ============================================================
# 2. 非有限值也不能触发新报警
# ============================================================

class TestNonFiniteDoesNotTrigger:
    @pytest.mark.parametrize('bad', NON_FINITE)
    def test_no_new_alarm(self, am, bad):
        am.check_alarm('D1', 'temp', bad, datetime.now())
        assert not _active(am, 'hi', 'temp'), f'{bad!r} 凭空触发了报警'

    def test_other_register_unaffected(self, am):
        """一条 NaN 数据不能影响同设备其它寄存器的规则。"""
        am.check_alarm('D1', 'press', 5.0, datetime.now())
        am.check_alarm('D1', 'temp', float('nan'), datetime.now())

        assert _active(am, 'lo', 'press'), '同设备另一条规则被 NaN 连带清掉了'


# ============================================================
# 3. 必须留痕
# ============================================================

def test_non_finite_is_logged(am, caplog):
    with caplog.at_level('WARNING'):
        am.check_alarm('D1', 'temp', float('nan'), datetime.now())

    assert any('非有限值' in r.getMessage() for r in caplog.records), \
        'NaN 被静默跳过了 —— 运维在日志里看不到任何痕迹'


# ============================================================
# 4. Modbus 解码缩放后的非有限值
# ============================================================

class _FakeClient:
    def __init__(self, decoded):
        self._decoded = decoded

    def decode_float32(self, regs):
        return self._decoded

    def decode_uint16(self, reg):
        return self._decoded

    def decode_int16(self, reg):
        return self._decoded


def _decode(decoded, register):
    from 采集层.data_collector import DataCollector
    collector = DataCollector(MagicMock(), MagicMock(), MagicMock())
    return collector._decode_register(_FakeClient(decoded), [0, 0], register)


class TestDecodeGuard:
    def test_scale_overflow_is_dropped(self):
        """`value * scale` 溢出成 inf 时必须丢弃，不能原样入库。"""
        register = {'name': 't', 'data_type': 'float32', 'scale': 1e300, 'offset': 0}
        assert _decode(1e300, register) is None

    def test_normal_scaling_still_works(self):
        register = {'name': 't', 'data_type': 'float32', 'scale': 0.1, 'offset': 5}
        assert _decode(100.0, register) == pytest.approx(15.0)

    def test_offset_overflow_is_dropped(self):
        register = {'name': 't', 'data_type': 'float32', 'scale': 1, 'offset': 1e309}
        assert _decode(1.0, register) is None

    def test_nan_from_client_is_dropped(self):
        """即使某个客户端没过滤 NaN（base_client 会过滤，但不是所有实现都过滤），
        缩放这一步也必须兜住。"""
        register = {'name': 't', 'data_type': 'float32', 'scale': 1, 'offset': 0}
        assert _decode(float('nan'), register) is None


# ============================================================
# 5. 静态守卫
# ============================================================

def test_check_alarm_guards_non_finite():
    import pathlib
    import re

    source = (pathlib.Path(PROJECT_ROOT) / '报警层' / 'alarm_manager.py').read_text(encoding='utf-8')
    body = source.split('def check_alarm', 1)[1].split('def _check_condition', 1)[0]

    assert re.search(r'try_finite_float\(value\)', body), \
        'check_alarm 没有非有限值防御 —— NaN 会把已激活报警清掉'
    assert 'return' in body, '防御分支必须有 return（不更新状态）'


def test_decode_register_guards_scaled_value():
    import pathlib
    import re

    source = (pathlib.Path(PROJECT_ROOT) / '采集层' / 'data_collector.py').read_text(encoding='utf-8')
    body = source.split('def _decode_register', 1)[1].split('def _collect_from_cache', 1)[0]

    assert re.search(r'try_finite_float\(scaled\)', body), \
        '_decode_register 的缩放结果没过非有限值检查（与 _collect_from_cache 不一致）'
