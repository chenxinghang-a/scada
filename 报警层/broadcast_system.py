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
import threading
from typing import Dict, List, Any, Optional, Callable
from datetime import datetime

logger = logging.getLogger(__name__)

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

    def to_dict(self) -> Dict[str, Any]:
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

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.enabled = self.config.get('enabled', True)
        self.simulation = self.config.get('simulation', True)

        self.areas: List[str] = self.config.get('areas', ['all'])
        self.default_area = self.config.get('default_area', 'all')
        self.preset_templates: Dict[str, str] = self.config.get('preset_templates', {})

        # MQTT客户端（延迟连接）
        self._mqtt_client = None
        self._mqtt_connected = False
        self._mqtt_config = self.config.get('mqtt', {})

        # 广播历史（内存保留最近200条）
        self._history: List[Dict[str, Any]] = []
        self._max_history = 200
        self._lock = threading.Lock()

        # 广播回调（用于前端同步显示）
        self._callbacks: List[Callable] = []

        logger.info(f"广播系统初始化: {'模拟模式' if self.simulation else 'MQTT模式'}")

    def add_callback(self, callback: Callable):
        """注册广播回调，前端可监听最新广播内容"""
        self._callbacks.append(callback)

    # ==================== 核心广播功能 ====================

    def speak(self, text: str, level: str = 'info',
              area: str = None, source: str = 'manual') -> Dict[str, Any]:
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
        if not self.simulation:
            self._mqtt_publish(msg)

        # 记录历史
        with self._lock:
            msg.status = 'sent'
            self._history.append(msg.to_dict())
            if len(self._history) > self._max_history:
                self._history.pop(0)

        # 触发回调（前端同步）
        for cb in self._callbacks:
            try:
                cb(msg.to_dict())
            except Exception as e:
                logger.debug(f"广播回调异常: {e}")

        return {'success': True, 'message': '广播已发送', 'data': msg.to_dict()}

    def speak_alarm(self, level: str, message: str,
                    device_id: str = '', area: str = None) -> Dict[str, Any]:
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
                     area: str = None, **kwargs) -> Dict[str, Any]:
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
                   level: str = 'info') -> Dict[str, Any]:
        """区域定向广播"""
        return self.speak(text=text, level=level, area=area, source='manual')

    def get_areas(self) -> List[str]:
        return list(self.areas)

    # ==================== 历史与状态 ====================
    def get_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(reversed(self._history[-limit:]))

    def get_status(self) -> Dict[str, Any]:
        return {
            'enabled': self.enabled,
            'simulation': self.simulation,
            'areas': self.areas,
            'mqtt_connected': self._mqtt_connected,
            'history_count': len(self._history),
        }

    # ==================== 内部方法 ====================
    def _log_broadcast(self, msg: BroadcastMessage):
        """日志广播（模拟模式核心）"""
        level_tag = {'critical': '紧急', 'warning': '警告', 'info': '信息'}.get(msg.level, '信息')
        tag = f"【广播-{level_tag}】"
        logger.info(f"{tag} 区域={msg.area} | {msg.text}")

    def _mqtt_publish(self, msg: BroadcastMessage):
        """通过MQTT发布到广播主题（对接IP广播/PA系统）"""
        if not MQTT_AVAILABLE:
            logger.warning("paho-mqtt未安装，无法发布广播消息")
            return

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

            if self._mqtt_client and self._mqtt_connected:
                import json
                self._mqtt_client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1)
                logger.debug(f"MQTT广播已发布: {topic}")
        except Exception as e:
            logger.error(f"MQTT广播发布失败: {e}")

    def _connect_mqtt(self):
        """连接MQTT Broker"""
        if not MQTT_AVAILABLE:
            return

        try:
            broker = self._mqtt_config.get('broker', 'localhost')
            port = self._mqtt_config.get('port', 1883)
            username = self._mqtt_config.get('username', '')
            password = self._mqtt_config.get('password', '')

            self._mqtt_client = mqtt.Client(client_id=f'scada_pa_{int(time.time())}')
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
            except Exception:
                pass
            self._mqtt_client = None
            self._mqtt_connected = False
        logger.info("广播系统已断开")
