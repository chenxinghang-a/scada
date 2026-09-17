"""设备自动重连循环的存活性回归测试。

背景（2026-09 审计遗留项）
--------------------------
`DeviceManager.switch_simulation_mode()` 内部调用 `disconnect_all()`，
而后者会把 `_reconnect_running` 置为 `False` —— 这在一处是**正确**的：
`disconnect_all()` 同时服务于 `run.py` 的关停路径。

但**模式切换不是关停**。原实现切换后从不重启重连循环，后果是
**自动重连永久静默失效**：运维切一次模拟/真实模式，之后任何设备掉线
都不会再重连，而且没有任何报错 —— 界面上只看到设备一直离线。

本测试锁死「模式切换不得杀死重连循环」，同时确认关停语义没被改坏。
"""
import threading
import time

import pytest
import yaml

from 采集层.device_manager import DeviceManager


@pytest.fixture
def mgr(tmp_path):
    """最小配置的设备管理器（无设备 → connect_all() 秒回，测试不受网络影响）"""
    cfg = tmp_path / 'devices.yaml'
    cfg.write_text(
        yaml.dump({'devices': []}, allow_unicode=True), encoding='utf-8'
    )
    return DeviceManager(config_path=str(cfg), simulation_mode=True,
                         use_enhanced_simulation=False)


def _wait_until(pred, timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def test_switch_mode_keeps_reconnect_loop_alive(mgr):
    """核心回归：切换模式后重连循环必须仍在运行。

    修复前 `_reconnect_running` 会被 disconnect_all() 置 False 且不再恢复，
    该断言失败。
    """
    mgr.start_reconnect_loop(interval=3600)  # 间隔拉大，避免测试期间真的重连
    assert mgr._reconnect_running is True

    mgr.switch_simulation_mode(False)

    assert mgr._reconnect_running is True, (
        'switch_simulation_mode() 之后自动重连循环被永久关闭了 —— '
        'disconnect_all() 的关停语义被误用在了模式切换路径上'
    )


def test_switch_mode_reports_loop_restoration(mgr):
    """返回值要如实说明是否恢复了重连循环，便于运维确认"""
    mgr.start_reconnect_loop(interval=3600)
    result = mgr.switch_simulation_mode(False)
    assert result['success'] is True
    assert result['reconnect_loop_restored'] is True


def test_switch_mode_does_not_start_loop_if_it_was_stopped(mgr):
    """循环本来就没跑（例如尚未 start_reconnect_loop）→ 切换后也不该被启动。

    避免「模式切换顺手启动了一个没人要的后台线程」这种反向错误。
    """
    assert mgr._reconnect_running is False
    result = mgr.switch_simulation_mode(False)
    assert mgr._reconnect_running is False
    assert result['reconnect_loop_restored'] is False


def test_reconnect_interval_is_preserved_across_switch(mgr):
    """恢复时要用原来的间隔，不能悄悄退回默认 30s"""
    mgr.start_reconnect_loop(interval=7)
    assert mgr._reconnect_interval == 7

    mgr.switch_simulation_mode(False)

    assert mgr._reconnect_interval == 7, '模式切换后重连间隔被改回了默认值'


def test_interval_defaults_and_is_recorded(mgr):
    """默认间隔 30s；start_reconnect_loop 必须把它记下来"""
    assert mgr._reconnect_interval == 30
    mgr.start_reconnect_loop()
    assert mgr._reconnect_interval == 30


def test_disconnect_all_still_stops_loop(mgr):
    """关停语义不能被我这次修复改坏：disconnect_all() 仍须停掉循环。

    这是 `run.py` 关停路径依赖的行为。
    """
    mgr.start_reconnect_loop(interval=3600)
    assert mgr._reconnect_running is True

    mgr.disconnect_all()

    assert mgr._reconnect_running is False, (
        'disconnect_all() 不再停止重连循环了 —— 关停时会留下游离线程'
    )


def test_reconnect_state_initialized_before_any_call(tmp_path):
    """`_reconnect_running` 必须在 __init__ 里就有值。

    此前它只靠 disconnect_all() / start_reconnect_loop() 赋值，
    任何更早读取它的路径都会 AttributeError。
    """
    cfg = tmp_path / 'empty.yaml'
    cfg.write_text('devices: []', encoding='utf-8')
    m = DeviceManager(config_path=str(cfg), simulation_mode=True,
                      use_enhanced_simulation=False)
    # 不调用任何方法，直接读
    assert m._reconnect_running is False
    assert m._reconnect_interval == 30


def test_loop_thread_actually_still_running(mgr):
    """不只查标志位 —— 确认后台线程本身还活着且会继续轮询。"""
    mgr.start_reconnect_loop(interval=3600)
    threads_before = {t.ident for t in threading.enumerate() if t.is_alive()}

    mgr.switch_simulation_mode(False)

    threads_after = {t.ident for t in threading.enumerate() if t.is_alive()}
    assert threads_after - threads_before or mgr._reconnect_running, (
        '模式切换后重连线程消失了'
    )
