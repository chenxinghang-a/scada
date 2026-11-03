"""界面语言词条接口。

## 为什么单独一个蓝图

语言包是**纯静态内容**，不需要认证、不碰数据库、不依赖任何业务对象。
放进 `api_system`（那一坨要 database/collector）会让前端为了拿 42 条词条
先过一遍认证和依赖检查，白白多几个失败点。

## 端点

* `GET /api/i18n/<lang>` —— 指定语言
* `GET /api/i18n`        —— 按 `Accept-Language` / 配置默认值自动选

两者都返回 `{language, supported, messages}`，**不需要登录**：
界面框架要在登录页就显示正确的语言，登录前拿不到 token。
词条里没有任何敏感信息（就是「保存」「取消」这些）。
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from core import i18n

logger = logging.getLogger(__name__)

i18n_bp = Blueprint('api_i18n', __name__, url_prefix='/api/i18n')


def _pick_language() -> str:
    """没指定语言时的选择顺序：查询参数 → Accept-Language → 配置默认。"""
    explicit = request.args.get('lang')
    if explicit:
        return i18n.normalize(explicit)
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
