"""审计写入失败时的行为（round 347）。

## 缺陷

`_audit()` 返回 bool（round 322 加的），但全仓 19 处调用里只有 **2 处**
检查了返回值（`write_with_verification` 与 `batch_control` 的 attempt）。
其余 17 处 —— 急停、旁路审批、故障降级、结果记录 —— **丢弃返回值**。

后果分两种：

1. **静默**：失败时 `_audit` 只打一行「审计日志写入失败」，
   **不带 action / operator**。现场看到这行不知道「哪次操作没被记下来」。
2. **假审批**：`approve_bypass` 先 `approvals.append()` 再写审计且不看结果。
   磁盘满时内存显示「2/2 审批通过、旁路生效」，磁盘上**没有任何审批记录** ——
   而联锁旁路恰恰是最需要双人复核留痕的操作。

## 修法

- `_audit` 失败日志升为 CRITICAL，带 `action / operator / detail` 三元组；
- `approve_bypass` 改成「先写审计、成功才落 approvals」——
  这是唯一能 fail-closed 的地方（审批还没生效，拒绝不影响现场）；
- **急停不 fail-closed**：安全优先，审计失败也必须停设备，但必须留 CRITICAL。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def control(tmp_path):
    """一个真实的 DeviceControlSafety，审计文件指向一个**目录**（写入必失败）。"""
    from 智能层.device_control import DeviceControlSafety

    device_manager = MagicMock()
    device_manager.devices = {}
    device_manager.get_all_devices.return_value = {}

    dc = DeviceControlSafety(MagicMock(), device_manager, MagicMock())
    dc.alarm_manager = MagicMock()
    dc.alarm_manager.alarm_output = None
    dc.alarm_manager.broadcast_system = None
    # 把审计文件指到一个**目录**上：open(..., 'a') 会抛 IsADirectoryError，
    # 从而稳定复现「磁盘满 / 只读目录」这一类的写入失败。
    dc._audit_file = tmp_path
    return dc


class TestAuditFailureIsLoud:
    """审计写不进去时必须**大声报**，且报得出是哪次操作。"""

    def test_returns_false(self, control):
        assert control._audit('probe', 'tester', 'detail') is False

    def test_logs_action_operator_detail(self, control, caplog):
        """日志必须带三元组 —— 否则现场不知道「哪次操作没被记下来」。"""
        import logging
        with caplog.at_level(logging.CRITICAL):
            control._audit('emergency_stop', 'alice', '冷却水中断')

        text = '\n'.join(r.getMessage() for r in caplog.records)
        assert 'emergency_stop' in text, f'日志里没有 action: {text}'
        assert 'alice' in text, f'日志里没有 operator: {text}'
        assert '冷却水中断' in text, f'日志里没有 detail: {text}'

    def test_level_is_critical(self, control, caplog):
        """自证缺口是严重事件，不能是普通 ERROR。"""
        import logging
        with caplog.at_level(logging.CRITICAL):
            control._audit('probe', 'tester', 'd')
        assert any(r.levelno >= logging.CRITICAL for r in caplog.records), \
            '审计失败没有按 CRITICAL 记录'


class TestBypassApprovalFailClosed:
    """审批记录写不进去，审批就不能算通过。"""

    def _make_request(self, control):
        import time as _t

        from 智能层.device_control import BypassRequest
        now = _t.time()
        req = BypassRequest(
            request_id='r1', interlock_id='il1', requested_by='alice',
            requested_at=now, expires_at=now + 1800,
            reason='检修', required_approvals=2,
        )
        control._bypass_requests['r1'] = req
        return req

    def test_approval_rejected_when_audit_fails(self, control):
        req = self._make_request(control)
        ok, msg = control.approve_bypass('r1', 'bob')
        assert ok is False, '审计失败时审批仍返回成功 —— 会出现「内存已批、磁盘无记录」'
        assert '审计' in msg, f'提示没说清原因: {msg}'

    def test_approvals_not_appended_on_failure(self, control):
        """失败时 `approvals` 不能变 —— 否则重试会重复计数。"""
        req = self._make_request(control)
        before = list(req.approvals)
        control.approve_bypass('r1', 'bob')
        assert req.approvals == before, f'审批人被加进去了: {req.approvals}'

    def test_approval_works_when_audit_ok(self, control, tmp_path):
        """审计正常时审批照常通过（确认 fail-closed 没有误伤）。"""
        control._audit_file = tmp_path / 'audit.log'
        req = self._make_request(control)
        ok, msg = control.approve_bypass('r1', 'bob')
        assert ok is True, msg
        assert req.approvals == ['bob']
        assert (tmp_path / 'audit.log').exists()


class TestEmergencyStopStillRuns:
    """急停**不能**因为审计失败就不执行（安全优先）。"""

    def test_estop_executes_despite_audit_failure(self, control):
        result = control.trigger_emergency_stop('测试')
        assert result.get('success') is True, (
            f'审计失败导致急停没执行 —— 安全动作不能被日志挡住: {result}')

    def test_estop_logs_critical_on_audit_failure(self, control, caplog):
        import logging
        with caplog.at_level(logging.CRITICAL):
            control.trigger_emergency_stop('测试')
        assert any('自证缺口' in r.getMessage() for r in caplog.records), \
            '急停执行了但审计失败，却没有 CRITICAL 提示'
