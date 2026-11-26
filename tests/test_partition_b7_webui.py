"""Partition B7 tests: built-in Web UI templates and static JavaScript fixes."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS_DIR = ROOT / '静态资源' / 'js'
TPL_DIR = ROOT / '模板'


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def _extract_js_function(src: str, fn_name: str) -> str:
    m = re.search(rf'function\s+{re.escape(fn_name)}\s*\([^)]*\)\s*\{{', src)
    assert m, f'{fn_name} not found'
    start = m.start()
    depth = 0
    for idx in range(m.end() - 1, len(src)):
        ch = src[idx]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return src[start:idx + 1]
    raise AssertionError(f'Unclosed brace for {fn_name}')


@pytest.mark.parametrize('rel_path', [
    '静态资源/js/main.js',
    '静态资源/js/dashboard.js',
    '静态资源/js/screen.js',
    '模板/control.html',
    '模板/devices.html',
    '模板/users.html',
    '模板/industry40.html',
    '模板/alarm_output.html',
])
def test_escape_html_preserves_falsy_zero_and_false_and_escapes_xss(rel_path: str):
    src = _read(ROOT / rel_path)
    fn_code = _extract_js_function(src, 'escapeHtml')
    assert 'if (!str)' not in fn_code and 'if (!s)' not in fn_code

    node = shutil.which('node')
    if node:
        script = (
            f"{fn_code}\n"
            "const assert = require('assert');\n"
            "assert.strictEqual(escapeHtml(0), '0');\n"
            "assert.strictEqual(escapeHtml(false), 'false');\n"
            "assert.strictEqual(escapeHtml(null), '');\n"
            "assert.strictEqual(escapeHtml(undefined), '');\n"
            "const xss = escapeHtml(\"<img src=x onerror='alert(1)'>\");\n"
            "assert(!xss.includes('<') && !xss.includes('>') && !xss.includes(\"'\"));\n"
        )
        proc = subprocess.run([node, '-e', script], capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr


def test_dashboard_and_screen_200ms_timers_are_bounded():
    for fname in ('dashboard.js', 'screen.js'):
        src = _read(JS_DIR / fname)
        assert re.search(r'attempts\s*>=\s*\d+', src), f'{fname} socket poll timer is unbounded'


def test_i18n_and_auth_logout_503_guards():
    i18n_js = _read(JS_DIR / 'i18n.js')
    assert "var initial = stored() || '';" in i18n_js

    main_js = _read(JS_DIR / 'main.js')
    assert 'response.status === 503' in main_js
    assert 'scada_refresh_token' in main_js
    assert 'token=; path=/; expires=Thu, 01 Jan 1970' in main_js

    base_html = _read(TPL_DIR / 'base.html')
    assert 'if (r.status === 401)' in base_html
    assert 'if (!r.ok) throw new Error' in base_html

    users_html = _read(TPL_DIR / 'users.html')
    assert 'if (r.status === 401)' in users_html
    assert '/auth/logout' in users_html
    assert 'token=; path=/; expires=Thu, 01 Jan 1970' in users_html
    assert not re.search(r'onclick="(toggleUser|deleteUser|editUser)\(', users_html)
    assert 'data-user-action' in users_html


def test_control_html_float_setpoints_dual_approval_and_delegation():
    src = _read(TPL_DIR / 'control.html')
    assert 'value: parseInt(value)' not in src
    assert 'const parsedVal = Number(value);' in src
    assert src.count('applied === false') >= 5
    assert 'data-pending-action' in src
    assert 'data-interlock-action' in src
    assert not re.search(r'onclick="(approvePending|rejectPending|bypassInterlock|restoreInterlock)\(', src)


def test_devices_html_category_rtu_serial_port_and_delegation():
    src = _read(TPL_DIR / 'devices.html')
    assert "getElementById('device_category').value = 'instrument'" in src
    assert "getElementById('device_category').value = device.device_category" in src
    assert "device_category: document.getElementById('device_category').value" in src
    assert 'device.serial_port || device.host' in src
    assert 'deviceConfig.serial_port = host;' in src
    assert 'data-device-action' in src
    assert not re.search(r'onclick="(testDevice|editDevice|stopDevice|startDevice|deleteDevice|showFaultInjectionModal)\(', src)


def test_config_html_readonly_alignment_and_xss_guards():
    src = _read(TPL_DIR / 'config.html')
    for ro_sec in ('collection', 'database', 'logging'):
        assert f"saveSection('{ro_sec}')" not in src
    assert "['collection', 'database', 'logging'].forEach" in src
    assert 'data-rule-action' in src
    assert not re.search(r'onclick="(editRule|deleteRule)\(', src)
    assert 'createTextNode' in src


def test_industry40_csv_sanitization_alarm_output_and_alarms_history():
    i40 = _read(TPL_DIR / 'industry40.html')
    fn_csv = _extract_js_function(i40, 'i40CsvCell')
    assert 'URL.revokeObjectURL' in i40

    node = shutil.which('node')
    if node:
        script = (
            f"{fn_csv}\n"
            "const assert = require('assert');\n"
            "assert.strictEqual(i40CsvCell('=1+1'), \"'=1+1\");\n"
            "assert.strictEqual(i40CsvCell('+SUM(A1)'), \"'+SUM(A1)\");\n"
            "assert.strictEqual(i40CsvCell('a,b'), '\"a,b\"');\n"
            "assert.strictEqual(i40CsvCell('a\"b'), '\"a\"\"b\"');\n"
        )
        proc = subprocess.run([node, '-e', script], capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr

    ao = _read(TPL_DIR / 'alarm_output.html')
    assert 'query: { token: wsToken }' in ao
    assert '目前后端暂无直接的规则CRUD API' not in ao
    assert '/api/alarm-rules/${encodeURIComponent(origId)}' in ao
    assert 'escapeHtml(broadcast.text' in ao

    alarms = _read(TPL_DIR / 'alarms.html')
    assert 'data-alarm-action' in alarms
    assert not re.search(r'onclick="(acknowledgeAlarm|shelveAlarm|unshelveAlarm)\(', alarms)
    assert 'encodeURIComponent(alarmId)' in alarms

    history = _read(TPL_DIR / 'history.html')
    assert 'encodeURIComponent(deviceId)' in history
    assert 'Number.isFinite(n)' in history
