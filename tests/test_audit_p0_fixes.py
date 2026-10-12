"""外部安全审计 P0 项整改的守卫测试（2026-10-12）。

对应审计报告的四项 P0/P1 缺陷：

1. **HA 共享密钥 + 角色闭环**
   - `HAManager` 配置了对端却不给密钥时曾经各自随机生成 → 心跳 HMAC 永远验不过
     → 备机「从未见过对端」→ 超时接管，而原主还在跑 → **双主**。
   - HA 角色此前只用于打日志，采集器无条件启动 → 备节点也采集/写控制。
2. **生产密钥 fail-closed**：`config._get_secret` 曾经缺密钥就生成随机值（只告警），
   `docker-compose.yml` 还写死 `change-me-in-production` / `admin123`。
3. **设备控制 fail-closed**：无联锁规则时写操作照放行；真实设备模式回退写死点位。
4. **网关 CLI 与文档不一致**：README 用 `--protocol`，代码只认 `--type`；
   `choices` 里列了没有实现的 `mqtt`（幽灵选项）。

每个断言都对应「改回旧行为就会变红」的判据。
"""
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
RUN_PY = BACKEND_ROOT / 'run.py'
CONFIG_PY = BACKEND_ROOT / 'config.py'
COMPOSE = BACKEND_ROOT / 'docker-compose.yml'
GATEWAY_RUN = BACKEND_ROOT / 'gateway' / 'run_gateway.py'


def _python_exe() -> str:
    return sys.executable


# ===========================================================================
# 1. HA 共享密钥
# ===========================================================================

class TestHASharedSecret:
    def test_peer_configured_without_secret_refuses_to_start(self):
        """配了对端却没有密钥 → 必须拒绝启动（而不是各自随机生成）"""
        from core.ha_manager import HAManager
        with pytest.raises(ValueError) as ei:
            HAManager(node_id='A', peer_address='10.0.0.2')
        assert 'HA_SHARED_SECRET' in str(ei.value)

    def test_peer_configured_with_secret_ok(self):
        from core.ha_manager import HAManager
        ha = HAManager(node_id='A', peer_address='10.0.0.2', shared_secret='s3cret')
        assert ha._shared_secret == b's3cret'

    def test_single_node_without_secret_still_ok(self):
        """单机模式不做心跳认证，无需密钥（不能把单机部署也卡死）"""
        from core.ha_manager import HAManager
        ha = HAManager(node_id='solo')          # peer_address 默认空
        assert ha.peer_address == ''
        assert isinstance(ha._shared_secret, bytes)

    def test_two_nodes_with_same_secret_verify_each_other(self):
        """正向对照：同一密钥下 HMAC 必须互验通过（证明不是「一律拒绝」）"""
        from core.ha_manager import HAManager
        a = HAManager(node_id='A', peer_address='10.0.0.2', shared_secret='same')
        b = HAManager(node_id='B', peer_address='10.0.0.1', shared_secret='same')
        payload = 'A:primary:100:1.0'
        assert b._verify_hmac(payload, a._compute_hmac(payload))

    def test_run_py_reads_secret_from_env(self):
        """接线守卫（AST）：run.py 必须把 HA_SHARED_SECRET 作为**关键字实参**传给 HAManager。

        ⚠️ 判据刻意走 AST 而不是「源码里出现这个字符串」：后者会被注释点亮 ——
        把真行删掉、留一行 `# shared_secret=os.environ.get('HA_SHARED_SECRET', '')`，
        字符串判据仍为绿，但功能已经没了（本仓 Bug 15「约束只活在注释里」同源）。
        """
        tree = ast.parse(RUN_PY.read_text(encoding='utf-8'))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == 'HAManager']
        assert calls, "run.py 里没有 HAManager(...) 调用"

        node = None
        for c in calls:
            for k in c.keywords:
                if k.arg == 'shared_secret':
                    node = k.value
        assert node is not None, "HAManager(...) 没有 shared_secret 关键字实参"

        # 期望形态：os.environ.get('HA_SHARED_SECRET', '')
        assert isinstance(node, ast.Call), f"shared_secret 实参形态不对: {ast.dump(node)[:90]}"
        assert isinstance(node.func, ast.Attribute) and node.func.attr == 'get', \
            f"shared_secret 不是 os.environ.get(...) 调用: {ast.dump(node)[:90]}"
        assert isinstance(node.func.value, ast.Attribute) and node.func.value.attr == 'environ'
        assert node.args and isinstance(node.args[0], ast.Constant) \
            and node.args[0].value == 'HA_SHARED_SECRET', \
            "shared_secret 读的不是 HA_SHARED_SECRET"

    def test_ha_key_fingerprint_is_logged(self):
        """运维可核对性：启用 HA 时必须打印密钥指纹（配了不同密钥仍会双主）"""
        from core.ha_manager import HAManager
        a = HAManager(node_id='A', peer_address='10.0.0.2', shared_secret='same')
        b = HAManager(node_id='B', peer_address='10.0.0.1', shared_secret='same')
        c = HAManager(node_id='C', peer_address='10.0.0.1', shared_secret='other')
        assert a.secret_fingerprint() == b.secret_fingerprint()
        assert a.secret_fingerprint() != c.secret_fingerprint()
        assert len(a.secret_fingerprint()) == 8
        assert 'same' not in a.secret_fingerprint()      # 不得泄漏密钥本身
        assert 'secret_fingerprint' in RUN_PY.read_text(encoding='utf-8')


class TestHARoleClosure:
    """HA 角色必须真正决定采集启停，而不是只打日志。"""

    @staticmethod
    def _func(tree, name):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        return None

    @staticmethod
    def _attr_calls(node):
        """收集形如 `obj.method` 的属性访问名（`data_collector.start` 这类）"""
        out = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Attribute):
                out.add(n.attr)
        return out

    def test_apply_ha_role_branches_on_role(self):
        """`start` 必须在「role == PRIMARY」分支里，`stop` 在另一分支。

        ⚠️ 只断言「函数体里同时出现过 .start 和 .stop」判别力不足：
        把 `if role == PRIMARY: start() else: stop()` 改成
        `start(); if False: stop()` 仍然为绿，但角色闭环已经退化。
        所以这里断言的是**结构**：存在一个以角色比较为条件、一支 start 一支 stop 的 if。
        """
        tree = ast.parse(RUN_PY.read_text(encoding='utf-8'))
        fn = self._func(tree, '_apply_ha_role')
        assert fn is not None, "run.py 里没有 _apply_ha_role —— HA 角色没有接到采集器上"

        def attrs(stmts):
            """收集一组语句里出现过的属性调用名（入参是语句**列表**）"""
            found = set()
            for stmt in stmts:
                for c in ast.walk(stmt):
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute):
                        found.add(c.func.attr)
            return found

        guarded = None
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            test_src = ast.dump(node.test)
            if 'PRIMARY' not in test_src and 'role' not in test_src:
                continue
            if 'start' in attrs(node.body) and 'stop' in attrs(node.orelse):
                guarded = node
                break
        assert guarded is not None, (
            "_apply_ha_role 里没有「按角色分支：一支 start、另一支 stop」的结构 —— "
            "角色闭环可能已退化成无条件启动采集"
        )

    def test_role_change_callback_is_a_real_function(self):
        """角色回调必须是**真实存在的具名函数**，而不是只打日志的 lambda 或被注释掉的调用。

        判据走 AST：`ast.parse` 天然不含注释，注释里的 `set_on_role_change(...)`
        不会出现在语法树里，所以「删掉真行、留个注释」会立刻变红。
        """
        tree = ast.parse(RUN_PY.read_text(encoding='utf-8'))
        args = []
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == 'set_on_role_change'):
                args.extend(n.args)
        assert args, "run.py 里没有 set_on_role_change(...) 调用"

        names = {a.id for a in args if isinstance(a, ast.Name)}
        assert '_on_ha_role_change' in names, (
            f"set_on_role_change 的实参必须是有函数体的具名回调（会启停采集），实际: "
            f"{[ast.dump(a)[:60] for a in args]}"
        )
        assert self._func(tree, '_on_ha_role_change') is not None, \
            "_on_ha_role_change 只有名字没有函数定义"

    def test_background_start_does_not_start_collector_unconditionally(self):
        """_background_start 不得无条件启动采集器（备节点会因此也采集）"""
        tree = ast.parse(RUN_PY.read_text(encoding='utf-8'))
        fn = self._func(tree, '_background_start')
        assert fn is not None
        direct = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == 'start'
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == 'data_collector'
        ]
        assert not direct, "data_collector.start() 仍在 _background_start 里无条件调用"


# ===========================================================================
# 2. 生产密钥 fail-closed
# ===========================================================================

def _import_config_env(extra_env: dict) -> subprocess.CompletedProcess:
    """在受控环境里 import config（_get_secret 在类体里执行，导入即触发）"""
    env = {k: v for k, v in os.environ.items()
           if k not in ('SCADA_ENV', 'SECRET_KEY', 'JWT_SECRET')}
    env.update(extra_env)
    return subprocess.run(
        [_python_exe(), '-c', 'import config; print("IMPORT_OK")'],
        cwd=str(BACKEND_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )


class TestProductionSecretFailClosed:
    def test_production_without_secret_refuses_to_start(self):
        r = _import_config_env({'SCADA_ENV': 'production'})
        assert r.returncode != 0, f"生产环境缺密钥竟然启动成功: {r.stdout}"
        assert 'RuntimeError' in r.stderr and 'SECRET_KEY' in r.stderr

    def test_production_with_weak_default_refuses_to_start(self):
        r = _import_config_env({
            'SCADA_ENV': 'production',
            'SECRET_KEY': 'change-me-in-production',
            'JWT_SECRET': 'change-me-in-production',
        })
        assert r.returncode != 0, "生产环境用了公开弱默认值竟然启动成功"
        assert 'RuntimeError' in r.stderr

    def test_development_without_secret_still_starts(self):
        """反向对照：开发环境不设密钥必须照常启动（否则本地开发全废）"""
        r = _import_config_env({'SCADA_ENV': 'development'})
        assert r.returncode == 0, r.stderr
        assert 'IMPORT_OK' in r.stdout

    def test_production_with_strong_secret_starts(self):
        r = _import_config_env({
            'SCADA_ENV': 'production',
            'SECRET_KEY': 'a' * 64,
            'JWT_SECRET': 'b' * 64,
        })
        assert r.returncode == 0, r.stderr
        assert 'IMPORT_OK' in r.stdout


class TestComposeSecrets:
    @staticmethod
    def _effective_text() -> str:
        """去掉注释行后的 compose 内容（注释里提到弱值不算缺陷）"""
        lines = []
        for line in COMPOSE.read_text(encoding='utf-8').splitlines():
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            lines.append(line)
        return '\n'.join(lines)

    def test_no_weak_defaults_in_compose(self):
        text = self._effective_text()
        assert 'change-me-in-production' not in text, \
            "docker-compose.yml 仍带公开弱默认 SECRET_KEY/JWT_SECRET"
        assert 'admin123' not in text, "docker-compose.yml 仍写死 Grafana admin123"

    def test_compose_requires_secrets(self):
        """必须用 `:?` 让 compose 在密钥缺失时直接报错，而不是回退到弱值。

        ⚠️ 断言用**运行时拼正则**而不是写死 `${VAR:?` 字面量：本仓的
        `tests/test_env_vars_wired.py` 会把整个 tests/ 源码当语料扫「读取点」，
        测试里写死的 `${GF_...:?` 会被误判成「本仓在读它」，进而让白名单守卫假红。
        """
        text = self._effective_text()
        for var in ('SECRET_KEY', 'JWT_SECRET', 'GF_SECURITY_ADMIN_PASSWORD'):
            pattern = re.escape(var) + r'=\$\{' + re.escape(var) + r':\?'
            assert re.search(pattern, text), (
                f"docker-compose.yml 里 {var} 没有用 `:?` 强制要求注入 —— "
                f"缺省时会静默回退到弱值"
            )

    def test_compose_marks_production(self):
        """容器部署即生产：必须设 SCADA_ENV=production，应用层 fail-closed 才生效"""
        text = COMPOSE.read_text(encoding='utf-8')
        assert re.search(r'^\s*-\s*SCADA_ENV=production\s*$', text, re.M)


# ===========================================================================
# 3. 设备控制 fail-closed
# ===========================================================================

def _dc(interlocks=None, simulation=False, devices=None):
    from 智能层.device_control import DeviceControlSafety
    from unittest.mock import MagicMock

    dm = MagicMock()
    dm.devices = devices or {'plc_a': {'protocol': 'modbus_tcp'}}
    dm.simulation_mode = simulation
    dm.get_all_devices.return_value = dict(dm.devices)
    cfg = {'interlocks': interlocks} if interlocks is not None else {}
    return DeviceControlSafety(MagicMock(), device_manager=dm, config=cfg)


class TestControlInterlockFailClosed:
    def test_real_mode_without_interlocks_refuses_write(self):
        """真实设备模式 + 零联锁规则 → 写操作必须被拒（此前只打 warning）"""
        dc = _dc(interlocks=None, simulation=False)
        assert dc._interlock_rules == {}
        r = dc.validate_write('plc_a', 0, 1, 'op')
        assert r['allowed'] is False
        assert '联锁' in r['reason']

    def test_simulation_without_interlocks_still_allows_write(self):
        """反向对照：模拟模式无物理风险，不能把演示链路卡死"""
        dc = _dc(interlocks=None, simulation=True)
        r = dc.validate_write('plc_a', 0, 1, 'op')
        assert r['allowed'] is True

    def test_real_mode_with_interlocks_allows_write(self):
        """有联锁配置时正常放行（证明不是「一律拒绝」）"""
        rules = [{'id': 'r1', 'condition': {'device_id': 'x', 'register': 't', 'operator': '>', 'value': 999},
                  'action': {'type': 'block_start'}}]
        dc = _dc(interlocks=rules, simulation=False)
        assert dc._interlock_rules
        r = dc.validate_write('plc_a', 0, 1, 'op')
        assert r['allowed'] is True


# ===========================================================================
# 4. 网关 CLI 与文档一致性
# ===========================================================================

class TestGatewayCli:
    def test_protocol_alias_is_accepted(self, tmp_path):
        """README 一直写 `--protocol`；它必须被接受，而不是 unrecognized arguments"""
        r = subprocess.run(
            [_python_exe(), str(GATEWAY_RUN), '--protocol', 'modbus',
             '--config', str(tmp_path / 'nope.yaml')],
            cwd=str(BACKEND_ROOT), capture_output=True, text=True, timeout=180,
        )
        combined = r.stdout + r.stderr
        assert 'unrecognized arguments' not in combined, combined
        # 走到了「加载配置失败」说明参数已被识别
        assert '加载配置失败' in combined, combined

    def test_invalid_protocol_rejected_by_choices(self):
        r = subprocess.run(
            [_python_exe(), str(GATEWAY_RUN), '--protocol', 'not-a-protocol'],
            cwd=str(BACKEND_ROOT), capture_output=True, text=True, timeout=180,
        )
        assert r.returncode != 0
        assert 'invalid choice' in (r.stdout + r.stderr)

    def test_choices_match_implementations(self):
        """元守卫：choices 里不得出现没有实现的幽灵选项（曾经的 mqtt）"""
        from gateway.run_gateway import GATEWAY_TYPES

        src = GATEWAY_RUN.read_text(encoding='utf-8')
        m = re.search(r"add_argument\(\s*'--type'\s*,\s*'--protocol'[^)]*choices=([^,\)]+)",
                      src, re.S)
        assert m, "没找到 --type/--protocol 的 choices 定义 —— 扫描口径坏了"
        expr = m.group(1).strip()
        if expr.startswith('sorted('):
            declared = set(GATEWAY_TYPES)          # 直接复用唯一真源
        else:
            declared = {x.strip().strip('\'"') for x in expr.strip('[]').split(',') if x.strip()}
        assert declared == set(GATEWAY_TYPES), (
            f"choices 与实现不一致：choices={sorted(declared)} "
            f"实现={sorted(GATEWAY_TYPES)}"
        )


class TestGatewayConfigSelection:
    """`--protocol X` 必须按 type 挑条目，而不是「取第 0 条再改它的 type」。

    历史缺陷（2026-10-13 复刻证实）：用默认 `gateway/config.yaml`（4 个条目）跑
    `--protocol s7`，会拿 **modbus_gateway_01** 的 host/port 去建 S7 网关 ——
    连错设备且不报错；同时 `gw_config['type'] = args.type` 会**原地改写**配置。
    """

    CONFIG = {
        'gateways': [
            {'type': 'modbus', 'gateway_id': 'modbus_gateway_01', 'host': '192.168.1.10', 'port': 502},
            {'type': 's7', 'gateway_id': 's7_gateway_01', 'host': '192.168.1.20', 'port': 102},
            {'type': 'opcua', 'gateway_id': 'opcua_gateway_01', 'host': '192.168.1.30', 'port': 4840},
        ]
    }

    def test_selects_by_type_not_first_entry(self):
        from gateway.run_gateway import select_gateway_configs
        sel = select_gateway_configs(self.CONFIG, 's7', run_all=False)
        assert len(sel) == 1
        assert sel[0]['gateway_id'] == 's7_gateway_01'
        assert sel[0]['host'] == '192.168.1.20'

    def test_does_not_mutate_input_config(self):
        from gateway.run_gateway import select_gateway_configs
        before = [dict(e) for e in self.CONFIG['gateways']]
        select_gateway_configs(self.CONFIG, 'opcua', run_all=False)
        assert self.CONFIG['gateways'] == before, "select_gateway_configs 原地改写了配置"

    def test_missing_type_returns_empty(self):
        from gateway.run_gateway import select_gateway_configs
        assert select_gateway_configs(self.CONFIG, 'iec104', run_all=False) == []

    def test_run_all_returns_every_declared_entry(self):
        from gateway.run_gateway import select_gateway_configs
        sel = select_gateway_configs(self.CONFIG, 'modbus', run_all=True)
        assert {e['type'] for e in sel} == {'modbus', 's7', 'opcua'}

    def test_legacy_single_entry_config_still_works(self):
        """老格式（配置文件本身就是单个网关条目、无 gateways 键）不能被改坏"""
        from gateway.run_gateway import select_gateway_configs
        legacy = {'gateway_id': 'only_one', 'host': '10.0.0.1', 'port': 502}
        sel = select_gateway_configs(legacy, 'modbus', run_all=False)
        assert len(sel) == 1 and sel[0]['type'] == 'modbus'
        assert 'type' not in legacy, "老格式分支不应把 type 写回入参"

    def test_cli_reports_missing_type_instead_of_using_wrong_one(self, tmp_path):
        """端到端：配置里没有该 type 时必须**报错退出**，而不是拿别的条目硬跑"""
        cfg = tmp_path / 'gw.yaml'
        cfg.write_text(
            'gateways:\n'
            '  - type: modbus\n'
            '    gateway_id: modbus_gateway_01\n'
            '    host: 192.168.1.10\n'
            '    port: 502\n',
            encoding='utf-8',
        )
        r = subprocess.run(
            [_python_exe(), str(GATEWAY_RUN), '--protocol', 'iec104', '--config', str(cfg)],
            cwd=str(BACKEND_ROOT), capture_output=True, text=True, timeout=180,
        )
        assert r.returncode != 0
        combined = r.stdout + r.stderr
        assert '没有 type=iec104 的网关条目' in combined, combined
