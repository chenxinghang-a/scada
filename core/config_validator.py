"""
配置文件Schema验证 - 确保YAML配置符合预期格式
防止错误配置导致系统异常
"""
import jsonschema
import yaml
import logging
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# 设备配置Schema
DEVICE_SCHEMA = {
    "type": "object",
    "required": ["devices"],
    "properties": {
        "devices": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "name", "protocol"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[a-zA-Z0-9_-]+$"},
                    "name": {"type": "string", "minLength": 1, "maxLength": 100},
                    "protocol": {"type": "string", "enum": ["modbus_tcp", "modbus_rtu", "opcua", "mqtt", "rest", "s7", "iec104", "fins", "mc", "dnp3"]},
                    "host": {"type": "string"},
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                    "unit_id": {"type": "integer", "minimum": 0, "maximum": 255},
                    "byte_order": {"type": "string", "enum": ["ABCD", "BADC", "CDAB", "DCBA"]},
                    "poll_interval": {"type": "number", "minimum": 0.1, "maximum": 3600},
                    "enabled": {"type": "boolean"},
                    "registers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name", "address"],
                            "properties": {
                                "name": {"type": "string"},
                                "address": {"type": "integer", "minimum": 0},
                                "data_type": {"type": "string", "enum": ["uint16", "int16", "float32", "float64", "uint32", "int32", "bool"]},
                                "scale": {"type": "number"},
                                "offset": {"type": "number"},
                                "unit": {"type": "string"},
                                "writable": {"type": "boolean"}
                            }
                        }
                    }
                }
            }
        }
    }
}

# 告警配置Schema
ALARM_SCHEMA = {
    "type": "object",
    "required": ["alarm_rules"],
    "properties": {
        "alarm_rules": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "device_id", "condition", "threshold"],
                "properties": {
                    "id": {"type": "string"},
                    "device_id": {"type": "string"},
                    "register": {"type": "string"},
                    "condition": {"type": "string", "enum": ["gt", "lt", "eq", "ne", "gte", "lte", "in_range", "out_of_range", "greater_than", "less_than", "equal", "not_equal"]},
                    "threshold": {"type": "number"},
                    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info"]},
                    "message": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "deadband": {"type": "number", "minimum": 0}
                }
            }
        }
    }
}

# 系统配置Schema
SYSTEM_SCHEMA = {
    "type": "object",
    "properties": {
        "app": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "version": {"type": "string"},
                "debug": {"type": "boolean"},
                "host": {"type": "string"},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535}
            }
        },
        "database": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "wal_mode": {"type": "boolean"},
                "pool_size": {"type": "integer", "minimum": 1, "maximum": 100}
            }
        },
        "mqtt": {
            "type": "object",
            "properties": {
                "broker_host": {"type": "string"},
                "broker_port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "keepalive": {"type": "integer", "minimum": 10, "maximum": 3600}
            }
        }
    }
}

SCHEMAS = {
    "devices": DEVICE_SCHEMA,
    "alarms": ALARM_SCHEMA,
    "system": SYSTEM_SCHEMA
}


def validate_config(config_path: str, schema_type: str) -> tuple[bool, list[str]]:
    """
    验证配置文件

    Args:
        config_path: YAML配置文件路径
        schema_type: Schema类型 (devices/alarms/system)

    Returns:
        (is_valid, errors) 元组
    """
    schema = SCHEMAS.get(schema_type)
    if not schema:
        return False, [f"未知的schema类型: {schema_type}"]

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
    except Exception as e:
        return False, [f"配置文件读取失败: {e}"]

    if config is None:
        return False, ["配置文件为空"]

    errors = []
    try:
        jsonschema.validate(instance=config, schema=schema)
    except jsonschema.ValidationError as e:
        errors.append(f"验证失败 [{e.json_path}]: {e.message}")
    except jsonschema.SchemaError as e:
        errors.append(f"Schema错误: {e.message}")

    return len(errors) == 0, errors


def validate_all_configs(config_dir: str = "配置") -> Dict[str, tuple[bool, list[str]]]:
    """验证所有配置文件"""
    results = {}
    config_path = Path(config_dir)

    for schema_type, schema_file in [
        ("devices", "devices.yaml"),
        ("alarms", "alarms.yaml"),
        ("system", "system.yaml")
    ]:
        file_path = config_path / schema_file
        if file_path.exists():
            results[schema_type] = validate_config(str(file_path), schema_type)
        else:
            results[schema_type] = (False, [f"配置文件不存在: {file_path}"])

    return results


def validate_startup_configs(config_dir: str = "配置") -> bool:
    """启动时验证所有配置，失败则记录警告"""
    results = validate_all_configs(config_dir)
    all_valid = True

    for name, (valid, errors) in results.items():
        if not valid:
            all_valid = False
            for error in errors:
                logger.warning(f"配置验证失败 [{name}]: {error}")
        else:
            logger.info(f"配置验证通过: {name}")

    return all_valid
