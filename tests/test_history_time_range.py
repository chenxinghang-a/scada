# -*- coding: utf-8 -*-
"""历史查询：时间范围真的生效 + 「原始」真的是原始（round 226）。

三个缺陷，叠在一起时**历史页的核心功能是坏的**，而界面看不出异常：

1. **参数名不匹配（范围筛选完全无效）**
   前端 `dataApi.getHistory(device, reg, { start, end })` 直接拼成
   `?start=...&end=...`；后端只读 `request.args.get('start_time'/'end_time')`
   → 永远 None → 回落到「最近 1 小时」。
   用户选了「近 7 天」，看到的仍是最近 1 小时，**没有任何提示**。
   （首次进页面 `timeRange` 是 null、不传参数，所以默认视图是对的 ——
    这正是它一直没被发现的原因。）

2. **UTC aware 与本地 naive 混比（偏 8 小时）**
   前端用 `Date.toISOString()` 传 UTC（`...T07:00:00.000Z`），
   `fromisoformat` 解析成 **aware**；而库里存的是 `datetime.now()` 的
   **本地 naive** 字符串。aware 绑进 SQL 会变成 `2026-10-21 07:00:00+00:00`，
   与库存值做**字符串比较** → 整段窗口偏 8 小时（UTC+8 下查出来是空的）。

3. **「原始」不是原始（静默聚合）**
   前端「原始」下拉项的值是 `''`，而后端 `request.args.get('interval', '1min')`
   在参数存在但为空时返回 `''`（不是默认值）→ 落到 `else` 分支被
   `GROUP BY 秒` 聚合：10Hz 数据被压成 1Hz 平均值，同秒多条被压成 1 条，
   而界面标注的是「原始明细（按采样间隔，不做聚合）」。

另外两个「静默」：
* 聚合结果按 `LIMIT 10000` **静默截断**，图表只画前 10000 个点、后半段消失；
* `5min` 分支**完全没有 LIMIT**（唯一漏网上限的分支）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from 存储层.database import MAX_HISTORY_POINTS, Database


# ---------------------------------------------------------------------------
# 1. 时间解析：UTC aware → 本地 naive
# ---------------------------------------------------------------------------

def test_parse_time_converts_utc_to_local_naive():
    """前端传 `...Z`（UTC），必须转成本地 naive —— 库里就是本地 naive。"""
    from 展示层.api.api_data import _parse_time

    raw = '2026-10-21T07:00:00.000Z'
    got = _parse_time(raw, None)

    assert got.tzinfo is None, f'解析结果仍带时区（{got.tzinfo}）—— 绑进 SQL 会偏 8 小时'
    expected = datetime.fromisoformat(raw).astimezone().replace(tzinfo=None)
    assert got == expected


def test_parse_time_keeps_naive_input():
    from 展示层.api.api_data import _parse_time

    assert _parse_time('2026-10-21T15:00:00', None) == datetime(2026, 10, 21, 15, 0)


def test_parse_time_falls_back_on_garbage():
    from 展示层.api.api_data import _parse_time

    fallback = datetime(2026, 1, 1, 0, 0)
    assert _parse_time('not-a-time', fallback) is fallback
    assert _parse_time(None, fallback) is fallback


# ---------------------------------------------------------------------------
# 2. DB 层：「原始」不聚合 / 未知粒度报错 / 上限生效
# ---------------------------------------------------------------------------

def _make_db(tmp_path) -> Database:
    return Database(str(tmp_path / 'history.db'))


def _seed_sub_second(database: Database, base: datetime, n: int = 5):
    """同一秒内的多条采样 —— 用来区分「原始」与「按秒聚合」。"""
    rows = [
        {
            'device_id': 'd1', 'register_name': 'temp', 'value': float(i),
            'unit': 'C', 'timestamp': base + timedelta(milliseconds=100 * i),
        }
        for i in range(n)
    ]
    database.insert_data_batch(rows)
    return rows


@pytest.mark.parametrize('interval', ['', 'raw', 'none'])
def test_raw_interval_returns_every_sample(tmp_path, interval):
    """`''` / `raw` / `none` 必须返回**每一条**采样，不按秒合并。"""
    db = _make_db(tmp_path)
    base = datetime(2026, 10, 21, 12, 0, 0)
    try:
        _seed_sub_second(db, base, n=5)
        rows = db.get_history_data(
            'd1', 'temp', base - timedelta(seconds=1), base + timedelta(seconds=1),
            interval=interval,
        )
        assert len(rows) == 5, (
            f'interval={interval!r} 只返回 {len(rows)} 条 —— 同一秒的多条被聚合了'
            '（前端标注的是「原始明细，不做聚合」）'
        )
        assert set(rows[0]) >= {'timestamp', 'value'}, f'原始模式字段不对: {sorted(rows[0])}'
        assert [r['value'] for r in rows] == [0.0, 1.0, 2.0, 3.0, 4.0], '原始模式不应改变顺序/取值'
    finally:
        db.close()


def test_minute_interval_still_aggregates(tmp_path):
    """回归：聚合粒度不能被上面的改动弄坏。"""
    db = _make_db(tmp_path)
    base = datetime(2026, 10, 21, 12, 0, 0)
    try:
        _seed_sub_second(db, base, n=5)
        rows = db.get_history_data(
            'd1', 'temp', base - timedelta(seconds=1), base + timedelta(seconds=1),
            interval='1min',
        )
        assert len(rows) == 1, f'1min 应合成 1 个桶，实际 {len(rows)}'
        assert rows[0]['sample_count'] == 5
        assert rows[0]['avg_value'] == pytest.approx(2.0)
    finally:
        db.close()


def test_unknown_interval_raises_instead_of_silently_aggregating(tmp_path):
    """拼错的粒度必须报错，不能静默变成「按秒聚合」。"""
    db = _make_db(tmp_path)
    try:
        with pytest.raises(ValueError) as ei:
            db.get_history_data('d1', 'temp', datetime(2026, 1, 1), datetime(2026, 1, 2),
                                interval='1miin')
        assert '1miin' in str(ei.value)
    finally:
        db.close()


def test_limit_is_honored_on_every_branch(tmp_path):
    """所有分支都必须遵守 limit（`5min` 分支原来完全没有 LIMIT）。

    ⚠️ 数据必须**每个桶一条**：如果 10 条样本都落在同一个 5 分钟桶里，
    结果只有 1 个桶，`len(rows) <= limit` 恒成立 —— 那种用例对
    「有没有 LIMIT」是瞎的（第一版就是这么写的，变异验证没变红）。
    """
    step_by_interval = {
        '': timedelta(seconds=1),      # 原始：每条一行
        'raw': timedelta(seconds=1),
        '1min': timedelta(minutes=1),
        '5min': timedelta(minutes=5),
        '1hour': timedelta(hours=1),
        '1day': timedelta(days=1),
    }
    base = datetime(2026, 10, 1, 0, 0, 0)

    for interval, step in step_by_interval.items():
        db = _make_db(tmp_path / f'limit_{interval or "raw"}.db')
        try:
            rows = [
                {
                    'device_id': 'd1', 'register_name': 'temp', 'value': float(i),
                    'unit': 'C', 'timestamp': base + step * i,
                }
                for i in range(10)
            ]
            db.insert_data_batch(rows)
            got = db.get_history_data(
                'd1', 'temp', base - step, base + step * 10,
                interval=interval, limit=3,
            )
            assert len(got) == 3, (
                f'interval={interval!r} 没有遵守 limit=3（返回 {len(got)} 条）'
                ' —— 结果集无上限，30 年数据能一次拉爆内存'
            )
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 3. API 层：范围参数真的被用上（含别名）+ 截断要告知
# ---------------------------------------------------------------------------

def _history_call(client, query: str):
    return client.get(f'/api/data/history/d1/temp?{query}', headers=None)


def test_endpoint_uses_start_time_params(client, auth_headers):
    """规范参数名 `start_time`/`end_time` 必须被传进 DB 层。"""
    db = client.application.database
    db.get_history_data = MagicMock(return_value=[])

    resp = client.get(
        '/api/data/history/d1/temp'
        '?start_time=2026-10-21T07:00:00.000Z&end_time=2026-10-21T08:00:00.000Z&interval=1min',
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)[:200]

    kwargs = db.get_history_data.call_args.kwargs
    expected = datetime.fromisoformat('2026-10-21T07:00:00+00:00').astimezone().replace(tzinfo=None)
    assert kwargs['start_time'] == expected, (
        '后端没把 start_time 传给 DB —— 时间范围筛选不生效（永远查最近 1 小时）'
    )
    assert kwargs['end_time'] == expected + timedelta(hours=1)


def test_endpoint_accepts_start_alias(client, auth_headers):
    """前端历史页/趋势对比传的是 `start`/`end`，必须同样生效。

    （更彻底的做法是前端改成规范名；后端同时兼容，避免老客户端/脚本失效。）
    """
    db = client.application.database
    db.get_history_data = MagicMock(return_value=[])

    resp = client.get(
        '/api/data/history/d1/temp'
        '?start=2026-10-21T07:00:00.000Z&end=2026-10-21T08:00:00.000Z&interval=1min',
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)[:200]

    kwargs = db.get_history_data.call_args.kwargs
    expected = datetime.fromisoformat('2026-10-21T07:00:00+00:00').astimezone().replace(tzinfo=None)
    assert kwargs['start_time'] == expected, '`start` 别名没生效 —— 前端选的区间被丢弃'


def test_endpoint_empty_interval_means_raw(client, auth_headers):
    """`?interval=`（前端「原始」）不能被当成「未知粒度」。"""
    db = client.application.database
    db.get_history_data = MagicMock(return_value=[])

    resp = client.get(
        '/api/data/history/d1/temp?interval=&start=2026-10-21T07:00:00Z&end=2026-10-21T08:00:00Z',
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert db.get_history_data.call_args.kwargs['interval'] == ''


def test_endpoint_rejects_reversed_range(client, auth_headers):
    db = client.application.database
    db.get_history_data = MagicMock(return_value=[])

    resp = client.get(
        '/api/data/history/d1/temp'
        '?start_time=2026-10-21T08:00:00Z&end_time=2026-10-21T07:00:00Z',
        headers=auth_headers,
    )
    assert resp.status_code == 400, '结束早于开始应直接拒绝，而不是返回空图'


def test_endpoint_rejects_oversized_range_before_querying(client, auth_headers):
    """30 年 × 1min ≈ 1580 万桶 —— 必须在查库**之前**拒绝，否则白扫全表。"""
    db = client.application.database
    db.get_history_data = MagicMock(return_value=[])

    resp = client.get(
        '/api/data/history/d1/temp'
        '?start_time=2000-01-01T00:00:00Z&end_time=2030-01-01T00:00:00Z&interval=1min',
        headers=auth_headers,
    )
    assert resp.status_code == 400
    msg = resp.get_json().get('error', '')      # 响应体里的中文是 \uXXXX 转义的，不能用原文匹配
    assert '聚合粒度' in msg, f'错误提示要告诉用户怎么改，实际: {msg!r}'
    db.get_history_data.assert_not_called()


def test_endpoint_reports_truncation(client, auth_headers):
    """结果被截断时必须告知 —— 静默截断会让图表后半段「凭空消失」。"""
    db = client.application.database
    db.get_history_data = MagicMock(return_value=[{'value': i} for i in range(MAX_HISTORY_POINTS + 1)])

    resp = client.get(
        '/api/data/history/d1/temp?interval=&start=2026-10-21T07:00:00Z&end=2026-10-21T08:00:00Z',
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body['data']) == MAX_HISTORY_POINTS
    assert body['truncated'] is True, '截断了却没告知 —— 用户会以为那段时间没数据'


def test_endpoint_not_truncated_when_under_limit(client, auth_headers):
    db = client.application.database
    db.get_history_data = MagicMock(return_value=[{'value': 1}])

    resp = client.get(
        '/api/data/history/d1/temp?interval=&start=2026-10-21T07:00:00Z&end=2026-10-21T08:00:00Z',
        headers=auth_headers,
    )
    assert resp.get_json()['truncated'] is False
