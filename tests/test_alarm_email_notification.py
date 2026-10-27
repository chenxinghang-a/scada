"""
邮件报警通知接线（round 243）

背景
----
三样东西全都在，就是没连起来：

1. `配置/alarms.yaml` 里有一整段 `notification:`（email / sms / sound），
   结构**正好匹配** `Notification.__init__` 的期望；
2. `报警层/notification.py`（183 行）实现了 SMTP + STARTTLS + HTML 转义，
   还有现成的 `send_alarm_notification(alarm_data)`；
3. `config.py` 的 `AlarmConfig.EMAIL_ENABLED` + 4 个 `SMTP_*` 也在。

但：
- `Notification` **只在测试里被实例化过**，生产代码零调用；
- `AlarmManager.load_config()` 只读 `alarm_rules` / `alarm_output` / `dedup` /
  `escalation`，**从来不读 `notification` 段**；
- `AlarmConfig` 那 5 个配置项全仓库零消费。

结果：运维把 `notification.email.enabled` 改成 true、填好 SMTP 和收件人，
报警时一封邮件都不会发出，而且日志里没有任何痕迹。

本轮把 `notification` 段接到 `AlarmManager`，并解决三件必须解决的事：
- **异步**：`send_email` 是同步 SMTP，一个不通的服务器能让报警线程卡十几秒；
- **去重**：放在 WebSocket 推送的去重点之后，复用同一套冷却/确认抑制，
  否则报警风暴会把收件人邮箱打爆；
- **字段映射**：`send_alarm_notification` 读 `level`/`message`/`value`，
  而报警 payload 用 `alarm_level`/`alarm_message`/`actual_value` ——
  直接传过去会发出一封只有标题、正文全是 `-` 的空壳邮件。
"""

import os
import sys
import textwrap
from datetime import datetime

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from 报警层.alarm_manager import AlarmManager  # noqa: E402


BASE_YAML = """
alarm_rules:
  - id: r1
    name: 温度过高
    device_id: D1
    register_name: temp
    condition: ">"
    threshold: 100
    level: critical
    enabled: true
dedup:
  enabled: true
  emit_cooldown_seconds: 60
  acknowledge_suppress_seconds: 600
"""


def _write_yaml(tmp_path, extra: str = '') -> str:
    path = tmp_path / "alarms.yaml"
    path.write_text(BASE_YAML + textwrap.dedent(extra), encoding='utf-8')
    return str(path)


@pytest.fixture
def make_manager(tmp_path):
    """构造真实的 AlarmManager（会起后台线程），用例结束统一 stop()"""
    from unittest.mock import MagicMock

    created = []

    def _make(extra_yaml: str = ''):
        am = AlarmManager(MagicMock(), _write_yaml(tmp_path, extra_yaml))
        created.append(am)
        return am

    yield _make

    for am in created:
        try:
            am.stop()
        except Exception:
            pass


class _FakeNotifier:
    """记录调用与收到的 payload"""

    def __init__(self, email_enabled=True, result=True, raises=None):
        self.email_enabled = email_enabled
        self._result = result
        self._raises = raises
        self.calls = []

    def send_alarm_notification(self, payload):
        self.calls.append(payload)
        if self._raises:
            raise self._raises
        return self._result


def _payload(**over):
    base = {
        'alarm_id': 'r1',
        'device_id': 'D1',
        'register_name': 'temp',
        'alarm_level': 'critical',
        'alarm_message': '温度过高',
        'threshold': 100,
        'actual_value': 123.4,
        'timestamp': '2026-10-22T06:00:00',
        'area': 'all',
        'dedup_key': 'r1:D1:temp',
    }
    base.update(over)
    return base


# ============================================================
# 1. 配置段真的被读
# ============================================================

class TestConfigIsRead:
    def test_notification_section_creates_notifier(self, make_manager):
        am = make_manager("""
        notification:
          email:
            enabled: false
            smtp_server: smtp.example.com
            smtp_port: 587
            username: ''
            password: ''
            recipients:
              - a@example.com
        """)
        assert am.notification is not None, 'load_config 没有读 notification 段'

    def test_no_notification_section_leaves_notifier_none(self, make_manager):
        am = make_manager()
        assert am.notification is None

    def test_incomplete_config_logs_error(self, make_manager, caplog):
        """启用了但缺 smtp_server/username/password → 必须 ERROR，不能静默。"""
        with caplog.at_level('ERROR'):
            make_manager("""
            notification:
              email:
                enabled: true
                smtp_server: ''
                username: ''
                password: ''
                recipients:
                  - a@example.com
            """)
        assert any('邮件通知已启用但缺少配置项' in r.getMessage() for r in caplog.records), caplog.text

    def test_enabled_without_recipients_logs_error(self, make_manager, caplog):
        with caplog.at_level('ERROR'):
            make_manager("""
            notification:
              email:
                enabled: true
                smtp_server: smtp.example.com
                username: u
                password: p
                recipients: []
            """)
        assert any('没有收件人' in r.getMessage() for r in caplog.records), caplog.text


# ============================================================
# 2. 发送行为
# ============================================================

class TestSending:
    def test_disabled_email_never_sends(self, make_manager):
        am = make_manager()
        fake = _FakeNotifier(email_enabled=False)
        am.set_notification(fake)

        am._notify_by_email(_payload())

        assert fake.calls == []

    def test_no_notifier_is_noop(self, make_manager):
        am = make_manager()
        am._notify_by_email(_payload())   # 不抛即通过

    def test_enabled_email_sends_once(self, make_manager):
        am = make_manager()
        fake = _FakeNotifier()
        am.set_notification(fake)

        am._notify_by_email(_payload())
        am._notify_executor.shutdown(wait=True)

        assert len(fake.calls) == 1

    def test_field_mapping(self, make_manager):
        """必须把 alarm_level/alarm_message/actual_value 映射成 level/message/value。

        `Notification.send_alarm_notification` 读的是后者；不映射会发出一封
        正文全是 `-` 的空壳邮件（标题还在，所以更难发现）。
        """
        am = make_manager()
        fake = _FakeNotifier()
        am.set_notification(fake)

        am._notify_by_email(_payload())
        am._notify_executor.shutdown(wait=True)

        sent = fake.calls[0]
        assert sent['level'] == 'critical'
        assert sent['message'] == '温度过高'
        assert sent['value'] == 123.4
        assert sent['device_id'] == 'D1'
        assert sent['register_name'] == 'temp'
        assert sent['threshold'] == 100

    def test_send_is_async(self, make_manager):
        """投递不能在调用线程里同步跑完 —— SMTP 超时会把报警主链路拖死。"""
        import threading
        import time

        am = make_manager()
        seen = {}

        class _SlowNotifier:
            email_enabled = True

            def send_alarm_notification(self, payload):
                seen['thread'] = threading.current_thread().name
                time.sleep(0.2)
                return True

        am.set_notification(_SlowNotifier())

        t0 = time.time()
        am._notify_by_email(_payload())
        elapsed = time.time() - t0

        assert elapsed < 0.15, f'_notify_by_email 阻塞了 {elapsed:.2f}s'
        am._notify_executor.shutdown(wait=True)
        assert seen.get('thread', '').startswith('alarm-email'), \
            f'邮件没在专用线程里发：{seen.get("thread")}'


# ============================================================
# 3. 失败必须可见
# ============================================================

class TestFailureVisibility:
    def test_false_result_is_counted(self, make_manager):
        am = make_manager()
        am.set_notification(_FakeNotifier(result=False))

        am._notify_by_email(_payload())
        am._notify_executor.shutdown(wait=True)

        assert am._output_errors.get('email', 0) == 1

    def test_exception_is_counted(self, make_manager):
        am = make_manager()
        am.set_notification(_FakeNotifier(raises=RuntimeError('SMTP 拒绝连接')))

        am._notify_by_email(_payload())
        am._notify_executor.shutdown(wait=True)

        assert am._output_errors.get('email', 0) == 1

    def test_exception_does_not_propagate(self, make_manager):
        """邮件炸了不能把报警主链路带崩。"""
        am = make_manager()
        am.set_notification(_FakeNotifier(raises=RuntimeError('boom')))

        am._notify_by_email(_payload())      # 不抛即通过
        am._notify_executor.shutdown(wait=True)


# ============================================================
# 4. 与去重的关系
# ============================================================

class TestDedupInteraction:
    def test_suppressed_alarm_sends_no_email(self, make_manager):
        """被去重抑制的报警不能再发邮件，否则同一报警每 10 秒一封。"""
        am = make_manager()
        fake = _FakeNotifier()
        am.set_notification(fake)
        # round 290：抑制判定改由 `_should_emit_detail()` 返回 (bool, 原因)，
        # 调用方按原因分桶打点；`_should_emit()` 保留为它的薄封装。
        # 这里替换的是**真正被调用的那个缝**，否则补丁不生效 ——
        # 邮件照发，用例变红（本轮就是被它抓到的）。
        am._should_emit_detail = lambda *a, **kw: (False, 'dedup_cooldown')

        am._trigger_alarm(am.rules['r1'], 'D1', 'temp', 123.4, datetime.now())

        if am._notify_executor is not None:
            am._notify_executor.shutdown(wait=True)
        assert fake.calls == [], '去重抑制期间仍然发了邮件'

    def test_email_call_sits_after_dedup_gate(self):
        """静态守卫：`_notify_by_email` 必须在 `_record_emit` 之后调用。

        顺序写反（邮件放在去重点之前）时，行为测试可能因为别的路径仍然绿，
        所以这里直接盯源码顺序。
        """
        import pathlib
        import re

        source = (pathlib.Path(PROJECT_ROOT) / '报警层' / 'alarm_manager.py').read_text(encoding='utf-8')
        body = source.split('def _trigger_alarm', 1)[1]

        i_emit = body.find('self._record_emit(')
        i_mail = body.find('self._notify_by_email(')

        assert i_emit != -1 and i_mail != -1, '找不到 _record_emit 或 _notify_by_email'
        assert i_emit < i_mail, '_notify_by_email 排在去重点之前 —— 报警风暴会打爆邮箱'


# ============================================================
# 5. 静态守卫：接线不能回退
# ============================================================

def test_load_config_reads_notification_section():
    import pathlib
    import re

    source = (pathlib.Path(PROJECT_ROOT) / '报警层' / 'alarm_manager.py').read_text(encoding='utf-8')
    assert re.search(r"config\.get\('notification'", source), \
        'load_config 没有读 notification 段 —— yaml 里的邮件配置等于白写'


def test_trigger_alarm_calls_notify():
    import pathlib
    import re

    source = (pathlib.Path(PROJECT_ROOT) / '报警层' / 'alarm_manager.py').read_text(encoding='utf-8')
    assert re.search(r'^\s+self\._notify_by_email\(alarm_payload\)', source, flags=re.MULTILINE), \
        '_trigger_alarm 没有调用 _notify_by_email(alarm_payload)'


def test_stop_shuts_down_executor():
    import pathlib
    import re

    source = (pathlib.Path(PROJECT_ROOT) / '报警层' / 'alarm_manager.py').read_text(encoding='utf-8')
    assert re.search(r'executor\.shutdown\(wait=True\)', source), \
        'stop() 没有关闭邮件线程池 —— 停机时会丢掉最后几条报警邮件'
