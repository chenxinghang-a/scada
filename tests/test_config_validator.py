"""
配置Schema验证测试
"""
import pytest
import yaml
import tempfile
import os
from pathlib import Path
from core.config_validator import (
    validate_config,
    validate_all_configs,
    validate_startup_configs,
    DEVICE_SCHEMA,
    ALARM_SCHEMA,
    SYSTEM_SCHEMA,
    SCHEMAS,
)


@pytest.fixture
def tmp_yaml(tmp_path):
    """创建临时YAML文件的辅助函数"""
    def _write(data, filename="test.yaml"):
        filepath = tmp_path / filename
        with open(filepath, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True)
        return str(filepath)
    return _write


# ==================== 设备配置验证 ====================

class TestDeviceSchema:
    """设备配置Schema验证"""

    def test_valid_device_config(self, tmp_yaml):
        """有效设备配置应通过验证"""
        config = {
            "devices": [
                {
                    "id": "plc_01",
                    "name": "西门子PLC",
                    "protocol": "modbus_tcp",
                    "host": "192.168.1.53",
                    "port": 502,
                    "enabled": True,
                    "registers": [
                        {"name": "temperature", "address": 0, "data_type": "float32", "scale": 1.0},
                        {"name": "pressure", "address": 2, "data_type": "float32", "unit": "MPa"},
                    ]
                }
            ]
        }
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "devices")
        assert valid is True
        assert errors == []

    def test_valid_minimal_device(self, tmp_yaml):
        """最小有效设备配置（只有必填字段）"""
        config = {
            "devices": [
                {"id": "dev_01", "name": "Test", "protocol": "mqtt"}
            ]
        }
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "devices")
        assert valid is True

    def test_missing_devices_key(self, tmp_yaml):
        """缺少devices键应失败"""
        config = {"some_key": []}
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "devices")
        assert valid is False
        assert len(errors) > 0

    def test_missing_required_device_fields(self, tmp_yaml):
        """缺少必填字段应失败"""
        config = {
            "devices": [
                {"id": "dev_01"}  # 缺少name和protocol
            ]
        }
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "devices")
        assert valid is False
        assert any("name" in e for e in errors)

    def test_invalid_protocol(self, tmp_yaml):
        """无效协议名应失败"""
        config = {
            "devices": [
                {"id": "dev_01", "name": "Test", "protocol": "invalid_protocol"}
            ]
        }
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "devices")
        assert valid is False
        assert any("protocol" in e or "invalid_protocol" in e for e in errors)

    def test_invalid_port_range(self, tmp_yaml):
        """端口超出范围应失败"""
        config = {
            "devices": [
                {"id": "dev_01", "name": "Test", "protocol": "modbus_tcp", "port": 99999}
            ]
        }
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "devices")
        assert valid is False
        assert any("port" in e or "99999" in e for e in errors)

    def test_invalid_device_id_pattern(self, tmp_yaml):
        """设备ID含非法字符应失败"""
        config = {
            "devices": [
                {"id": "dev 01!", "name": "Test", "protocol": "modbus_tcp"}
            ]
        }
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "devices")
        assert valid is False

    def test_valid_all_protocols(self, tmp_yaml):
        """所有合法协议都应通过"""
        for proto in ["modbus_tcp", "modbus_rtu", "opcua", "mqtt", "rest", "s7", "iec104"]:
            config = {
                "devices": [
                    {"id": f"dev_{proto}", "name": "Test", "protocol": proto}
                ]
            }
            path = tmp_yaml(config, f"test_{proto}.yaml")
            valid, errors = validate_config(path, "devices")
            assert valid is True, f"Protocol {proto} should be valid: {errors}"

    def test_valid_register_data_types(self, tmp_yaml):
        """所有合法寄存器数据类型应通过"""
        config = {
            "devices": [
                {
                    "id": "dev_01", "name": "Test", "protocol": "modbus_tcp",
                    "registers": [
                        {"name": f"reg_{dt}", "address": i, "data_type": dt}
                        for i, dt in enumerate(["uint16", "int16", "float32", "float64", "uint32", "int32", "bool"])
                    ]
                }
            ]
        }
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "devices")
        assert valid is True


# ==================== 告警配置验证 ====================

class TestAlarmSchema:
    """告警配置Schema验证"""

    def test_valid_alarm_config(self, tmp_yaml):
        """有效告警配置应通过验证"""
        config = {
            "alarm_rules": [
                {
                    "id": "temp_high",
                    "device_id": "plc_01",
                    "register": "temperature",
                    "condition": "gt",
                    "threshold": 100.0,
                    "severity": "critical",
                    "message": "温度过高",
                    "enabled": True,
                    "deadband": 2.0
                }
            ]
        }
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "alarms")
        assert valid is True
        assert errors == []

    def test_missing_alarm_rules(self, tmp_yaml):
        """缺少alarm_rules应失败"""
        config = {"other_key": []}
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "alarms")
        assert valid is False

    def test_missing_required_alarm_fields(self, tmp_yaml):
        """缺少必填告警字段应失败"""
        config = {
            "alarm_rules": [
                {"id": "a1"}  # 缺少device_id, register, condition, threshold
            ]
        }
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "alarms")
        assert valid is False

    def test_invalid_condition(self, tmp_yaml):
        """无效条件类型应失败"""
        config = {
            "alarm_rules": [
                {
                    "id": "a1", "device_id": "d1", "register": "r1",
                    "condition": "between", "threshold": 10
                }
            ]
        }
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "alarms")
        assert valid is False

    def test_invalid_severity(self, tmp_yaml):
        """无效告警级别应失败"""
        config = {
            "alarm_rules": [
                {
                    "id": "a1", "device_id": "d1", "register": "r1",
                    "condition": "gt", "threshold": 10,
                    "severity": "urgent"  # 不在枚举中
                }
            ]
        }
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "alarms")
        assert valid is False

    def test_valid_all_conditions(self, tmp_yaml):
        """所有合法条件类型应通过"""
        for cond in ["gt", "lt", "eq", "ne", "gte", "lte", "in_range", "out_of_range"]:
            config = {
                "alarm_rules": [
                    {"id": "a1", "device_id": "d1", "register": "r1",
                     "condition": cond, "threshold": 10}
                ]
            }
            path = tmp_yaml(config, f"test_{cond}.yaml")
            valid, errors = validate_config(path, "alarms")
            assert valid is True, f"Condition {cond} should be valid: {errors}"

    def test_valid_all_severities(self, tmp_yaml):
        """所有合法告警级别应通过"""
        for sev in ["critical", "high", "medium", "low", "info"]:
            config = {
                "alarm_rules": [
                    {"id": "a1", "device_id": "d1", "register": "r1",
                     "condition": "gt", "threshold": 10, "severity": sev}
                ]
            }
            path = tmp_yaml(config, f"test_{sev}.yaml")
            valid, errors = validate_config(path, "alarms")
            assert valid is True, f"Severity {sev} should be valid: {errors}"

    def test_negative_deadband_fails(self, tmp_yaml):
        """负数死区应失败"""
        config = {
            "alarm_rules": [
                {"id": "a1", "device_id": "d1", "register": "r1",
                 "condition": "gt", "threshold": 10, "deadband": -1}
            ]
        }
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "alarms")
        assert valid is False


# ==================== 系统配置验证 ====================

class TestSystemSchema:
    """系统配置Schema验证"""

    def test_valid_system_config(self, tmp_yaml):
        """有效系统配置应通过验证"""
        config = {
            "app": {"name": "SCADA", "version": "1.0", "debug": False, "host": "127.0.0.1", "port": 5000},
            "database": {"path": "data/scada.db", "wal_mode": True, "pool_size": 10},
            "mqtt": {"broker_host": "localhost", "broker_port": 1883, "keepalive": 60}
        }
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "system")
        assert valid is True

    def test_empty_system_config(self, tmp_yaml):
        """空对象应通过（所有字段可选）"""
        config = {}
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "system")
        assert valid is True

    def test_invalid_port_in_system(self, tmp_yaml):
        """无效端口应失败"""
        config = {"app": {"port": 99999}}
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "system")
        assert valid is False

    def test_invalid_pool_size(self, tmp_yaml):
        """无效连接池大小应失败"""
        config = {"database": {"pool_size": 0}}
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "system")
        assert valid is False


# ==================== 通用验证逻辑 ====================

class TestValidateConfig:
    """通用验证逻辑测试"""

    def test_empty_file_fails(self, tmp_path):
        """空文件应失败"""
        filepath = tmp_path / "empty.yaml"
        filepath.write_text("", encoding='utf-8')
        valid, errors = validate_config(str(filepath), "devices")
        assert valid is False
        assert any("空" in e for e in errors)

    def test_invalid_yaml_fails(self, tmp_path):
        """无效YAML应失败"""
        filepath = tmp_path / "bad.yaml"
        filepath.write_text("{{{{invalid yaml", encoding='utf-8')
        valid, errors = validate_config(str(filepath), "devices")
        assert valid is False
        assert any("读取失败" in e for e in errors)

    def test_nonexistent_file_fails(self):
        """不存在的文件应失败"""
        valid, errors = validate_config("/nonexistent/path.yaml", "devices")
        assert valid is False
        assert any("读取失败" in e for e in errors)

    def test_unknown_schema_type(self, tmp_yaml):
        """未知schema类型应失败"""
        config = {"key": "value"}
        path = tmp_yaml(config)
        valid, errors = validate_config(path, "unknown_type")
        assert valid is False
        assert any("未知" in e for e in errors)


# ==================== 批量验证 ====================

class TestValidateAllConfigs:
    """批量配置验证"""

    def test_validate_all_returns_results_for_all_types(self, tmp_path):
        """应返回所有schema类型的结果"""
        # 创建配置目录和文件
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # 创建有效的devices.yaml
        devices = {"devices": [{"id": "d1", "name": "Test", "protocol": "mqtt"}]}
        with open(config_dir / "devices.yaml", 'w', encoding='utf-8') as f:
            yaml.dump(devices, f)

        # 创建有效的alarms.yaml
        alarms = {"alarm_rules": [
            {"id": "a1", "device_id": "d1", "register": "r1", "condition": "gt", "threshold": 10}
        ]}
        with open(config_dir / "alarms.yaml", 'w', encoding='utf-8') as f:
            yaml.dump(alarms, f)

        # 创建有效的system.yaml
        system = {"app": {"name": "test"}}
        with open(config_dir / "system.yaml", 'w', encoding='utf-8') as f:
            yaml.dump(system, f)

        results = validate_all_configs(str(config_dir))
        assert "devices" in results
        assert "alarms" in results
        assert "system" in results
        assert all(v[0] for v in results.values())

    def test_missing_files_reported(self, tmp_path):
        """缺失的配置文件应报告错误"""
        config_dir = tmp_path / "empty_config"
        config_dir.mkdir()

        results = validate_all_configs(str(config_dir))
        for name, (valid, errors) in results.items():
            assert valid is False
            assert any("不存在" in e for e in errors)


# ==================== 启动验证 ====================

class TestValidateStartupConfigs:
    """启动时配置验证"""

    def test_startup_valid_configs(self, tmp_path, caplog):
        """有效配置应返回True并记录INFO"""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        devices = {"devices": [{"id": "d1", "name": "Test", "protocol": "mqtt"}]}
        with open(config_dir / "devices.yaml", 'w', encoding='utf-8') as f:
            yaml.dump(devices, f)

        alarms = {"alarm_rules": [
            {"id": "a1", "device_id": "d1", "register": "r1", "condition": "gt", "threshold": 10}
        ]}
        with open(config_dir / "alarms.yaml", 'w', encoding='utf-8') as f:
            yaml.dump(alarms, f)

        system = {"app": {"name": "test"}}
        with open(config_dir / "system.yaml", 'w', encoding='utf-8') as f:
            yaml.dump(system, f)

        import logging
        with caplog.at_level(logging.INFO):
            result = validate_startup_configs(str(config_dir))

        assert result is True
        assert any("验证通过" in r.message for r in caplog.records)

    def test_startup_invalid_configs(self, tmp_path, caplog):
        """无效配置应返回False并记录WARNING"""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # 写入无效的devices（缺少必填字段）
        bad_devices = {"devices": [{"id": "d1"}]}
        with open(config_dir / "devices.yaml", 'w', encoding='utf-8') as f:
            yaml.dump(bad_devices, f)

        alarms = {"alarm_rules": [
            {"id": "a1", "device_id": "d1", "register": "r1", "condition": "gt", "threshold": 10}
        ]}
        with open(config_dir / "alarms.yaml", 'w', encoding='utf-8') as f:
            yaml.dump(alarms, f)

        system = {"app": {"name": "test"}}
        with open(config_dir / "system.yaml", 'w', encoding='utf-8') as f:
            yaml.dump(system, f)

        import logging
        with caplog.at_level(logging.WARNING):
            result = validate_startup_configs(str(config_dir))

        assert result is False
        assert any("验证失败" in r.message for r in caplog.records)
