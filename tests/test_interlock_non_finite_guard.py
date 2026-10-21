"""
安全联锁的非有限值防御（round 245）

缺陷
----
`DeviceControlSafety.check_interlocks()` 的阈值比较对 NaN 全部返回 False，
于是 `triggered=False`，走进「条件解除」分支：

    self._interlock_states[rule_id] = False
    logger.info("联锁条件解除: ...")

而 `_interlock_states` 正是 **block_start 联锁的准入依据** ——
`batch_control(action='start')` 与 `validate_write` 都读它。

实测（注入一条 `block_start` 联锁 + 120 触发 + NaN）：

    超温后联锁状态: {'il_temp': True}
    喂 NaN 后联锁状态: {'il_temp': False}
    block_start 保护是否生效: False   ← 超温状态下可以启动设备了

也就是说**一条 NaN 读数能把安全联锁整个绕过**，而且日志还写着
「联锁条件解除」，运维会以为条件真的恢复了。这比 round 244 的报警
「虚假恢复」更严重：那个只是显示问题，这个是**安全功能失效**。

修法与 `AlarmManager.check_alarm` 一致：非有限值 = 读数无效，
**本条不更新状态**（既不触发也不解除），留 warning。

⚠️ 另附一个与代码无关、但必须知道的事实：仓库里
`配置/interlocks.yaml` **不存在**，`配置/devices.yaml` 也没有 `interlocks`
段，`run.py` 构造 `DeviceControlSafety` 时没传 `config` ——
**生产环境启动后联锁规则是 0 条**，只在启动日志里有一句 WARNING。
本文件的用例靠 `config={'interlocks': [...]}` 注入规则，
所以它们验证的是「配了规则之后行为对不对」，不代表线上已经配了规则。
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from unittest.mock import MagicMock  # noqa: E402

from 智能层.device_control import DeviceControlSafety  # noqa: E402

NON_FINITE = [float('nan'), float('inf'), float('-inf')]


def _rule(rule_id='il_temp', op='>', threshold=100.0, action_type='block_start'):
    return {
        'id': rule_id,
        'name': '温度超限联锁',
        'condition': {
            'type': 'threshold',
            'device_id': 'D1',
            'register': 'temp',
            'operator': op,
            'value': threshold,
        },
        'action': {'type': action_type, 'device_id': 'D1'},
        'alarm': {'level': 'critical', 'message': '温度超限'},
    }


@pytest.fixture
def dcm():
    return DeviceControlSafety(MagicMock(), MagicMock(), MagicMock(),
                               config={'interlocks': [_rule()]})


def _triggered(dcm, rule_id='il_temp'):
    return dcm.get_interlock_status()[rule_id]['triggered']


# ============================================================
# 1. 非有限值不能解除已触发的联锁（核心）
# ============================================================

class TestNonFiniteDoesNotRelease:
    @pytest.mark.parametrize('bad', NON_FINITE)
    def test_high_operator_survives(self, dcm, bad):
        dcm.check_interlocks('D1', 'temp', 120.0)
        assert _triggered(dcm)

        dcm.check_interlocks('D1', 'temp', bad)

        assert _triggered(dcm), \
            f'{bad!r} 把已触发的联锁解除了 —— block_start 保护随之失效'

    @pytest.mark.parametrize('bad', NON_FINITE)
    def test_low_operator_survives(self, bad):
        """`<` 规则同样：`inf < 100` 为 False，一条 +Inf 就能解除联锁。"""
        manager = DeviceControlSafety(
            MagicMock(), MagicMock(), MagicMock(),
            config={'interlocks': [_rule(op='<', threshold=50.0)]})
        manager.check_interlocks('D1', 'temp', 10.0)
        assert _triggered(manager)

        manager.check_interlocks('D1', 'temp', bad)

        assert _triggered(manager), f'{bad!r} 把「低于阈值」联锁解除了'

    def test_state_machine_intact_after_nan(self, dcm):
        """NaN 之后真实恢复必须仍能正常解除联锁。"""
        dcm.check_interlocks('D1', 'temp', 120.0)
        dcm.check_interlocks('D1', 'temp', float('nan'))
        assert _triggered(dcm)

        dcm.check_interlocks('D1', 'temp', 80.0)
        assert not _triggered(dcm), '正常值反而解不掉联锁了'


# ============================================================
# 2. 非有限值也不能触发新联锁
# ============================================================

class TestNonFiniteDoesNotTrigger:
    @pytest.mark.parametrize('bad', NON_FINITE)
    def test_no_new_interlock(self, dcm, bad):
        dcm.check_interlocks('D1', 'temp', bad)
        assert not _triggered(dcm), f'{bad!r} 凭空触发了联锁'


# ============================================================
# 3. 必须留痕
# ============================================================

def test_non_finite_is_logged(dcm, caplog):
    with caplog.at_level('WARNING'):
        dcm.check_interlocks('D1', 'temp', float('nan'))

    assert any('非有限值' in r.getMessage() for r in caplog.records), \
        'NaN 被静默跳过了 —— 运维在日志里看不到痕迹'


def test_no_false_release_log(dcm, caplog):
    """NaN 时不能再打「联锁条件解除」—— 那句日志会让人以为条件真的恢复了。"""
    dcm.check_interlocks('D1', 'temp', 120.0)

    with caplog.at_level('INFO'):
        caplog.clear()
        dcm.check_interlocks('D1', 'temp', float('nan'))

    assert not any('联锁条件解除' in r.getMessage() for r in caplog.records), \
        'NaN 触发了一条假的「联锁条件解除」日志'


# ============================================================
# 4. block_start 保护真的还在（端到端）
# ============================================================

def test_block_start_guard_still_blocks_after_nan(dcm):
    """`_interlock_states` 是 block_start 的唯一依据，这里直接断言它。

    （`batch_control` 还需要 device_manager 提供可控设备列表，
    在单测里搭起来噪声太大；状态位是真正的判断依据，断言它更直接。）
    """
    dcm.check_interlocks('D1', 'temp', 120.0)
    dcm.check_interlocks('D1', 'temp', float('nan'))

    status = dcm.get_interlock_status()['il_temp']
    assert status['triggered'] is True
    assert status['bypassed'] is False, '联锁不该被顺手旁路掉'


# ============================================================
# 5. 静态守卫
# ============================================================

def test_check_interlocks_guards_non_finite():
    import pathlib
    import re

    source = (pathlib.Path(PROJECT_ROOT) / '智能层' / 'device_control.py').read_text(encoding='utf-8')
    body = source.split('def check_interlocks', 1)[1].split('def _execute_interlock_action', 1)[0]

    assert re.search(r'try_finite_float\(value\)', body), \
        'check_interlocks 没有非有限值防御 —— NaN 会解除安全联锁'
    assert 'return' in body


def test_guard_runs_before_taking_lock():
    """防御必须在 `with self._lock:` **之前**早返回，不要持锁做无意义的工作。"""
    import pathlib

    source = (pathlib.Path(PROJECT_ROOT) / '智能层' / 'device_control.py').read_text(encoding='utf-8')
    body = source.split('def check_interlocks', 1)[1]

    i_guard = body.find('try_finite_float(value)')
    i_lock = body.find('with self._lock:')

    assert i_guard != -1 and i_lock != -1
    assert i_guard < i_lock, '非有限值防御写在了锁里面（或锁之后）'
