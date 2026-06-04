"""
API标准化测试
验证新的响应格式和错误码体系
"""
import pytest
from unittest.mock import patch, MagicMock


class TestAPIStandardization:
    """API响应标准化测试"""

    def test_api_success_response_format(self, client, auth_headers):
        """验证成功响应格式标准化"""
        resp = client.get('/api/devices', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        # 验证响应包含devices字段
        assert 'devices' in data or 'data' in data

    def test_api_error_response_format(self, client, auth_headers):
        """验证错误响应格式标准化"""
        resp = client.get('/api/devices/nonexistent_device', headers=auth_headers)
        assert resp.status_code == 404
        data = resp.get_json()
        # 验证错误响应包含error字段
        assert 'error' in data

    def test_api_unauthorized_response(self, client):
        """验证未认证响应"""
        resp = client.get('/api/devices')
        assert resp.status_code == 401

    def test_api_forbidden_response(self, client, auth_headers):
        """验证权限不足响应"""
        # 使用viewer角色尝试访问admin端点
        resp = client.post('/api/devices', json={'id': 'test'}, headers=auth_headers)
        # 根据角色可能返回403或401
        assert resp.status_code in [401, 403]


class TestErrorCodes:
    """错误码体系测试"""

    def test_error_codes_import(self):
        """验证错误码模块可导入"""
        from 展示层.api.error_codes import (
            SUCCESS, INTERNAL_ERROR, INVALID_REQUEST,
            DEVICE_NOT_FOUND, ALARM_NOT_FOUND,
            get_error_message
        )
        assert SUCCESS == 'SUCCESS'
        assert INTERNAL_ERROR == 'E1000'
        assert DEVICE_NOT_FOUND == 'E3001'

    def test_error_message_lookup(self):
        """验证错误码消息查找"""
        from 展示层.api.error_codes import get_error_message
        assert get_error_message('E1000') == '服务器内部错误'
        assert get_error_message('E3001') == '设备不存在'
        assert get_error_message('UNKNOWN') == '未知错误'


class TestAPIResponseHelpers:
    """API响应辅助函数测试"""

    def test_api_success_function(self):
        """验证api_success函数"""
        from 展示层.api._common import api_success
        result = api_success({'key': 'value'}, '测试成功')
        data = result.get_json()
        assert data['success'] is True
        assert data['message'] == '测试成功'
        assert data['data'] == {'key': 'value'}

    def test_api_error_function(self):
        """验证api_error函数"""
        from 展示层.api._common import api_error
        result = api_error('测试错误', 400, 'E1001')
        data = result.get_json()
        assert data['success'] is False
        assert data['error'] == '测试错误'
        assert data['error_code'] == 'E1001'
        assert result.status_code == 400

    def test_api_paginated_function(self):
        """验证api_paginated函数"""
        from 展示层.api._common import api_paginated
        items = [{'id': 1}, {'id': 2}]
        result = api_paginated(items, total=10, page=1, per_page=2)
        data = result.get_json()
        assert data['success'] is True
        assert len(data['data']) == 2
        assert data['pagination']['total'] == 10
        assert data['pagination']['pages'] == 5


class TestDatabaseIndexes:
    """数据库索引测试"""

    def test_composite_indexes_exist(self, db):
        """验证复合索引已创建"""
        cursor = db.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")
        indexes = {row[0] for row in cursor.fetchall()}
        assert 'idx_alarm_device_ack' in indexes
        assert 'idx_alarm_id_device_register' in indexes
        assert 'idx_history_device_register_time' in indexes


class TestConfigValidation:
    """配置验证测试"""

    def test_get_int_valid(self):
        """验证有效整数配置"""
        with patch.dict('os.environ', {'TEST_INT': '42'}):
            from config import _get_int
            assert _get_int('TEST_INT', 10) == 42

    def test_get_int_invalid(self):
        """验证无效整数配置使用默认值"""
        with patch.dict('os.environ', {'TEST_INT': 'abc'}):
            from config import _get_int
            assert _get_int('TEST_INT', 10) == 10

    def test_get_int_out_of_range(self):
        """验证超出范围的整数使用默认值"""
        with patch.dict('os.environ', {'TEST_INT': '100'}):
            from config import _get_int
            assert _get_int('TEST_INT', 10, min_val=0, max_val=50) == 10

    def test_get_bool_true(self):
        """验证布尔配置true值"""
        with patch.dict('os.environ', {'TEST_BOOL': 'true'}):
            from config import _get_bool
            assert _get_bool('TEST_BOOL', False) is True

    def test_get_bool_false(self):
        """验证布尔配置false值"""
        with patch.dict('os.environ', {'TEST_BOOL': 'false'}):
            from config import _get_bool
            assert _get_bool('TEST_BOOL', True) is False


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
