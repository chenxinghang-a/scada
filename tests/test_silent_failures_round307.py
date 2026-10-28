# -*- coding: utf-8 -*-
"""round 307：把「只被测试调用」的功能接上出口 —— ISA-18.2 旁路。

`AlarmManager.shelve_alarm / unshelve_alarm / get_shelved_alarms` 三件套
一直是死 API（`tools/_scan_dead_api.py` 扫出来的 90 个之一）：
后端逻辑齐全、前端有「已旁路」筛选，但**没有任何端点能触发旁路**，
维护窗口里想屏蔽一条刷屏报警只能去改 yaml。
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _mgr():
    m = MagicMock()
    m.shelve_alarm.return_value = True
    m.unshelve_alarm.return_value = True
    m.get_shelved_alarms.return_value = [
        {'alarm_key': ['r1', 'd1', 't'], 'reason': '维护', 'shelved_by': 'op',
         'shelved_until': '2026-10-28T23:00:00'},
    ]
    return m


def test_list_shelved(client, auth_headers, app):
    app.alarm_manager = _mgr()

    resp = client.get('/api/alarms/shelved', headers=auth_headers)

    assert resp.status_code == 200
    assert resp.get_json()['shelved'][0]['reason'] == '维护'


def test_shelve_creates_shelving(client, auth_headers, app):
    mgr = _mgr()
    app.alarm_manager = mgr

    resp = client.post('/api/alarms/r1/shelve', headers=auth_headers,
                       json={'device_id': 'd1', 'register_name': 't',
                             'reason': '检修', 'duration_minutes': 45})

    assert resp.status_code == 200
    assert resp.get_json()['success'] is True
    args = mgr.shelve_alarm.call_args[0]
    assert args[0] == 'r1' and args[1] == 'd1' and args[2] == 't'
    assert mgr.shelve_alarm.call_args.kwargs['duration_minutes'] == 45, '时长没有透传'


def test_shelve_rejects_missing_key(client, auth_headers, app):
    mgr = _mgr()
    app.alarm_manager = mgr

    resp = client.post('/api/alarms/r1/shelve', headers=auth_headers,
                       json={'reason': '检修'})

    assert resp.status_code == 400
    mgr.shelve_alarm.assert_not_called()


def test_shelve_rejects_zero_duration(client, auth_headers, app):
    """0 分钟在 manager 里会变成「永久静默」（shelved_until=None）。"""
    mgr = _mgr()
    app.alarm_manager = mgr

    resp = client.post('/api/alarms/r1/shelve', headers=auth_headers,
                       json={'device_id': 'd1', 'register_name': 't',
                             'duration_minutes': 0})

    assert resp.status_code == 400
    msg = resp.get_json().get('error', '')
    assert '永久静默' in msg or '正数' in msg, msg
    mgr.shelve_alarm.assert_not_called()


def test_shelve_rejects_non_finite_duration(client, auth_headers, app):
    mgr = _mgr()
    app.alarm_manager = mgr

    for bad in ('abc', float('inf'), float('nan'), -5):
        resp = client.post('/api/alarms/r1/shelve', headers=auth_headers,
                           json={'device_id': 'd1', 'register_name': 't',
                                 'duration_minutes': bad})
        assert resp.status_code == 400, f'{bad!r} 没有被拒绝'
    mgr.shelve_alarm.assert_not_called()


def test_unshelve_removes_shelving(client, auth_headers, app):
    mgr = _mgr()
    app.alarm_manager = mgr

    resp = client.post('/api/alarms/r1/unshelve', headers=auth_headers,
                       json={'device_id': 'd1', 'register_name': 't'})

    assert resp.status_code == 200
    mgr.unshelve_alarm.assert_called_once_with('r1', 'd1', 't')


def test_unshelve_returns_404_when_not_shelved(client, auth_headers, app):
    mgr = _mgr()
    mgr.unshelve_alarm.return_value = False
    app.alarm_manager = mgr

    resp = client.post('/api/alarms/r1/unshelve', headers=auth_headers,
                       json={'device_id': 'd1', 'register_name': 't'})

    assert resp.status_code == 404
