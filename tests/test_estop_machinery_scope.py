"""E-STOP 的「机械类」判定边界与归零行为（追溯表 SF-01）。

## 背景

急停的软件侧语义是：**只停机械类设备/点位，传感器与安全设备继续工作**
（`device_control.emergency_stop` 里就是这么筛设备的）。
模拟客户端对应 `_estop_override()`：急停激活时，把名字被判为「机械类」的
寄存器值强制返回 0。

这条"归零"原来**零测试覆盖** —— `_estop_override` / `_is_machinery`
在整个 tests/ 里 grep 不到。

## 这里钉的两件事

1. **归零确实发生**：激活后机械类返回 0，解除后恢复原值。
2. **"机械类"的边界**：`_is_machinery` 的注释写着
   「Word-boundary match to avoid false positives like 'position'
   matching in 'temperature_position'」，但实际呢？
   `(?<![a-z])` 只排除**小写字母**，而 `_` 不是字母，所以
   `temperature_position` 里的 `position` **照样命中** —— 注释描述的
   防护并不存在。

   后果：急停时 `temperature_position`（温度测点位置）、`flow_speed`
   （流体速度）这类**工艺参数**会被归零，操作员看到温度/流量掉到 0，
   会误判现场状态。急停的语义是"停机"，不是"把所有数都清零"。
"""

from __future__ import annotations

import pytest

from 采集层.simulated_client import (
    _ESTOP_FROZEN_VALUES,
    _estop_override,
    _is_machinery,
    set_estop_state,
)


@pytest.fixture(autouse=True)
def _clean_estop():
    """每个用例前后都把全局急停状态复位 —— 它是模块级 globals。"""
    set_estop_state(False)
    yield
    set_estop_state(False)


class TestMachineryBoundary:
    """`_is_machinery` 该认什么、不该认什么。"""

    @pytest.mark.parametrize('name', [
        'spindle_speed',        # 主轴转速
        'line_speed',           # 产线速度
        'spindle_position',     # 主轴位置
        'conveyor_belt',        # 传送带
        'mold_clamping_force',  # 锁模力
        'shot_count',           # 注塑模次
        'cycle_time',           # 机械周期
    ])
    def test_machinery_recognized(self, name):
        assert _is_machinery(name) is True, f'{name} 应判为机械类'

    @pytest.mark.parametrize('name', [
        'boiler_temperature',
        'boiler_pressure',
        'bus_voltage',
        'motor_current',
        'tank_level',
        'ambient_humidity',
    ])
    def test_process_values_not_machinery(self, name):
        assert _is_machinery(name) is False, f'{name} 是工艺参数，不该判为机械类'

    @pytest.mark.parametrize('name', [
        # 名字里带 speed/position，但修饰的是**工艺量**，不是机械动作。
        # 急停时把它们归零，会让操作员看到"温度/流量掉到 0"的假象。
        'temperature_position',
        'flow_speed',
    ])
    def test_process_value_with_machinery_word_is_not_machinery(self, name):
        assert _is_machinery(name) is False, (
            f'{name} 含机械类关键词，但整体是工艺量 —— '
            f'急停时归零会误导操作员（这正是 _is_machinery 注释里声称已避免、'
            f'实际没避免的情况）'
        )


class TestEstopZeroing:
    """归零行为本身。"""

    def test_machinery_zeroed_when_active(self):
        set_estop_state(True)
        assert _estop_override('spindle_speed', 1234.5) == 0.0

    def test_process_value_untouched_when_active(self):
        set_estop_state(True)
        assert _estop_override('boiler_temperature', 88.5) == 88.5, (
            '急停只停机械类；温度/压力这类监视量必须继续显示真实值'
        )

    def test_nothing_changes_when_inactive(self):
        set_estop_state(False)
        assert _estop_override('spindle_speed', 1234.5) == 1234.5

    def test_release_clears_frozen_values(self):
        set_estop_state(True)
        _estop_override('spindle_speed', 100.0)
        assert _ESTOP_FROZEN_VALUES, '激活期间应记录被冻结的值'
        set_estop_state(False)
        assert _ESTOP_FROZEN_VALUES == {}, '解除急停必须清空冻结表'

    def test_zeroing_is_idempotent(self):
        """重复调用仍返回 0（采集会反复读同一个点位）。"""
        set_estop_state(True)
        for _ in range(3):
            assert _estop_override('conveyor_speed', 42.0) == 0.0
