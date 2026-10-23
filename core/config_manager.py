"""
配置管理器
统一管理YAML配置和Python配置类
"""

import logging
import os
import re
import threading
import yaml
from pathlib import Path
from typing import Any, Dict, Optional

from core.safe_numeric import try_int
from core.config_io import dump_yaml

logger = logging.getLogger(__name__)

_MISSING = object()  # 区分"键不存在"和"键存在但值为None"

# 匹配 ${VAR} 或 ${VAR:-default} 形式的环境变量引用
_ENV_VAR_PATTERN = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}')


def expand_env_vars(value: Any, _source: str = '') -> Any:
    """
    递归展开配置值中的 ${VAR} / ${VAR:-default} 环境变量引用。

    - 环境变量已设置：替换为其值。
    - 未设置且写了 :-default：替换为 default。
    - 未设置且无默认值：保留原始占位符并打印警告（避免静默变成空串
      造成"看着能跑"的假配置，部署方必须能在日志中发现缺失）。
    """
    if isinstance(value, str):
        def _repl(match: 're.Match') -> str:
            var_name = match.group(1)
            default = match.group(2)
            env_value = os.environ.get(var_name)
            if env_value is not None:
                return env_value
            if default is not None:
                return default
            logger.warning(
                "配置项引用的环境变量 %s 未设置（%s），保留占位符 ${%s}。"
                "请在 .env 或部署环境中设置该变量。",
                var_name, _source or 'YAML配置', var_name,
            )
            return match.group(0)
        return _ENV_VAR_PATTERN.sub(_repl, value)
    if isinstance(value, dict):
        return {k: expand_env_vars(v, _source) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env_vars(item, _source) for item in value]
    return value


class ConfigManager:
    """
    配置管理器

    支持：
    1. YAML配置文件加载
    2. 环境变量覆盖
    3. 配置验证
    4. 配置热更新
    """

    _lock = threading.Lock()
    _configs: Dict[str, Dict[str, Any]] = {}
    _watchers: Dict[str, list] = {}
    
    @classmethod
    def load_yaml(cls, config_path: str, reload: bool = False) -> Dict[str, Any]:
        """
        加载YAML配置文件
        
        Args:
            config_path: 配置文件路径
            reload: 是否强制重新加载
            
        Returns:
            配置字典
        """
        config_path = str(config_path)

        # 检查缓存
        with cls._lock:
            if config_path in cls._configs and not reload:
                return cls._configs[config_path]

        try:
            path = Path(config_path)
            if not path.exists():
                logger.warning(f"配置文件不存在: {config_path}")
                return {}

            with open(path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}

            # 展开 ${VAR} / ${VAR:-default} 环境变量引用
            config = expand_env_vars(config, _source=config_path)

            # 缓存配置
            with cls._lock:
                cls._configs[config_path] = config
            logger.info(f"加载配置文件: {config_path}")

            # 通知观察者
            cls._notify_watchers(config_path, config)

            return config
        except Exception as e:
            logger.error(f"加载配置文件失败: {config_path}, 错误: {e}")
            return {}
    
    @classmethod
    def save_yaml(cls, config_path: str, config: Dict[str, Any]) -> bool:
        """
        保存配置到YAML文件

        round 256：两处修正 ——
        1. 原来 `open(..., 'w')` 先截断再 dump，序列化异常会留下 0 字节文件；
           改走 `core.config_io.dump_yaml`（原子写 + 只重写变化的键、保留注释）。
        2. 原来**不管写没写成功**都更新缓存并通知观察者 —— 磁盘失败而内存说
           "已保存"，进程内读到的都是新值、重启全丢。现在写成功才更新。

        Args:
            config_path: 配置文件路径
            config: 配置字典

        Returns:
            bool: 是否真的落盘成功。
        """
        try:
            path = Path(config_path)
            path.parent.mkdir(parents=True, exist_ok=True)

            if not dump_yaml(path, config):
                logger.error(f"保存配置文件失败（原文件保持不变）: {config_path}")
                return False

            # 更新缓存
            with cls._lock:
                cls._configs[config_path] = config
            logger.info(f"保存配置文件: {config_path}")

            # 通知观察者
            cls._notify_watchers(config_path, config)
            return True
        except Exception as e:
            logger.error(f"保存配置文件失败: {config_path}, 错误: {e}")
            return False
    
    @classmethod
    def get(cls, config_path: str, key: str, default: Any = None) -> Any:
        """
        获取配置值
        
        Args:
            config_path: 配置文件路径
            key: 配置键（支持点号分隔的路径，如 'database.host'）
            default: 默认值
            
        Returns:
            配置值
        """
        with cls._lock:
            config = cls._configs.get(config_path, {}).copy()

        # 支持点号分隔的路径
        keys = key.split('.')
        value = config

        for k in keys:
            if isinstance(value, dict):
                value = value.get(k, _MISSING)
            else:
                return default

        return default if value is _MISSING else value
    
    @classmethod
    def set(cls, config_path: str, key: str, value: Any):
        """
        设置配置值
        
        Args:
            config_path: 配置文件路径
            key: 配置键（支持点号分隔的路径）
            value: 配置值
        """
        with cls._lock:
            if config_path not in cls._configs:
                cls._configs[config_path] = {}

            config = cls._configs[config_path]
            keys = key.split('.')

            # 创建嵌套字典
            for k in keys[:-1]:
                if k not in config:
                    config[k] = {}
                config = config[k]

            config[keys[-1]] = value
    
    @classmethod
    def watch(cls, config_path: str, callback: callable):
        """
        监听配置变化
        
        Args:
            config_path: 配置文件路径
            callback: 回调函数，参数为 (config_path, config)
        """
        with cls._lock:
            if config_path not in cls._watchers:
                cls._watchers[config_path] = []

            cls._watchers[config_path].append(callback)
    
    @classmethod
    def _notify_watchers(cls, config_path: str, config: Dict[str, Any]):
        """通知配置观察者"""
        with cls._lock:
            watchers = list(cls._watchers.get(config_path, []))
        for callback in watchers:
            try:
                callback(config_path, config)
            except Exception as e:
                logger.error(f"配置观察者回调失败: {e}")
    
    @classmethod
    def get_all_configs(cls) -> Dict[str, Dict[str, Any]]:
        """
        获取所有已加载的配置

        Returns:
            配置路径到配置字典的映射
        """
        with cls._lock:
            return cls._configs.copy()

    @classmethod
    def clear(cls):
        """清除所有配置缓存"""
        with cls._lock:
            cls._configs.clear()
            cls._watchers.clear()
        logger.debug("清除所有配置缓存")


#: 系统级配置文件的相对路径（`配置/system.yaml`）。
SYSTEM_CONFIG_REL = '配置/system.yaml'

#: `collection` 段的兜底默认值。
#:
#: ⚠️ 这些默认值**必须与 `配置/system.yaml` 里的值一致** ——
#: 它们是「配置读不到时」的兜底，不是第二套真源。
#: `tests/test_system_config_knobs.py` 会断言这一点。
COLLECTION_DEFAULTS = {
    #: 采集线程池上限（并发采集连接数）。2026-10-10（round 210）之前这里
    #: 是 `ThreadPoolExecutor(max_workers=20)` 的**硬编码 20**，而
    #: `配置/system.yaml` 写的是 10 —— 两边对不上，而配置那侧**零读取点**。
    'max_concurrent': 20,
    #: 设备配置**没写** `collection_interval` 时用的采集间隔（秒）。
    #: 此前是 `device_config.get('collection_interval', 5)` 的硬编码 5。
    'default_interval': 5,
}


#: `performance.connection_pool` 段的兜底默认值（round 257 接线）。
#:
#: ⚠️ 这些值**必须与 `配置/system.yaml` 里的值一致** —— 它们是「配置读不到时」
#: 的兜底，不是第二套真源。`tests/test_system_config_knobs.py` 会断言这一点。
#:
#: 接线前的事实（round 257 实测）：`采集层/device_manager.py` 里是
#: `ConnectionPool(max_size=50, max_idle_time=300, max_lifetime=3600)` 的硬编码，
#: 而 `配置/system.yaml` 写的是 `size: 10` / `timeout: 30` —— **两边都对不上**，
#: 且 `timeout: 30` 到底指"空闲回收"还是"建立连接超时"根本没说清。
#: 运维按 10 调小连接数，实际仍是 50。
CONNECTION_POOL_DEFAULTS = {
    #: 连接池容量上限（同时保持的客户端连接数）。
    'size': 50,
    #: 空闲多久回收（秒）。
    'idle_timeout_seconds': 300,
    #: 连接最大存活时间（秒），到期强制重建，规避长连接泄漏。
    'max_lifetime_seconds': 3600,
}


def load_connection_pool_settings() -> Dict[str, int]:
    """读取 `配置/system.yaml` 的 `performance.connection_pool` 段。

    Returns:
        `{'size': int, 'idle_timeout_seconds': int, 'max_lifetime_seconds': int}`；
        配置缺失/非法时逐项回退 :data:`CONNECTION_POOL_DEFAULTS`。
    """
    cfg = load_system_config()
    perf = cfg.get('performance') if isinstance(cfg, dict) else None
    section = perf.get('connection_pool') if isinstance(perf, dict) else None
    out = dict(CONNECTION_POOL_DEFAULTS)
    if not isinstance(section, dict):
        return out
    for key, fallback in CONNECTION_POOL_DEFAULTS.items():
        raw = section.get(key)
        val = try_int(raw)
        if val is None or val <= 0:
            # YAML 的 `.inf` / `.nan` 是合法字面量；`int(inf)` 抛 OverflowError。
            logger.warning("performance.connection_pool.%s=%r 非法，回退默认 %d",
                           key, raw, fallback)
            continue
        out[key] = val
    return out


def load_system_config(reload: bool = True) -> Dict[str, Any]:
    """读取 `配置/system.yaml`。

    为什么单独一个助手：`配置/system.yaml` 此前**只被「读来展示/改写」**
    （API 的 GET/PUT 配置端点、启动期 schema 校验），**没有任何模块把它当
    运行时设置读**。于是它里面那一堆旋钮（`collection.*` / `alarm.*` …）
    全是装饰品 —— 改了不起作用，甚至与代码里的硬编码值**对不上**
    （实测：`collection.max_concurrent: 10` vs 代码 `max_workers=20`）。
    round 210 开始把真正需要的旋钮接到这里，口径见
    `tests/test_system_config_knobs.py`。

    Args:
        reload: 是否强制重新读盘（默认 True —— 改配置不用重启）。

    Returns:
        配置字典；读不到时返回 `{}`（调用方用自己的兜底默认值）。
    """
    try:
        import paths
        return ConfigManager.load_yaml(paths.resolve(SYSTEM_CONFIG_REL), reload=reload) or {}
    except Exception as e:  # noqa: BLE001 —— 配置读不到不能把调用方搞死
        logger.warning("读取 %s 失败（用兜底默认值）：%s", SYSTEM_CONFIG_REL, e)
        return {}


def load_collection_settings() -> Dict[str, int]:
    """读取 `配置/system.yaml` 的 `collection` 段（采集参数）。

    Returns:
        `{'max_concurrent': int, 'default_interval': int}`；
        配置缺失/非法时逐项回退 :data:`COLLECTION_DEFAULTS`。
    """
    cfg = load_system_config()
    section = cfg.get('collection') if isinstance(cfg, dict) else None
    out = dict(COLLECTION_DEFAULTS)
    if not isinstance(section, dict):
        return out
    for key, fallback in COLLECTION_DEFAULTS.items():
        raw = section.get(key)
        val = try_int(raw)
        if val is None:
            # YAML 的 `.inf` / `.nan` 是合法字面量，`int(inf)` 抛的是
            # OverflowError（不在 TypeError/ValueError 里）→ 原本会直接崩在启动阶段。
            logger.warning("collection.%s=%r 非法，回退默认 %d", key, raw, fallback)
            continue
        if val <= 0:
            logger.warning("collection.%s=%r 不是正数，回退默认 %d", key, raw, fallback)
            continue
        out[key] = val
    return out
