"""
真实广播系统
完全独立的真实硬件实现，连接实际的广播设备
"""

import logging
import json
from typing import Any
from datetime import datetime

from .interfaces import IBroadcastSystem

logger = logging.getLogger(__name__)


class RealBroadcastSystem(IBroadcastSystem):
    """
    真实广播系统
    
    特点：
    - 完全独立，连接真实的广播设备
    - 通过MQTT协议发送广播指令
    - 需要实际的广播硬件才能运行
    - 适用于生产环境
    """

    def __init__(self, config: dict[str, Any] | None = None):
        """
        初始化真实广播系统
        
        Args:
            config: 配置字典，必须包含MQTT连接信息
        """
        self.config = config or {}
        self._enabled = self.config.get('enabled', True)
        
        # MQTT配置
        self.mqtt_broker = self.config.get('mqtt_broker', 'localhost')
        self.mqtt_port = self.config.get('mqtt_port', 1883)
        self.topic_prefix = self.config.get('topic_prefix', 'pa/')
        
        # 广播区域
        self.areas = self.config.get('areas', ['车间A', '车间B', '仓库', '办公楼'])
        
        # 预设模板
        self.preset_templates = self.config.get('preset_templates', {
            'alarm_critical': '注意！{area}发生严重报警：{message}，请立即处置！',
            'alarm_warning': '提醒：{area}出现告警：{message}，请关注。',
            'evacuation': '请注意，{area}发生紧急状况，请沿疏散通道撤离！',
            'all_clear': '广播通知，{area}警报解除，恢复正常。',
        })
        
        # MQTT客户端（延迟创建）
        self._mqtt_client = None
        
        # 广播历史
        self.history: list[dict[str, Any]] = []
        
        logger.info(f"[真实] 广播系统初始化完成: {self.mqtt_broker}:{self.mqtt_port}")

    @property
    def enabled(self) -> bool:
        """是否启用"""
        return self._enabled

    def _get_mqtt_client(self):
        """获取MQTT客户端（懒创建）"""
        if self._mqtt_client is None:
            try:
                import paho.mqtt.client as mqtt
                
                def on_connect(client, userdata, flags, rc):
                    if rc == 0:
                        logger.info(f"[真实] MQTT连接成功: {self.mqtt_broker}:{self.mqtt_port}")
                    else:
                        logger.error(f"[真实] MQTT连接失败: {rc}")

                self._mqtt_client = mqtt.Client()
                self._mqtt_client.on_connect = on_connect
                self._mqtt_client.connect(self.mqtt_broker, self.mqtt_port)
                self._mqtt_client.loop_start()
                
            except Exception as e:
                logger.error(f"[真实] MQTT客户端创建失败: {e}")
                return None
        return self._mqtt_client

    def _publish(self, topic: str, payload: dict[str, Any]) -> bool:
        """
        发布MQTT消息
        
        Args:
            topic: 主题
            payload: 消息内容
            
        Returns:
            是否成功
        """
        client = self._get_mqtt_client()
        if not client:
            return False
            
        try:
            message = json.dumps(payload, ensure_ascii=False)
            result = client.publish(topic, message)
            return result.rc == 0
        except Exception as e:
            logger.error(f"[真实] MQTT发布失败: {e}")
            return False

    def speak(self, text: str, level: str = 'info', area: str = None, source: str = 'manual') -> dict[str, Any]:
        """
        语音广播
        
        Args:
            text: 广播内容
            level: 级别
            area: 广播区域
            source: 来源
            
        Returns:
            广播结果
        """
        if not self._enabled:
            return {'success': False, 'message': '广播系统未启用'}

        # 确定广播区域
        target_area = area if area and area in self.areas else 'all'
        
        # 构建MQTT消息
        payload = {
            'text': text,
            'level': level,
            'area': target_area,
            'source': source,
            'timestamp': datetime.now().isoformat()
        }
        
        # 发送到MQTT
        topic = f"{self.topic_prefix}{target_area}"
        success = self._publish(topic, payload)
        
        if success:
            logger.info(f"[真实广播] [{level.upper()}] 区域: {target_area} | 来源: {source} | 内容: {text}")
        else:
            logger.error(f"[真实广播] 发送失败: {payload}")
        
        # 记录历史
        record = {
            'timestamp': datetime.now().isoformat(),
            'text': text,
            'level': level,
            'area': target_area,
            'source': source,
            'mode': 'real',
            'mqtt_topic': topic,
            'success': success
        }
        self.history.append(record)
        
        # 限制历史记录数量
        if len(self.history) > 1000:
            self.history = self.history[-500:]

        return {
            'success': success,
            'message': f'广播已发送到 {target_area}' if success else '广播发送失败',
            'area': target_area,
            'timestamp': record['timestamp']
        }

    def get_areas(self) -> list[str]:
        """
        获取可用广播区域
        
        Returns:
            区域列表
        """
        return self.areas.copy()

    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """
        获取广播历史
        
        Args:
            limit: 返回数量
            
        Returns:
            历史记录列表
        """
        return self.history[-limit:]

    def get_status(self) -> dict[str, Any]:
        """
        获取系统状态
        
        Returns:
            状态字典
        """
        return {
            'enabled': self._enabled,
            'mode': 'real',
            'mqtt_broker': self.mqtt_broker,
            'mqtt_port': self.mqtt_port,
            'areas': self.areas,
            'history_count': len(self.history),
            'last_broadcast': self.history[-1] if self.history else None
        }
