# -*- coding: utf-8 -*-
"""工厂自写插件适配器（round 377）。

给工厂一个**可开发、可交付**的扩展点：在 `扩展/adapters/` 下放一个 `.py`，
配置里写 `transport: plugin` + `plugin: <文件名>` 就能接进来，不需要改主程序。

插件契约（最小实现）
--------------------
```python
PLUGIN_INFO = {
    'version': '1.0',
    'description': '对接 XX 厂 MES',
    'author': '工厂自动化部',
}

class Adapter:
    def __init__(self, settings: dict):
        self.url = settings['url']

    def read(self) -> dict:
        # 返回 {点位名: 值}，或返回原始载荷并配合 adapter.points 做映射
        return {'temperature': 25.5, 'run_state': 'RUNNING'}

    def write_point(self, name: str, value) -> bool:
        ...  # 可选
```

也可以导出 `create_adapter(settings) -> object` 代替 `Adapter` 类。

安全边界（必须知道）
--------------------
* 只从固定目录加载（默认 `扩展/adapters/`，可用 `SCADA_PLUGIN_DIR` 覆盖）；
* 插件名必须是合法 Python 标识符，路径做父目录校验，禁止 `../`；
* **插件代码以主进程权限运行**，没有沙箱、没有超时强杀 —— 只部署可信代码；
* 导入/构造/读取失败都只影响这一台设备：记 ERROR + `stats.last_error`，
  采集循环继续，不会拖垮进程。
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import paths

from core.action_result import is_failure
from core.safe_numeric import try_finite_float

from .adapter_spec import (
    AdapterConfigError,
    AdapterDataError,
    AdapterSpec,
    map_message,
)

logger = logging.getLogger(__name__)

#: 环境变量覆盖插件目录（部署时指向工厂自己的可写目录）
PLUGIN_DIR_ENV = 'SCADA_PLUGIN_DIR'
#: 默认插件目录（相对项目根，冻结时是 exe 同目录，方便现场放插件）
DEFAULT_PLUGIN_SUBDIR = ('扩展', 'adapters')

_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_MODULE_PREFIX = 'scada_plugin_'


class PluginError(RuntimeError):
    """插件加载/运行失败（调用方应记 last_error 并拒绝该设备）。"""


# ---------------------------------------------------------------------------
# 目录 / 发现
# ---------------------------------------------------------------------------

def get_plugin_dir() -> Path:
    """插件根目录。优先 `SCADA_PLUGIN_DIR`，否则 `<项目根>/扩展/adapters`。"""
    raw = os.environ.get(PLUGIN_DIR_ENV)
    if raw and raw.strip():
        path = Path(raw.strip()).expanduser()
        return path if path.is_absolute() else (paths.PROJECT_ROOT / path)
    return paths.PROJECT_ROOT.joinpath(*DEFAULT_PLUGIN_SUBDIR)


def plugin_path(name: str) -> Path:
    """把插件名解析成文件路径，并做**父目录校验**（防目录穿越）。"""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise PluginError(f'非法的插件名: {name!r}（只允许字母/数字/下划线）')
    base = get_plugin_dir().resolve()
    path = (base / f'{name}.py').resolve()
    if path.parent != base:
        raise PluginError(f'插件路径越界: {path}')
    if not path.is_file():
        raise PluginError(f'插件不存在: {path}（目录: {base}）')
    return path


def _read_metadata(path: Path) -> dict[str, Any]:
    """只读 AST 取 `PLUGIN_INFO` / docstring，**不执行**插件代码。

    `PLUGIN_INFO` 里可选 `settings_schema`（JSON Schema 形状的 dict），
    会原样返回给 `GET /api/devices/adapters`，供界面生成配置表单。
    """
    meta: dict[str, Any] = {'version': '', 'description': '', 'author': '',
                            'settings_schema': None}
    try:
        source = path.read_text(encoding='utf-8')
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError):
        return meta

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == 'PLUGIN_INFO'
                   for t in node.targets):
            continue
        try:
            info = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            continue
        if isinstance(info, dict):
            for key in ('version', 'description', 'author'):
                if info.get(key):
                    meta[key] = str(info[key])
            schema = info.get('settings_schema')
            if isinstance(schema, dict):
                meta['settings_schema'] = schema
        break

    if not meta['description']:
        doc = ast.get_docstring(tree) or ''
        meta['description'] = doc.strip().splitlines()[0] if doc.strip() else ''
    return meta


def discover_plugins() -> list[dict[str, Any]]:
    """列出插件目录里可用的适配器（不导入、不执行，单个坏文件不影响其它）。"""
    base = get_plugin_dir()
    if not base.is_dir():
        return []

    plugins: list[dict[str, Any]] = []
    for path in sorted(base.glob('*.py')):
        if path.name.startswith('_'):
            continue
        name = path.stem
        if not _NAME_RE.fullmatch(name):
            continue
        info = _read_metadata(path)
        plugins.append({
            'name': name,
            'version': info['version'],
            'description': info['description'],
            'author': info['author'],
            'settings_schema': info['settings_schema'],
            'available': True,
            'error': None,
        })
    return plugins


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def _load_module(name: str, path: Path):
    mod_name = f'{_MODULE_PREFIX}{name}'
    mtime = path.stat().st_mtime
    cached = sys.modules.get(mod_name)

    try:
        if cached is not None and getattr(cached, '__file__', None) == str(path):
            if getattr(cached, '__plugin_mtime__', None) == mtime:
                return cached
            module = importlib.reload(cached)
        else:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                raise PluginError(f'无法为 {path} 创建导入规格')
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
    except PluginError:
        raise
    except Exception as exc:        # noqa: BLE001 —— 插件代码的任何异常
        sys.modules.pop(mod_name, None)
        raise PluginError(f'导入插件 {name} 失败: {type(exc).__name__}: {exc}') from exc

    module.__plugin_mtime__ = mtime
    return module


def clear_plugin_cache() -> int:
    """清掉已导入的插件模块，让下一次 connect() 重新读文件（开发期热更新）。"""
    removed = 0
    for mod_name in list(sys.modules):
        if mod_name.startswith(_MODULE_PREFIX):
            sys.modules.pop(mod_name, None)
            removed += 1
    return removed


def load_plugin(name: str, settings: Mapping[str, Any] | None = None):
    """按名字加载插件并实例化；失败抛 `PluginError`。"""
    path = plugin_path(name)
    module = _load_module(name, path)
    payload = dict(settings or {})

    factory = getattr(module, 'create_adapter', None)
    try:
        if callable(factory):
            plugin = factory(payload)
        else:
            cls = getattr(module, 'Adapter', None)
            if cls is None:
                raise PluginError(
                    f'插件 {name} 既没有 create_adapter(settings) 也没有 Adapter 类')
            plugin = cls(payload)
    except PluginError:
        raise
    except Exception as exc:        # noqa: BLE001
        raise PluginError(
            f'插件 {name} 初始化失败: {type(exc).__name__}: {exc}') from exc

    if not (callable(getattr(plugin, 'read', None))
            or callable(getattr(plugin, 'poll', None))):
        raise PluginError(f'插件 {name} 必须实现 read() 或 poll() 方法')
    return plugin


# ---------------------------------------------------------------------------
# 客户端（与 REST/OPC UA/MQTT 客户端同接口）
# ---------------------------------------------------------------------------

class PluginAdapterClient:
    """按 `transport: plugin` 配置轮询工厂插件的采集客户端。"""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.device_id = config.get('id', 'plugin_device')
        self.device_name = config.get('name', '插件设备')

        self._config_error: str | None = None
        try:
            self.spec: AdapterSpec | None = AdapterSpec.from_device_config(config)
        except AdapterConfigError as exc:
            self.spec = None
            self._config_error = str(exc)
            logger.error("插件适配器配置非法 device=%s: %s", self.device_id, exc)

        if self.spec is not None and self.spec.transport != 'plugin':
            self._config_error = (f'PluginAdapterClient 只处理 transport=plugin，'
                                  f'收到 {self.spec.transport!r}')
            self.spec = None
            logger.error("插件适配器配置错误 device=%s: %s",
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
        self._plugin: Any = None
        #: 插件对象串行访问：轮询线程与 `/api/control` 的写可能同时到达，
        #: 插件作者没有义务实现线程安全。
        self._plugin_lock = threading.Lock()

        self.stats: dict[str, Any] = {
            'reads': 0,
            'failed_reads': 0,
            'writes': 0,
            'connected_since': None,
            'last_read_time': None,
            'last_error': None,
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        if self.spec is None:
            self.stats['last_error'] = self._config_error or '插件配置非法'
            logger.error("插件适配器无法连接 device=%s: %s",
                         self.device_id, self.stats['last_error'])
            return False

        try:
            self._plugin = load_plugin(self.spec.plugin, self.spec.settings)
            starter = getattr(self._plugin, 'start', None) \
                or getattr(self._plugin, 'connect', None)
            if callable(starter):
                result = starter()
                if result is False:
                    raise PluginError('插件 start()/connect() 返回 False')

            mapped = self._read_mapped()
            with self._lock:
                self.latest_data = mapped

            self.connected = True
            self.stats['connected_since'] = datetime.now().isoformat()
            logger.info("插件适配器已连接: %s（插件 %s，%d 个点位）",
                        self.device_id, self.spec.plugin, len(mapped))

            if self._thread is None or not self._thread.is_alive():
                self._stop_event.clear()
                self._thread = threading.Thread(
                    target=self._poll_loop,
                    name=f'plugin-adapter-{self.device_id}',
                    daemon=True,
                )
                self._thread.start()
            return True

        except Exception as exc:      # noqa: BLE001 —— 连接失败必须返回 False
            self.stats['failed_reads'] += 1
            self.stats['last_error'] = str(exc)
            self.connected = False
            logger.error("插件适配器连接失败 device=%s: %s", self.device_id, exc)
            self._close_plugin()
            return False

    start = connect

    def stop(self):
        """与其他客户端命名对齐（DeviceManager 只管 stop/connect）。"""
        self.disconnect()

    def disconnect(self):
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive() \
                and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._close_plugin()
        self.connected = False
        logger.info("插件适配器已断开: %s", self.device_id)

    def _close_plugin(self):
        plugin, self._plugin = self._plugin, None
        if plugin is None:
            return
        closer = getattr(plugin, 'stop', None) or getattr(plugin, 'close', None)
        if callable(closer):
            try:
                closer()
            except Exception as exc:      # noqa: BLE001
                logger.warning("关闭插件失败 device=%s: %s", self.device_id, exc)

    # ------------------------------------------------------------------
    # 读取与映射
    # ------------------------------------------------------------------

    def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                mapped = self._read_mapped()
                with self._lock:
                    self.latest_data = mapped
                self._notify(mapped)
            except Exception as exc:      # noqa: BLE001 —— 单轮失败不能停线程
                self.stats['failed_reads'] += 1
                self.stats['last_error'] = str(exc)
                logger.warning("插件适配器读取失败 device=%s: %s",
                               self.device_id, exc)
            self._stop_event.wait(self.poll_interval)

    def _read_mapped(self) -> dict[str, dict[str, Any]]:
        reader = getattr(self._plugin, 'read', None) \
            or getattr(self._plugin, 'poll', None)
        if not callable(reader):
            raise PluginError('插件实例缺少 read()/poll()')
        with self._plugin_lock:
            payload = reader()
        self.stats['reads'] += 1
        self.stats['last_read_time'] = datetime.now().isoformat()

        if self.spec.points:
            return map_message(payload, self.spec.points, self.spec.records_path)
        return _map_flat_payload(payload, self.spec)

    def _notify(self, mapped: Mapping[str, dict[str, Any]]) -> None:
        for name, item in mapped.items():
            for callback in list(self._data_callbacks):
                try:
                    callback(self.device_id, name, item.get('value'),
                             item.get('unit', ''))
                except Exception as exc:      # noqa: BLE001
                    logger.error("插件适配器回调异常 %s/%s: %s",
                                 self.device_id, name, exc)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def get_latest_data(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {name: dict(item) for name, item in self.latest_data.items()}

    def add_data_callback(self, callback: Callable[..., Any]):
        self._data_callbacks.append(callback)

    def get_stats(self) -> dict[str, Any]:
        stats = dict(self.stats)
        stats['connected'] = self.connected
        stats['poll_interval'] = self.poll_interval
        stats['plugin'] = self.spec.plugin if self.spec else None
        return stats

    def write_point(self, name: str, value: Any) -> bool:
        """调用插件的写接口；未实现或明确失败都返回 False。"""
        if self._plugin is None or not self.connected:
            return False
        writer = getattr(self._plugin, 'write_point', None) \
            or getattr(self._plugin, 'write', None)
        if not callable(writer):
            logger.warning("插件 %s 未实现 write_point，拒绝写入 %s",
                           self.device_id, name)
            return False
        try:
            with self._plugin_lock:
                result = writer(name, value)
        except Exception as exc:      # noqa: BLE001
            self.stats['last_error'] = str(exc)
            logger.error("插件写入异常 %s/%s=%r: %s",
                         self.device_id, name, value, exc)
            return False

        # 项目口径：False / {'success': False} 才算失败，None 视为无结论（成功）
        if is_failure(result):
            self.stats['last_error'] = f'插件写入返回失败: {result!r}'
            logger.error("插件写入失败 %s/%s=%r: %r",
                         self.device_id, name, value, result)
            return False
        self.stats['writes'] += 1
        return True


def _map_flat_payload(payload: Any, spec: AdapterSpec) -> dict[str, dict[str, Any]]:
    """没有 `points` 时：插件直接返回 `{点位: 值}` 或 `{点位: {value, unit}}`。"""
    if not isinstance(payload, Mapping):
        raise AdapterDataError(
            f'插件 read() 必须返回对象（{{点位: 值}}），实际 {type(payload).__name__}')

    timestamp = datetime.now().isoformat()
    mapped: dict[str, dict[str, Any]] = {}
    for name, raw in payload.items():
        if not isinstance(name, str) or not name:
            continue
        unit = ''
        if isinstance(raw, Mapping) and 'value' in raw:
            unit = str(raw.get('unit') or '')
            raw = raw.get('value')

        if isinstance(raw, bool):
            value: Any = 1.0 if raw else 0.0
        else:
            numeric = try_finite_float(raw)
            if numeric is not None:
                value = numeric
            elif isinstance(raw, str):
                value = raw
            else:
                raise AdapterDataError(
                    f'插件点位 {name} 的值 {raw!r} 既不是有限数值也不是字符串')

        mapped[name] = {
            'value': value,
            'unit': unit,
            'quality': 192,
            'timestamp': timestamp,
        }

    if not mapped:
        raise AdapterDataError('插件 read() 没有返回任何有效点位')
    return mapped
