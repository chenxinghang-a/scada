"""界面语言词条接口。

## 为什么单独一个蓝图

语言包是**纯静态内容**，不需要认证、不碰数据库、不依赖任何业务对象。
放进 `api_system`（那一坨要 database/collector）会让前端为了拿 42 条词条
先过一遍认证和依赖检查，白白多几个失败点。

## 端点

* `GET /api/i18n/<lang>` —— 指定语言
* `GET /api/i18n`        —— 按 `Accept-Language` / 配置默认值自动选
* `PUT /api/i18n`        —— 写回部署默认语言（`配置/system.yaml` 的 `ui.language`）

GET 返回 `{language, supported, messages}`，**不需要登录**：
界面框架要在登录页就显示正确的语言，登录前拿不到 token。
词条里没有任何敏感信息（就是「保存」「取消」这些）。
PUT 会改服务端配置，需要登录。
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from core import i18n
from 用户层.auth import jwt_required
from ._common import load_yaml_config, save_yaml_config

logger = logging.getLogger(__name__)

i18n_bp = Blueprint('api_i18n', __name__, url_prefix='/api/i18n')

_SYSTEM_CONFIG = '配置/system.yaml'


def _configured_language() -> str | None:
    """读部署默认语言（`配置/system.yaml` 的 `ui.language`）。

    BUG-100：`core/i18n.py` 的文档把 `ui.language` 列为优先级 3，
    但这些代码路径此前**从未读过**它 —— 前端也没有写回入口，
    于是「换浏览器打开还是同一种语言」这个承诺完全落空。
    """
    try:
        cfg = load_yaml_config(_SYSTEM_CONFIG) or {}
    except Exception as e:      # noqa: BLE001 —— 配置坏掉不该让取词条 500
        logger.warning("读取 %s 失败，忽略 ui.language: %s", _SYSTEM_CONFIG, e)
        return None
    ui = cfg.get('ui')
    if isinstance(ui, dict) and ui.get('language'):
        return i18n.normalize(ui['language'])
    return None


def _pick_language() -> str:
    """没指定语言时的选择顺序：查询参数 → 配置默认 → Accept-Language。"""
    explicit = request.args.get('lang')
    if explicit:
        return i18n.normalize(explicit)
    configured = _configured_language()
    if configured:
        return configured
    header = request.headers.get('Accept-Language')
    if header:
        return i18n.normalize(header)
    return i18n.DEFAULT_LANG


# 用 `'/'` 而不是 `''`：项目里的路由静态扫描器（tests/test_api_authorization.py）
# 会把空路由后面的 `methods=[...]` 一起吞进去，导致白名单永远对不上。
# `route('/')` + `url_prefix='/api/i18n'` → 完整路径 `/api/i18n/`，
# 访问 `/api/i18n` 时 Flask 自动 308 到带斜杠的版本，前端 fetch 会跟随。
@i18n_bp.route('/', methods=['GET'])
def get_current_bundle():
    """按请求头自动选语言（前端启动时先调这个）。"""
    return jsonify(i18n.bundle(_pick_language()))


@i18n_bp.route('/<lang>', methods=['GET'])
def get_bundle(lang: str):
    """指定语言取词条。认不出就回退默认，不报错 —— 前端不该因为语言写错而白屏。"""
    return jsonify(i18n.bundle(lang))


@i18n_bp.route('/', methods=['PUT'])
@jwt_required
def set_language():
    """写回部署默认语言（BUG-100）。

    只落 `ui.language`，不动其它配置段；写盘失败必须报错 ——
    回 `success: True` 而后端没保存，就是本项目最常见的那种假成功。
    """
    data = request.get_json(silent=True) or {}
    raw = data.get('language')
    if not raw:
        return jsonify({'success': False, 'message': '缺少 language 参数'}), 400
    lang = i18n.normalize(raw)
    if lang not in i18n.SUPPORTED:
        return jsonify({'success': False,
                        'message': f'不支持的语言: {raw}'}), 400

    cfg = load_yaml_config(_SYSTEM_CONFIG) or {}
    ui = cfg.get('ui')
    if not isinstance(ui, dict):
        ui = {}
    ui['language'] = lang
    cfg['ui'] = ui

    if not save_yaml_config(_SYSTEM_CONFIG, cfg):
        logger.error("界面默认语言保存失败: %s", lang)
        return jsonify({'success': False, 'message': '语言配置保存失败'}), 500

    logger.info("界面默认语言已更新: %s", lang)
    return jsonify({'success': True, 'language': lang})
