# -*- coding: utf-8 -*-
"""声明式适配器规格：把"第三方接口"变成配置，而不是代码分支（round 375）。

为什么需要
----------
原来的 `rest` 协议只能"一个点位一个 URL + 一条 `json_path`"：
对接 MES / IoT 平台这类**一次返回一组记录**的接口时，要么写新客户端，
要么在业务代码里塞特判 —— 每接一家就改一次代码，正是"写死逻辑的玩具"。

本模块定义 `adapter` 段：**请求怎么发、响应怎么拆、字段怎么映射**全部数据化。
真正的 HTTP 传输在 `采集层/custom_adapter.py`，这里保持纯函数，便于单测。

配置示例
--------
```yaml
- id: mes_line1
  name: MES 产线接口
  protocol: custom
  poll_interval: 5
  adapter:
    transport: http
    base_url: "https://mes.example.com/api/v1"
    auth:
      type: bearer
      token: "${MES_TOKEN}"        # 从环境变量取，不把密钥写进仓库
    request:
      method: GET
      path: "/lines/{device_id}/points"
      params: {detail: "full"}
      timeout: 5
    response:
      records: "data.points"       # 列表：每个元素是一组点位；不写则整个响应当一条
    points:
      - {name: temperature, path: "value", type: float, scale: 0.1, unit: "°C"}
      - {name: run_state, path: "state", type: string,
         enum: {RUNNING: 1, IDLE: 0, FAULT: -1}}
      - {name: pressure, path: "metrics.pressure", type: float, required: true}
    write:                          # 可选：反向控制
      method: POST
      path: "/points/{name}"
      json: {value: "{value}"}
```

设计约束
--------
* **未知字段直接报错**：`records` 拼错成 `record` 时，如果静默忽略，接口会
  一直"采集成功但 0 个点" —— 这是最难查的一类配置错误。
* **路径支持数组下标**：`data.items[0].value`、`a.b[2].c`。
* **数值必须有限**：NaN/Inf 会让后续统计全变 NaN，入口直接拒绝。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.engineering_units import from_raw, scale_offset_of
from core.safe_numeric import to_finite_float, try_finite_float, try_int

__all__ = [
    'AdapterConfigError',
    'AdapterDataError',
    'AdapterSpec',
    'PointSpec',
    'RequestSpec',
    'dig',
    'join_url',
    'map_message',
    'map_payload',
    'parse_point_specs',
]

_ALLOWED_TRANSPORTS = {'http'}
_ALLOWED_METHODS = {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}
_ALLOWED_TYPES = {'auto', 'float', 'int', 'bool', 'string'}
_ALLOWED_AUTH_TYPES = {'none', 'basic', 'bearer', 'api_key'}

_ALLOWED_TOP = {'transport', 'base_url', 'request', 'response', 'points',
                'write', 'poll_interval', 'auth', 'name'}
_ALLOWED_REQUEST = {'method', 'path', 'params', 'json', 'headers', 'timeout'}
_ALLOWED_RESPONSE = {'records'}
_ALLOWED_WRITE = _ALLOWED_REQUEST
_ALLOWED_POINT = {'name', 'path', 'type', 'unit', 'scale', 'offset',
                  'enum', 'required'}
_ALLOWED_AUTH = {'type', 'token', 'username', 'password', 'header', 'value'}

_ENV_PATTERN = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')
_TOKEN_PATTERN = re.compile(r'([^.\[\]]+)|\[(\d+)\]')

_MISSING = object()


class AdapterConfigError(ValueError):
    """适配器配置非法（启动/加载时抛，调用方应拒绝该设备）。"""


class AdapterDataError(ValueError):
    """响应内容与配置不匹配（运行期抛，调用方计一次采集失败）。"""


@dataclass(frozen=True)
class PointSpec:
    """一个点位的映射规则。"""

    name: str
    path: str
    type: str = 'auto'
    unit: str = ''
    scale: float = 1.0
    offset: float = 0.0
    enum: Mapping[str, float] = field(default_factory=dict)
    required: bool = False


@dataclass(frozen=True)
class RequestSpec:
    """一次 HTTP 请求的静态部分（运行期只做占位符替换）。"""

    method: str
    path: str
    params: Mapping[str, Any] = field(default_factory=dict)
    json: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout: float | None = None


@dataclass(frozen=True)
class AdapterSpec:
    """整台设备的适配器规格。"""

    base_url: str
    request: RequestSpec
    points: tuple[PointSpec, ...]
    records_path: str | None = None
    write: RequestSpec | None = None
    poll_interval: float | None = None
    auth: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_device_config(cls, config: Mapping[str, Any]) -> 'AdapterSpec':
        """从设备配置里解析 `adapter` 段；任何非法项都抛 `AdapterConfigError`。"""
        adapter = config.get('adapter')
        if not isinstance(adapter, Mapping):
            raise AdapterConfigError(
                'protocol=custom 需要 adapter 段（transport/base_url/request/points）')
        _reject_unknown(adapter, _ALLOWED_TOP, 'adapter')

        transport = str(adapter.get('transport', 'http')).lower()
        if transport not in _ALLOWED_TRANSPORTS:
            raise AdapterConfigError(
                f'不支持的 adapter.transport: {transport!r}（当前只支持 http）')

        base_url = _env(adapter.get('base_url'))
        if not isinstance(base_url, str) or not base_url.strip():
            raise AdapterConfigError('adapter.base_url 必须是非空字符串')
        if not base_url.lower().startswith(('http://', 'https://')):
            raise AdapterConfigError(f'base_url 必须是 http(s) 地址: {base_url!r}')

        request = _request_spec(adapter.get('request'), 'adapter.request',
                                require_path=True)

        response = adapter.get('response') or {}
        if not isinstance(response, Mapping):
            raise AdapterConfigError('adapter.response 必须是对象')
        _reject_unknown(response, _ALLOWED_RESPONSE, 'adapter.response')
        records_path = _env(response.get('records'))
        if records_path is not None and (not isinstance(records_path, str)
                                         or not records_path.strip()):
            raise AdapterConfigError('adapter.response.records 必须是非空字符串')

        points = parse_point_specs(adapter.get('points'), 'adapter.points')

        write = None
        if adapter.get('write') is not None:
            write = _request_spec(adapter.get('write'), 'adapter.write',
                                  require_path=True)

        poll = adapter.get('poll_interval')
        if poll is not None:
            poll = try_finite_float(poll)
            if poll is None or poll <= 0:
                raise AdapterConfigError(
                    f'adapter.poll_interval 必须是正数: {adapter.get("poll_interval")!r}')

        auth = _auth_spec(adapter.get('auth'))

        return cls(
            base_url=base_url.rstrip('/'),
            request=request,
            points=points,
            records_path=records_path.strip() if records_path else None,
            write=write,
            poll_interval=poll,
            auth=auth,
        )


# ---------------------------------------------------------------------------
# 解析辅助
# ---------------------------------------------------------------------------

def _reject_unknown(data: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise AdapterConfigError(
            f'{where} 含未知字段 {unknown}（允许: {sorted(allowed)}）')


def _env(value: Any) -> Any:
    """递归展开 `${VAR}`；变量缺失直接报错，避免把字面量当密钥发出去。"""
    if isinstance(value, str):
        def _sub(match: re.Match) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise AdapterConfigError(
                    f'配置引用了未设置的环境变量 ${{{name}}}')
            return os.environ[name]
        return _ENV_PATTERN.sub(_sub, value)
    if isinstance(value, list):
        return [_env(v) for v in value]
    if isinstance(value, Mapping):
        return {k: _env(v) for k, v in value.items()}
    return value


def _request_spec(raw: Any, where: str, *, require_path: bool) -> RequestSpec:
    if not isinstance(raw, Mapping):
        raise AdapterConfigError(f'{where} 必须是对象')
    _reject_unknown(raw, _ALLOWED_REQUEST, where)

    method = str(raw.get('method', 'GET')).upper()
    if method not in _ALLOWED_METHODS:
        raise AdapterConfigError(
            f'{where}.method 非法: {method!r}（允许 {sorted(_ALLOWED_METHODS)}）')

    path = _env(raw.get('path', ''))
    if require_path and (not isinstance(path, str) or not path.strip()):
        raise AdapterConfigError(f'{where}.path 必须是非空字符串')

    timeout = raw.get('timeout')
    if timeout is not None:
        timeout = try_finite_float(timeout)
        if timeout is None or timeout <= 0:
            raise AdapterConfigError(f'{where}.timeout 必须是正数')

    return RequestSpec(
        method=method,
        path=path if isinstance(path, str) else '',
        params=_env(raw.get('params') or {}),
        json=_env(raw.get('json')),
        headers=_env(raw.get('headers') or {}),
        timeout=timeout,
    )


def parse_point_specs(raw_points: Any, where: str = 'points') -> tuple[PointSpec, ...]:
    """校验并编译一组点位映射（HTTP `adapter.points` 与 MQTT `topics[].points` 共用）。"""
    if not isinstance(raw_points, list) or not raw_points:
        raise AdapterConfigError(f'{where} 必须是非空列表')
    points = tuple(_point_spec(item, idx, where) for idx, item in enumerate(raw_points))
    names = [p.name for p in points]
    if len(names) != len(set(names)):
        raise AdapterConfigError(f'{where} 存在重名点位: {names}')
    return points


def _point_spec(raw: Any, idx: int, where_prefix: str = 'adapter.points') -> PointSpec:
    where = f'{where_prefix}[{idx}]'
    if not isinstance(raw, Mapping):
        raise AdapterConfigError(f'{where} 必须是对象')
    _reject_unknown(raw, _ALLOWED_POINT, where)

    name = raw.get('name')
    if not isinstance(name, str) or not name.strip():
        raise AdapterConfigError(f'{where}.name 必须是非空字符串')
    path = raw.get('path')
    if not isinstance(path, str) or not path.strip():
        raise AdapterConfigError(f'{where}.path 必须是非空字符串（点位取值路径）')

    data_type = str(raw.get('type', 'auto')).lower()
    if data_type not in _ALLOWED_TYPES:
        raise AdapterConfigError(
            f'{where}.type 非法: {data_type!r}（允许 {sorted(_ALLOWED_TYPES)}）')

    try:
        scale, offset = scale_offset_of(raw)
    except ValueError as exc:
        raise AdapterConfigError(f'{where} 的 scale/offset 非法: {exc}') from exc

    enum_raw = raw.get('enum') or {}
    if not isinstance(enum_raw, Mapping):
        raise AdapterConfigError(f'{where}.enum 必须是对象（原值 → 数值）')
    enum: dict[str, float] = {}
    for key, val in enum_raw.items():
        try:
            num = to_finite_float(val, f'{where}.enum[{key!r}]')
        except ValueError as exc:
            raise AdapterConfigError(str(exc)) from exc
        enum[str(key)] = num

    return PointSpec(
        name=name.strip(),
        path=path.strip(),
        type=data_type,
        unit=str(raw.get('unit', '') or ''),
        scale=scale,
        offset=offset,
        enum=enum,
        required=bool(raw.get('required', False)),
    )


def _auth_spec(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {'type': 'none'}
    if not isinstance(raw, Mapping):
        raise AdapterConfigError('adapter.auth 必须是对象')
    _reject_unknown(raw, _ALLOWED_AUTH, 'adapter.auth')

    auth_type = str(raw.get('type', 'none')).lower()
    if auth_type not in _ALLOWED_AUTH_TYPES:
        raise AdapterConfigError(
            f'adapter.auth.type 非法: {auth_type!r}'
            f'（允许 {sorted(_ALLOWED_AUTH_TYPES)}）')

    return {
        'type': auth_type,
        'token': _env(raw.get('token', '')),
        'username': _env(raw.get('username', '')),
        'password': _env(raw.get('password', '')),
        'header': str(_env(raw.get('header', 'X-API-Key'))),
        'value': _env(raw.get('value', '')),
    }


# ---------------------------------------------------------------------------
# 取值 / 映射（纯函数）
# ---------------------------------------------------------------------------

def join_url(base_url: str, path: str) -> str:
    """拼接 base_url 与 path；path 是绝对 URL 时原样返回。"""
    if path.lower().startswith(('http://', 'https://')):
        return path
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def dig(data: Any, path: str | None) -> Any:
    """按 `a.b[0].c` 取值；取不到返回内部 `_MISSING`（与"值就是 None"区分）。

    注意：调用方应先判断 `is None` —— JSON 里的 `null` 和"字段不存在"
    在业务上都算"没有值"，映射时统一按缺失处理。
    """
    if not path:
        return data
    current = data
    for match in _TOKEN_PATTERN.finditer(path):
        name, index = match.group(1), match.group(2)
        if name is not None:
            if not isinstance(current, Mapping) or name not in current:
                return _MISSING
            current = current[name]
        if index is not None:
            if not isinstance(current, (list, tuple)):
                return _MISSING
            # 正则保证是数字，仍走 try_int：守卫禁止裸 int()（`int('inf')` 会
            # 抛 OverflowError，而这类静态闸门就是防"只捕 ValueError"的写法）
            position = try_int(index)
            if position is None:
                return _MISSING
            try:
                current = current[position]
            except (IndexError, TypeError):
                return _MISSING
    return current


_TRUE_VALUES = {'true', '1', 'on', 'yes', 'y'}
_FALSE_VALUES = {'false', '0', 'off', 'no', 'n'}


def convert_value(raw: Any, point: PointSpec) -> Any:
    """单个原始值 → 工程值（按点位规则）。失败抛 `AdapterDataError`。"""
    if point.enum:
        lookup = str(raw)
        if lookup not in point.enum:
            for key, value in point.enum.items():
                if key.lower() == lookup.lower():
                    return value
            raise AdapterDataError(
                f'点位 {point.name} 的值 {raw!r} 不在 enum 映射表'
                f'{sorted(point.enum)} 中')
        return point.enum[lookup]

    if point.type == 'string':
        return str(raw)

    if point.type == 'bool':
        if isinstance(raw, bool):
            return 1.0 if raw else 0.0
        text = str(raw).strip().lower()
        if text in _TRUE_VALUES:
            return 1.0
        if text in _FALSE_VALUES:
            return 0.0
        raise AdapterDataError(f'点位 {point.name} 的值 {raw!r} 无法转成布尔')

    if isinstance(raw, bool):
        # bool 是 int 的子类：`float(True) == 1.0`，混进温度序列会把均值拽偏
        raise AdapterDataError(f'点位 {point.name} 声明为数值，但收到布尔值 {raw!r}')

    if point.type == 'auto':
        numeric = try_finite_float(raw)
        if numeric is not None:
            return from_raw(numeric, point.scale, point.offset)
        if isinstance(raw, str):
            return raw
        raise AdapterDataError(
            f'点位 {point.name} 的值 {raw!r} 无法识别为数值或字符串')

    if point.type == 'int':
        number = try_int(raw)
        if number is None:
            raise AdapterDataError(f'点位 {point.name} 的值 {raw!r} 不是整数')
        return float(round(from_raw(number, point.scale, point.offset)))

    # float
    numeric = try_finite_float(raw)
    if numeric is None:
        raise AdapterDataError(f'点位 {point.name} 的值 {raw!r} 不是有限数值')
    return from_raw(numeric, point.scale, point.offset)


def _extract_records(payload: Any, records_path: str | None,
                     where: str) -> list[Any]:
    if records_path:
        found = dig(payload, records_path)
        if found is _MISSING or not isinstance(found, list):
            raise AdapterDataError(
                f'{where}={records_path!r} 未取到列表'
                f'（实际: {type(found).__name__ if found is not _MISSING else "缺失"}）')
        if not found:
            raise AdapterDataError(f'{where}={records_path!r} 是空列表')
        return found
    return [payload]


def _map_records(records: list[Any], points: tuple[PointSpec, ...],
                 timestamp: str, source: str) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for idx, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise AdapterDataError(
                f'第 {idx} 条记录不是对象（{type(record).__name__}）')
        for point in points:
            raw = dig(record, point.path)
            if raw is _MISSING or raw is None:
                if point.required:
                    raise AdapterDataError(
                        f'必填点位 {point.name} 缺失（path={point.path!r}）')
                continue
            try:
                value = convert_value(raw, point)
            except AdapterDataError:
                if point.required:
                    raise
                # 非必填点：坏值跳过，不影响其余点位（客户端按"缺了哪个点"
                # 从最新快照里能直接看出来；标 required: true 才会整批失败）。
                continue
            mapped[point.name] = {
                'value': value,
                'unit': point.unit,
                'quality': 192,
                'timestamp': timestamp,
            }

    if not mapped:
        raise AdapterDataError(
            f'{source}没有匹配到任何点位（检查 records 与 points[].path）')
    return mapped


def map_payload(payload: Any, spec: AdapterSpec,
                now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """把一整个 HTTP 响应体映射成 `{point_name: {value, unit, quality, timestamp}}`。

    Raises:
        AdapterDataError: records 不是列表 / 必填点缺失 / 没有任何点位命中。
    """
    timestamp = (now or datetime.now()).isoformat()
    records = _extract_records(payload, spec.records_path, 'response.records')
    return _map_records(records, spec.points, timestamp, '响应')


def map_message(payload: Any, points: tuple[PointSpec, ...],
                records_path: str | None = None,
                now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """映射一条 MQTT 消息（不依赖 `AdapterSpec`，规则与 HTTP 完全一致）。

    MQTT 侧此前只有 `json_path + name`：一个主题只能出一个数值点，枚举/类型/
    批量记录都得写代码。这里复用同一套 `PointSpec`，配置即可扩展。
    """
    timestamp = (now or datetime.now()).isoformat()
    records = _extract_records(payload, records_path, 'records')
    return _map_records(records, points, timestamp, '消息')
