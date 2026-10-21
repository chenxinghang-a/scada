# -*- coding: utf-8 -*-
"""非有限值（NaN / ±Infinity）不得进入业务逻辑 —— 行为 + 静态双重闸门（round 224）。

背景
----
同一类缺陷在仓库里出现过**二十多处**，写法一字不差：

    try:
        v = int(raw)
    except (TypeError, ValueError):
        <回退默认 / 报 400>

`int(float('inf'))` 抛的是 **OverflowError**，不在那个元组里 → 漏成 HTTP 500
或启动崩溃。`float()` 更糟：`float('nan')` **根本不抛异常**，而 NaN 与任何数
比较都是 False，所以「范围校验」`v < min or v > max` 对 NaN **完全失效**。

`inf` / `nan` 能从正常输入路径进来：

* JSON：`1e400` 被解析成 `inf`（float 上溢不报错）；裸 `NaN` 字面量也照收；
* YAML：`.inf` / `.nan` 是**规范内**字面量；
* MQTT / 设备原始字节：float32 位型异常可直接得到 NaN。

本文件两条腿：
* 行为用例：验证 `core/safe_numeric.py` 与各入口真的拒掉；
* 静态守卫：扫源码，禁止再出现「裸 int()/float() + 只捕 TypeError/ValueError」。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent

#: 不参与静态扫描的目录（产物 / 依赖 / 历史归档 / 测试自身）
SKIP_DIRS = {
    '.venv', 'dist', 'build', 'legacy', 'node_modules', '__pycache__', '.git', 'tests',
    '.pytest_cache', '.mypy_cache', '.ruff_cache',
}

#: 前缀匹配的临时目录。`tests/test_frozen_layout_paths.py` 会把 config.py
#: 拷进 `.pytest_tmp-<pid>/frozen-root0/_internal/` 做冻结布局模拟 ——
#: 那是**副本**，不该被本闸门当成源码（全量跑时曾因此误报）。
SKIP_PREFIXES = ('.pytest_tmp',)

#: 唯一允许实现「数值解析」的地方 —— 规则收口在这里。
NUMERIC_MODULE = 'core/safe_numeric.py'

#: 已知且**有意**保留的例外：{(相对路径, 该行源码文本): 原因}
ALLOWED_BARE_CONVERSIONS: dict[tuple[str, str], str] = {
    (
        'config.py',
        'result = int(value)',
    ): '`os.environ.get()` 只可能返回 str，`int(str)` 抛不出 OverflowError'
       '（int("inf") 是 ValueError，已被接住）。且 config.py 必须保持零内部依赖 ——'
       '它在冻结产物里单独放置，import core.* 会触发 core/__init__ 形成循环依赖，'
       'tests/test_frozen_layout_paths.py 盯着这条不变量（本轮试过，9 条用例当场报错）。',
    (
        '.github/scripts/smoke_packaged_backend.py',
        'port = int(part)',
    ): 'CI 冒烟脚本解析自己拼的 "5000,5001" 字符串；int("inf") 抛 ValueError 已被接住，'
       'inf 不可能出现在这条路径上。',
    (
        '.github/scripts/smoke_packaged_backend.py',
        'return int(resp.status), raw.decode("utf-8", "replace")',
    ): 'http.client 的 status 本身就是 int，不存在浮点来源。',
}


# ---------------------------------------------------------------------------
# 1. safe_numeric 本体
# ---------------------------------------------------------------------------

def test_to_int_rejects_inf_with_value_error():
    from core.safe_numeric import to_int

    with pytest.raises(ValueError) as ei:
        to_int(float('inf'))
    # 关键：不是 OverflowError —— 否则调用方的 `except ValueError` 接不住
    assert 'finite' in str(ei.value)


def test_to_int_rejects_nan():
    from core.safe_numeric import to_int

    with pytest.raises(ValueError):
        to_int(float('nan'))


@pytest.mark.parametrize('ok,expect', [(12, 12), ('12', 12), (12.7, 12), ('0', 0), (True, 1)])
def test_to_int_keeps_original_semantics(ok, expect):
    """不能为了挡 inf 把正常语义改掉（截断、字符串数字、bool）。"""
    from core.safe_numeric import to_int

    assert to_int(ok) == expect


def test_to_finite_float_rejects_all_non_finite():
    from core.safe_numeric import to_finite_float

    for bad in (float('nan'), float('inf'), float('-inf'), 'nan', 'inf', '-inf', 'NaN'):
        with pytest.raises(ValueError):
            to_finite_float(bad)


def test_try_int_returns_none_instead_of_raising():
    from core.safe_numeric import try_int

    assert try_int(float('inf')) is None
    assert try_int(float('nan')) is None
    assert try_int('abc') is None
    assert try_int('7') == 7


# ---------------------------------------------------------------------------
# 2. API 入口
# ---------------------------------------------------------------------------

def test_safe_int_raises_value_error_not_overflow():
    """`safe_int(inf)` 必须抛 ValueError —— api_error_handler 只接这个。"""
    from 展示层.api._common import safe_int

    with pytest.raises(ValueError):
        safe_int(float('inf'), 'address')


def test_safe_int_accepts_normal():
    from 展示层.api._common import safe_int

    assert safe_int('100') == 100
    assert safe_int(7.9) == 7


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf'), 'nan', 'inf', '-inf', 'NaN'])
def test_safe_float_rejects_non_finite(bad):
    from 展示层.api._common import safe_float

    with pytest.raises(ValueError) as ei:
        safe_float(bad, 'value')
    assert 'finite' in str(ei.value) or '有限' in str(ei.value)


@pytest.mark.parametrize('ok,expect', [('5.5', 5.5), (-3, -3.0), (0, 0.0), ('-0.25', -0.25)])
def test_safe_float_accepts_finite(ok, expect):
    from 展示层.api._common import safe_float

    assert safe_float(ok, 'value') == expect


def test_api_write_register_rejects_nan(client, auth_headers):
    """端到端：Flask 的 get_json() 接受裸 `NaN` 字面量，必须在参数解析处拦下。

    这条走真实路由，覆盖「safe_float → 400」的接线，而不只是单测函数。
    """
    resp = client.post(
        '/api/devices/dev1/write-register',
        data='{"address": 100, "value": NaN}',
        content_type='application/json',
        headers=auth_headers,
    )
    assert resp.status_code == 400, f'NaN 写入返回 {resp.status_code}'
    body = resp.get_json() or {}
    text = str(body.get('error', '')) + str(body.get('message', ''))
    assert 'finite' in text or '有限' in text or 'NaN' in text, text


@pytest.mark.parametrize('bad', [float('inf'), float('nan'), 1e400])
def test_validate_port_rejects_non_finite(bad):
    from 展示层.api.api_devices import _validate_port

    with pytest.raises(ValueError) as ei:
        _validate_port(bad)
    assert '端口' in str(ei.value)


def test_validate_port_still_checks_range():
    from 展示层.api.api_devices import _validate_port

    assert _validate_port(502) == 502
    with pytest.raises(ValueError):
        _validate_port(70000)
    with pytest.raises(ValueError):
        _validate_port(0)


def test_json_body_1e400_is_inf_and_rejected():
    """文档化攻击面：`1e400` 在 Python JSON 里就是 `inf`，不是解析错误。"""
    from core.safe_numeric import to_finite_float

    assert json.loads('{"value": 1e400}')['value'] == float('inf')
    assert json.loads('{"value": NaN}')['value'] != json.loads('{"value": NaN}')['value']  # nan != nan
    with pytest.raises(ValueError):
        to_finite_float(json.loads('{"value": 1e400}')['value'])


# ---------------------------------------------------------------------------
# 3. 配置里的 YAML `.inf` / `.nan` 不能让启动崩掉
# ---------------------------------------------------------------------------

def test_collection_config_tolerates_yaml_inf(monkeypatch):
    """YAML 里 `interval: .inf` 是合法写法，必须回退默认值而不是抛异常。"""
    from core import config_manager

    monkeypatch.setattr(
        config_manager, 'load_system_config',
        lambda **kw: {'collection': {'max_concurrent': float('inf'),
                                     'default_interval': float('nan')}},
    )
    got = config_manager.load_collection_settings()

    assert got['max_concurrent'] == config_manager.COLLECTION_DEFAULTS['max_concurrent']
    assert got['default_interval'] == config_manager.COLLECTION_DEFAULTS['default_interval']


def test_retention_config_tolerates_yaml_inf(tmp_path):
    from core import retention_scheduler

    path = tmp_path / 'system.yaml'
    path.write_text(
        # 手写 YAML：`.inf` / `.nan` 是规范内字面量，safe_dump 也能输出
        'database:\n'
        '  retention:\n'
        '    enabled: true\n'
        '    raw_data_days: .inf\n'
        '    alarm_days: .nan\n'
        '    compressed_data_days: 30\n',
        encoding='utf-8',
    )
    got = retention_scheduler.read_retention_config(str(path))

    assert got is not None, '配置非法不应导致整段失效（应逐项回退默认）'
    assert got['history_days'] == retention_scheduler.WINDOW_DEFAULTS['history_days']
    assert got['alarm_days'] == retention_scheduler.WINDOW_DEFAULTS['alarm_days']
    assert got['archive_days'] == 30


# ---------------------------------------------------------------------------
# 4. 安全相关：请求签名的过期窗口不能被 NaN 绕过
# ---------------------------------------------------------------------------

def _signature_app(secret: str = 'test-secret'):
    from flask import Flask, jsonify

    from core.request_signature import signature_required

    app = Flask(__name__)
    app.config['API_SIGNATURE_SECRET'] = secret

    @app.route('/probe', methods=['GET'])
    @signature_required
    def probe():
        return jsonify({'ok': True})

    return app


def _signed_headers(secret: str, timestamp: str, nonce: str) -> dict:
    from core.request_signature import (
        NONCE_HEADER, SIGNATURE_HEADER, TIMESTAMP_HEADER, generate_signature,
    )

    sig = generate_signature(secret, 'GET', '/probe', '', timestamp, nonce)
    return {TIMESTAMP_HEADER: timestamp, NONCE_HEADER: nonce, SIGNATURE_HEADER: sig}


def test_signature_rejects_nan_timestamp_even_with_valid_hmac():
    """核心安全用例。

    旧实现 `ts = float(timestamp)`：`X-Timestamp: NaN` 转得动、不抛异常，
    `age = abs(now - nan)` = nan，而 `nan > MAX_TIMESTAMP_AGE` 恒为 **False**
    → **过期检查被绕过**。持有密钥的一方（合法客户端 / 密钥泄露）可以把任意
    旧请求配上新 nonce 重放，5 分钟窗口形同虚设。
    """
    secret = 'test-secret'
    app = _signature_app(secret)
    headers = _signed_headers(secret, 'NaN', 'nonce-nan-1')

    resp = app.test_client().get('/probe', headers=headers)

    assert resp.status_code == 401, (
        f'NaN 时间戳被放行了（{resp.status_code}）—— 过期检查对 NaN 失效'
    )
    assert resp.get_json().get('error_code') == 'INVALID_TIMESTAMP'


@pytest.mark.parametrize('ts', ['inf', '-inf', 'Infinity'])
def test_signature_rejects_infinite_timestamp(ts):
    secret = 'test-secret'
    app = _signature_app(secret)
    headers = _signed_headers(secret, ts, f'nonce-{ts}')

    resp = app.test_client().get('/probe', headers=headers)
    assert resp.status_code == 401


def test_signature_still_accepts_fresh_timestamp():
    """防止「修 NaN」修成「全都拒」。"""
    import time

    secret = 'test-secret'
    app = _signature_app(secret)
    headers = _signed_headers(secret, str(int(time.time())), 'nonce-ok-1')

    resp = app.test_client().get('/probe', headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:200]


def test_signature_rejects_stale_timestamp():
    """原有过期语义不能被改坏。"""
    import time

    secret = 'test-secret'
    app = _signature_app(secret)
    stale = str(int(time.time()) - 3600)
    headers = _signed_headers(secret, stale, 'nonce-old-1')

    resp = app.test_client().get('/probe', headers=headers)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 5. 安全相关：报警延时 / 写入量程不能是 inf
# ---------------------------------------------------------------------------

def test_alarm_delay_inf_falls_back_to_zero():
    """`delay: .inf` 会让 `max(0, inf)` = inf → 报警永不触发（静默失效）。"""
    from 报警层.alarm_manager import AlarmManager

    got = AlarmManager._normalize_delay(float('inf'), 'rule-x')
    assert got == 0.0, f'delay=inf 被当成合法延时 → 该条报警永不触发（得到 {got}）'

    got_nan = AlarmManager._normalize_delay(float('nan'), 'rule-y')
    assert got_nan == 0.0


def test_alarm_delay_still_accepts_numbers():
    from 报警层.alarm_manager import AlarmManager

    assert AlarmManager._normalize_delay(30, 'r') == 30.0
    assert AlarmManager._normalize_delay('12.5', 'r') == 12.5
    assert AlarmManager._normalize_delay(None, 'r') == 0.0


def test_write_limits_with_infinite_bounds_are_rejected():
    """`write_limits: [-.inf, .inf]`（YAML 合法）等于「不设限」，
    但界面/配置上看是配了量程的 —— 必须当非法配置拒掉，不能静默变宽松。"""
    from 智能层.device_control import DeviceControlSafety

    dm = MagicMock()
    dm.devices = {
        'probe_dev': {'write_limits': {'temperature': [float('-inf'), float('inf')]}},
    }
    dc = DeviceControlSafety(MagicMock(), device_manager=dm)

    limits = dc._write_limits.get('probe_dev', {})
    assert 'temperature' not in limits, (
        f'inf 量程被登记为合法范围 → 任何值都能写进现场设备（{limits.get("temperature")}）'
    )


def test_write_limits_with_normal_bounds_still_registered():
    from 智能层.device_control import DeviceControlSafety

    dm = MagicMock()
    dm.devices = {'probe_dev': {'write_limits': {'temperature': [0, 100]}}}
    dc = DeviceControlSafety(MagicMock(), device_manager=dm)

    assert dc._write_limits['probe_dev']['temperature'] == (0.0, 100.0)


# ---------------------------------------------------------------------------
# 6. 静态守卫：禁止再写「裸 int()/float() + 只捕 TypeError/ValueError」
# ---------------------------------------------------------------------------

def _iter_py_files() -> list[Path]:
    out = []
    for p in REPO.rglob('*.py'):
        parts = p.relative_to(REPO).parts
        if any(part in SKIP_DIRS for part in parts):
            continue
        if any(part.startswith(SKIP_PREFIXES) for part in parts):
            continue
        out.append(p)
    return out


def _conversion_calls(node: ast.AST) -> list[ast.Call]:
    """try 体内出现的 int(...) / float(...) 调用。"""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            if n.func.id in ('int', 'float'):
                out.append(n)
    return out


def _handler_catches_without_overflow(handler: ast.ExceptHandler) -> bool:
    """该 handler 是否捕了 TypeError/ValueError 却**没**捕 OverflowError。"""
    names: set[str] = set()

    def collect(t: ast.AST | None) -> None:
        if t is None:
            return
        if isinstance(t, ast.Tuple):
            for e in t.elts:
                collect(e)
        elif isinstance(t, ast.Name):
            names.add(t.id)
        elif isinstance(t, ast.Attribute):
            names.add(t.attr)

    collect(handler.type)
    if not names:
        return False          # 裸 `except:` 交给静默异常守卫去管
    caught_num = bool(names & {'TypeError', 'ValueError'})
    return caught_num and 'OverflowError' not in names


def test_no_bare_int_float_guarded_only_by_value_error():
    """扫出所有「会漏 OverflowError / 放行 inf-nan」的数值转换。"""
    hits: list[str] = []
    for path in _iter_py_files():
        rel = path.relative_to(REPO).as_posix()
        if rel == NUMERIC_MODULE:
            continue          # 唯一真源，规则就在那儿
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = text.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            calls = _conversion_calls(ast.Module(body=node.body, type_ignores=[]))
            if not calls:
                continue
            if not any(_handler_catches_without_overflow(h) for h in node.handlers):
                continue
            for call in calls:
                src = lines[call.lineno - 1].strip()
                if (rel, src) in ALLOWED_BARE_CONVERSIONS:
                    continue
                hits.append(f'{rel}:{call.lineno}  {src}')

    assert not hits, (
        '以下位置用裸 int()/float() 且只捕 (TypeError, ValueError) —— '
        '`int(float("inf"))` 抛的是 OverflowError（漏成 500），'
        '`float("nan")` 则根本不抛、让范围校验对 NaN 失效。\n'
        '改用 `core.safe_numeric` 的 `to_int` / `to_finite_float`（失败抛 ValueError）'
        '或 `try_int` / `try_finite_float`（失败返回 None）。\n'
        '若确实需要保留，在 ALLOWED_BARE_CONVERSIONS 里登记并写明原因。\n\n'
        + '\n'.join('  ' + h for h in hits)
    )


def test_allowlist_entries_still_exist():
    """豁免项若已不存在（代码被改/删），必须从清单里删掉 —— 只增不减会腐烂。"""
    stale: list[str] = []
    for (rel, src), _reason in ALLOWED_BARE_CONVERSIONS.items():
        path = REPO / rel
        if not path.exists() or src not in path.read_text(encoding='utf-8', errors='replace'):
            stale.append(f'{rel} :: {src}')

    assert not stale, (
        'ALLOWED_BARE_CONVERSIONS 里有条目已失效（对应代码不存在了），请删除：\n  '
        + '\n  '.join(stale)
    )
