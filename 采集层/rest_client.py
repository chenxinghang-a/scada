"""
REST HTTP设备客户端模块
通过REST API采集设备数据，支持对接各类智能网关和工业物联网平台

典型使用场景：
- 对接涂鸦/阿里云/华为云等IoT平台的设备API
- 对接智能网关（如研华、MOXA）的HTTP接口
- 对接PLC的内置Web Server（如西门子S7-1200/1500）
- 对接第三方SCADA系统的数据接口
"""

import json
import time
import logging
import threading
import requests
from typing import Any, Callable
from datetime import datetime

from core.circuit_breaker import get_circuit_breaker, CircuitBreakerOpenError
from core.safe_numeric import try_int

logger = logging.getLogger(__name__)


class RESTDeviceClient:
    """
    REST HTTP设备客户端
    通过HTTP请求采集设备数据，支持轮询和Webhook两种模式

    配置示例：
    ```yaml
    devices:
      - id: gateway_01
        name: "智能网关1号"
        protocol: rest
        base_url: "http://192.168.1.200/api"
        endpoints:
          - name: "temperature"
            path: "/sensors/temp"
            method: GET
            json_path: "data.value"
            unit: "°C"
          - name: "humidity"
            path: "/sensors/humi"
            method: GET
            json_path: "data.value"
            unit: "%RH"
        poll_interval: 10
        auth_type: bearer
        auth_token: "your_token_here"
    ```
    """

    def __init__(self, config: dict[str, Any]):
        """
        初始化REST设备客户端

        Args:
            config: 设备配置字典
        """
        self.config = config
        self.device_id = config.get('id', 'rest_device')
        self.device_name = config.get('name', 'REST设备')
        self.base_url = config.get('base_url', 'http://localhost')

        # 端点配置
        self.endpoints = config.get('endpoints', [])

        # 轮询间隔
        self.poll_interval = config.get('poll_interval', 10)

        # 认证配置
        self.auth_type = config.get('auth_type', 'none')  # none/basic/bearer/api_key
        self.auth_token = config.get('auth_token', '')
        self.auth_username = config.get('auth_username', '')
        self.auth_password = config.get('auth_password', '')
        self.api_key_header = config.get('api_key_header', 'X-API-Key')
        self.api_key_value = config.get('api_key_value', '')

        # 自定义请求头
        self.custom_headers = config.get('headers', {})

        # 连接状态
        self.connected = False

        # 最新数据缓存（线程安全）
        self.latest_data: dict[str, dict[str, Any]] = {}
        self._data_lock = threading.Lock()

        # 数据回调
        self._data_callbacks: list[Callable[..., Any]] = []

        # 轮询线程
        self._thread: threading.Thread | None = None
        self._running = False

        # HTTP会话（复用连接）
        self._session: requests.Session | None = None

        # 熔断器（保护外部HTTP调用）
        self._circuit_breaker = get_circuit_breaker(
            name=f"rest_client_{self.device_id}",
            failure_threshold=config.get('circuit_breaker_threshold', 5),
            timeout_seconds=config.get('circuit_breaker_timeout', 60),
        )

        # 统计
        self.stats: dict[str, Any] = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'connected_since': None,
            'last_request_time': None,
            'last_error': None
        }

        logger.info(f"REST设备客户端初始化: {self.base_url}")

    def add_data_callback(self, callback: Callable[..., Any]):
        """添加数据回调函数"""
        self._data_callbacks.append(callback)

    def connect(self) -> bool:
        """
        建立连接（创建HTTP会话，测试连通性）

        Returns:
            bool: 连接是否成功
        """
        try:
            self._session = requests.Session()
            # round 369：**禁用系统代理**。REST 设备是现场内网/本机服务，
            # 一旦服务器上配置了 HTTP_PROXY/HTTPS_PROXY，requests 会走
            # `trust_env` 把采集与控制写入全部发给代理 —— 内网地址代理不可达，
            # 表现为"所有 REST 设备同时掉线"。
            # （与 Round 364 对 tdengine_client 的修复同口径。）
            self._session.trust_env = False
            self._session.proxies = {'http': None, 'https': None}
            self._session.headers.update({'Content-Type': 'application/json'})
            self._session.headers.update(self.custom_headers)

            # 设置认证
            self._setup_auth()

            # 设置超时（connect 3s, read 5s — 工业场景下不宜太长）
            #
            # ⚠️ round 341：原来写的是 `self._session.timeout = (3, 5)` ——
            # `requests.Session` **没有 timeout 属性**，这行只是给对象挂了
            # 一个自定义属性，requests 完全不看它。结果是**所有 REST 请求
            # 都没有超时**，对端不响应时永久阻塞：
            # 采集线程卡死在这台设备上，该设备的采集任务再也不返回，
            # 而日志里连「超时」都不会出现（因为根本没超时）。
            # 必须作为 `timeout=` 参数逐个传（见下方 _request 调用）。
            self._timeout = self._resolve_timeout()

            # 测试连通性（尝试第一个端点）
            if self.endpoints:
                first_ep = self.endpoints[0]
                url = self.base_url.rstrip('/') + first_ep.get('path', '/')
                method = str(first_ep.get('method', 'GET')).upper()
                # round 370（BUG-026）：探针必须带上端点自己的参数/请求体。
                # 原来只发裸请求：需要 `params` 的 GET 或需要 body 的 POST
                # 一律 400/422 → `raise_for_status()` 抛错 → 整台设备判"连接失败"，
                # 而实际用 curl 带参数访问完全正常。
                probe_kwargs: dict[str, Any] = {}
                if first_ep.get('params') and method in ('GET', 'DELETE'):
                    probe_kwargs['params'] = first_ep['params']
                probe_body = (first_ep.get('json') or first_ep.get('probe_body')
                              or first_ep.get('write_body'))
                if probe_body and method in ('POST', 'PUT', 'PATCH'):
                    probe_kwargs['json'] = probe_body
                resp = self._session.request(
                    method=method,
                    url=url,
                    timeout=getattr(self, '_timeout', (3, 5)),
                    **probe_kwargs,
                )
                resp.raise_for_status()

            self.connected = True
            self.stats['connected_since'] = datetime.now().isoformat()
            logger.info(f"REST设备已连接: {self.base_url}")

            # 启动轮询。用 Event 而不是 sleep：断开设备时能立刻唤醒退出，
            # 不用等满一个轮询周期。
            self._running = True
            self._stop_event = threading.Event()
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._thread.start()

            return True

        except Exception as e:
            logger.error(f"REST设备连接失败: {e}")
            self.stats['last_error'] = str(e)
            self.connected = False
            return False

    def disconnect(self):
        """断开连接"""
        self._running = False
        stop_event = getattr(self, '_stop_event', None)
        if stop_event is not None:
            stop_event.set()      # 立刻唤醒 sleep 中的轮询线程
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        if self._session:
            self._session.close()
            self._session = None
        self.connected = False
        logger.info(f"REST设备已断开: {self.device_id}")

    def get_latest_data(self) -> dict[str, dict[str, Any]]:
        """获取所有端点的最新数据"""
        with self._data_lock:
            return dict(self.latest_data)

    def read_endpoint(self, endpoint_config: dict[str, Any]) -> Any:
        """
        读取单个端点数据（带熔断器保护）

        Args:
            endpoint_config: 端点配置

        Returns:
            端点返回的数据
        """
        url = self.base_url.rstrip('/') + endpoint_config.get('path', '/')
        method = endpoint_config.get('method', 'GET').upper()
        params = endpoint_config.get('params', {})
        body = endpoint_config.get('body')

        # 使用熔断器保护
        try:
            def _do_request():
                resp = self._session.request(
                    method=method,
                    url=url,
                    params=params if method == 'GET' else None,
                    json=body if method in ('POST', 'PUT', 'PATCH') else None,
                    timeout=getattr(self, '_timeout', (3, 5))
                )
                resp.raise_for_status()
                return resp.json()

            data = self._circuit_breaker.call(_do_request)

            self.stats['total_requests'] += 1
            self.stats['successful_requests'] += 1
            self.stats['last_request_time'] = datetime.now().isoformat()

            json_path = endpoint_config.get('json_path', '')
            value = self._extract_by_path(data, json_path) if json_path else data
            return value

        except CircuitBreakerOpenError as e:
            # 熔断器打开，快速失败
            self.stats['total_requests'] += 1
            self.stats['failed_requests'] += 1
            self.stats['last_error'] = f"熔断器打开: {e}"
            logger.warning(f"REST请求被熔断器拒绝 [{endpoint_config.get('path')}]: {e}")
            return None

        except Exception as e:
            self.stats['total_requests'] += 1
            self.stats['failed_requests'] += 1
            self.stats['last_error'] = str(e)
            logger.error(f"REST请求失败 [{endpoint_config.get('path')}]: {e}")
            return None

    def write_endpoint(self, endpoint_config: dict[str, Any], value: Any,
                       method: str | None = None) -> bool:
        """
        写入端点数据（控制命令）

        Args:
            endpoint_config: 端点配置
            value: 要写入的值
            method: 显式指定 HTTP 方法（round 331）。
                ``展示层/api/api_control.py`` 按用户选的 POST/PUT 用关键字传进来；
                不传时退回 ``endpoint_config['write_method']``。

        ⚠️ round 331：原来只有 2 个形参，而 API 层无条件用
        ``client.write_endpoint(..., method=method)`` 调用 —— 真实设备
        （``RESTDeviceClient``）**每一次写入都 TypeError → 500**，
        客户端只看到 ``{"error": "Internal server error"}``。
        这个功能在真实模式下从来没成功过，且没有一条正向测试覆盖。

        Returns:
            bool: 是否成功
        """
        # round 370（BUG-026）：写路径必须与读路径同口径 ——
        # 连接态检查、熔断器、统计一个都不能少。原来直接 `self._session.request`：
        # ① 连接断开后仍继续发（`_session` 可能已是 None → AttributeError）；
        # ② 对端连续失败时不熔断，采集线程被拖死；
        # ③ `/api/system/status` 的读写统计里永远看不到写失败。
        if not self.connected or self._session is None:
            logger.warning("REST 设备未连接，拒绝写入: %s", self.base_url)
            self.stats['failed_requests'] = self.stats.get('failed_requests', 0) + 1
            return False

        url = self.base_url.rstrip('/') + endpoint_config.get('path', '/')
        body = endpoint_config.get('write_body', {'value': value})
        if callable(body):
            body = body(value)
        write_method = method or endpoint_config.get('write_method', 'POST')

        def _do_write():
            resp = self._session.request(
                method=write_method,
                url=url,
                json=body,
                timeout=getattr(self, '_timeout', (3, 5))
            )
            resp.raise_for_status()
            return resp

        try:
            self._circuit_breaker.call(_do_write)
            self.stats['total_requests'] += 1
            self.stats['successful_requests'] += 1
            self.stats['writes'] = self.stats.get('writes', 0) + 1
            return True

        except Exception as e:
            self.stats['total_requests'] += 1
            self.stats['failed_requests'] += 1
            self.stats['last_error'] = f'写入失败: {e}'
            logger.error(f"REST写入失败: {e}")
            return False

    def _resolve_timeout(self) -> tuple[float, float]:
        """解析超时配置，返回 ``(connect, read)`` 秒。

        支持三种写法（现场网关响应速度差异很大，必须可配）：
            timeout: 10                 → (10, 10)
            timeout: [3, 10]            → (3, 10)
            timeout: {connect: 3, read: 10}
        非法值退回默认 (3, 5) 并告警 —— 不能因为一个配置写错就让采集无超时。
        """
        from core.safe_numeric import try_finite_float

        default = (3.0, 5.0)
        raw = self.config.get('timeout', default)

        def _pair(connect_v, read_v):
            c = try_finite_float(connect_v)
            r = try_finite_float(read_v)
            if c is None or r is None or c <= 0 or r <= 0:
                logger.warning(
                    "[REST] %s: timeout=%r 非法，用默认 %s", self.device_name, raw, default)
                return default
            return (c, r)

        if isinstance(raw, dict):
            return _pair(raw.get('connect', default[0]), raw.get('read', default[1]))
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            return _pair(raw[0], raw[1])
        single = try_finite_float(raw)
        if single is not None and single > 0:
            return (single, single)
        logger.warning("[REST] %s: timeout=%r 无法解析，用默认 %s", self.device_name, raw, default)
        return default

    def _setup_auth(self):
        """设置HTTP认证"""
        if self.auth_type == 'bearer':
            self._session.headers['Authorization'] = f'Bearer {self.auth_token}'
        elif self.auth_type == 'basic':
            self._session.auth = (self.auth_username, self.auth_password)
        elif self.auth_type == 'api_key':
            self._session.headers[self.api_key_header] = self.api_key_value

    def _poll_loop(self):
        """轮询采集循环"""
        while self._running:
            try:
                for ep in self.endpoints:
                    ep_name = ep.get('name', ep.get('path', 'unknown'))
                    value = self.read_endpoint(ep)

                    if value is not None:
                        with self._data_lock:
                            self.latest_data[ep_name] = {
                                'value': value,
                                'unit': ep.get('unit', ''),
                                'timestamp': datetime.now().isoformat(),
                                'quality': 'good',
                                'endpoint': ep.get('path', ''),
                                'device_id': self.device_id
                            }

                        # 触发回调
                        for callback in self._data_callbacks:
                            try:
                                callback(self.device_id, ep_name, value, ep.get('unit', ''))
                            except Exception as e:
                                logger.error(f"数据回调异常: {e}")
                    else:
                        # 标记为通信故障
                        with self._data_lock:
                            self.latest_data[ep_name] = {
                                'value': None,
                                'quality': 'bad',
                                'timestamp': datetime.now().isoformat(),
                                'error': self.stats.get('last_error', 'unknown')
                            }

            except Exception as e:
                logger.error(f"轮询异常: {e}")

            # round 369：阻塞式 sleep 换成 Event.wait —— disconnect() 能立刻
            # 唤醒线程退出；原来最长要等一个 poll_interval（现场可配到几十秒）。
            stop_event = getattr(self, '_stop_event', None)
            if stop_event is not None:
                stop_event.wait(self.poll_interval)
            else:
                time.sleep(self.poll_interval)

    @staticmethod
    def _extract_by_path(data: Any, path: str) -> Any:
        """
        从JSON数据中按路径提取值

        Args:
            data: JSON数据（已解析）
            path: 点分路径，如 "data.sensors[0].value"

        Returns:
            提取到的值
        """
        if not path:
            return data

        parts = path.split('.')
        current = data

        for part in parts:
            if current is None:
                return None

            # 处理数组索引: sensors[0]
            if '[' in part and part.endswith(']'):
                bracket_pos = part.index('[')
                key = part[:bracket_pos]
                idx_str = part[bracket_pos + 1:-1]
                idx = try_int(idx_str)
                if idx is None:
                    return None

                # 先按key导航
                if key:
                    if not isinstance(current, dict):
                        return None
                    current = current.get(key)

                # 再按索引取值
                if isinstance(current, list) and 0 <= idx < len(current):
                    current = current[idx]
                else:
                    return None
            elif isinstance(current, dict):
                current = current.get(part)
            else:
                return None

        return current
