# -*- coding: utf-8 -*-
"""round 364：TDengine REST 请求被系统代理劫持。

`requests.post()` 默认读环境变量 `HTTP_PROXY` / `HTTPS_PROXY`。
开发机和不少现场服务器都配了代理，于是连 `localhost:6041` 也去走代理，
报 `ProxyError: Unable to connect to proxy` —— 日志是 **ERROR** 级别，
看起来像「TDengine 挂了」，实际是本地请求被代理拦了。

时序库永远部署在本机或内网，必须直连。
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def test_tdengine_rest_request_disables_proxy():
    """REST 请求必须显式 `proxies={'http': None, 'https': None}`。

    断言用精确形态：`proxies={'http': None, 'https': None}`，
    不做「有没有 proxies 字样」的宽松匹配 —— 那样注释里写一句也能过。
    """
    src = (_REPO / 'timeseries' / 'tdengine_client.py').read_text(encoding='utf-8')

    assert "proxies={'http': None, 'https': None}" in src, (
        'TDengine 的 REST 请求没有关闭代理 —— 配了系统代理的机器上会报 '
        'ProxyError，并被记成 ERROR 级（看起来像时序库挂了）')


def test_tdengine_connection_failure_is_warning_not_fatal():
    """连不上时必须降级为 WARNING 并继续启动（TDengine 是可选组件）。"""
    src = (_REPO / 'run.py').read_text(encoding='utf-8')
    idx = src.find('TDengine连接失败')
    assert idx > 0, 'run.py 里找不到「TDengine连接失败」的降级分支'
    # 那一行必须是 logger.warning，不能是 error
    line_start = src.rfind('\n', 0, idx) + 1
    line = src[line_start:src.find('\n', idx)]
    assert 'warning' in line.lower(), f'降级分支不是 WARNING: {line.strip()}'
