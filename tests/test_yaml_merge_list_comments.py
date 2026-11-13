"""字典列表的「保注释增删」：设备清单改一台，不该把整份文件的注释洗掉。

## 背景

`_merge_block` 原来只处理映射。设备清单是 `devices: [ {...}, ... ]`，
只要增删一台设备，整个列表就被重写 —— 实测 `devices_simulated.yaml`
16025 行里 127 行注释、键序、排版一次全丢。

数据没丢，但"本文件由 tools/gen_factory.py 生成""现场可以直接改这个文件"
"凭据一律 ${VAR} 占位"这些**唯一说明**跟着没了。下一个拿到文件的人
会直接手改，然后被下次生成覆盖。

修法见 `core/config_io.py::_merge_list_by_id`：按 id 对齐，
未变条目逐字保留，只重写真正变了的那一条。

## 这里钉什么

  · 删一项：其余项（含它们的注释）逐字保留
  · 加一项：追加到末尾，原有项不受影响
  · 改一项：只有该项被重写，别的项一个字符都不动
  · 三项操作后 `yaml.safe_load` 都必须与原 dict **完全等价**
    （这条是硬约束：`dump_yaml` 内部也会校验，不等价就退回整份重写，
     那样注释照样丢 —— 所以等价性必须由用例直接从外部验证）
"""

from __future__ import annotations

import io
import os

import yaml

from core.config_io import dump_yaml

SRC = """# =============================================================================
# 仿真设备清单
# - 凭据字段一律使用 ${VAR} 环境变量占位，禁止明文入库。
# - 本文件由 tools/gen_factory.py 生成，现场可改但重新生成会覆盖。
# =============================================================================

devices:
  # ---- 1 号锅炉 ----
  - id: boiler_01
    name: 一号锅炉
    host: 192.168.1.10
    port: 502

  # ---- 2 号锅炉（备用，2026 年检修）----
  - id: boiler_02
    name: 二号锅炉
    host: 192.168.1.11
    port: 502

  # ---- 循环水泵 ----
  - id: pump_01
    name: 循环水泵
    host: 192.168.1.12
    port: 502
"""


def _load_devices():
    return yaml.safe_load(SRC)['devices']


def _write(tmp_path, data):
    p = tmp_path / 'devices_probe.yaml'
    io.open(p, 'w', encoding='utf-8').write(SRC)
    assert dump_yaml(str(p), data) is True, 'dump_yaml 应返回 True'
    return io.open(p, encoding='utf-8').read(), p


class TestHeaderAlwaysKept:
    def test_file_header_survives_any_edit(self, tmp_path):
        devs = _load_devices()
        devs.pop(0)                       # 删掉第一台
        text, _ = _write(tmp_path, {'devices': devs})
        assert '仿仿真设备清单'.replace('仿', '') in text or '仿真设备清单' in text
        assert '${VAR}' in text, '文件头里关于凭据占位的说明必须还在'
        assert 'gen_factory' in text, '「由 gen_factory 生成」这句必须还在'


class TestDelete:
    def test_other_items_keep_their_comments(self, tmp_path):
        devs = [d for d in _load_devices() if d['id'] != 'boiler_02']
        text, _ = _write(tmp_path, {'devices': devs})

        assert 'boiler_02' not in text, '被删的设备不该还在'
        assert '# ---- 1 号锅炉 ----' in text, '未删项的注释丢了'
        assert '# ---- 循环水泵 ----' in text, '未删项的注释丢了'

    def test_result_is_equivalent(self, tmp_path):
        devs = [d for d in _load_devices() if d['id'] != 'boiler_01']
        text, p = _write(tmp_path, {'devices': devs})
        assert yaml.safe_load(text) == {'devices': devs}


class TestAdd:
    def test_new_item_appended_and_old_untouched(self, tmp_path):
        devs = _load_devices()
        devs.append({'id': 'chiller_09', 'name': '新冷水机',
                     'host': '192.168.1.99', 'port': 502})
        text, _ = _write(tmp_path, {'devices': devs})

        assert 'chiller_09' in text
        assert '# ---- 1 号锅炉 ----' in text
        assert '# ---- 2 号锅炉（备用，2026 年检修）----' in text
        assert '# ---- 循环水泵 ----' in text

    def test_result_is_equivalent(self, tmp_path):
        devs = _load_devices()
        devs.append({'id': 'new_01', 'name': 'X', 'host': '1.1.1.1', 'port': 1})
        text, _ = _write(tmp_path, {'devices': devs})
        assert yaml.safe_load(text) == {'devices': devs}


class TestModify:
    def test_only_target_item_rewritten(self, tmp_path):
        devs = _load_devices()
        for d in devs:
            if d['id'] == 'boiler_02':
                d['name'] = '二号锅炉（已改名）'
        text, _ = _write(tmp_path, {'devices': devs})

        assert '二号锅炉（已改名）' in text
        # 其他两条的注释必须原样
        assert '# ---- 1 号锅炉 ----' in text
        assert '# ---- 循环水泵 ----' in text

    def test_result_is_equivalent(self, tmp_path):
        devs = _load_devices()
        for d in devs:
            if d['id'] == 'pump_01':
                d['port'] = 1502
        text, _ = _write(tmp_path, {'devices': devs})
        assert yaml.safe_load(text) == {'devices': devs}


class TestFallbackIsSafe:
    """不是「带 id 的字典列表」时不能接管，必须退回整份重写。"""

    def test_plain_scalar_list_still_works(self, tmp_path):
        p = tmp_path / 'plain.yaml'
        io.open(p, 'w', encoding='utf-8').write(
            "# 头注释\nitems:\n  - 1\n  - 2\n  - 3\n")
        assert dump_yaml(str(p), {'items': [1, 2, 3]}) is True
        text = io.open(p, encoding='utf-8').read()
        assert yaml.safe_load(text) == {'items': [1, 2, 3]}

    def test_list_of_dicts_without_id_falls_back(self, tmp_path):
        p = tmp_path / 'noid.yaml'
        io.open(p, 'w', encoding='utf-8').write(
            "# 头注释\nrows:\n  - a: 1\n  - a: 2\n")
        data = {'rows': [{'a': 1}, {'a': 3}]}
        assert dump_yaml(str(p), data) is True
        text = io.open(p, encoding='utf-8').read()
        assert yaml.safe_load(text) == data, '没有 id 的列表也必须写对（可以丢注释）'
