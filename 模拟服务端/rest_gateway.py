"""REST 模拟网关：给每台 `protocol: rest` 设备起一个**真** HTTP 服务。

## 为什么不用 Flask

主应用自己就跑 Flask（`展示层/`），再拉一个 Flask 实例进来有两个问题：
依赖更重、并且容易和主应用的端口/配置打架。这里只需要「按路径返回 JSON」，
标准库 `http.server` 足够，且能精确控制响应体形状 —— 而响应体形状正是要验证的
东西（`RESTDeviceClient` 靠 `json_path` 取数，路径对不上就是 `None`）。

## 端口从哪来

配置里的 `sim_endpoint: {host, port}` 是**权威**的（`tools/gen_factory.py`
按协议分段分配：REST 从 15500 起，个别设备直接落到 15582+）。所以：

1. 有 `sim_endpoint.port` → 用它（否则配置里写好的 `base_url` 会指向没人监听的端口）
2. 没有 → 从 `base_port` 递增分配（老配置没有这个字段）

## 响应体形状

配置里的 `json_path` 是**客户端取数路径**，所以服务端要按它**反向构造嵌套**：

    json_path: data.value  ->  {"data": {"value": 12.3}}

这样 `RESTDeviceClient._extract_by_path(data, 'data.value')` 才取得到值。

同一个 `path` 下可能有多个端点（`agv_system_01` 的 `/api/agv/fleet` 一次返回
total/active/idle/...），必须**合并**成一个 JSON 对象，不能按 path 去重成最后一个。
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from core.safe_numeric import try_finite_float, try_int

logger = logging.getLogger(__name__)


def build_nested(json_path: str, value: Any) -> Any:
    """按点分路径反向构造嵌套 dict：`a.b` + 1 -> `{'a': {'b': 1}}`。"""
    parts = [p for p in str(json_path or '').split('.') if p]
    if not parts:
        return value
    out: Any = value
    for key in reversed(parts):
        out = {key: out}
    return out


def deep_merge(dst: dict[str, Any], src: Any) -> dict[str, Any]:
    """把 `src`（可能是嵌套 dict）并进 `dst`，同名字典递归合并。"""
    if not isinstance(src, dict):
        return dst
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            deep_merge(dst[key], value)
        else:
            dst[key] = value
    return dst


class _DeviceGatewayServer(ThreadingHTTPServer):
    """一台设备一个 HTTP 服务；把设备上下文挂在 server 上供 handler 读取。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], hub: Any, device_id: str,
                 routes: dict[str, list[dict[str, Any]]]):
        self.hub = hub
        self.device_id = device_id
        self.routes = routes
        super().__init__(addr, _GatewayHandler)


class _GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    # 现场网关每 1 秒被轮询一次，默认的 stderr 访问日志会刷爆日志文件，
    # 而且和 SCADA 自己的结构化日志混在一起无法解析。
    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("[rest-sim %s] " + fmt, self.server.device_id, *args)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定命名
        path = urlsplit(self.path).path
        if len(path) > 1:
            path = path.rstrip('/')

        if path == '/health':
            self._send_json(200, {
                'ok': True,
                'device_id': self.server.device_id,
                'endpoints': sorted(self.server.routes),
            })
            return

        # round 370（BUG-091）：路由表现在同时收 GET 与写端点，
        # GET 必须只挑 GET，否则同一个 path 的 POST 端点会被当成读端点，
        # 返回值恒为 None → 503。
        endpoints = [e for e in (self.server.routes.get(path) or [])
                     if str(e.get('method', 'GET')).upper() == 'GET']
        if not endpoints:
            self._send_json(404, {
                'error': 'not_found',
                'path': path,
                'device_id': self.server.device_id,
            })
            return

        body: dict[str, Any] = {}
        missing: list[str] = []
        for endpoint in endpoints:
            name = endpoint.get('name') or path
            value = self._read_point(name)
            if value is None:
                missing.append(name)
                continue
            built = build_nested(endpoint.get('json_path', ''), value)
            if isinstance(built, dict):
                deep_merge(body, built)
            else:
                # json_path 为空 = 整个响应体就是该值；同路径多端点时无法合并，
                # 只能取第一个，其余丢掉（配置里不存在这种写法）
                body = built  # type: ignore[assignment]
                break

        if not body and missing:
            # 200 + null 会让客户端把「点位不存在」当成正常数据入库，
            # 用 503 让 RESTDeviceClient 明确计一次失败。
            self._send_json(503, {
                'error': 'point_unavailable',
                'points': missing,
                'device_id': self.server.device_id,
            })
            return

        if missing:
            logger.debug("[rest-sim %s] %s 部分点位缺失: %s",
                         self.server.device_id, path, missing)

        self._send_json(200, body)

    def do_POST(self) -> None:  # noqa: N802
        self._handle_write()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle_write()

    def _handle_write(self) -> None:
        """处理 REST 设备的控制写入（round 369）。

        原来 `do_POST` / `do_PUT` 硬编码返回 405：`--simulator` 模式下
        `RESTDeviceClient.write_endpoint()` 向模拟网关发 POST/PUT 必然收到
        `405 Method Not Allowed`，前端"写端点"功能 100% 失败 —— 模拟环境
        根本验证不了这条控制链路。
        """
        path = urlsplit(self.path).path
        if len(path) > 1:
            path = path.rstrip('/')

        length = try_int(self.headers.get('Content-Length')) or 0
        raw = self.rfile.read(length) if length > 0 else b''
        try:
            payload: Any = json.loads(raw.decode('utf-8')) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {
                'error': 'invalid_json',
                'device_id': self.server.device_id,
            })
            return

        # 只认写端点（POST/PUT）
        endpoints = [e for e in (self.server.routes.get(path) or [])
                     if str(e.get('method', 'GET')).upper() in ('POST', 'PUT')]
        if not endpoints:
            self._send_json(404, {
                'error': 'not_found',
                'path': path,
                'device_id': self.server.device_id,
            })
            return

        written: dict[str, Any] = {}
        failed: dict[str, str] = {}
        for endpoint in endpoints:
            name = endpoint.get('name') or path
            value = self._extract_write_value(payload, endpoint, name)
            if value is None:
                failed[name] = '未在请求体中提供该点位的值'
                continue
            try:
                ok = bool(self.server.hub.write_point(
                    self.server.device_id, name, value))
            except Exception as exc:      # noqa: BLE001
                logger.warning("[rest-sim %s] 写入 %s 失败: %s",
                               self.server.device_id, name, exc)
                ok = False
            if ok:
                written[name] = value
            else:
                failed[name] = '点位不存在或不可写'

        if not written:
            self._send_json(400, {
                'error': 'write_rejected',
                'device_id': self.server.device_id,
                'failed': failed,
            })
            return

        self._send_json(200, {
            'success': True,
            'device_id': self.server.device_id,
            'written': written,
            **({'failed': failed} if failed else {}),
        })

    @staticmethod
    def _extract_write_value(payload: Any, endpoint: dict, name: str) -> Any:
        """从请求体里取该端点的目标值。

        兼容三种现场写法：
          1. ``{"value": 42}``              —— RESTDeviceClient 的默认形状
          2. ``{"data": {"value": 42}}``    —— 按 json_path 嵌套
          3. ``{"<point_name>": 42}``       —— 批量/点位字典
        """
        if isinstance(payload, dict):
            if 'value' in payload:
                return payload['value']
            json_path = str(endpoint.get('json_path') or '')
            if json_path:
                cur: Any = payload
                for part in json_path.split('.'):
                    if not isinstance(cur, dict) or part not in cur:
                        cur = None
                        break
                    cur = cur[part]
                if cur is not None:
                    return cur
            if name in payload:
                return payload[name]
            if len(payload) == 1:
                return next(iter(payload.values()))
        return payload if not isinstance(payload, (dict, list)) else None

    def _method_not_allowed(self) -> None:
        self._send_json(405, {
            'error': 'method_not_allowed',
            'device_id': self.server.device_id,
            'allow': 'GET',
        }, extra_headers={'Allow': 'GET'})

    def _read_point(self, name: str) -> Any:
        """取点位当前值；数值统一成 float，字符串/布尔原样返回。

        `agv_system_status`、`machine_mode` 这类点位在配置里是字符串语义，
        强行 `float()` 会变成 None → 整个端点 503，现场看起来像网关坏了。
        """
        try:
            snap = self.server.hub.snapshot(self.server.device_id) or {}
        except (KeyError, ValueError, RuntimeError) as exc:
            logger.warning("[rest-sim %s] 读取快照失败: %s", self.server.device_id, exc)
            return None

        raw = snap.get(name)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            # 数值字符串要转成数字：JSON 里 "12.3" 和 12.3 在客户端那边是两种类型
            numeric = try_finite_float(raw)
            return numeric if numeric is not None else raw
        return try_finite_float(raw)

    def _send_json(self, status: int, payload: Any,
                   extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


class RestSimGateway:
    """所有 `protocol: rest` 设备的模拟网关集合（每台设备独立端口）。"""

    def __init__(self, hub: Any, base_port: int = 15500, host: str = '127.0.0.1'):
        self.hub = hub
        self.base_port = base_port
        self.host = host
        self._servers: dict[str, _DeviceGatewayServer] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._ports: dict[str, int] = {}

    def start(self) -> dict[str, int]:
        """起所有 REST 设备的 HTTP 服务，返回 {device_id: port}。"""
        devices = getattr(self.hub, 'devices', {}) or {}
        rest_devices = [
            (dev_id, cfg) for dev_id, cfg in sorted(devices.items())
            if isinstance(cfg, dict)
            and str(cfg.get('protocol', '')).lower() == 'rest'
            and cfg.get('enabled') is not False
        ]

        used_ports: set[int] = set()
        for offset, (device_id, cfg) in enumerate(rest_devices):
            if device_id in self._servers:
                continue
            routes = self._build_routes(cfg)
            if not routes:
                logger.warning("REST 设备 %s 没有 endpoints，跳过", device_id)
                continue

            host, port = self._resolve_addr(cfg, offset)
            if port in used_ports:
                logger.error("REST 模拟网关 %s 的端口 %d 已被其它设备占用，跳过",
                             device_id, port)
                continue

            try:
                server = _DeviceGatewayServer((host, port), self.hub, device_id, routes)
            except OSError as exc:
                # 端口被占用时不能静默跳过：少一台设备会让「采集不到」看起来像
                # 客户端问题。日志里必须留下端口号和原因。
                logger.error("REST 模拟网关 %s 绑定 %s:%d 失败: %s",
                             device_id, host, port, exc)
                continue

            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={'poll_interval': 0.2},
                name=f'rest-sim-{device_id}', daemon=True)
            thread.start()
            used_ports.add(port)
            self._servers[device_id] = server
            self._threads[device_id] = thread
            self._ports[device_id] = port
            logger.info("REST 模拟网关 %s -> http://%s:%d（%d 个路径）",
                        device_id, host, port, len(routes))

        return dict(self._ports)

    def stop(self) -> None:
        """停所有网关（幂等）。"""
        for device_id, server in list(self._servers.items()):
            server.shutdown()
            server.server_close()
            thread = self._threads.get(device_id)
            if thread and thread.is_alive():
                thread.join(timeout=5.0)
            logger.info("REST 模拟网关已停止: %s", device_id)
        self._servers.clear()
        self._threads.clear()
        self._ports.clear()

    def _resolve_addr(self, cfg: dict[str, Any], offset: int) -> tuple[str, int]:
        sim = cfg.get('sim_endpoint')
        if isinstance(sim, dict):
            port = sim.get('port')
            if isinstance(port, int) and 0 < port < 65536:
                host = str(sim.get('host') or self.host)
                return host, port
            logger.warning("REST 设备 %s 的 sim_endpoint.port=%r 非法，退回自动分配",
                           cfg.get('id'), port)
        return self.host, self.base_port + offset

    @staticmethod
    def _build_routes(cfg: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        """把 endpoints[] 转成 {path: [endpoint, ...]}；只收 GET。

        用列表而不是单值：同一 path 可以有多个端点（不同 json_path 各取一个字段），
        按单值存会只剩最后一个，客户端只能拿到一个点位。
        """
        routes: dict[str, list[dict[str, Any]]] = {}
        for endpoint in cfg.get('endpoints') or []:
            if not isinstance(endpoint, dict):
                continue
            path = str(endpoint.get('path') or '').strip()
            if not path:
                logger.warning("REST 设备 %s 有端点缺 path，跳过: %r",
                               cfg.get('id'), endpoint)
                continue
            method = str(endpoint.get('method', 'GET')).upper()
            # round 370（BUG-091）：**写端点也要建路由**。
            # 原来在这里 `continue` 掉一切非 GET，而 `do_POST/do_PUT`
            # 早已实现（round 369）—— 结果写请求永远走到 404。
            if method not in ('GET', 'POST', 'PUT'):
                logger.debug("REST 模拟网关不支持 %s %s，跳过", method, path)
                continue
            endpoint['method'] = method
            if not path.startswith('/'):
                path = '/' + path
            routes.setdefault(path, []).append(endpoint)
        return routes
