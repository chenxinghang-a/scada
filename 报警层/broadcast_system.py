"""
工业广播系统模块
面向工业4.0的安全广播与语音通知，支持:
- 语音播报/文本转语音（软件模拟）
- MQTT主题发布（对接IP网络广播/对讲系统）
- 预设报警话术模板
- 区域定向广播
- 手动喊话

场景:
- 报警发生时自动播报设备、区域、级别
- 现场广播喇叭/音柱发出语音
- 多区域工厂可按区域选择广播范围
- 支持传统PA系统与IP广播对接
"""

import logging
import time
import uuid
import threading
from typing import Any, Callable
from datetime import datetime

logger = logging.getLogger(__name__)

#: 广播发布熔断（round 287）：连续失败多少次后停止尝试连接/发布。
#: 与声光 DO 通道（round 286）同一判据 —— 通道坏了要**快速失败并停下来**，
#: 不能让每条报警都卡在一次 broker 连接超时上。
BROADCAST_CIRCUIT_FAIL_THRESHOLD = 3

#: 熔断持续多久（秒）。广播的恢复代价比 DO 小，但也不必每 10 秒试探一次。
BROADCAST_CIRCUIT_OPEN_SECONDS = 30.0

# 可选依赖: paho-mqtt（对接IP广播/对讲系统）
try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    logger.debug("paho-mqtt未安装，广播系统将以本地模拟模式运行")


class BroadcastMessage:
    """广播消息结构体"""
    def __init__(self, text: str, level: str = 'info',
                 area: str = 'all', source: str = 'system'):
        self.text = text
        self.level = level
        self.area = area
        self.source = source
        self.timestamp = datetime.now().isoformat()
        self.status = 'pending'

    def to_dict(self) -> dict[str, Any]:
        return {
            'text': self.text,
            'level': self.level,
            'area': self.area,
            'source': self.source,
            'timestamp': self.timestamp,
            'status': self.status
        }


class BroadcastSystem:
    """
    工业广播系统

    支持两种输出通道:
    1) 本地模拟（日志/前端播放）：无需硬件
    2) MQTT发布到广播主题（对接IP网络广播/PA系统）

    配置示例（config dict）:
    {
        'enabled': True,
        'simulation': True,
        'mqtt': {
            'broker': '192.168.1.200',
            'port': 1883,
            'username': '',
            'password': '',
            'topic_prefix': 'pa/'
        },
        'areas': ['车间A', '车间B', '仓库', '办公楼'],
        'default_area': 'all',
        'preset_templates': {
            'alarm_critical': '注意！{area}发生严重报警：{message}，请立即处置！',
            'alarm_warning': '提醒：{area}出现告警：{message}，请关注。',
            'evacuation': '请注意，{area}发生紧急状况，请沿疏散通道撤离！',
            'all_clear': '广播测试，{area}警报解除，恢复正常。',
        }
    }
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.enabled = self.config.get('enabled', True)
        self.simulation = self.config.get('simulation', True)

        self.areas: list[str] = self.config.get('areas', ['all'])
        self.default_area = self.config.get('default_area', 'all')
        self.preset_templates: dict[str, str] = self.config.get('preset_templates', {})

        # MQTT客户端（延迟连接）
        self._mqtt_client = None
        self._mqtt_connected = False
        self._mqtt_config = self.config.get('mqtt', {})

        #: 连续发布失败次数（成功即清零）—— round 287
        self._fail_streak = 0
        #: 熔断到期时刻（**单调时钟**；None 表示未熔断）
        self._circuit_open_until: float | None = None

        # 广播历史（内存保留最近200条）
        self._history: list[dict[str, Any]] = []
        self._max_history = 200
        self._lock = threading.Lock()

        # 广播回调（用于前端同步显示）
        self._callbacks: list[Callable[..., Any]] = []

        logger.info(f"广播系统初始化: {'模拟模式' if self.simulation else 'MQTT模式'}")

    def add_callback(self, callback: Callable[..., Any]):
        """注册广播回调，前端可监听最新广播内容"""
        self._callbacks.append(callback)

    # ==================== 核心广播功能 ====================

    def speak(self, text: str, level: str = 'info',
              area: str = "", source: str = 'manual') -> dict[str, Any]:
        """
        发起广播

        Args:
            text: 广播内容
            level: 级别 info/warning/critical
            area: 广播区域（默认all）
            source: 来源 manual/system/alarm
        Returns:
            广播结果字典
        """
        if not self.enabled:
            return {'success': False, 'message': '广播系统未启用'}

        area = area or self.default_area
        msg = BroadcastMessage(text=text, level=level, area=area, source=source)

        # 本地日志/模拟
        self._log_broadcast(msg)

        # MQTT发布（硬件对接）
        # round 287：**必须把真实结果带出去**。原来这里丢弃返回值，
        # 于是 Broker 挂掉、paho 没装、发布返回错误码，`speak()` 全都回
        # `success: True`「广播已发送」—— 现场音柱没响，界面说已广播。
        delivered = True
        if not self.simulation:
            delivered = self._mqtt_publish(msg)

        # 记录历史
        with self._lock:
            msg.status = 'sent' if delivered else 'failed'
            self._history.append(msg.to_dict())
            if len(self._history) > self._max_history:
                self._history.pop(0)

        # 触发回调（前端同步）—— 失败也回调，但 status 是 'failed'，
        # 前端据此显示"广播失败"而不是"已广播"。
        for cb in self._callbacks:
            try:
                cb(msg.to_dict())
            except Exception as e:
                logger.debug(f"广播回调异常: {e}")

        if not delivered:
            return {
                'success': False,
                'message': '广播**未发出**：MQTT 发布失败（现场音柱可能没有声音）',
                'data': msg.to_dict(),
            }
        return {'success': True, 'message': '广播已发送', 'data': msg.to_dict()}

    def speak_alarm(self, level: str, message: str,
                    device_id: str = '', area: str = "") -> dict[str, Any]:
        """
        报警联动广播（自动按级别选择话术）

        Args:
            level: 报警级别 critical/warning/info
            message: 报警消息
            device_id: 设备ID
            area: 广播区域
        """
        template_key = f'alarm_{level}'
        template = self.preset_templates.get(template_key, '{message}')
        text = template.format(area=area or self.default_area,
                               message=message,
                               device_id=device_id)
        return self.speak(text=text, level=level, area=area, source='alarm')

    def speak_preset(self, template_key: str,
                     area: str = "", **kwargs) -> dict[str, Any]:
        """
        使用预设模板广播

        Args:
            template_key: 预设模板名称
            area: 广播区域
            **kwargs: 模板参数
        """
        template = self.preset_templates.get(template_key)
        if not template:
            return {'success': False, 'message': f'预设模板 {template_key} 不存在'}
        text = template.format(area=area or self.default_area, **kwargs)
        return self.speak(text=text, area=area, source='preset')

    # ==================== 区域广播 ====================
    def speak_area(self, area: str, text: str,
                   level: str = 'info') -> dict[str, Any]:
        """区域定向广播"""
        return self.speak(text=text, level=level, area=area, source='manual')

    def get_areas(self) -> list[str]:
        return list(self.areas)

    # ==================== 历史与状态 ====================
    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._history[-limit:]))

    def get_status(self) -> dict[str, Any]:
        circuit_open = self._circuit_open_until is not None
        remaining = 0.0
        if circuit_open:
            remaining = max(0.0, self._circuit_open_until - time.monotonic())
        return {
            'enabled': self.enabled,
            'simulation': self.simulation,
            'areas': self.areas,
            'mqtt_connected': self._mqtt_connected,
            'history_count': len(self._history),
            #: MQTT 通道健康度（round 287）—— 此前只有 `mqtt_connected`，
            #: 而"连上了但发布一直失败"完全看不出来。
            'mqtt': {
                'fail_streak': self._fail_streak,
                'circuit_open': circuit_open,
                'circuit_remaining_seconds': round(remaining, 1),
                'fail_threshold': BROADCAST_CIRCUIT_FAIL_THRESHOLD,
            },
        }

    # ==================== 内部方法 ====================
    def _log_broadcast(self, msg: BroadcastMessage):
        """日志广播（模拟模式核心）"""
        level_tag = {'critical': '紧急', 'warning': '警告', 'info': '信息'}.get(msg.level, '信息')
        tag = f"【广播-{level_tag}】"
        logger.info(f"{tag} 区域={msg.area} | {msg.text}")

    # ==================== 发布失败熔断（round 287） ====================

    def _circuit_is_open(self) -> bool:
        """熔断是否打开（期间不连接、不发布）。"""
        until = self._circuit_open_until
        if until is None:
            return False
        if time.monotonic() >= until:
            self._circuit_open_until = None
            logger.info("广播通道熔断到期，恢复尝试（此前连续失败 %d 次）",
                        self._fail_streak)
            return False
        return True

    def _record_publish_failure(self, detail: str) -> None:
        self._fail_streak += 1
        if (self._fail_streak >= BROADCAST_CIRCUIT_FAIL_THRESHOLD
                and self._circuit_open_until is None):
            self._circuit_open_until = time.monotonic() + BROADCAST_CIRCUIT_OPEN_SECONDS
            logger.error(
                "广播通道连续失败 %d 次（%s）—— 熔断 %.0f 秒，期间不再尝试连接 Broker；"
                "**现场音柱不会有声音**，请检查 MQTT Broker 与网络",
                self._fail_streak, detail, BROADCAST_CIRCUIT_OPEN_SECONDS)

    def _record_publish_success(self) -> None:
        if self._fail_streak or self._circuit_open_until is not None:
            logger.warning("广播通道恢复（此前连续失败 %d 次）", self._fail_streak)
        self._fail_streak = 0
        self._circuit_open_until = None

    def _mqtt_publish(self, msg: BroadcastMessage) -> bool:
        """通过MQTT发布到广播主题（对接IP广播/PA系统）。

        Returns:
            bool: 消息是否**真的发出去了**。

        round 287：原实现返回 None 且把失败吞成一行 error，于是 `speak()`
        无论发没发出去都回 `success: True`「广播已发送」。后果连锁：
        `AlarmManager._call_output_method` 把 `{'success': True}` 当成功 →
        `_record_output_success()` 把失败计数清零 → round 267 加的
        「通知通道连续失败要报警」**对广播通道永远不生效**。
        Broker 挂了、paho 没装、QoS 发布返回错误码，运维一概看不到。
        """
        if not MQTT_AVAILABLE:
            logger.warning("paho-mqtt未安装，无法发布广播消息")
            self._record_publish_failure('paho-mqtt 未安装')
            return False

        if self._circuit_is_open():
            logger.debug("广播通道熔断中，跳过发布")
            return False

        topic_prefix = self._mqtt_config.get('topic_prefix', 'pa/')
        topic = f"{topic_prefix}{msg.area}"

        payload = {
            'text': msg.text,
            'level': msg.level,
            'timestamp': msg.timestamp,
            'source': msg.source
        }

        try:
            if self._mqtt_client is None:
                self._connect_mqtt()

            if not (self._mqtt_client and self._mqtt_connected):
                self._record_publish_failure('MQTT 未连接')
                return False

            import json
            info = self._mqtt_client.publish(
                topic, json.dumps(payload, ensure_ascii=False), qos=1)

            # paho 的 publish() 在"本地就失败了"（未连接/队列满/参数错）时
            # 通过 MQTTMessageInfo.rc 返回错误码；rc == 0 才是真的交出去了。
            rc = getattr(info, 'rc', 0)
            if rc != 0:
                self._record_publish_failure(f'publish 返回错误码 rc={rc}')
                return False

            self._record_publish_success()
            logger.debug(f"MQTT广播已发布: {topic}")
            return True
        except Exception as e:
            self._record_publish_failure(f'发布异常: {e}')
            logger.error(f"MQTT广播发布失败: {e}")
            return False

    def _connect_mqtt(self):
        """连接MQTT Broker"""
        if not MQTT_AVAILABLE:
            return

        try:
            broker = self._mqtt_config.get('broker', 'localhost')
            port = self._mqtt_config.get('port', 1883)
            username = self._mqtt_config.get('username', '')
            password = self._mqtt_config.get('password', '')

            try:
                self._mqtt_client = mqtt.Client(client_id=f'scada_pa_{uuid.uuid4().hex[:12]}')
            except TypeError:
                self._mqtt_client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
                                                client_id=f'scada_pa_{uuid.uuid4().hex[:12]}')
            if username:
                self._mqtt_client.username_pw_set(username, password)

            def on_connect(client, userdata, flags, rc):
                if rc == 0:
                    self._mqtt_connected = True
                    logger.info(f"广播MQTT已连接: {broker}:{port}")
                else:
                    logger.error(f"广播MQTT连接失败，返回码: {rc}")

            def on_disconnect(client, userdata, rc):
                self._mqtt_connected = False
                if rc != 0:
                    logger.warning("广播MQTT意外断开")

            self._mqtt_client.on_connect = on_connect
            self._mqtt_client.on_disconnect = on_disconnect
            self._mqtt_client.connect(broker, port, keepalive=60)
            self._mqtt_client.loop_start()
        except Exception as e:
            logger.error(f"广播MQTT连接失败: {e}")

    def disconnect(self):
        """断开连接"""
        if self._mqtt_client:
            try:
                self._mqtt_client.loop_stop()
                self._mqtt_client.disconnect()
            except Exception as e:
                # 关闭已失效的MQTT客户端可能抛错，属预期内且无副作用
                # （引用随后被置空，不会继续使用）
                logger.debug("关闭广播MQTT客户端失败: %s", e)
            self._mqtt_client = None
            self._mqtt_connected = False
        logger.info("广播系统已断开")
