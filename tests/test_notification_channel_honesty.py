# -*- coding: utf-8 -*-
"""通知通道的诚实化：短信不许假成功、sound 不许假装有消费方（round 250）。

背景
----
round 243 把**邮件**接进了报警链路（三块拼图都在、就是没连起来）。
本轮核对另外两个通道，发现两个「看起来在工作」的缺口：

1. **短信：占位实现却报告成功**
   `报警层/notification.py:send_sms()` 原来是
   「`logger.info("短信发送: 发送N条短信")` + `return True`」——
   一条短信都没发，却告诉调用方成功。而且它**没有任何生产调用方**。
   这是比「没接线」更坏的形态：接线的瞬间，运维就会在日志里看到
   「短信发送: 发送3条短信」，于是放心地依赖一个从不工作的通道。

   修法（round 250）：
   - `send_sms` 改为明确报错 + 返回 False，docstring 里写清要实现它
     需要补的三件事（阿里云 RPC 签名 / SendSms 调用 / 无凭证可测的签名单测）；
   - **照常接进报警链路**（`_notify_by_sms`，与邮件同构：异步 + 失败记账）。
     启用 `notification.sms` 后每条报警都会「尝试 → 失败 → ERROR +
     `output_errors['sms']`」—— 缺口是**响亮的**，而不是此前的静默无底洞；
   - yaml 的 sms 段把现状和实现清单写清楚。

2. **sound：没有任何消费方**
   `配置/alarms.yaml` 的 `notification.sound` 段（critical_sound /
   warning_sound 两个 mp3 文件名）—— 后端不播放音频，Vue 前端与旧版
   静态前端都没有 `new Audio()` / `.mp3` 播放代码。也就是说把 enabled
   改成 true、填好文件名，**不会发出任何声音**。真正的「声光」在
   `报警层/alarm_output.py`（Modbus DO → 灯塔+蜂鸣器，接通可用），
   语音广播在 `broadcast_system.py`（MQTT → 现场音柱）。
   本轮只在 yaml 里如实标注（删字段有前端契约风险，见提交信息）。

本文件把这两件事钉死，防止下一个人又以为它们在工作。
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from 报警层.alarm_manager import AlarmManager  # noqa: E402
from 报警层.notification import Notification  # noqa: E402


# ===========================================================================
# 1. send_sms 不许假成功
# ===========================================================================

class TestSmsHonesty:
    def test_enabled_sms_returns_false(self):
        """启用也不许返回 True —— 占位实现返回 True 就是假成功。"""
        n = Notification({'sms': {'enabled': True}})
        assert n.send_sms(['13800138000'], '报警') is False

    def test_unimplemented_is_logged_as_error(self, caplog):
        """失败必须留 ERROR 痕迹（不是 INFO），否则启用后无从判断。"""
        import logging
        n = Notification({'sms': {'enabled': True}})
        with caplog.at_level(logging.ERROR):
            n.send_sms(['13800138000'], '报警')
        assert any('未实现' in r.message or '占位' in r.message
                   for r in caplog.records), caplog.text

    def test_docstring_states_what_is_missing(self):
        """docstring 必须写清要实现它需要补什么（下一个人照着做）。"""
        doc = Notification.send_sms.__doc__ or ''
        for keyword in ('aliyun', 'HMAC-SHA1', 'SendSms'):
            assert keyword in doc, f'send_sms 的 docstring 没提 {keyword}'


# ===========================================================================
# 2. 短信接进报警链路：启用后失败是响亮的
# ===========================================================================

class TestSmsWiring:
    @pytest.fixture
    def manager(self, tmp_path):
        import yaml
        cfg = {
            'alarm_rules': [{
                'id': 'r_sms', 'name': '温度过高', 'device_id': 'D1',
                'register_name': 'temp', 'condition': 'greater_than',
                'threshold': 100, 'level': 'critical', 'enabled': True,
            }],
            'notification': {
                'sms': {'enabled': True, 'phone_numbers': ['13800138000']},
            },
        }
        p = tmp_path / 'alarms.yaml'
        p.write_text(yaml.dump(cfg, allow_unicode=True), encoding='utf-8')
        am = AlarmManager(MagicMock(), str(p))
        am.alarm_output = None
        am.broadcast_system = None
        yield am
        am.stop()

    def test_sms_attempted_and_counted(self, manager, caplog):
        """启用短信后：每条报警尝试一次，失败进 output_errors['sms']。"""
        import logging
        with caplog.at_level(logging.ERROR):
            manager._trigger_alarm(
                {'id': 'r_sms', 'name': '温度过高', 'level': 'critical',
                 'device_id': 'D1', 'register_name': 'temp', 'threshold': 100},
                'D1', 'temp', 120.0, datetime.now())
            # 等异步投递的 future 落地（单线程池，很快）
            for _ in range(50):
                if manager._get_output_error_counts().get('sms'):
                    break
                time.sleep(0.05)

        counts = manager._get_output_error_counts()
        assert counts.get('sms', 0) >= 1, (
            '启用了短信却没有任何失败记录 —— 又变成静默无底洞了')
        assert any('未实现' in r.message or '占位' in r.message
                   for r in caplog.records), caplog.text

    def test_sms_not_attempted_when_disabled(self, tmp_path):
        """未启用时不该尝试（也不该有失败计数）。"""
        import yaml
        cfg = {
            'alarm_rules': [{
                'id': 'r_sms', 'name': '温度过高', 'device_id': 'D1',
                'register_name': 'temp', 'condition': 'greater_than',
                'threshold': 100, 'level': 'critical', 'enabled': True,
            }],
            'notification': {'sms': {'enabled': False}},
        }
        p = tmp_path / 'alarms.yaml'
        p.write_text(yaml.dump(cfg, allow_unicode=True), encoding='utf-8')
        am = AlarmManager(MagicMock(), str(p))
        am.alarm_output = None
        am.broadcast_system = None
        try:
            am._trigger_alarm(
                {'id': 'r_sms', 'name': '温度过高', 'level': 'critical',
                 'device_id': 'D1', 'register_name': 'temp', 'threshold': 100},
                'D1', 'temp', 120.0, datetime.now())
            time.sleep(0.3)
            assert am._get_output_error_counts().get('sms', 0) == 0, (
                '短信未启用却产生了失败计数')
        finally:
            am.stop()

    def test_output_errors_has_sms_channel(self, manager):
        """统计里必须有 sms 这一项（恒 0 也比没有这项好——没有=没人看得到）。"""
        counts = manager._get_output_error_counts()
        assert 'sms' in counts
        assert counts['sms'] == 0

    def test_setup_warns_at_startup_not_first_alarm(self, tmp_path, caplog):
        """启用了没实现的通道，**启动时**就要说清。

        否则运维要把系统跑到第一条报警才从 output_errors 里发现"短信没实现"——
        而那时候可能已经过去几天了。启动即失败比运行时才发现好。
        """
        import logging
        import yaml

        cfg = {
            'alarm_rules': [],
            'notification': {'sms': {'enabled': True,
                                     'phone_numbers': ['13800138000']}},
        }
        p = tmp_path / 'alarms.yaml'
        p.write_text(yaml.dump(cfg, allow_unicode=True), encoding='utf-8')

        with caplog.at_level(logging.ERROR):
            am = AlarmManager(MagicMock(), str(p))
        try:
            assert any('尚未实现' in r.message for r in caplog.records), (
                '启用了 sms 但启动日志里没有任何说明 —— 运维要把系统跑到'
                '第一条报警才发现短信没实现')
        finally:
            am.stop()


# ===========================================================================
# 3. 事实必须留在**耐久**的地方
# ===========================================================================

class TestDurableTruth:
    """为什么这些断言不查 yaml 注释。

    `展示层/api/_common.py:save_yaml_config` 用 `yaml.dump` 写回配置 ——
    **yaml 注释不会被保留**。也就是说：运维第一次通过 API 改报警配置
    （加规则 / 改去重 / 改升级规则），`配置/alarms.yaml` 里所有人话注释
    会被一次性抹平。所以「sms 未实现」「sound 没有消费方」这些事实
    必须落在**代码**里（docstring / 启动 ERROR / 本文件的断言），
    yaml 注释只是给人看的加分项，不能当唯一真源。

    真源优先级：代码行为 > 测试断言 > yaml 注释。
    """

    def _text(self):
        return (PROJECT_ROOT / '配置' / 'alarms.yaml').read_text(encoding='utf-8')

    def test_sms_disabled_by_default(self):
        """短信默认禁用（yaml 解析，不依赖注释存活）。"""
        import yaml
        cfg = yaml.safe_load(self._text())
        assert cfg['notification']['sms']['enabled'] is False, (
            'sms 默认必须是禁用 —— 在 send_sms 实现完成前启用它只会制造噪音')

    def test_sound_section_has_no_consumer(self):
        """`notification.sound` 没有任何消费方 —— 这条事实由本用例守着。

        一旦有人接上了播放（后端或前端），本用例变红，提示他同步更新
        `配置/alarms.yaml` 的 sound 段注释与 `send_sms` 附近的说明。
        在那之前，运维必须知道：把 enabled 改成 true、填好文件名，
        **不会发出任何声音**；真正的声光在 `报警层/alarm_output.py`
        （Modbus DO → 灯塔+蜂鸣器），语音广播在 `broadcast_system.py`。
        """
        import re

        prod_dirs = ['报警层', '采集层', '存储层', '展示层', '智能层',
                     '用户层', 'core', 'timeseries', 'gateway', 'tools']
        pattern = re.compile(r"critical_sound|warning_sound|\.get\('sound'\)"
                             r"|\.get\(\"sound\"\)")
        hits = []
        for d in prod_dirs:
            base = PROJECT_ROOT / d
            if not base.is_dir():
                continue
            for p in base.rglob('*.py'):
                if '__pycache__' in str(p):
                    continue
                for i, line in enumerate(
                        p.read_text(encoding='utf-8').splitlines(), 1):
                    if pattern.search(line):
                        hits.append(f'{p.relative_to(PROJECT_ROOT)}:{i}')
        # 只允许出现在「接受并原样存起来」的 API 处（api_alarms 的
        # notification 更新端点），不允许有真正的消费方。
        allowed = {'展示层/api/api_alarms.py'}
        real_consumers = [h for h in hits
                          if not any(h.startswith(a) for a in allowed)]
        assert not real_consumers, (
            f'sound 配置出现了真实消费方: {real_consumers} —— '
            f'请同步更新 配置/alarms.yaml 的 sound 段说明')


# ===========================================================================
# 4. 静态守卫：send_sms 不许回到「打日志就返回 True」
# ===========================================================================

def test_send_sms_does_not_return_true_unconditionally():
    """send_sms 不许出现 `return True`（真语句，不是 docstring 里的散文）。

    用 AST 而不是字符串匹配：round 249 的 hasattr 守卫就是这么误报的 ——
    正则/子串会命中注释和 docstring 里提到这个词的地方。
    """
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(Notification.send_sms))
    tree = ast.parse(src)
    true_returns = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and node.value.value is True
    ]
    assert not true_returns, (
        f'send_sms 在第 {true_returns} 行 return True —— 它一条短信都不会发，'
        f'返回 True 就是把未实现伪装成成功')
    # 反向确认：确有 return False（不是把方法删空了）
    false_returns = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and node.value.value is False
    ]
    assert false_returns, 'send_sms 里没有 return False'
