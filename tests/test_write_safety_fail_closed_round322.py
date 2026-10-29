# -*- coding: utf-8 -*-
"""round 322：写入链路的**失效方向**（安全层必须 fail-closed）。

## 为什么单独写这一组

主人原话：「至少不能出严重 bug 才行，工业软件出问题我得坐牢的」。

所以这一轮不看"有没有异常"，只看一件事：**出故障时，系统是拒绝还是放行？**
下面每条用例都是一个"安全层拿不准"的场景，期望值统一是**拒绝**。

## 本轮修的三处（全是 fail-open）

### 1. 未知寄存器直接放行（最严重）

```python
profile = self._profiles.get(address)
if profile is None:
    return True, '未知寄存器（无安全档案）'      # ← 量程/联锁/二次确认全部跳过
```

设备配置里没有这个地址 → **整条安全校验链被短路**，值直接下发到从站。
触发路径全都很现实：地址写错一位、配置漏登记、调用方传了超出配置的地址
（不同厂商的地址映射表并不统一，落到哪个寄存器只有从站知道）。

### 2. 回读不一致仍报"成功"

```python
result = {'success': True, ...}
if verified is False:
    result['message'] = '写入成功，但回读验证失败（设备未接受该值）'
```

`success` 仍是 True。前端只看 `success` → 弹绿条；接口调用方按 2xx 判定成功。
而设备**并没有变成期望的值**。判据：**"没生效"就是失败**。

### 3. 安全模块不可用时"降级"成弱校验继续写

`api_control` 在 `device_control is None` 时，只查 INT16 范围就下发 ——
急停、联锁、旁路、二次确认、回读验证全部消失，而调用方看到同一个 200。
安全层的正确降级方向是**拒绝**。

## 顺带：幂等保护原本只在已删除的降级分支里

`_recent_writes` 的 2 秒去重 + `_IN_FLIGHT` 占位原来写在降级分支里 ——
也就是说**真正在跑的主路径完全没有防重复提交**。已搬到主路径。
"""

import threading
import time
from unittest.mock import MagicMock

import pytest

from core.write_safety import WriteSafetyValidator


DEVICE = {
    'id': 'd1', 'name': '注塑机', 'protocol': 'modbus_tcp',
    'registers': [
        {'address': 1, 'name': 'temperature', 'unit': '°C',
         'data_type': 'int16', 'access': 'rw'},
        {'address': 2, 'name': 'status', 'unit': '',
         'data_type': 'uint16', 'access': 'ro'},
        {'address': 3, 'name': 'boiler_pressure', 'unit': 'MPa',
         'data_type': 'int16', 'access': 'rw'},
    ],
}


@pytest.fixture
def validator():
    return WriteSafetyValidator(DEVICE)


# ---------------------------------------------------------------------------
# 1. 未知寄存器：必须拒绝
# ---------------------------------------------------------------------------

def test_unknown_register_is_rejected(validator):
    """**本轮核心**：配置里没有的地址必须拒绝（原来 return True 一路放行）。"""
    allowed, reason = validator.validate_write(999, 10.0)
    assert allowed is False, (
        '未知寄存器被放行了 —— 量程/联锁/二次确认全部被跳过，值会直接下发到从站')
    assert '不在设备' in reason or '登记' in reason
    assert validator.blocked_count == 1, '被拒绝的写入必须计数（运维要能看见）'


def test_unknown_register_rejected_for_every_value(validator):
    """反向对照：不是"某些值被拒"，而是**这个地址一律拒绝**（含合法量程内的值）。"""
    for value in (0, 1, 50, 65535):
        allowed, _ = validator.validate_write(999, value)
        assert allowed is False, f'value={value} 被放行了'


# ---------------------------------------------------------------------------
# 2. 已知寄存器：原有的四道校验不能退化
# ---------------------------------------------------------------------------

def test_readonly_register_is_rejected(validator):
    allowed, reason = validator.validate_write(2, 1)
    assert allowed is False and '只读' in reason


def test_out_of_range_is_rejected(validator):
    allowed, reason = validator.validate_write(1, 999999)
    assert allowed is False and '越界' in reason


def test_non_finite_is_rejected(validator):
    for bad in (float('nan'), float('inf'), float('-inf')):
        allowed, _ = validator.validate_write(1, bad)
        assert allowed is False, f'{bad} 被放行了（NaN 比较恒 False，量程判不出）'


def test_boiler_pressure_limit_is_enforced(validator):
    """锅炉压力设定值不得超过 4.0 MPa（GB/T 15969）。

    ⚠️ 实测：`_INTERLOCK_RULES` 里四条规则都是**常量阈值**，而且量程表
    （`_SAFETY_RULES`）给的范围**与它完全相同**（boiler_pressure: [0,4]）。
    所以先命中的永远是量程校验，联锁那一段对这几个寄存器**不可达**。
    这不构成安全缺口（限值照样拦住），但也别指望它是"动态联锁" ——
    它不看 `current_val`，只是第二个静态上限。
    """
    allowed, reason = validator.validate_write(3, 5.0, current_value=3.5)
    assert allowed is False
    assert '越界' in reason or '联锁' in reason


def test_valid_write_passes(validator):
    """反向对照：合法写入不能被拦死（否则系统没法用）。"""
    allowed, reason = validator.validate_write(1, 50.0)
    assert allowed is True, reason


# ---------------------------------------------------------------------------
# 3. 端到端：拒绝之后**一个字节都不许发出去**
# ---------------------------------------------------------------------------

class _RecordingModbus:
    """只记录"有没有真的被调用"，不实现协议。"""

    def __init__(self, read_value=50):
        self.writes = []
        self.read_value = read_value

    def write_register(self, address, value, slave):
        self.writes.append((address, value))
        return MagicMock(isError=lambda: False)

    def read_holding_registers(self, address, count, slave=None):
        return [self.read_value]


def _modbus_client():
    from 采集层.modbus_client import ModbusClient
    cfg = dict(DEVICE)
    cfg.update({'host': '127.0.0.1', 'port': 502, 'slave_id': 1})
    client = ModbusClient(cfg)
    fake = _RecordingModbus()
    client.client = fake
    client.connected = True
    return client, fake


def test_rejected_write_never_reaches_the_device():
    """**最重要的一条**：被安全层拒绝的写入，不能出现在从站的调用记录里。

    这条比"返回 False"更硬 —— 返回 False 也可能是"发出去以后失败了"。
    """
    client, fake = _modbus_client()
    ok = client.write_single_register(999, 10)
    assert ok is False
    assert fake.writes == [], f'被拒绝的写入仍然下发了: {fake.writes}'


def test_accepted_write_reaches_the_device():
    """反向对照：合法写入必须真的下发（否则上面的用例是假绿）。"""
    client, fake = _modbus_client()
    ok = client.write_single_register(1, 50)
    assert ok is True, '合法写入被拦了 —— 系统不可用'
    assert fake.writes == [(1, 50)]


# ---------------------------------------------------------------------------
# 4. 回读不一致 = 失败（不是"成功但有个 warning"）
# ---------------------------------------------------------------------------

class _FakeReadResponse:
    """模仿 pymodbus 的响应对象：`ModbusClient` 用的是 `.isError()` + `.registers`。

    （第一版直接返回 list，`ModbusClient` 会抛
    `'list' object has no attribute 'isError'` → 回读失败 → `verified=None`，
    用例测的就不是"值不一致"而是"读不出来"了。）
    """

    def __init__(self, registers):
        self.registers = registers

    def isError(self):
        return False


class _MismatchModbus:
    """写进去 100，读回来 90（模拟从站不接受该值 / 映射错位）。"""

    def __init__(self):
        self.writes = []

    def write_register(self, address, value, slave):
        self.writes.append((address, value))
        return MagicMock(isError=lambda: False)

    def read_holding_registers(self, address, count, slave=None):
        return _FakeReadResponse([90])


def test_readback_mismatch_is_reported_as_failure():
    """回读不一致 → `success=False` + `effective=False`。

    端到端走 `DeviceControlSafety.write_with_verification`：
    这是操作员点"下发"时真正经过的路径。
    """
    from 智能层.device_control import DeviceControlSafety

    cfg = dict(DEVICE)
    cfg.update({'host': '127.0.0.1', 'port': 502, 'slave_id': 1})
    from 采集层.modbus_client import ModbusClient
    client = ModbusClient(cfg)
    client.client = _MismatchModbus()
    client.connected = True

    dm = MagicMock()
    dm.devices = {'d1': cfg}
    dm.get_client = lambda did: client

    dc = DeviceControlSafety(MagicMock(), dm, None,
                             config={'interlocks': []})
    res = dc.write_with_verification('d1', 1, 100, 'op')

    assert res['verified'] is False
    assert res['success'] is False, (
        '回读不一致却报成功 —— 操作员会以为设定值已经改了')
    assert res['effective'] is False
    assert '未生效' in res['message']


# ---------------------------------------------------------------------------
# 5. 审计留痕：写不进去就不许执行 + 哈希链
# ---------------------------------------------------------------------------

def _dc(tmp_path, monkeypatch):
    """构造一个 `DeviceControlSafety`，审计文件指向 tmp_path。"""
    from 智能层.device_control import DeviceControlSafety
    import 智能层.device_control as dc_mod

    monkeypatch.setattr(dc_mod.paths, 'resolve',
                        lambda p: tmp_path / p.replace('data/', ''), raising=False)

    cfg = dict(DEVICE)
    cfg.update({'host': '127.0.0.1', 'port': 502, 'slave_id': 1})
    from 采集层.modbus_client import ModbusClient
    client = ModbusClient(cfg)
    client.client = _RecordingModbus(read_value=50)
    client.connected = True

    dm = MagicMock()
    dm.devices = {'d1': cfg}
    dm.get_client = lambda did: client
    return DeviceControlSafety(MagicMock(), dm, None, config={'interlocks': []})


def test_audit_failure_blocks_write(tmp_path, monkeypatch):
    """**审计写不进去 → 拒绝执行写入**。

    原来审计落盘失败只打一行 ERROR，操作照常执行 ——
    于是磁盘满 / 目录只读时，操作**做了但没有任何记录**，
    出事时"查不到"和"没发生"没法区分。
    """
    dc = _dc(tmp_path, monkeypatch)
    # 把审计文件指向一个不可创建的路径（父级是文件）
    blocker = tmp_path / 'not_a_dir'
    blocker.write_text('x', encoding='utf-8')
    dc._audit_file = blocker / 'audit.jsonl'

    res = dc.write_with_verification('d1', 1, 50, 'op')

    assert res['success'] is False
    assert res.get('audit_failed') is True
    assert '审计日志不可写' in res['message']


def test_audit_failure_means_no_write_reaches_device(tmp_path, monkeypatch):
    """审计失败时**一个字节都不许下发**（与"被安全层拒绝"同样硬）。"""
    dc = _dc(tmp_path, monkeypatch)
    blocker = tmp_path / 'not_a_dir'
    blocker.write_text('x', encoding='utf-8')
    dc._audit_file = blocker / 'audit.jsonl'

    client = dc.device_manager.get_client('d1')
    res = dc.write_with_verification('d1', 1, 50, 'op')

    assert res['success'] is False
    assert client.client.writes == [], f'审计失败却下发了写入: {client.client.writes}'


def test_audit_hash_chain_is_continuous(tmp_path, monkeypatch):
    """哈希链必须连续：每条 `prev` == 上一条 `hash`，且 `hash` 可复算。

    作用：删/改中间任意一条，后面的链就对不上 —— 能证明日志没被动过。
    """
    import hashlib
    import json

    dc = _dc(tmp_path, monkeypatch)
    for i in range(3):
        assert dc._audit('unit_test', 'op', f'第 {i} 条') is True

    lines = [json.loads(ln) for ln in
             dc._audit_file.read_text(encoding='utf-8').splitlines() if ln.strip()]
    assert len(lines) == 3

    prev = ''
    for entry in lines:
        assert entry['prev'] == prev, '链断了：prev 不等于上一条的 hash'
        body = {k: v for k, v in entry.items() if k != 'hash'}
        expect = hashlib.sha256(
            (entry['prev'] + json.dumps(body, ensure_ascii=False, sort_keys=True)
             ).encode('utf-8')).hexdigest()[:16]
        assert entry['hash'] == expect, 'hash 不可复算 —— 记录可能被改过'
        prev = entry['hash']


def test_audit_failure_does_not_pollute_memory(tmp_path, monkeypatch):
    """落盘失败时**不许**往内存列表里塞 —— 否则接口能看到"文件里没有"的记录。"""
    dc = _dc(tmp_path, monkeypatch)
    blocker = tmp_path / 'not_a_dir'
    blocker.write_text('x', encoding='utf-8')
    dc._audit_file = blocker / 'audit.jsonl'

    before = len(dc._audit_log)
    assert dc._audit('x', 'op', 'y') is False
    assert len(dc._audit_log) == before


def test_write_attempt_is_audited_before_execution(tmp_path, monkeypatch):
    """写入必须留下 `write_attempt`（执行前）与 `write_success`（执行后）两条。"""
    dc = _dc(tmp_path, monkeypatch)
    res = dc.write_with_verification('d1', 1, 50, 'op')
    assert res['success'] is True

    actions = [e['action'] for e in dc.get_audit_log(limit=20)]
    assert 'write_attempt' in actions, '缺少执行前的意图记录'
    assert 'write_success' in actions
    # attempt 必须在 success 之前（时间序：get_audit_log 是倒序，所以 attempt 在后）
    assert actions.index('write_success') < actions.index('write_attempt')
