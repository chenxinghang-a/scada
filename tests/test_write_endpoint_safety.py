"""REST 端点写入的安全闸门与客户端签名兼容（round 331，round 333 收紧）。

背景（两个独立缺陷，都在 `展示层/api/api_control.py` 的 `write-endpoint`）
----------------------------------------------------------------------------
**① 绕过全部安全闸门。** 这是 REST 型设备**唯一的写通道**，却直接
`device_manager.get_client()` 拿客户端就写：急停中可写、联锁触发中可写、
安全审计链里没有这条记录、无回读确认。

**② 真实设备路径必然 500。** 三个内置客户端的 `write_endpoint` 签名不一致
（`enhanced_simulated_client` 3 参带 `method`，`rest_client` / `simulated_client`
2 参），而 API 无条件用关键字传 `method=` → `RESTDeviceClient` 每次
`TypeError`，且该路由没挂 `@api_error_handler`，异常直穿成通用 500。

**round 333 收紧（初版自身也是 fail-open）**
--------------------------------------------
初版用 `getattr(dc, '_estop_active', False)` / `'_interlock_rules'` /
`'_interlock_bypass'` 读私有属性 —— **三处默认值全部指向「放行」**：
属性改名或缺失时（DI 替换、测试桩、重构），急停中仍可下发端点写入，
而且没有任何告警。现在改用公共方法 `get_estop_status()` /
`get_interlock_status()`，**取不到或结构不符一律 503**。
同时补上结果审计（原来只有 attempt，没有 ok / failed）。
"""

from unittest.mock import MagicMock


def _setup(app, client_obj, endpoints=None):
    """装一台 REST 设备 + 一个可控的安全模块（走公共方法，不碰私有属性）。"""
    dm = app.device_manager
    dm.devices = {'rest_dev': {
        'protocol': 'rest',
        'name': 'REST设备',
        'endpoints': endpoints or [
            {'name': 'ep1', 'path': '/cmd', 'write_method': 'POST'},
        ],
    }}
    dm.get_client.return_value = client_obj

    dc = MagicMock()
    dc.get_estop_status.return_value = {'active': False}
    dc.get_interlock_status.return_value = {}
    dc._audit = MagicMock(return_value=True)
    app.device_control = dc
    return dc


def _interlock(*, action_device=None, cond_device=None, triggered=True,
               bypassed=False, action_type='write_register'):
    """造一条 `get_interlock_status()` 的单项。"""
    act = {'type': action_type}
    if action_device is not None:
        act['device_id'] = action_device
    cond = {}
    if cond_device is not None:
        cond['device_id'] = cond_device
    return {'triggered': triggered, 'bypassed': bypassed,
            'action': act, 'condition': cond}


def _post(client, auth_headers, payload=None):
    return client.post('/api/devices/rest_dev/write-endpoint',
                       json=payload or {'endpoint': 'ep1', 'value': 1},
                       headers=auth_headers)


class _OkClient:
    connected = True

    def write_endpoint(self, endpoint_config, value, method='PUT'):
        return True


class TestWriteEndpointSignatureCompat:
    """真实客户端的 `write_endpoint` 只有 2 个形参 —— 必须能调通。"""

    def test_two_arg_client_is_callable(self, app, client, auth_headers):
        """RESTDeviceClient 风格（2 参）→ 不能 TypeError。"""
        class TwoArgClient:
            connected = True

            def write_endpoint(self, endpoint_config, value):
                return True

        _setup(app, TwoArgClient())
        r = _post(client, auth_headers)
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['success'] is True

    def test_three_arg_client_still_works(self, app, client, auth_headers):
        """enhanced_simulated_client 风格（3 参带 method）→ 仍然可用。"""
        seen = {}

        class ThreeArgClient:
            connected = True

            def write_endpoint(self, endpoint_config, value, method='PUT'):
                seen['method'] = method
                return True

        _setup(app, ThreeArgClient())
        r = _post(client, auth_headers, {'endpoint': 'ep1', 'value': 1,
                                         'method': 'POST'})
        assert r.status_code == 200, r.get_json()
        assert seen['method'] == 'POST', 'API 传的 method 没透传给客户端'

    def test_kwargs_client_also_works(self, app, client, auth_headers):
        """接受 **kwargs 的实现也应被认为「能吃 method」。"""
        class KwargsClient:
            connected = True

            def write_endpoint(self, endpoint_config, value, **kwargs):
                return True

        _setup(app, KwargsClient())
        r = _post(client, auth_headers)
        assert r.status_code == 200, r.get_json()

    def test_unsupported_client_returns_501(self, app, client, auth_headers):
        """既没有 put/post 也没有 write_endpoint → 501，而不是静默 400。"""
        class BareClient:
            connected = True

        _setup(app, BareClient())
        r = _post(client, auth_headers)
        assert r.status_code == 501
        assert '不支持端点写入' in r.get_json()['message']


class TestWriteEndpointSafetyGates:
    """四项前置闸门：安全模块 / 急停 / 已触发联锁 / 审计。"""

    def test_estop_blocks_write(self, app, client, auth_headers):
        dc = _setup(app, _OkClient())
        dc.get_estop_status.return_value = {'active': True}
        r = _post(client, auth_headers)
        assert r.status_code == 403
        assert '急停' in r.get_json()['message']

    def test_triggered_interlock_blocks_write(self, app, client, auth_headers):
        dc = _setup(app, _OkClient())
        dc.get_interlock_status.return_value = {
            'rule1': _interlock(action_device='rest_dev')}
        r = _post(client, auth_headers)
        assert r.status_code == 403
        assert 'rule1' in r.get_json()['message']

    def test_interlock_matched_by_condition_device(self, app, client,
                                                   auth_headers):
        """round 333：`condition.device_id` 命中也要拦。

        原来只看 `action.device_id` —— 配一条 `action: {type: emergency_stop}`
        （没有 device_id）的规则时，它触发后该设备的端点写入仍能过。
        """
        dc = _setup(app, _OkClient())
        dc.get_interlock_status.return_value = {
            'rule1': _interlock(cond_device='rest_dev',
                                action_type='emergency_stop')}
        r = _post(client, auth_headers)
        assert r.status_code == 403
        assert 'rule1' in r.get_json()['message']

    def test_bypassed_interlock_does_not_block(self, app, client, auth_headers):
        """反向对照：联锁已旁路 → 正常写入（别修过头把旁路也拦了）。"""
        dc = _setup(app, _OkClient())
        dc.get_interlock_status.return_value = {
            'rule1': _interlock(action_device='rest_dev', bypassed=True)}
        r = _post(client, auth_headers)
        assert r.status_code == 200, r.get_json()

    def test_other_device_interlock_does_not_block(self, app, client,
                                                   auth_headers):
        """反向对照：联锁针对别的设备 → 不拦这台。"""
        dc = _setup(app, _OkClient())
        dc.get_interlock_status.return_value = {
            'rule1': _interlock(action_device='other_dev')}
        r = _post(client, auth_headers)
        assert r.status_code == 200, r.get_json()

    def test_missing_safety_module_returns_503(self, app, client, auth_headers):
        _setup(app, _OkClient())
        app.device_control = None
        r = _post(client, auth_headers)
        assert r.status_code == 503
        assert '安全模块' in r.get_json()['message']

    def test_audit_failure_blocks_write(self, app, client, auth_headers):
        """审计写不进磁盘 → 拒绝下发（round 322 的 fail-closed 原则）。"""
        dc = _setup(app, _OkClient())
        dc._audit = MagicMock(return_value=False)
        r = _post(client, auth_headers)
        assert r.status_code == 503
        assert '审计' in r.get_json()['message']


class TestSafetyStateUnreadableIs503:
    """round 333：**读不到安全状态就拒绝**，不能默认「没急停 / 无联锁」。

    初版用 `getattr(dc, '_estop_active', False)` —— 属性缺失时默认 False
    等于「没急停」→ 放行。这正是本项目反复批判的 fail-open 形状。
    """

    def test_estop_status_raises_returns_503(self, app, client, auth_headers):
        dc = _setup(app, _OkClient())
        dc.get_estop_status.side_effect = RuntimeError('boom')
        r = _post(client, auth_headers)
        assert r.status_code == 503, '读不到急停状态却放行了'
        assert '急停' in r.get_json()['message']

    def test_estop_status_wrong_shape_returns_503(self, app, client,
                                                  auth_headers):
        dc = _setup(app, _OkClient())
        dc.get_estop_status.return_value = {'unexpected': 1}   # 缺 active
        r = _post(client, auth_headers)
        assert r.status_code == 503

    def test_estop_status_not_dict_returns_503(self, app, client, auth_headers):
        dc = _setup(app, _OkClient())
        dc.get_estop_status.return_value = 'ok'
        r = _post(client, auth_headers)
        assert r.status_code == 503

    def test_interlock_status_raises_returns_503(self, app, client,
                                                 auth_headers):
        dc = _setup(app, _OkClient())
        dc.get_interlock_status.side_effect = RuntimeError('boom')
        r = _post(client, auth_headers)
        assert r.status_code == 503, '读不到联锁状态却放行了'
        assert '联锁' in r.get_json()['message']

    def test_interlock_status_not_dict_returns_503(self, app, client,
                                                   auth_headers):
        dc = _setup(app, _OkClient())
        dc.get_interlock_status.return_value = ['not', 'a', 'dict']
        r = _post(client, auth_headers)
        assert r.status_code == 503


class TestAuditRecordsResult:
    """round 333：审计要三态齐全（attempt / ok / failed）。

    原来只有 attempt，成功只写通用操作日志、失败一条 `_audit` 都没有 ——
    出事时无法自证「到底下发了没有、结果是什么」。
    """

    @staticmethod
    def _actions(dc):
        return [c.args[0] for c in dc._audit.call_args_list]

    def test_success_records_ok(self, app, client, auth_headers):
        dc = _setup(app, _OkClient())
        _post(client, auth_headers)
        acts = self._actions(dc)
        assert any(a.endswith('_attempt') for a in acts), '缺 attempt'
        assert any(a.endswith('_ok') for a in acts), '缺成功结果审计'

    def test_client_failure_records_failed(self, app, client, auth_headers):
        class FailingClient:
            connected = True

            def write_endpoint(self, endpoint_config, value, method='PUT'):
                return False

        dc = _setup(app, FailingClient())
        _post(client, auth_headers)
        acts = self._actions(dc)
        assert any(a.endswith('_attempt') for a in acts)
        assert any(a.endswith('_failed') for a in acts), '缺失败结果审计'

    def test_missing_endpoint_records_failed(self, app, client, auth_headers):
        """参数错（端点不存在）也要留一条结果，否则只有 attempt 无法自证。"""
        dc = _setup(app, _OkClient())
        _post(client, auth_headers, {'endpoint': 'nope', 'value': 1})
        acts = self._actions(dc)
        assert any(a.endswith('_failed') for a in acts)
