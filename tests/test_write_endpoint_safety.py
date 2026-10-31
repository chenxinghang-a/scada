"""REST 端点写入的安全闸门与客户端签名兼容（round 331）。

背景（两个独立缺陷，都在 `展示层/api/api_control.py` 的 `write-endpoint`）
----------------------------------------------------------------------------
**① 绕过全部安全闸门。** 这是 REST 型设备**唯一的写通道**，却直接
`device_manager.get_client()` 拿客户端就写：急停中可写、联锁触发中可写、
安全审计链里没有这条记录、无回读确认。同文件的 `write-register` /
`write-coil` 都走 `device_control`，唯独这条漏了。

**② 真实设备路径必然 500。** 三个内置客户端的 `write_endpoint` 签名不一致：
`enhanced_simulated_client` 是 3 参（带 `method`），`rest_client` /
`simulated_client` 是 2 参；而 API 无条件用关键字传 `method=`。
`RESTDeviceClient`（真实设备）每次都 `TypeError`，且该路由没挂
`@api_error_handler`，异常直穿成通用 500 —— 客户端只看到
`{"error": "Internal server error"}`，运维无从判断。

修法：三个客户端签名统一补 `method` 形参（可选，退回配置里的 write_method）；
API 侧补「安全模块存在性 / 急停 / 已触发联锁 / 审计落盘」四项前置闸门，
并给写入调用包一层可读错误。
"""

from unittest.mock import MagicMock


def _setup(app, client_obj, endpoints=None):
    """装一台 REST 设备 + 一个可控的安全模块。"""
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
    dc._estop_active = False
    dc._interlock_rules = {}
    dc._interlock_states = {}
    dc._interlock_bypass = set()
    dc._audit = MagicMock(return_value=True)
    app.device_control = dc
    return dc


def _post(client, auth_headers, payload=None):
    return client.post('/api/devices/rest_dev/write-endpoint',
                       json=payload or {'endpoint': 'ep1', 'value': 1},
                       headers=auth_headers)


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

    def _ok_client(self, app):
        class OkClient:
            connected = True

            def write_endpoint(self, endpoint_config, value, method='PUT'):
                return True

        return _setup(app, OkClient())

    def test_estop_blocks_write(self, app, client, auth_headers):
        dc = self._ok_client(app)
        dc._estop_active = True
        r = _post(client, auth_headers)
        assert r.status_code == 403
        assert '急停' in r.get_json()['message']

    def test_triggered_interlock_blocks_write(self, app, client, auth_headers):
        dc = self._ok_client(app)
        dc._interlock_rules = {'rule1': {'id': 'rule1',
                                         'action': {'device_id': 'rest_dev'}}}
        dc._interlock_states = {'rule1': True}
        r = _post(client, auth_headers)
        assert r.status_code == 403
        assert 'rule1' in r.get_json()['message']

    def test_bypassed_interlock_does_not_block(self, app, client, auth_headers):
        """反向对照：联锁已旁路 → 正常写入（别修过头把旁路也拦了）。"""
        dc = self._ok_client(app)
        dc._interlock_rules = {'rule1': {'id': 'rule1',
                                         'action': {'device_id': 'rest_dev'}}}
        dc._interlock_states = {'rule1': True}
        dc._interlock_bypass = {'rule1'}
        r = _post(client, auth_headers)
        assert r.status_code == 200, r.get_json()

    def test_other_device_interlock_does_not_block(self, app, client,
                                                   auth_headers):
        """反向对照：联锁针对别的设备 → 不拦这台。"""
        dc = self._ok_client(app)
        dc._interlock_rules = {'rule1': {'id': 'rule1',
                                         'action': {'device_id': 'other_dev'}}}
        dc._interlock_states = {'rule1': True}
        r = _post(client, auth_headers)
        assert r.status_code == 200, r.get_json()

    def test_missing_safety_module_returns_503(self, app, client, auth_headers):
        _setup(app, MagicMock(connected=True))
        app.device_control = None
        r = _post(client, auth_headers)
        assert r.status_code == 503
        assert '安全模块' in r.get_json()['message']

    def test_audit_failure_blocks_write(self, app, client, auth_headers):
        """审计写不进磁盘 → 拒绝下发（round 322 的 fail-closed 原则）。"""
        dc = self._ok_client(app)
        dc._audit = MagicMock(return_value=False)
        r = _post(client, auth_headers)
        assert r.status_code == 503
        assert '审计' in r.get_json()['message']
