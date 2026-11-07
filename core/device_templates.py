# -*- coding: utf-8 -*-
"""工厂可配置的设备模板库（round 381）。

内置模板保留在 `展示层/api/api_devices.py`，工厂自己的模板放
`配置/device_template_library.yaml`：加一个设备型号不用改代码、不用重启。

⚠️ 默认文件名**不是** `配置/device_templates.yaml`：
那个文件是 `tools/gen_factory.py` 的输入（`templates:` 是设备族映射），
和本模块要求的 `templates:` 列表形态不兼容，不能混用。

文件格式::

    templates:
      - id: three_phase_meter      # 必填，^[A-Za-z0-9_-]+$
        name: 三相电表             # 必填
        protocol: modbus_tcp       # modbus_tcp/.../custom/mc/fins
        registers: [...]

错误策略：
* 文件不存在 → `[]`（没配自定义模板是正常状态）；
* YAML 语法错 / 顶层不是映射 / `templates` 不是列表 → `TemplateConfigError`；
* 单条模板缺 id/name、id 非法、protocol 不认识 → 跳过并 warning，不影响其它条。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import paths
import yaml

__all__ = [
    'ALLOWED_PROTOCOLS',
    'DEFAULT_TEMPLATES_PATH',
    'TemplateConfigError',
    'load_custom_templates',
    'merge_templates',
]

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATES_PATH = '配置/device_template_library.yaml'
ALLOWED_PROTOCOLS = frozenset({
    'modbus_tcp', 'modbus_rtu', 'opcua', 'mqtt', 'rest', 'custom', 'mc', 'fins',
})
_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]+$')


class TemplateConfigError(ValueError):
    """整份模板库不可用（单个坏条目只跳过，不抛）。"""


def load_custom_templates(path: str = DEFAULT_TEMPLATES_PATH) -> list[dict[str, Any]]:
    """读取并校验工厂自定义模板；单条非法只跳过并记录 warning。"""
    resolved = paths.resolve_config_file(path)
    if not resolved.exists():
        logger.debug('设备模板库不存在，跳过: %s', resolved)
        return []

    try:
        text = resolved.read_text(encoding='utf-8')
    except OSError as exc:
        raise TemplateConfigError(f'{path}: 读取失败 — {exc}') from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TemplateConfigError(f'{path}: YAML 语法错误 — {exc}') from exc

    if data is None:
        logger.warning('设备模板库是空文件，按无自定义模板处理: %s', path)
        return []
    if not isinstance(data, dict):
        raise TemplateConfigError(
            f'{path}: 顶层必须是映射（形如 `templates: [...]`），'
            f'实际是 {type(data).__name__}')

    raw = data.get('templates')
    if raw is None:
        raise TemplateConfigError(f'{path}: 缺少顶层 `templates` 列表')
    if not isinstance(raw, list):
        hint = ''
        if isinstance(raw, dict):
            hint = ('；当前是映射形态 —— 那是 tools/gen_factory.py 的设备族模板，'
                    f'模板库请用 {DEFAULT_TEMPLATES_PATH}')
        raise TemplateConfigError(
            f'{path}: `templates` 必须是列表，实际是 {type(raw).__name__}{hint}')

    templates: list[dict[str, Any]] = []
    for idx, item in enumerate(raw, 1):
        parsed = _parse_template(item, idx, path)
        if parsed is not None:
            templates.append(parsed)

    logger.debug('设备模板库加载完成: %s（%d/%d 条可用）',
                 path, len(templates), len(raw))
    return templates


def _parse_template(item: Any, idx: int, path: str) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        logger.warning('模板库跳过第 %d 条（%s）: 不是对象', idx, path)
        return None

    tpl = dict(item)
    raw_id = tpl.get('id')
    label = f'第 {idx} 条' + (
        f' `{raw_id}`' if isinstance(raw_id, str) and raw_id.strip() else '')

    if not isinstance(raw_id, str) or not raw_id.strip():
        logger.warning('模板库跳过%s（%s）: 缺少 id', label, path)
        return None
    tid = raw_id.strip()
    if not _ID_PATTERN.match(tid):
        logger.warning('模板库跳过%s（%s）: id 只能由字母/数字/下划线/短横线组成',
                       label, path)
        return None

    name = tpl.get('name')
    if not isinstance(name, str) or not name.strip():
        logger.warning('模板库跳过%s（%s）: 缺少 name', label, path)
        return None

    protocol = tpl.get('protocol')
    if not isinstance(protocol, str) or protocol.strip().lower() not in ALLOWED_PROTOCOLS:
        logger.warning('模板库跳过%s（%s）: protocol=%r 不在支持列表 %s',
                       label, path, protocol, sorted(ALLOWED_PROTOCOLS))
        return None

    tpl['id'] = tid
    tpl['name'] = name.strip()
    tpl['protocol'] = protocol.strip().lower()
    return tpl


def merge_templates(builtin: list[dict[str, Any]],
                    custom: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 id 合并；自定义覆盖内置，内置顺序不变，自定义独有项追加。"""
    custom_by_id: dict[str, dict[str, Any]] = {}
    for tpl in custom or []:
        if not isinstance(tpl, dict) or not isinstance(tpl.get('id'), str):
            logger.warning('merge_templates 忽略无 id 的自定义模板')
            continue
        tid = tpl['id']
        if tid in custom_by_id:
            logger.warning('自定义模板 id 重复: %s（后者覆盖前者）', tid)
        custom_by_id[tid] = tpl

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tpl in builtin or []:
        if not isinstance(tpl, dict) or not tpl.get('id'):
            continue
        tid = tpl['id']
        if tid in seen:
            logger.warning('内置模板 id 重复，忽略后一条: %s', tid)
            continue
        seen.add(tid)
        merged.append(custom_by_id.get(tid, tpl))

    # 必须遍历去重后的映射（值=最后一条），否则同 id 两条自定义模板会把
    # 第一条追加进去，与“后者覆盖前者”的语义矛盾。
    for tid, tpl in custom_by_id.items():
        if tid in seen:
            continue
        seen.add(tid)
        merged.append(tpl)
    return merged
