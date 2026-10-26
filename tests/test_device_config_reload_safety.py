# -*- coding: utf-8 -*-
"""设备配置热重载必须 **fail-closed**：写坏的配置不许清空设备表（round 284）。

缺陷形态
--------
`DeviceManager._load_config_into()` 原来把**所有异常吞掉**：

    try:
        ... yaml.safe_load ...
    except Exception as e:
        logger.error(f"加载配置文件异常: {e}")      # ← target 保持空

调用方（`reload_config` / `load_config`）根本不知道解析失败了，于是拿一张
**空表**去替换 `self.devices`。后果是连锁的：

* 全部设备从配置表里消失；
* round 283 加的 `sync_device_tasks()` 差集变成"全部移除" →
  **采集任务被全部停掉** → 现场所有设备停止采集；
* 而接口回的是 `success: true`「移除 N 台设备」，日志只有一行 error。

触发条件一点都不极端：编辑器保存的中间态（先清空再写入）、
`devices:` 后面内容被误删、文件权限变化导致读不到、yaml 缩进写错一格。

判据
----
**配置解析失败 ≠ 配置里没有设备**。前者必须保留原状并报错（fail-closed），
后者才是真的"把设备删光了"。系统分不清时，宁可**不生效** ——
不生效只影响一次改动，误生效会让整个现场停止采集。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

VALID_YAML = """devices:
  - id: dev_a
    name: 设备A
    protocol: modbus_tcp
    host: 127.0.0.1
    port: 502
    slave_id: 1
    enabled: true
    registers:
      - name: temp
        address: 0
        data_type: uint16
  - id: dev_b
    name: 设备B
    protocol: modbus_tcp
    host: 127.0.0.1
    port: 502
    slave_id: 1
    enabled: true
    registers:
      - name: temp
        address: 0
        data_type: uint16
"""

BROKEN_YAML = """devices:
  - id: dev_a
    name: 设备A
    protocol: modbus_tcp
    host: 127.0.0.1
     port: 502        # ← 缩进错一格
"""


@pytest.fixture
def mgr(tmp_path):
    from 采集层.device_manager import DeviceManager

    cfg = tmp_path / 'devices.yaml'
    cfg.write_text(VALID_YAML, encoding='utf-8')
    m = DeviceManager(config_path=str(cfg))
    assert set(m.get_all_devices()) == {'dev_a', 'dev_b'}, '前提不成立：初始配置没加载'
    m._cfg_path = cfg
    return m


def _write(mgr, text):
    mgr._cfg_path.write_text(text, encoding='utf-8')


# ---------------------------------------------------------------------------
# 各种"解析失败"都必须被拒绝，且原表不动
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('content, label', [
    (BROKEN_YAML, 'yaml 缩进错'),
    ('', '空文件（0 字节）'),
    ('   \n', '只有空白'),
    ('devices: []\n', 'devices 是空列表'),
    ('devices:\n', 'devices 是 null'),
    ('devices: not_a_list\n', 'devices 不是列表'),
    ('- just\n- a\n- list\n', '顶层是列表不是映射'),
])
def test_bad_config_is_rejected_and_table_kept(mgr, content, label):
    _write(mgr, content)

    diff = mgr.reload_config()

    assert diff['rejected'], f'{label}：配置被静默接受了 —— {diff}'
    assert set(mgr.get_all_devices()) == {'dev_a', 'dev_b'}, (
        f'{label}：设备表被清空/改坏了 —— 现场会全部停止采集')
    assert diff['removed'] == [], f'{label}：居然报"移除了设备" — {diff}'


def test_missing_config_file_is_rejected(mgr):
    mgr._cfg_path.unlink()

    diff = mgr.reload_config()

    assert diff['rejected']
    assert set(mgr.get_all_devices()) == {'dev_a', 'dev_b'}


def test_empty_reload_allowed_when_explicitly_requested(mgr):
    """真的要把设备删光时，显式 `allow_empty=True` 仍然允许（否则没法清空）。"""
    _write(mgr, 'devices: []\n')

    diff = mgr.reload_config(allow_empty=True)

    assert diff['rejected'] is None
    assert diff['removed'] == ['dev_a', 'dev_b']
    assert mgr.get_all_devices() == {}


def test_good_config_still_reloads(mgr):
    """回归：正常重载不受影响（防止守卫把功能一起挡掉）。"""
    _write(mgr, """devices:
  - id: dev_b
    name: 设备B
    protocol: modbus_tcp
    host: 127.0.0.1
    port: 502
    slave_id: 1
    enabled: true
    registers:
      - name: temp
        address: 0
        data_type: uint16
  - id: dev_c
    name: 设备C
    protocol: modbus_tcp
    host: 127.0.0.1
    port: 502
    slave_id: 1
    enabled: true
    registers:
      - name: temp
        address: 0
        data_type: uint16
""")

    diff = mgr.reload_config()

    assert diff['rejected'] is None
    assert diff['added'] == ['dev_c']
    assert diff['removed'] == ['dev_a']
    assert set(mgr.get_all_devices()) == {'dev_b', 'dev_c'}


def test_load_config_keeps_old_table_on_failure(mgr):
    """`load_config()` 同理：解析失败不许替换成空表。"""
    _write(mgr, BROKEN_YAML)

    ok = mgr.load_config()

    assert ok is False
    assert set(mgr.get_all_devices()) == {'dev_a', 'dev_b'}


# ---------------------------------------------------------------------------
# API：被拒绝时**绝不能**去同步采集任务（否则差集=全部移除）
# ---------------------------------------------------------------------------

def test_reload_endpoint_refuses_when_config_rejected(app, client, auth_headers):
    app.device_manager = MagicMock()
    app.device_manager.reload_config.return_value = {
        'added': [], 'removed': [], 'kept': ['dev_a', 'dev_b'],
        'rejected': '解析结果为 0 台设备，而原配置有 2 台 —— 疑似文件被清空或写坏',
    }
    app.data_collector = MagicMock()

    resp = client.post('/api/devices/reload', headers=auth_headers)

    assert resp.status_code == 400, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body['success'] is False
    assert '未生效' in body['message'] or '0 台设备' in body['message']
    assert not app.data_collector.sync_device_tasks.called, (
        '配置被拒绝了还去同步采集任务 —— 差集是"全部移除"，'
        '现场所有设备会被停止采集')


def test_reload_endpoint_end_to_end_with_broken_yaml(app, client, auth_headers, tmp_path):
    """端到端：真的写坏配置文件 → 接口 400，且采集任务一个都没停。"""
    from 采集层.device_manager import DeviceManager

    cfg = tmp_path / 'devices.yaml'
    cfg.write_text(VALID_YAML, encoding='utf-8')
    real_mgr = DeviceManager(config_path=str(cfg))

    app.device_manager = real_mgr
    app.data_collector = MagicMock()
    app.data_collector.sync_device_tasks.return_value = {
        'started': [], 'stopped': [], 'unchanged': ['dev_a', 'dev_b'],
        'skipped': False}

    cfg.write_text(BROKEN_YAML, encoding='utf-8')

    resp = client.post('/api/devices/reload', headers=auth_headers)

    assert resp.status_code == 400
    assert set(real_mgr.get_all_devices()) == {'dev_a', 'dev_b'}
    assert not app.data_collector.sync_device_tasks.called
