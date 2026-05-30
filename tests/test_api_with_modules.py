"""
API端点测试 - 带模块挂载的完整路径测试
提升展示层/api/覆盖率
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime


class TestControlAPIWithModules:
    """Control API - 带device_control模块"""

    def test_estop_status_with_module(self, client, auth_headers, app):
        """GET /api/control/estop/status 带模块"""
        mock_dc = MagicMock()
        mock_dc.get_estop_status.return_value = {'active': False, 'reason': ''}
        app.device_control = mock_dc
        resp = client.get('/api/control/estop/status', headers=auth_headers)
        assert resp.status_code == 200

    def test_interlocks_with_module(self, client, auth_headers, app):
        """GET /api/control/interlocks 带模块"""
        mock_dc = MagicMock()
        mock_dc.get_interlock_status.return_value = {'rules': []}
        app.device_control = mock_dc
        resp = client.get('/api/control/interlocks', headers=auth_headers)
        assert resp.status_code == 200

    def test_health_with_module(self, client, auth_headers, app):
        """GET /api/control/health 带模块"""
        mock_dc = MagicMock()
        mock_dc.get_device_health_summary.return_value = {'devices': {}}
        app.device_control = mock_dc
        resp = client.get('/api/control/health', headers=auth_headers)
        assert resp.status_code == 200

    def test_estop_trigger(self, client, auth_headers, app):
        """POST /api/control/estop 带模块"""
        mock_dc = MagicMock()
        mock_dc.trigger_emergency_stop.return_value = {'success': True}
        app.device_control = mock_dc
        resp = client.post('/api/control/estop', json={'reason': 'test'},
                           headers=auth_headers)
        assert resp.status_code == 200

    def test_estop_reset(self, client, auth_headers, app):
        """POST /api/control/estop/reset 带模块"""
        mock_dc = MagicMock()
        mock_dc.reset_emergency_stop.return_value = {'success': True}
        app.device_control = mock_dc
        resp = client.post('/api/control/estop/reset', headers=auth_headers)
        assert resp.status_code == 200

    def test_audit_with_module(self, client, auth_headers, app):
        """GET /api/control/audit 带模块"""
        mock_dc = MagicMock()
        mock_dc.get_audit_log.return_value = []
        app.device_control = mock_dc
        resp = client.get('/api/control/audit', headers=auth_headers)
        assert resp.status_code == 200

    def test_status_with_module(self, client, auth_headers, app):
        """GET /api/control/status 带模块"""
        mock_dc = MagicMock()
        mock_dc.get_full_status.return_value = {'estop': False}
        app.device_control = mock_dc
        resp = client.get('/api/control/status', headers=auth_headers)
        assert resp.status_code == 200

    def test_pending_bypasses_with_module(self, client, auth_headers, app):
        """GET /api/control/interlocks/bypass-pending 带模块"""
        mock_dc = MagicMock()
        mock_dc.get_pending_bypasses.return_value = []
        app.device_control = mock_dc
        resp = client.get('/api/control/interlocks/bypass-pending', headers=auth_headers)
        assert resp.status_code == 200

    def test_bypass_interlock_with_module(self, client, auth_headers, app):
        """POST /api/control/interlocks/<id>/bypass 带模块"""
        mock_dc = MagicMock()
        mock_dc.bypass_interlock.return_value = True
        app.device_control = mock_dc
        resp = client.post('/api/control/interlocks/rule1/bypass',
                           json={'reason': 'test'}, headers=auth_headers)
        assert resp.status_code == 200

    def test_restore_interlock_with_module(self, client, auth_headers, app):
        """POST /api/control/interlocks/<id>/restore 带模块"""
        mock_dc = MagicMock()
        mock_dc.restore_interlock.return_value = True
        app.device_control = mock_dc
        resp = client.post('/api/control/interlocks/rule1/restore',
                           headers=auth_headers)
        assert resp.status_code == 200

    def test_batch_control_with_module(self, client, auth_headers, app):
        """POST /api/control/batch 带模块"""
        mock_dc = MagicMock()
        mock_dc.batch_control.return_value = {'success': True, 'message': 'done'}
        app.device_control = mock_dc
        resp = client.post('/api/control/batch', json={'action': 'stop'},
                           headers=auth_headers)
        assert resp.status_code == 200


class TestIndustry40WithModules:
    """Industry 4.0 API - 带模块挂载"""

    def test_health_with_pm(self, client, auth_headers, app):
        """GET /api/industry40/health 带预测性维护模块"""
        mock_pm = MagicMock()
        mock_pm.get_health_scores.return_value = {'dev1': {'health_score': 95.0}}
        app.predictive_maintenance = mock_pm
        resp = client.get('/api/industry40/health', headers=auth_headers)
        assert resp.status_code == 200

    def test_health_device_with_pm(self, client, auth_headers, app):
        """GET /api/industry40/health/<id> 带模块"""
        mock_pm = MagicMock()
        mock_pm.get_device_health.return_value = {'health_score': 95.0}
        app.predictive_maintenance = mock_pm
        resp = client.get('/api/industry40/health/dev1', headers=auth_headers)
        assert resp.status_code == 200

    def test_health_device_not_found(self, client, auth_headers, app):
        """GET /api/industry40/health/<id> 设备无数据"""
        mock_pm = MagicMock()
        mock_pm.get_device_health.return_value = None
        app.predictive_maintenance = mock_pm
        resp = client.get('/api/industry40/health/dev1', headers=auth_headers)
        assert resp.status_code == 404

    def test_maintenance_alerts_with_pm(self, client, auth_headers, app):
        """GET /api/industry40/maintenance-alerts 带模块"""
        mock_pm = MagicMock()
        mock_pm.get_maintenance_alerts.return_value = []
        app.predictive_maintenance = mock_pm
        resp = client.get('/api/industry40/maintenance-alerts', headers=auth_headers)
        assert resp.status_code == 200

    def test_oee_with_module(self, client, auth_headers, app):
        """GET /api/industry40/oee 带模块"""
        mock_oee = MagicMock()
        mock_oee.get_all_oee.return_value = {'dev1': {'oee_percent': 85.0}}
        app.oee_calculator = mock_oee
        resp = client.get('/api/industry40/oee', headers=auth_headers)
        assert resp.status_code == 200

    def test_oee_device_with_module(self, client, auth_headers, app):
        """GET /api/industry40/oee/<id> 带模块"""
        mock_oee = MagicMock()
        mock_oee.get_device_oee.return_value = {'oee_percent': 85.0}
        app.oee_calculator = mock_oee
        resp = client.get('/api/industry40/oee/dev1', headers=auth_headers)
        assert resp.status_code == 200

    def test_oee_device_not_found(self, client, auth_headers, app):
        """GET /api/industry40/oee/<id> 无数据"""
        mock_oee = MagicMock()
        mock_oee.get_device_oee.return_value = None
        app.oee_calculator = mock_oee
        resp = client.get('/api/industry40/oee/dev1', headers=auth_headers)
        assert resp.status_code == 404

    def test_spc_with_module(self, client, auth_headers, app):
        """GET /api/industry40/spc/<id>/<reg> 带模块"""
        mock_spc = MagicMock()
        mock_spc.get_control_chart.return_value = {}
        mock_spc.get_capability.return_value = {}
        app.spc_analyzer = mock_spc
        resp = client.get('/api/industry40/spc/dev1/temp', headers=auth_headers)
        assert resp.status_code == 200

    def test_spc_violations_with_module(self, client, auth_headers, app):
        """GET /api/industry40/spc/violations 带模块"""
        mock_spc = MagicMock()
        mock_spc.get_violations.return_value = []
        app.spc_analyzer = mock_spc
        resp = client.get('/api/industry40/spc/violations', headers=auth_headers)
        assert resp.status_code == 200

    def test_energy_with_module(self, client, auth_headers, app):
        """GET /api/industry40/energy 带模块"""
        mock_em = MagicMock()
        mock_em.get_energy_summary.return_value = {'total_kwh': 1000}
        app.energy_manager = mock_em
        resp = client.get('/api/industry40/energy', headers=auth_headers)
        assert resp.status_code == 200

    def test_energy_cost_with_module(self, client, auth_headers, app):
        """GET /api/industry40/energy/cost 带模块"""
        mock_em = MagicMock()
        mock_em.get_energy_cost_breakdown.return_value = {}
        app.energy_manager = mock_em
        resp = client.get('/api/industry40/energy/cost', headers=auth_headers)
        assert resp.status_code == 200

    def test_energy_carbon_with_module(self, client, auth_headers, app):
        """GET /api/industry40/energy/carbon 带模块"""
        mock_em = MagicMock()
        mock_em.get_carbon_emission.return_value = {}
        app.energy_manager = mock_em
        resp = client.get('/api/industry40/energy/carbon', headers=auth_headers)
        assert resp.status_code == 200

    def test_energy_power_with_module(self, client, auth_headers, app):
        """GET /api/industry40/energy/power 带模块"""
        mock_em = MagicMock()
        mock_em.get_total_power.return_value = 500.0
        mock_em.get_realtime_power.return_value = {}
        app.energy_manager = mock_em
        resp = client.get('/api/industry40/energy/power', headers=auth_headers)
        assert resp.status_code == 200

    def test_energy_tariff_with_module(self, client, auth_headers, app):
        """GET /api/industry40/energy/tariff 带模块"""
        mock_em = MagicMock()
        mock_em.get_tariff_config.return_value = {}
        app.energy_manager = mock_em
        resp = client.get('/api/industry40/energy/tariff', headers=auth_headers)
        assert resp.status_code == 200

    def test_energy_tariff_update_with_module(self, client, auth_headers, app):
        """PUT /api/industry40/energy/tariff 带模块"""
        mock_em = MagicMock()
        mock_em.update_tariff.return_value = {'success': True, 'config': {}}
        app.energy_manager = mock_em
        resp = client.put('/api/industry40/energy/tariff',
                          json={'tariff': {'peak': 1.5}},
                          headers=auth_headers)
        assert resp.status_code == 200

    def test_energy_anomaly_config_with_module(self, client, auth_headers, app):
        """GET /api/industry40/energy/anomaly-config 带模块"""
        mock_em = MagicMock()
        mock_em.get_anomaly_config.return_value = {}
        app.energy_manager = mock_em
        resp = client.get('/api/industry40/energy/anomaly-config', headers=auth_headers)
        assert resp.status_code == 200

    def test_energy_anomaly_config_update(self, client, auth_headers, app):
        """PUT /api/industry40/energy/anomaly-config 带模块"""
        mock_em = MagicMock()
        mock_em.update_anomaly_config.return_value = {'success': True, 'config': {}}
        app.energy_manager = mock_em
        resp = client.put('/api/industry40/energy/anomaly-config',
                          json={'enabled': True}, headers=auth_headers)
        assert resp.status_code == 200

    def test_edge_status_with_module(self, client, auth_headers, app):
        """GET /api/industry40/edge/status 带模块"""
        mock_edge = MagicMock()
        mock_edge.get_status.return_value = {'running': True}
        app.edge_decision = mock_edge
        resp = client.get('/api/industry40/edge/status', headers=auth_headers)
        assert resp.status_code == 200

    def test_edge_rules_with_module(self, client, auth_headers, app):
        """GET /api/industry40/edge/rules 带模块"""
        mock_edge = MagicMock()
        mock_edge.get_rules.return_value = []
        app.edge_decision = mock_edge
        resp = client.get('/api/industry40/edge/rules', headers=auth_headers)
        assert resp.status_code == 200

    def test_edge_log_with_module(self, client, auth_headers, app):
        """GET /api/industry40/edge/log 带模块"""
        mock_edge = MagicMock()
        mock_edge.get_decision_log.return_value = []
        app.edge_decision = mock_edge
        resp = client.get('/api/industry40/edge/log', headers=auth_headers)
        assert resp.status_code == 200

    def test_devices_status_with_module(self, client, auth_headers, app):
        """GET /api/industry40/devices/status 带模块"""
        mock_oee = MagicMock()
        mock_oee.get_all_device_states.return_value = {}
        app.oee_calculator = mock_oee
        resp = client.get('/api/industry40/devices/status', headers=auth_headers)
        assert resp.status_code == 200

    def test_vibration_with_module(self, client, auth_headers, app):
        """GET /api/industry40/vibration 带模块"""
        mock_va = MagicMock()
        mock_va.get_vibration_scores.return_value = {}
        app.vibration_analyzer = mock_va
        resp = client.get('/api/industry40/vibration', headers=auth_headers)
        assert resp.status_code == 200

    def test_vibration_device_with_module(self, client, auth_headers, app):
        """GET /api/industry40/vibration/<id> 带模块"""
        mock_va = MagicMock()
        mock_va.get_device_vibration.return_value = {'score': 85.0}
        app.vibration_analyzer = mock_va
        resp = client.get('/api/industry40/vibration/dev1', headers=auth_headers)
        assert resp.status_code == 200

    def test_vibration_device_not_found(self, client, auth_headers, app):
        """GET /api/industry40/vibration/<id> 无数据"""
        mock_va = MagicMock()
        mock_va.get_device_vibration.return_value = None
        app.vibration_analyzer = mock_va
        resp = client.get('/api/industry40/vibration/dev1', headers=auth_headers)
        assert resp.status_code == 404

    def test_vibration_spectrum_with_module(self, client, auth_headers, app):
        """GET /api/industry40/vibration/<id>/spectrum 带模块"""
        mock_va = MagicMock()
        mock_va.get_spectrum.return_value = {'frequencies': [], 'amplitudes': []}
        app.vibration_analyzer = mock_va
        resp = client.get('/api/industry40/vibration/dev1/spectrum', headers=auth_headers)
        assert resp.status_code == 200

    def test_vibration_bearing_with_module(self, client, auth_headers, app):
        """GET /api/industry40/vibration/<id>/bearing 带模块"""
        mock_va = MagicMock()
        mock_va.check_bearing_fault.return_value = {'fault_detected': False}
        app.vibration_analyzer = mock_va
        resp = client.get('/api/industry40/vibration/dev1/bearing', headers=auth_headers)
        assert resp.status_code == 200


class TestAlarmAPIWithModules:
    """Alarm API - 带模块"""

    def test_alarm_output_with_output(self, client, auth_headers, app):
        """GET /api/alarm-output/status 带输出模块"""
        mock_output = MagicMock()
        mock_output.get_status.return_value = {'enabled': True}
        app.alarm_manager.alarm_output = mock_output
        app.alarm_manager.broadcast_system = None
        resp = client.get('/api/alarm-output/status', headers=auth_headers)
        assert resp.status_code == 200

    def test_alarm_output_acknowledge(self, client, auth_headers, app):
        """POST /api/alarm-output/acknowledge 带输出"""
        mock_output = MagicMock()
        app.alarm_manager.alarm_output = mock_output
        resp = client.post('/api/alarm-output/acknowledge', headers=auth_headers)
        assert resp.status_code == 200

    def test_broadcast_speak_with_system(self, client, auth_headers, app):
        """POST /api/broadcast/speak 带广播系统"""
        mock_bs = MagicMock()
        mock_bs.speak.return_value = {'success': True, 'message': 'sent'}
        app.alarm_manager.broadcast_system = mock_bs
        resp = client.post('/api/broadcast/speak',
                           json={'text': 'Test broadcast'},
                           headers=auth_headers)
        assert resp.status_code == 200

    def test_broadcast_areas_with_system(self, client, auth_headers, app):
        """GET /api/broadcast/areas 带广播系统"""
        mock_bs = MagicMock()
        mock_bs.get_areas.return_value = ['Zone A', 'Zone B']
        app.alarm_manager.broadcast_system = mock_bs
        resp = client.get('/api/broadcast/areas', headers=auth_headers)
        assert resp.status_code == 200

    def test_broadcast_history_with_system(self, client, auth_headers, app):
        """GET /api/broadcast/history 带广播系统"""
        mock_bs = MagicMock()
        mock_bs.get_history.return_value = []
        app.alarm_manager.broadcast_system = mock_bs
        resp = client.get('/api/broadcast/history', headers=auth_headers)
        assert resp.status_code == 200


class TestDeviceAPIWithModules:
    """Device API - 带模块"""

    def test_add_device_opcua_validation(self, client, auth_headers, app):
        """POST /api/devices OPC UA缺少endpoint"""
        resp = client.post('/api/devices', json={
            'id': 'opc1', 'name': 'OPC', 'protocol': 'opcua'
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_add_device_mqtt_validation(self, client, auth_headers, app):
        """POST /api/devices MQTT缺少topics"""
        resp = client.post('/api/devices', json={
            'id': 'mqtt1', 'name': 'MQTT', 'protocol': 'mqtt',
            'host': '127.0.0.1', 'port': 1883
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_add_device_rest_validation(self, client, auth_headers, app):
        """POST /api/devices REST缺少endpoints"""
        resp = client.post('/api/devices', json={
            'id': 'rest1', 'name': 'REST', 'protocol': 'rest',
            'base_url': 'http://localhost'
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_add_device_modbus_missing_host(self, client, auth_headers, app):
        """POST /api/devices Modbus缺少host"""
        resp = client.post('/api/devices', json={
            'id': 'mb1', 'name': 'MB', 'protocol': 'modbus_tcp'
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_stop_device_non_mechanical(self, client, auth_headers, app):
        """POST /api/devices/<id>/stop 非机械类设备"""
        app.device_manager.devices = {
            'sensor1': {'protocol': 'modbus_tcp', 'name': 'Sensor'}
        }
        resp = client.post('/api/devices/sensor1/stop', headers=auth_headers)
        assert resp.status_code == 400

    def test_start_device_non_mechanical(self, client, auth_headers, app):
        """POST /api/devices/<id>/start 非机械类设备"""
        app.device_manager.devices = {
            'sensor1': {'protocol': 'modbus_tcp', 'name': 'Sensor'}
        }
        resp = client.post('/api/devices/sensor1/start', headers=auth_headers)
        assert resp.status_code == 400
