# -*- coding: utf-8 -*-
"""round 355：设备删除后各层「没清干净」的残留。

主题：一台设备被删除，牵扯 8 个 holder + 数据库。
漏任何一处，表现都是「已删除的东西还在」—— 而且大多**重启后复活**。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1. SPC：还有三个复合键容器被漏掉
# ---------------------------------------------------------------------------

def test_spc_forget_device_clears_buffers_and_counters():
    """`data_buffers` / `_total_fed` / `_violation_seen` 也必须是复合键清理。

    漏掉的后果：删掉的设备样本继续占内存；重新添加同名设备时
    **首轮判异直接继承旧样本**（控制图一上来就"样本充足"或一上来就报警）；
    `_violation_seen` 不清则同一违规在新设备上不再报。
    """
    from 智能层.spc_analyzer import SPCAnalyzer

    spc = SPCAnalyzer({})
    for key in ('d1:temp', 'd1:pressure', 'd10:temp'):
        spc.data_buffers[key].append(1.0)
        spc._total_fed[key] = 10
        spc._violation_seen[key].add(('rule1', 'fp'))

    assert spc.forget_device('d1') is True

    assert [k for k in spc.data_buffers if k.startswith('d1:')] == [], '测量缓冲没清'
    assert [k for k in spc._total_fed if k.startswith('d1:')] == [], '喂入计数没清'
    assert [k for k in spc._violation_seen if k.startswith('d1:')] == [], '判异指纹没清'
    assert 'd10:temp' in spc.data_buffers, '前缀匹配误伤了 d10'


# ---------------------------------------------------------------------------
# 2. 设备控制：`_degraded_devices` 是 set，长期没被清
# ---------------------------------------------------------------------------

def test_device_control_forget_device_clears_degraded_set():
    """删掉再重加的设备不能继续留在 `_degraded_devices` 里。

    留着的话，`update_device_health` 的 `if device_id not in self._degraded_devices`
    会挡住 —— 新设备**再次掉线时不触发安全降级**（关阀/切手动），
    而界面上它看起来是台正常设备。
    """
    from 智能层.device_control import DeviceControlSafety

    dc = DeviceControlSafety(MagicMock(), MagicMock(), MagicMock())
    dc._degraded_devices.add('d1')
    dc._device_lock_owners['d1'] = 'operator1'

    assert dc.forget_device('d1') is True

    assert 'd1' not in dc._degraded_devices, '降级集合没清 —— 重加的设备不会再降级'
    assert 'd1' not in dc._device_lock_owners, '操作锁属主没清'


# ---------------------------------------------------------------------------
# 3. 数据库：删设备时未恢复的报警必须收尾
# ---------------------------------------------------------------------------

def test_delete_device_data_clears_uncleared_alarms(tmp_path):
    """否则 `_restore_active_alarms()` 下次启动会把它们重新捞上屏。

    现象：设备列表里没有这台设备，看板上却挂着它的活跃报警，
    点确认也只能确认一条幽灵。
    """
    from 存储层.database import Database

    db = Database(str(tmp_path / 'scada.db'))
    db.insert_alarm('r1', 'd1', 'temp', 'warning', 'x',
                    threshold=1.0, actual_value=2.0, timestamp=datetime.now())

    db.delete_device_data('d1')

    with db.get_connection(readonly=True) as conn:
        rows = conn.execute(
            'SELECT cleared FROM alarm_records WHERE device_id = ?', ('d1',)).fetchall()
    assert rows, '记录被物理删除了 —— 应保留可追溯，只标记 cleared'
    assert all(r[0] == 1 for r in rows), '未恢复报警没有被标记为已清除'
