# -*- coding: utf-8 -*-
"""声明式自定义接口适配器（round 375）。

把"对接一家第三方接口就写一个客户端"变成"写一段 YAML"：

* 请求：method / path / params / json / headers / timeout；
* 认证：none / basic / bearer / api_key（密钥支持 `${ENV}`）；
* 响应：`records` 指定列表位置，`points[].path` 逐点取值；
* 映射：float / int / bool / string / auto、enum、scale/offset、required；
* 反向：可选的 `write` 段，按点位名回写。

协议名统一为 `custom`。设计说明与完整字段表见 `采集层/adapter_spec.py`
和 `文档/自定义接口适配.md`。
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Callable

import requests

from core.action_result import is_failure
from core.circuit_breaker import CircuitBreakerOpenError, get_circuit_breaker
from core.safe_numeric import try_finite_float

from .adapter_spec import (
    AdapterConfigError,
    AdapterDataError,
    AdapterSpec,
    RequestSpec,
    map_payload,
)

logger = logging.getLogger(__name__)

#: 数值型响应字段默认超时（连接, 读取）—— 工业内网不宜太长
_DEFAULT_TIMEOUT = (3, 5)


class CustomAdapterClient:
    """按 `adapter` 配置轮询第三方 HTTP 接口的采集客户端。

    与 `RESTDeviceClient` 的区别：REST 是"一个点位一个 URL"，本类是
    "一个接口一组记录"，响应结构、字段路径、枚举映射全部由配置决定。
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.device_id = config.get('id', 'custom_device')
        self.device_name = config.get('name', '自定义接口设备')

        self._config_error: str | None = None
        try:
            self.spec: AdapterSpec | None = AdapterSpec.from_device_config(config)
        except AdapterConfigError as exc:
            # 不在构造函数里抛：`DeviceManager` 的工厂可能被多处直接调用，
            # 抛出去会变成「加一台配置错的设备，整个采集器起不来」。
            # 这里记下原因，connect() 明确失败并在 stats.last_error 里可查。
            self.spec = None
            self._config_error = str(exc)
            logger.error("自定义适配器配置非法 device=%s: %s", self.device_id, exc)

        # round 377：本类是 HTTP 实现；插件传输由 PluginAdapterClient 负责。
        # 直接构造时明确报错，避免"配了 plugin 却按 HTTP 发请求"。
        if self.spec is not None and self.spec.transport != 'http':
            self._config_error = (f'CustomAdapterClient 只处理 transport=http，'
                                  f'收到 {self.spec.transport!r}')
            self.spec = None
            logger.error("自定义适配器配置错误 device=%s: %s",
                         self.device_id, self._config_error)

        device_interval = try_finite_float(config.get('poll_interval'))
        self.poll_interval = (
            self.spec.poll_interval if (self.spec and self.spec.poll_interval)
            else device_interval if (device_interval and device_interval > 0)
            else 5.0
        )

        self.connected = False
        self.latest_data: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._data_callbacks: list[Callable[..., Any]] = []

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._session: requests.Session | None = None
        #: requests.Session 非线程安全：轮询线程与 write_point 必须串行使用
        self._session_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._timeout = _DEFAULT_TIMEOUT

        breaker_threshold = config.get('circuit_breaker_threshold', 5)
        breaker_timeout = config.get('circuit_breaker_timeout', 60)
        self._circuit_breaker = get_circuit_breaker(
            name=f"custom_adapter_{self.device_id}",
            failure_threshold=breaker_threshold,
            timeout_seconds=breaker_timeout,
        )
        self._write_breaker = get_circuit_breaker(
            name=f"custom_adapter_{self.device_id}_write",
            failure_threshold=breaker_threshold,
            timeout_seconds=breaker_timeout,
        )

        self.stats: dict[str, Any] = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'writes': 0,
            'connected_since': None,
            'last_request_time': None,
            'last_error': None,
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """建立会话、做一次探测、启动轮询线程。"""
        if self.spec is None:
            self.stats['last_error'] = self._config_error or '适配器配置非法'
            logger.error("自定义适配器无法连接 device=%s: %s",
                         self.device_id, self.stats['last_error'])
            return False

        with self._lifecycle_lock:
            if (self.connected and self._session is not None
                    and self._thread is not None and self._thread.is_alive()):
                return True

            try:
                # 重复 connect 时旧会话必须先关：否则每次重连泄漏一个连接池，
                # 旧池里的 keep-alive 连接还挂在 Broker/网关侧。
                self._close_session()
                self._session = requests.Session()
                # 现场接口在内网：不能被服务器上的 HTTP_PROXY 劫持
                self._session.trust_env = False
                self._session.proxies = {'http': None, 'https': None}
                self._session.headers.update({'Content-Type': 'application/json'})
                self._session.headers.update(dict(self.spec.request.headers))
                self._setup_auth()

                # 探测：既验证网络/认证，也验证 records/points 路径与真实响应匹配。
                # 配置拼错时在这里就失败，而不是"采集成功但 0 个点"。
                mapped = self._fetch_and_map()
                with self._lock:
                    self.latest_data = mapped

                self.connected = True
                self.stats['connected_since'] = datetime.now().isoformat()
                logger.info("自定义适配器已连接: %s（%d 个点位）",
                            self.device_id, len(mapped))

                if self._thread is None or not self._thread.is_alive():
                    self._stop_event.clear()
                    self._thread = threading.Thread(
                        target=self._poll_loop,
                        name=f"custom-adapter-{self.device_id}",
                        daemon=True,
                    )
                    self._thread.start()
                return True

            except Exception as exc:      # noqa: BLE001 —— 连接失败必须返回 False
                self.stats['failed_requests'] += 1
                self.stats['last_error'] = str(exc)
                self.connected = False
                logger.error("自定义适配器连接失败 device=%s: %s", self.device_id, exc)
                self._close_session()
                return False

    def disconnect(self):
        """停止轮询并关闭会话（幂等）。"""
        with self._lifecycle_lock:
            self._stop_event.set()
            thread = self._thread
            if thread is not None and thread.is_alive() \
                    and thread is not threading.current_thread():
                thread.join(timeout=5)
                if thread.is_alive():
                    # 不能把引用丢掉：线程仍可能在使用 session，重连会起第二条轮询线程
                    logger.error(
                        "自定义适配器轮询线程 5s 内未退出 device=%s，保留引用避免双轮询",
                        self.device_id)
                    self.connected = False
                    return
            self._thread = None
            self._close_session()
            self.connected = False
            logger.info("自定义适配器已断开: %s", self.device_id)

    start = connect
    stop = disconnect

    # ------------------------------------------------------------------
    # 轮询与映射
    # ------------------------------------------------------------------

    def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                mapped = self._fetch_and_map()
                with self._lock:
                    # 整批替换：可选点位消失时不能把上一轮的旧值一直挂着
                    self.latest_data = mapped
                self._notify(mapped)
            except CircuitBreakerOpenError as exc:
                self.stats['failed_requests'] += 1
                self.stats['last_error'] = f'熔断器打开: {exc}'
                # round 385：熔断期间缓存里的旧值不能再报 quality=192（Good），
                # 否则上层把熔断前的数据当新鲜数据使用（fail-open）。
                self._mark_quality_bad()
                logger.warning("自定义适配器请求被熔断 device=%s: %s",
                               self.device_id, exc)
            except Exception as exc:      # noqa: BLE001 —— 单轮失败不能停线程
                self.stats['failed_requests'] += 1
                self.stats['last_error'] = str(exc)
                self._mark_quality_bad()
                logger.warning("自定义适配器采集失败 device=%s: %s",
                               self.device_id, exc)
            self._stop_event.wait(self.poll_interval)

    def _fetch_and_map(self) -> dict[str, dict[str, Any]]:
        payload = self._fetch(self.spec.request)
        return map_payload(payload, self.spec)

    def _fetch(self, request: RequestSpec, *,
               path_context: dict[str, Any] | None = None,
               body_override: Any = None,
               expect_json: bool = True,
               breaker: Any = None,
               allow_redirects: bool = True) -> Any:
        """发一次请求并返回 JSON；熔断器包住整个调用。"""
        context = {'device_id': self.device_id}
        if path_context:
            context.update(path_context)

        path = _fill_text(request.path, context)
        url = path if path.lower().startswith(('http://', 'https://')) \
            else f"{self.spec.base_url}/{path.lstrip('/')}"

        def _do_request():
            kwargs: dict[str, Any] = {
                'timeout': request.timeout or self._timeout,
            }
            if not allow_redirects:
                kwargs['allow_redirects'] = False
            params = _fill_obj(request.params, context)
            if params:
                kwargs['params'] = params
            body = body_override if body_override is not None \
                else _fill_obj(request.json, context)
            if body is not None:
                kwargs['json'] = body
            headers = _fill_obj(request.headers, context)
            if headers:
                kwargs['headers'] = headers

            # requests.Session 不是线程安全的：轮询线程与 write_point 会并发
            # 使用同一个连接池，必须串行化（代价是写请求排在采集请求之后）。
            with self._session_lock:
                session = self._session
                if session is None:
                    raise AdapterDataError('HTTP 会话未就绪')
                response = session.request(request.method, url, **kwargs)
            response.raise_for_status()
            if not expect_json:
                if getattr(response, 'status_code', None) == 204:
                    return {}
                raw_content = getattr(response, 'content', None)
                raw_text = getattr(response, 'text', None)
                if raw_content == b'' and (raw_text is None or (isinstance(raw_text, str) and not raw_text.strip())):
                    return {}
                if raw_content is None and isinstance(raw_text, str) and not raw_text.strip():
                    return {}
                try:
                    return response.json()
                except ValueError:
                    return {}
            try:
                return response.json()
            except ValueError as exc:
                raise AdapterDataError(
                    f'响应不是 JSON（{response.headers.get("Content-Type", "?")}）') from exc

        self.stats['total_requests'] += 1
        target_breaker = breaker if breaker is not None else self._circuit_breaker
        payload = target_breaker.call(_do_request)
        self.stats['successful_requests'] += 1
        self.stats['last_request_time'] = datetime.now().isoformat()
        return payload

    @staticmethod
    def _accepts_quality_kwarg(callback: Callable[..., Any]) -> bool:
        try:
            sig = inspect.signature(callback)
        except (TypeError, ValueError):
            return False
        for p in sig.parameters.values():
            if p.kind == inspect.Parameter.VAR_KEYWORD or p.name == 'quality':
                return True
        return False

    def _notify(self, mapped: dict[str, dict[str, Any]]) -> None:
        for name, item in mapped.items():
            for callback in list(self._data_callbacks):
                try:
                    if self._accepts_quality_kwarg(callback):
                        callback(self.device_id, name, item.get('value'),
                                 item.get('unit', ''), quality=item.get('quality', 192))
                    else:
                        callback(self.device_id, name, item.get('value'),
                                 item.get('unit', ''))
                except Exception as exc:      # noqa: BLE001 —— 回调失败不影响采集
                    logger.error("自定义适配器回调异常 %s/%s: %s",
                                 self.device_id, name, exc)

    # ------------------------------------------------------------------
    # 对外接口（与 REST/OPC UA/MQTT 客户端一致）
    # ------------------------------------------------------------------

    def get_latest_data(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            data = {name: dict(item) for name, item in self.latest_data.items()}
        # round 385：断线后快照只能以 quality=0（Bad）交付
        if not self.connected:
            for item in data.values():
                item['quality'] = 0
        return data

    def _mark_quality_bad(self) -> None:
        """把当前缓存的质量码降为 0（Bad），值保留便于排查。"""
        with self._lock:
            for item in self.latest_data.values():
                item['quality'] = 0

    def add_data_callback(self, callback: Callable[..., Any]):
        self._data_callbacks.append(callback)

    def get_stats(self) -> dict[str, Any]:
        stats = dict(self.stats)
        stats['connected'] = self.connected
        stats['poll_interval'] = self.poll_interval
        return stats

    def write_point(self, name: str, value: Any) -> bool:
        """按 `adapter.write` 回写一个点位；未配置 write 时返回 False。"""
        if self._session is None or not self.connected or self.spec is None:
            # 拒绝写入与通信失败必须可区分（否则调用方只能猜）
            logger.warning("自定义适配器未连接，拒绝写入 %s/%s=%r（connected=%s）",
                           self.device_id, name, value, self.connected)
            return False
        if self.spec.write is None:
            logger.warning("设备 %s 未配置 adapter.write，拒绝写入 %s",
                           self.device_id, name)
            return False

        # round 385：NaN/±Infinity 一律拒绝。`requests` 会把它们序列化成
        # 非法的 `NaN`/`Infinity` 字面量；部分网关照收并写进寄存器，
        # 等于把"没有数值"当成设定值下发（与 safe_numeric 的统一口径一致）。
        if not isinstance(value, bool) and isinstance(value, (int, float)):
            if try_finite_float(value) is None:
                self.stats['failed_requests'] += 1
                self.stats['last_error'] = f'写入值不是有限数值: {value!r}'
                logger.error("自定义适配器拒绝非有限写入值 %s/%s=%r",
                             self.device_id, name, value)
                return False

        context = {'device_id': self.device_id, 'name': str(name), 'value': value}
        try:
            body = _fill_obj(self.spec.write.json, context)
            payload = self._fetch(
                self.spec.write,
                path_context=context,
                body_override=body,
                expect_json=False,
                breaker=self._write_breaker,
                allow_redirects=False,
            )
            if is_failure(payload) or (
                isinstance(payload, Mapping) and payload.get('ok') is False
            ):
                self.stats['failed_requests'] += 1
                self.stats['last_error'] = f'对端写入返回失败: {payload!r}'
                logger.error("自定义适配器对端写入失败 %s/%s=%r: %r",
                             self.device_id, name, value, payload)
                return False
        except Exception as exc:      # noqa: BLE001 —— 写失败必须返回 False
            self.stats['failed_requests'] += 1
            self.stats['last_error'] = str(exc)
            logger.error("自定义适配器写入失败 %s/%s=%r: %s",
                         self.device_id, name, value, exc)
            return False

        self.stats['writes'] += 1
        return True

    # ------------------------------------------------------------------
    # 认证 / 清理
    # ------------------------------------------------------------------

    def _setup_auth(self) -> None:
        auth = self.spec.auth or {}
        auth_type = str(auth.get('type', 'none')).lower()
        if auth_type == 'none':
            return

        token = str(auth.get('token') or '')
        header = str(auth.get('header') or 'X-API-Key')

        if auth_type == 'basic':
            username = str(auth.get('username') or '')
            if not username:
                raise AdapterConfigError('auth.type=basic 需要 username')
            self._session.auth = (username, str(auth.get('password') or ''))

        elif auth_type == 'bearer':
            if not token:
                raise AdapterConfigError('auth.type=bearer 需要 token')
            self._session.headers['Authorization'] = f'Bearer {token}'

        elif auth_type == 'api_key':
            value = str(auth.get('value') or token)
            if not value:
                raise AdapterConfigError('auth.type=api_key 需要 value（或 token）')
            self._session.headers[header] = value

    def _close_session(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            try:
                session.close()
            except Exception as exc:      # noqa: BLE001
                # 关闭失败意味着连接池/socket 残留，必须可见
                logger.warning("关闭自定义适配器会话失败: %s", exc)


# ---------------------------------------------------------------------------
# 模板替换（读请求只认 device_id；写请求额外认 name/value）
# ---------------------------------------------------------------------------

def _fill_text(text: str, context: dict[str, Any]) -> str:
    # 单次扫描：逐项 str.replace 会把「值里含 {其它占位符}」再替换一遍
    # （例如 name='{value}'），造成串值。re.sub 的回调保证只替换原始文本。
    pattern = re.compile(r'\{(' + '|'.join(
        re.escape(k) for k in context) + r')\}') if context else None
    if pattern is None:
        return str(text)
    return pattern.sub(lambda m: str(context[m.group(1)]), str(text))


def _fill_obj(obj: Any, context: dict[str, Any]) -> Any:
    """递归替换占位符；**整个字符串就是占位符时保留原类型**（数字仍是数字）。"""
    if isinstance(obj, str):
        for key, value in context.items():
            if obj == '{' + key + '}':
                return value
        return _fill_text(obj, context)
    if isinstance(obj, list):
        return [_fill_obj(item, context) for item in obj]
    if isinstance(obj, dict):
        return {key: _fill_obj(value, context) for key, value in obj.items()}
    return obj
