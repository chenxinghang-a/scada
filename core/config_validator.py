"""
配置文件Schema验证 - 确保YAML配置符合预期格式
防止错误配置导致系统异常
"""
import jsonschema
import yaml
import logging
import paths
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

#: 报警合理性（ISA-18.2 §9 alarm rationalization）必填项。
#:
#: 为什么单列一份而不是塞进 JSON Schema 的 `required`：
#: 结构合法性（有没有 id/threshold）与**运行完整性**（这条报警为什么存在、
#: 出了要做什么、多久必须响应）是两个不同的问题。前者是格式错误，
#: 后者是**管理缺陷** —— 混在一起会让"少填一个 cause"把整个配置判成非法，
#: 而现场需要的是"能跑，但要报出缺哪些"。
#:
#: ISA-18.2 要求每条报警都能回答四个问题，缺一条这条报警在审计里就不成立：
#:   cause                 —— 为什么会报（工艺/设备根因）
#:   consequence           —— 不处理会怎样（安全/环保/质量/产量）
#:   action                —— 操作员具体做什么（可执行的动作，不是"注意一下"）
#:   max_response_time_sec —— 多久内必须响应（决定优先级与升级策略）
REQUIRED_RATIONALIZATION_FIELDS = {
    'cause': '报警根因（工艺/设备原因）',
    'consequence': '不处理的后果（安全/环保/质量/产量）',
    'action': '操作员应采取的动作（可执行）',
    'max_response_time_sec': '最大允许响应时间（秒）',
}

#: 合理性字段的最小文本长度 —— "无"/"待定"/"注意"这类占位符不算填了
MIN_RATIONALIZATION_TEXT = 4


def validate_alarm_rationalization(config: dict) -> list[str]:
    """检查报警规则是否完成合理性描述（ISA-18.2 §9）。

    Args:
        config: `配置/alarms.yaml` 解析出来的字典

    Returns:
        问题列表（空列表 = 全部合规）。每条问题都带规则 id 与缺失项，
        便于直接贴进整改单。
    """
    problems: list[str] = []
    rules = config.get('alarm_rules') if isinstance(config, dict) else None
    if not isinstance(rules, list):
        return ['alarm_rules 不是列表，无法检查合理性']

    for idx, rule in enumerate(rules):
        if not isinstance(rule, dict):
            problems.append(f'第 {idx + 1} 条规则不是字典')
            continue
        rule_id = rule.get('id') or f'#{idx + 1}'
        # 只要求**启用**的规则：停用规则允许先留空（现场常见：先停掉再补）
        if not rule.get('enabled', True):
            continue
        for field, label in REQUIRED_RATIONALIZATION_FIELDS.items():
            value = rule.get(field)
            if field == 'max_response_time_sec':
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                    problems.append(
                        f'规则 {rule_id}: 缺少有效的 {field}（{label}），当前值 {value!r}')
                continue
            if not isinstance(value, str) or len(value.strip()) < MIN_RATIONALIZATION_TEXT:
                problems.append(
                    f'规则 {rule_id}: 缺少 {field}（{label}），'
                    f'或内容过短（<{MIN_RATIONALIZATION_TEXT} 字）：{value!r}')
    return problems


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
                    "name": {"type": "string"},
                    "device_id": {"type": "string"},
                    # ⚠️ round 261：真实配置用的是 `register_name`，而 schema 一直
                    #    写着 `register` —— 校验器校验的是一套**不存在的词汇**，
                    #    等于形同虚设。两个都列上（老写法仍可解析），
                    #    但"真实文件用哪个"由 tests/test_alarm_rationalization.py 钉住。
                    "register_name": {"type": "string"},
                    "register": {"type": "string"},
                    "condition": {"type": "string", "enum": ["gt", "lt", "eq", "ne", "gte", "lte", "in_range", "out_of_range", "greater_than", "less_than", "equal", "not_equal", "external"]},
                    "threshold": {"type": "number"},
                    # 真实配置用 `level`，schema 原先写 `severity`
                    "level": {"type": "string", "enum": ["critical", "high", "medium", "low", "info", "warning"]},
                    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info", "warning"]},
                    "message": {"type": "string"},
                    "description": {"type": "string"},
                    "area": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    # 抑制抖动的两个旋钮：delay = 持续多久才算报警；deadband = 回差
                    "delay": {"type": "number", "minimum": 0},
                    "deadband": {"type": "number", "minimum": 0},
                    # ISA-18.2 §9 合理性（必填性由 validate_alarm_rationalization 管）
                    "cause": {"type": "string"},
                    "consequence": {"type": "string"},
                    "action": {"type": "string"},
                    "max_response_time_sec": {"type": "number", "exclusiveMinimum": 0}
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
    """验证所有配置文件

    ``config_dir`` 走 :func:`paths.resolve` 解析为绝对路径。
    历史实现直接 ``Path("配置")``，只在「CWD 恰好是项目根」时成立 ——
    打包后的应用（``_internal/配置/``）与从服务/计划任务启动时**一律找不到**，
    于是启动日志里永远出现三行「配置文件不存在: 配置\\devices.yaml」，
    把「配置真的坏了」和「路径不对」混成同一个信号，校验形同虚设。
    """
    results = {}
    config_path = paths.resolve(config_dir)

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

    # ISA-18.2 §9：报警合理性（为什么存在 / 后果 / 动作 / 响应时限）
    # 不算"配置非法"（不阻断启动），但必须**每次启动都报出来** ——
    # 缺这些字段的报警在审计里不成立，而现场往往就是这么漏掉的。
    alarm_path = paths.resolve(config_dir) / 'alarms.yaml'
    if alarm_path.exists():
        try:
            with open(alarm_path, 'r', encoding='utf-8') as f:
                alarm_cfg = yaml.safe_load(f) or {}
            gaps = validate_alarm_rationalization(alarm_cfg)
            if gaps:
                logger.warning(
                    "报警合理性（ISA-18.2 §9）不完整，共 %d 项：%s",
                    len(gaps), '；'.join(gaps[:5]) + (' …' if len(gaps) > 5 else ''))
            else:
                logger.info("报警合理性检查通过（%d 条规则全部有 cause/consequence/action/时限）",
                            len(alarm_cfg.get('alarm_rules') or []))
        except Exception as e:      # noqa: BLE001 —— 合理性检查失败不能阻断启动
            logger.warning("报警合理性检查失败（不影响启动）: %s", e)

    return all_valid
