"""
模拟参数档加载器
==================
把「某台设备各点位怎么造数据」从代码搬到 ``配置/simulation_profiles.yaml``。

设计要点：
1. **只补不改**：profile 只填设备配置里**没写**的键，内联 ``sim`` 段永远优先。
   这样单台设备要特调时直接写进设备配置即可，不必改共享的 profile。
2. **失败不致命**：profile 文件缺失/语法错/设备没配，都只告警并返回原配置。
   模拟参数是「调试期假设」，不该让采集进程起不来。
3. **按 mtime 缓存**：全量测试会构造上百个 simulator，每次读盘会明显变慢。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)

_PROFILE_PATH = Path(__file__).resolve().parent.parent / '配置' / 'simulation_profiles.yaml'

_lock = threading.Lock()
_cache: Optional[Dict[str, Any]] = None
_cache_mtime: float = -1.0


def _load_profiles() -> Dict[str, Any]:
    """读取并缓存 profiles（按文件 mtime 失效）。"""
    global _cache, _cache_mtime

    try:
        mtime = _PROFILE_PATH.stat().st_mtime
    except OSError:
        # 没有该文件是正常情况（真实部署不需要模拟参数档）
        return {}

    with _lock:
        if _cache is not None and mtime == _cache_mtime:
            return _cache

        try:
            raw = yaml.safe_load(_PROFILE_PATH.read_text(encoding='utf-8')) or {}
        except (yaml.YAMLError, OSError) as exc:
            logger.warning("[模拟档] 读取 %s 失败，本次不应用任何档: %s",
                           _PROFILE_PATH.name, exc)
            _cache, _cache_mtime = {}, mtime
            return _cache

        profiles = raw.get('profiles') or {}
        if not isinstance(profiles, dict):
            logger.warning("[模拟档] %s 的 profiles 段不是映射，已忽略",
                           _PROFILE_PATH.name)
            profiles = {}

        _cache, _cache_mtime = profiles, mtime
        logger.info("[模拟档] 已加载 %d 台设备的模拟参数档", len(profiles))
        return _cache


def _merge_missing(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    """把 src 里 dst 没有的键补进 dst（递归一层 dict）。"""
    for key, value in src.items():
        if key not in dst or dst[key] is None:
            dst[key] = value
        elif isinstance(dst[key], dict) and isinstance(value, dict):
            _merge_missing(dst[key], value)


def apply_profile(device_config: Dict[str, Any]) -> bool:
    """把 profile 补进设备配置（就地修改）。

    Args:
        device_config: 设备配置字典，需含 ``id``。

    Returns:
        True 表示应用了 profile（或至少尝试过且有内容），False 表示没有对应档。
    """
    if not isinstance(device_config, dict):
        return False

    device_id = device_config.get('id')
    if not device_id:
        return False

    profile = _load_profiles().get(device_id)
    if not isinstance(profile, dict):
        return False

    # ---- 设备级：simulation.process_model / simulation.monitoring ----
    sim_cfg = device_config.get('simulation')
    if not isinstance(sim_cfg, dict):
        sim_cfg = {}
        device_config['simulation'] = sim_cfg

    if isinstance(profile.get('process_model'), dict):
        pm = sim_cfg.get('process_model')
        if not isinstance(pm, dict):
            pm = {}
            sim_cfg['process_model'] = pm
        _merge_missing(pm, profile['process_model'])

    if 'monitoring' in profile and 'monitoring' not in sim_cfg:
        sim_cfg['monitoring'] = profile['monitoring']

    # ---- 点位级：把 points 合并到各点位的 sim 段 ----
    points = profile.get('points') or {}
    if not isinstance(points, dict) or not points:
        return True

    applied = 0
    for section in ('registers', 'nodes', 'topics', 'endpoints'):
        for point in (device_config.get(section) or []):
            if not isinstance(point, dict):
                continue
            name = point.get('name')
            if not name or name not in points:
                continue
            rule = points[name]
            if not isinstance(rule, dict):
                logger.warning("[模拟档] %s.%s 的规则不是映射，已跳过",
                               device_id, name)
                continue
            existing = point.get('sim')
            if not isinstance(existing, dict):
                existing = {}
                point['sim'] = existing
            _merge_missing(existing, rule)
            applied += 1

    if applied:
        logger.debug("[模拟档] %s: 应用 %d 个点位规则", device_id, applied)
    return True
