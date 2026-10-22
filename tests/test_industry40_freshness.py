# -*- coding: utf-8 -*-
"""智能层读接口必须说明数据新鲜度（round 254）。

背景
----
`/industry40/*` 的读接口把分析器的字典**原样返回**。而分析器的数据只在
「采集器喂数」时更新：

- 降级把某个功能停掉（`degradation_manager` 的 ENFORCED_FEATURES）时，
  `_dispatch_intelligence` 直接跳过该模块 → 数据**不再更新**；
- 设备全离线、采集线程死掉时同理。

于是这些接口会**一直返回最后一帧旧值**：

    {"dev1:temp": {"health_score": 95, "updated_at": "2026-10-22T10:00:00"}}

看板上这就是「设备健康 95」，没人知道它是 8 小时前的数据。
这比「没有数据」更危险 —— **冻结的健康评分看起来像健康的设备**。

修法（非破坏性，前端零改动）：
1. `_freshness()` 计算 `fresh/stale/unknown` + 数据年龄 + 降级停用说明；
2. 挂在**响应头**（`X-Data-Freshness` 等）—— 前端 `industry40.ts` 把
   `/industry40/health` 当扁平字典用（`Record<string, HealthScore>`），
   body 里加字段会直接打断它；
3. 新增 `GET /api/industry40/freshness` 汇总端点，方便运维/看板。

本文件把这三件事钉死。
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _fresh(**kw):
    from 展示层.api.api_industry40 import _freshness
    return _freshness(**kw)


# ===========================================================================
# 1. _freshness 的判定
# ===========================================================================

class TestFreshnessHelper:
    def test_fresh_when_recent(self):
        records = {'d1:r': {'health_score': 90,
                            'updated_at': datetime.now().isoformat()}}
        out = _fresh(records=records)
        assert out['freshness'] == 'fresh'
        assert out['data_age_seconds'] < 5

    def test_stale_when_old(self):
        old = (datetime.now() - timedelta(hours=8)).isoformat()
        records = {'d1:r': {'health_score': 90, 'updated_at': old}}
        out = _fresh(records=records)
        assert out['freshness'] == 'stale'
        assert out['data_age_seconds'] > 3600
        assert '阈值' in out['stale_reason']

    def test_unknown_when_no_timestamp(self):
        """没有时间戳就明说"无法判断"，不要默认当新鲜。"""
        records = {'d1': {'status': 'running', 'since': 'x'}}
        out = _fresh(records=records)
        assert out['freshness'] == 'unknown'
        assert '无法判断' in out['stale_reason']

    def test_epoch_timestamp_supported(self):
        records = {'d1': {'value': 1, 'timestamp': datetime.now().timestamp()}}
        out = _fresh(records=records)
        assert out['freshness'] == 'fresh'

    def test_broken_timestamp_does_not_crash(self):
        records = {'d1': {'updated_at': '不是时间'}, 'd2': {'updated_at': 'x'}}
        out = _fresh(records=records)
        assert out['freshness'] == 'unknown'

    def test_newest_wins(self):
        """多条记录取最新的那条算年龄。"""
        records = {
            'old': {'updated_at': (datetime.now() - timedelta(hours=9)).isoformat()},
            'new': {'updated_at': datetime.now().isoformat()},
        }
        out = _fresh(records=records)
        assert out['freshness'] == 'fresh'

    def test_empty_records_is_unknown(self):
        assert _fresh(records={})['freshness'] == 'unknown'


# ===========================================================================
# 2. 降级停喂必须被点明
# ===========================================================================

class TestDegradationAwareness:
    def test_disabled_feature_is_reported(self, monkeypatch):
        """功能被降级停用 → 明确说"停止喂数"，而不是只报 stale。

        这条最重要：只报 stale 的话，运维会去查"数据源是不是挂了"，
        而真正原因是系统自己把喂数停了（降级）。

        用**真实的** degradation_manager，把等级调到 MODERATE ——
        该等级真的会禁用 predictive_maintenance（见 DISABLED_FEATURES），
        比伪造一个假 manager 更可信。
        """
        from core.degradation_manager import degradation_manager as dm
        from core.degradation_manager import DegradationLevel

        monkeypatch.setattr(dm, '_level', DegradationLevel.MODERATE)
        assert dm.is_feature_enabled('predictive_maintenance') is False, (
            'MODERATE 等级应当禁用预测性维护（前置条件不成立）')

        out = _fresh(records={'d1': {'updated_at': datetime.now().isoformat()}},
                     feature='predictive_maintenance')
        assert out['feature_enabled'] is False
        assert out['freshness'] == 'stale'
        assert '降级' in out['stale_reason'] and '停止喂数' in out['stale_reason']

    def test_enabled_feature_reports_true(self, monkeypatch):
        from core.degradation_manager import degradation_manager as dm
        from core.degradation_manager import DegradationLevel

        monkeypatch.setattr(dm, '_level', DegradationLevel.NORMAL)
        out = _fresh(records={'d1': {'updated_at': datetime.now().isoformat()}},
                     feature='predictive_maintenance')
        assert out['feature_enabled'] is True
        assert out['freshness'] == 'fresh'


# ===========================================================================
# 3. 响应头（不改 body）+ 汇总端点
# ===========================================================================

class TestApiSurface:
    @pytest.fixture
    def fake_pm(self, app):
        from unittest.mock import MagicMock
        pm = MagicMock()
        pm.get_health_scores.return_value = {
            'd1:temp': {'health_score': 95,
                        'updated_at': (datetime.now() - timedelta(hours=8)).isoformat()}
        }
        pm.get_device_health.return_value = {
            'd1:temp': {'health_score': 95,
                        'updated_at': datetime.now().isoformat()}
        }
        app.predictive_maintenance = pm
        return pm

    def test_health_body_shape_unchanged(self, fake_pm, client, auth_headers):
        """**前端契约**：body 必须还是扁平字典（键是 device:register）。

        第一版我把 body 改成 `{scores: ..., freshness: ...}`，
        而 `src/api/industry40.ts` 写的是
        `getHealthScores() as Promise<Record<string, HealthScore>>` ——
        那会直接打断前端。改成响应头后 body 不变。
        """
        r = client.get('/api/industry40/health', headers=auth_headers)
        assert r.status_code == 200, r.get_json()
        data = r.get_json()['data']
        assert 'd1:temp' in data, f'body 结构被改了: {list(data)[:5]}'
        assert 'scores' not in data, 'body 里混进了包装字段 —— 前端会解析不到设备'

    def test_freshness_in_headers(self, fake_pm, client, auth_headers):
        r = client.get('/api/industry40/health', headers=auth_headers)
        assert r.headers.get('X-Data-Freshness') == 'stale'
        assert int(float(r.headers['X-Data-Age-Seconds'])) > 3600

    def test_fresh_data_header(self, fake_pm, client, auth_headers):
        r = client.get('/api/industry40/health/d1', headers=auth_headers)
        assert r.status_code == 200
        assert r.headers.get('X-Data-Freshness') == 'fresh'

    def test_summary_endpoint(self, fake_pm, client, auth_headers):
        r = client.get('/api/industry40/freshness', headers=auth_headers)
        assert r.status_code == 200, r.get_json()
        data = r.get_json()['data']
        assert 'predictive_maintenance' in data
        assert data['predictive_maintenance']['freshness'] == 'stale'
        # 未挂载的模块要如实报 unavailable，而不是消失
        assert data['vibration_analyzer']['freshness'] in ('unavailable', 'unknown', 'stale', 'fresh')

    def test_summary_reports_unmounted_module(self, app, client, auth_headers):
        app.vibration_analyzer = None
        r = client.get('/api/industry40/freshness', headers=auth_headers)
        data = r.get_json()['data']
        assert data['vibration_analyzer']['freshness'] == 'unavailable'
        assert '未挂载' in data['vibration_analyzer']['stale_reason']
