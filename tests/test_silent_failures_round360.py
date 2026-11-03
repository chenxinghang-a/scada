# -*- coding: utf-8 -*-
"""round 360：设备生命周期监听器「注册了但没人通知」。

`run.py` 里 `add_device_remove_listener(_forget_device_state)` 注册了一个清理回调，
日志还打「已注册设备删除监听器（清理 OEE/能耗/振动/预测维护/SPC/安全联锁/时序）」——
但 `notify_device_removed()` 在**生产代码里零调用**（只有测试在调）。

后果：那条监听器从注册那天起就没被触发过，`tsdb_adapter`（时序库侧）的
按设备状态**从未清理**；而日志让人以为它一直在工作。

两条清理函数的内容还不一致：
· `api_devices._forget_device_everywhere` —— 走 `current_app`，拿不到 `tsdb_adapter`
· `run.py::_forget_device_state` —— 闭包变量，能拿到 `tsdb_adapter`，但没有 `alarm_manager`
"""

from __future__ import annotations

from pathlib import Path
import pytest

_REPO = Path(__file__).resolve().parent.parent


def test_delete_device_code_notifies_listeners_in_both_branches():
    """两个删除成功分支都必须 `notify_device_removed`。

    这里不用 HTTP 层测：`tests/conftest.py` 的 `app` fixture 是最小 Flask，
    只注册了部分蓝图，`DELETE /api/devices/<id>` 会 404。
    直接断言源码里两处调用都在 —— 删除路径只有这两条出口。
    """
    src = (_REPO / '展示层' / 'api' / 'api_devices.py').read_text(encoding='utf-8')

    forget_calls = src.count('_forget_device_everywhere(device_id)')
    notify_calls = src.count('notify_device_removed(device_id)')

    assert forget_calls == 2, f'删除成功分支数量变了（{forget_calls} 处），请复核本测试'
    assert notify_calls == 2, (
        f'只有 {notify_calls} 处 notify_device_removed，应为 2 处 —— '
        '漏掉的那个分支里监听器收不到通知，时序库侧不会清理')


def test_run_py_listener_covers_alarm_manager():
    """`run.py::_forget_device_state` 的模块元组必须含 `alarm_manager`。

    它此前只有 OEE/能耗/振动/预测维护/SPC/采集/安全联锁/时序 ——
    报警层的按设备状态（已触发标记、旁路、冷却、升级跟踪）没人清。
    """
    src = (_REPO / 'run.py').read_text(encoding='utf-8')
    start = src.find('def _forget_device_state')
    assert start > 0, 'run.py 里没找到 _forget_device_state'
    block = src[start:start + 900]

    assert 'alarm_manager' in block, (
        '_forget_device_state 的清理列表里没有 alarm_manager —— '
        '删掉设备后它在报警层的已触发标记/旁路/升级跟踪会残留')
    assert 'tsdb_adapter' in block, '清理列表里丢了 tsdb_adapter（时序库侧）'
