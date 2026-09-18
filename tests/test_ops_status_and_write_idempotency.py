# -*- coding: utf-8 -*-
"""临时验证：round 168c 展示层两条遗留修复（跑完即删）。"""
import threading
import time

import pytest


class _SlowClient:
    def __init__(self, delay=0.6):
        self.connected = True
        self.delay = delay
        self.calls = 0

    def write_single_register(self, address, value):
        self.calls += 1
        time.sleep(self.delay)
        return True


def test_db_tables_error_list_returns_500(app, client, auth_headers, monkeypatch):
    """get_table_stats() 返回 [{'error':...}] 时，接口必须回非 2xx。"""
    import 展示层.api.api_ops as api_ops
    monkeypatch.setattr(api_ops.db_maintainer, 'get_table_stats',
                        lambda: [{'error': '数据库文件损坏'}])
    r = client.get('/api/ops/db/tables', headers=auth_headers)
    assert r.status_code == 500, f'期望 500，实际 {r.status_code}: {r.get_json()}'
    assert r.get_json().get('success') is not True


def test_db_tables_ok_still_200(app, client, auth_headers, monkeypatch):
    """正常形态必须不受影响。"""
    import 展示层.api.api_ops as api_ops
    monkeypatch.setattr(api_ops.db_maintainer, 'get_table_stats',
                        lambda: [{'table': 'history_data', 'rows': 10}])
    r = client.get('/api/ops/db/tables', headers=auth_headers)
    assert r.status_code == 200, f'期望 200，实际 {r.status_code}: {r.get_json()}'


def test_audit_log_records_error_on_failure(app, client, auth_headers, monkeypatch):
    """清理失败时审计日志必须记 error，不能谎报 success。"""
    import 展示层.api.api_ops as api_ops
    monkeypatch.setattr(api_ops.data_cleaner, 'clean_history_data',
                        lambda days: {'status': 'error', 'error': '磁盘只读'})
    seen = {}
    real = api_ops.ops_audit.log_operation

    def spy(operation, **kw):
        seen.update(kw)
        seen['operation'] = operation
        return real(operation, **kw)

    monkeypatch.setattr(api_ops.ops_audit, 'log_operation', spy)
    r = client.post('/api/ops/cleanup/history', json={'retention_days': 90},
                    headers=auth_headers)
    assert r.status_code == 500, f'期望 500，实际 {r.status_code}'
    assert seen.get('result') == 'error', f'审计日志谎报: {seen}'
    assert seen.get('operator') and seen['operator'] != 'system', \
        f'operator 未落到真实用户: {seen}'


def test_write_register_concurrent_second_gets_409(app, client, auth_headers, monkeypatch):
    """并发重复写入：第二个请求必须拿到 409，而不是「成功(幂等)」。"""
    app.device_control = None
    fake = _SlowClient(0.6)
    monkeypatch.setattr(app.device_manager, 'get_client', lambda d: fake)
    monkeypatch.setattr('展示层.api.api_control._recent_writes', {}, raising=False)
    import 展示层.api.api_control as ac
    ac._recent_writes.clear()

    codes = []

    def go():
        c = app.test_client()
        r = c.post('/api/devices/dev1/write-register',
                   json={'address': 100, 'value': 5}, headers=auth_headers)
        codes.append(r.status_code)

    t1 = threading.Thread(target=go)
    t2 = threading.Thread(target=go)
    t1.start()
    time.sleep(0.2)
    t2.start()
    t1.join()
    t2.join()

    assert 409 in codes, f'并发重复写入未挡住: {codes}'
    assert fake.calls == 1, f'寄存器被写入了 {fake.calls} 次，应为 1 次'
