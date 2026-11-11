"""报警健康度分类：orphan / standing / stale（ISA-18.2 口径）。"""

from datetime import datetime, timedelta

from 报警层.alarm_analytics import analyse

NOW = datetime(2026, 11, 11, 12, 0, 0)


def _state(first, last=None, acked=False):
    return {
        'alarm_id': 'r1', 'device_id': 'd1', 'register_name': 't',
        'first_trigger_time': first,
        'last_trigger_time': last or first,
        'acknowledged': acked,
    }


def test_orphan_when_rule_missing():
    states = {('gone', 'd1', 't'): _state(NOW - timedelta(minutes=1))}
    report = analyse(states, rules={'r1': {}}, now=NOW)
    assert report['counts']['orphan'] == 1
    assert report['orphan'][0]['alarm_id'] == 'gone'
    # 孤立报警不应同时被算作 standing/stale（分类互斥，避免重复治理）
    assert report['counts']['standing'] == 0
    assert report['counts']['stale'] == 0


def test_standing_when_old_enough():
    states = {('r1', 'd1', 't'): _state(NOW - timedelta(hours=30),
                                        last=NOW - timedelta(minutes=1))}
    report = analyse(states, rules={'r1': {}}, now=NOW, standing_hours=24)
    assert report['counts']['standing'] == 1
    assert report['standing'][0]['age_hours'] == 30.0


def test_stale_when_not_refreshed():
    states = {('r1', 'd1', 't'): _state(NOW - timedelta(hours=2),
                                        last=NOW - timedelta(minutes=45))}
    report = analyse(states, rules={'r1': {}}, now=NOW,
                     standing_hours=24, stale_minutes=30)
    assert report['counts']['stale'] == 1
    assert report['stale'][0]['idle_minutes'] == 45.0


def test_fresh_alarm_is_counted_as_normal():
    states = {('r1', 'd1', 't'): _state(NOW - timedelta(minutes=5),
                                        last=NOW - timedelta(minutes=1),
                                        acked=True)}
    report = analyse(states, rules={'r1': {}}, now=NOW)
    assert report['counts'] == {'orphan': 0, 'standing': 0, 'stale': 0}
    assert report['total'] == 1
    assert report['unacknowledged'] == 0
    assert report['ack_rate'] == 1.0


def test_naive_aware_mixed_does_not_raise():
    aware = datetime(2026, 11, 11, 13, 0, 0).astimezone()
    states = {('r1', 'd1', 't'): _state(aware, aware)}
    report = analyse(states, rules=['r1'], now=NOW)
    assert report['total'] == 1


def test_empty_states():
    report = analyse({}, rules={}, now=NOW)
    assert report['total'] == 0 and report['ack_rate'] is None
