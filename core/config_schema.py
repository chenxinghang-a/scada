"""配置文档的结构校验（fail-fast）。

依据：
* Yin et al., "An Empirical Study on Configuration Errors in Commercial and
  Open Source Systems", SOSP 2011；
* Xu et al., "Early Detection of Configuration Errors to Reduce Failure
  Damage", OSDI 2016 —— 在**加载期**一次报全。

设计约束（来自对抗性复查）：
1. **校验器自身绝不能抛异常**：任何不可哈希/畸形值都要变成一条错误，
   否则 devices 侧会把「校验器崩溃」当成「配置解析失败」，整表拒载。
2. 协议必填字段、data_type 大小写、点位容器元素类型都要校验 —— 这些
   都是"加载期能发现、运行期才炸"的典型。
3. 严重级别与 `alarm_manager.SEVERITY_ORDER` 对齐（high/medium/low 合法）。
"""

from __future__ import annotations

from typing import Any, Iterable

#: 设备协议白名单（与 DeviceManager.SUPPORTED_PROTOCOLS 保持一致）。
DEVICE_PROTOCOLS = {
    'modbus_tcp', 'modbus_rtu', 'opcua', 'mqtt', 'rest',
    's7', 'dnp3', 'iec104', 'mc', 'fins', 'custom',
}

#: 各协议在加载期就能判定的必填字段。
#:
#: 刻意**不包含** modbus_rtu 与 opcua：前者连接信息可能写成 `port: COM3`
#: （串口名）或由部署环境注入，后者既可能写 `endpoint` 也可能写 `host`+`port`；
#: 在加载期强判这两类会产生大量误报（仓库的 devices_simulated.yaml 里
#: 8 台 RTU 设备本来就靠仿真、没有串口）。它们的连接错误留给连接期上报。
REQUIRED_BY_PROTOCOL = {
    'modbus_tcp': ('host',),
    'mqtt': ('host', 'topics'),
    'rest': ('base_url',),
    's7': ('host',),
    'dnp3': ('host',),
    'iec104': ('host',),
    'mc': ('host',),
    'fins': ('host',),
}

#: modbus 解码器真正实现的六种（gateway/modbus_gateway.py 的分支）。
MODBUS_DATA_TYPES = {'uint16', 'int16', 'uint32', 'int32', 'float32', 'float64'}
#: 其它协议额外允许的类型。
OTHER_DATA_TYPES = {'int64', 'uint64', 'bool', 'boolean', 'coil', 'word', 'string'}
DEVICE_DATA_TYPES = MODBUS_DATA_TYPES | OTHER_DATA_TYPES

#: 与 `alarm_manager.SEVERITY_ORDER` 的键保持一致：
#: info/low/medium/warning/high/critical。`emergency` 降级语义不存在，不能放行。
ALARM_LEVELS = {'info', 'low', 'medium', 'warning', 'high', 'critical'}

ALARM_CONDITIONS = {
    'greater_than', 'less_than', 'equal', 'not_equal',
    'greater_equal', 'less_equal', 'outside', 'inside', 'bool',
}

#: 结构性错误：出现即无法安全加载（条目级错误可"跳过该条"而不是拒载整表）。
STRUCTURAL_PREFIXES = ('顶层', '缺少 `devices`', '`devices` 不是列表',
                       '`devices` 为空')

_POINT_SECTIONS = (
    ('nodes', 'node_id'),
    ('topics', 'topic'),
    ('endpoints', 'path'),
)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _in_set(value: Any, allowed: Iterable[str]) -> bool:
    """安全的集合判定：非字符串一律 False（避免 unhashable TypeError）。"""
    return isinstance(value, str) and value in allowed


def _err(errors: list[str], path: str, msg: str) -> None:
    errors.append(f'{path}: {msg}')


def _as_int(value: Any) -> int | None:
    """把 `"502"` / `502` 归一成 int；bool/非法值返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def has_structural_errors(errors: Iterable[str]) -> bool:
    return any(str(e).startswith(STRUCTURAL_PREFIXES) for e in errors)


def validate_devices_document(doc: Any) -> list[str]:
    """校验 `devices.yaml` 顶层结构，返回**全部**错误（空 = 通过）。

    本函数保证不抛异常：任何畸形输入都转成错误字符串。
    """
    errors: list[str] = []
    try:
        _validate_devices_into(doc, errors)
    except Exception as exc:      # noqa: BLE001 —— 校验器不得成为新的故障源
        errors.append(f'校验器内部错误（请上报）: {type(exc).__name__}: {exc}')
    return errors


def _validate_devices_into(doc: Any, errors: list[str]) -> None:
    if not isinstance(doc, dict):
        errors.append(f'顶层不是映射（实际 {type(doc).__name__}）')
        return

    devices = doc.get('devices')
    if devices is None:
        errors.append('缺少 `devices` 段')
        return
    if not isinstance(devices, list):
        errors.append(f'`devices` 不是列表（实际 {type(devices).__name__}）')
        return
    if not devices:
        errors.append('`devices` 为空列表（现场将无任何设备）')

    seen_ids: dict[str, int] = {}
    for idx, dev in enumerate(devices):
        path = f'devices[{idx}]'
        if not isinstance(dev, dict):
            _err(errors, path, f'条目不是映射（实际 {type(dev).__name__}）')
            continue

        dev_id = dev.get('id')
        if not isinstance(dev_id, str) or not dev_id.strip():
            _err(errors, path, 'id 缺失或不是非空字符串')
        else:
            path = f'devices[{idx}]({dev_id})'
            if dev_id in seen_ids:
                _err(errors, path, f'id 与 devices[{seen_ids[dev_id]}] 重复')
            else:
                seen_ids[dev_id] = idx

        protocol = dev.get('protocol')
        if not isinstance(protocol, str) or not protocol:
            _err(errors, path, f'protocol={protocol!r} 必须是非空字符串')
        elif not _in_set(protocol, DEVICE_PROTOCOLS):
            _err(errors, path, f'protocol={protocol!r} 不在白名单 {sorted(DEVICE_PROTOCOLS)}')
        else:
            for required in REQUIRED_BY_PROTOCOL.get(protocol, ()):
                if not dev.get(required):
                    _err(errors, path, f'{protocol} 协议缺少必填字段 {required}')

        port = dev.get('port')
        if port is not None:
            # RTU 的 `port` 可能是串口名（COM3 / /dev/ttyS0），不按 TCP 端口校验
            _is_serial_name = (protocol == 'modbus_rtu'
                               and isinstance(port, str) and not port.strip().isdigit())
            if not _is_serial_name:
                port_num = _as_int(port)
                if port_num is None or not 1 <= port_num <= 65535:
                    _err(errors, path, f'port={port!r} 非法（1~65535）')

        if 'enabled' in dev and not isinstance(dev['enabled'], bool):
            _err(errors, path, f'enabled={dev["enabled"]!r} 不是布尔值')

        for key in ('collection_interval', 'poll_interval'):
            if key in dev:
                val = dev[key]
                num = _as_int(val) if isinstance(val, str) else val
                if not _is_number(num) or num <= 0:
                    _err(errors, path, f'{key}={val!r} 必须为正数')

        registers = dev.get('registers')
        if registers is not None:
            if not isinstance(registers, list):
                _err(errors, path, f'registers 不是列表（实际 {type(registers).__name__}）')
            else:
                for ridx, reg in enumerate(registers):
                    rpath = f'{path}.registers[{ridx}]'
                    if not isinstance(reg, dict):
                        _err(errors, rpath, '条目不是映射')
                        continue
                    if not reg.get('name'):
                        _err(errors, rpath, 'name 缺失')
                    if 'address' not in reg:
                        _err(errors, rpath, 'address 缺失')
                    else:
                        addr = _as_int(reg.get('address'))
                        if addr is None or not 0 <= addr <= 65535:
                            _err(errors, rpath, f'address={reg.get("address")!r} 非法（0~65535）')
                    dt = reg.get('data_type')
                    if dt is not None:
                        if not isinstance(dt, str) or dt != dt.lower():
                            _err(errors, rpath, f'data_type={dt!r} 必须是小写字符串')
                        elif dt not in DEVICE_DATA_TYPES:
                            _err(errors, rpath, f'data_type={dt!r} 不在白名单')
                        elif (str(protocol).startswith('modbus')
                              and dt not in MODBUS_DATA_TYPES):
                            _err(errors, rpath,
                                 f'modbus 不支持 data_type={dt!r}（解码器只有六种）')
                    for key in ('scale', 'offset'):
                        if key in reg and not _is_number(reg[key]):
                            _err(errors, rpath, f'{key}={reg[key]!r} 不是数值')

        for section, required_key in _POINT_SECTIONS:
            seq = dev.get(section)
            if seq is None:
                continue
            if not isinstance(seq, list):
                _err(errors, path, f'{section} 不是列表（实际 {type(seq).__name__}）')
                continue
            for sidx, item in enumerate(seq):
                spath = f'{path}.{section}[{sidx}]'
                if isinstance(item, str):
                    if section != 'topics':
                        _err(errors, spath, '条目是字符串（该区要求映射）')
                    continue
                if not isinstance(item, dict):
                    _err(errors, spath, '条目不是映射')
                    continue
                if required_key == 'topic':
                    continue      # MQTT 主题允许 `- scada/dev1/temp` 与映射混用
                if not item.get(required_key):
                    _err(errors, spath, f'缺少 {required_key}')


def validate_alarms_document(doc: Any) -> list[str]:
    """校验 `alarms.yaml` 的 `alarm_rules` 段，返回全部错误。"""
    errors: list[str] = []
    try:
        _validate_alarms_into(doc, errors)
    except Exception as exc:      # noqa: BLE001
        errors.append(f'校验器内部错误（请上报）: {type(exc).__name__}: {exc}')
    return errors


def _validate_alarms_into(doc: Any, errors: list[str]) -> None:
    if not isinstance(doc, dict):
        errors.append(f'顶层不是映射（实际 {type(doc).__name__}）')
        return

    rules = doc.get('alarm_rules')
    if rules is None:
        errors.append('缺少 `alarm_rules` 段')
        return
    if not isinstance(rules, list):
        errors.append(f'`alarm_rules` 不是列表（实际 {type(rules).__name__}）')
        return
    if not rules:
        errors.append('`alarm_rules` 为空（全部报警规则失效）')

    seen: dict[str, int] = {}
    for idx, rule in enumerate(rules):
        path = f'alarm_rules[{idx}]'
        if not isinstance(rule, dict):
            _err(errors, path, '条目不是映射')
            continue

        rid = rule.get('id')
        if not isinstance(rid, str) or not rid.strip():
            _err(errors, path, 'id 缺失或不是非空字符串')
        else:
            path = f'alarm_rules[{idx}]({rid})'
            if rid in seen:
                _err(errors, path, f'id 与 alarm_rules[{seen[rid]}] 重复')
            else:
                seen[rid] = idx

        level = rule.get('level', 'warning')
        if not _in_set(level, ALARM_LEVELS):
            _err(errors, path, f'level={level!r} 不在 {sorted(ALARM_LEVELS)}')

        cond = rule.get('condition')
        if cond is not None:
            if not _in_set(cond, ALARM_CONDITIONS):
                _err(errors, path, f'condition={cond!r} 不在 {sorted(ALARM_CONDITIONS)}')
            elif cond != 'bool' and not _is_number(rule.get('threshold')):
                # 有比较条件却没有阈值 → 运行期 `None > x` 直接 TypeError
                _err(errors, path, f'condition={cond!r} 缺少数值 threshold')

        if 'threshold' in rule and not _is_number(rule['threshold']):
            _err(errors, path, f'threshold={rule["threshold"]!r} 不是数值')

        for key in ('delay', 'deadband'):
            if key in rule:
                val = rule[key]
                if not _is_number(val):
                    _err(errors, path, f'{key}={val!r} 不是数值')
                elif val < 0:
                    _err(errors, path, f'{key}={val!r} 不能为负')

        if 'enabled' in rule and not isinstance(rule['enabled'], bool):
            _err(errors, path, f'enabled={rule["enabled"]!r} 不是布尔值')

        if 'device_id' in rule and not isinstance(rule['device_id'], str):
            _err(errors, path, 'device_id 必须是字符串')

        if not rule.get('register_name') and not rule.get('device_id'):
            # 既没绑点位也没绑设备 → 规则永远不会命中，属"静默失效"
            _err(errors, path, 'register_name/device_id 至少填一个（否则永不命中）')


def format_errors(kind: str, errors: Iterable[str], limit: int = 12) -> str:
    """把错误列表压成一行，便于日志（超出部分给总数）。"""
    items = list(errors)
    head = '; '.join(items[:limit])
    if len(items) > limit:
        head += f'; …（共 {len(items)} 条）'
    return f'{kind} 配置校验失败: {head}'
