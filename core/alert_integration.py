"""
监控告警集成模块
提供统一的告警通知接口
"""

import logging
import threading
from typing import Dict, List, Any, Callable, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class AlertSeverity:
    """告警严重程度"""
    INFO = 'info'
    WARNING = 'warning'
    CRITICAL = 'critical'
    EMERGENCY = 'emergency'


class AlertChannel:
    """告警通道"""

    def __init__(self, name: str, handler: Callable[[Dict[str, Any]], None], enabled: bool = True):
        self.name = name
        self.handler = handler
        self.enabled = enabled

    def send(self, alert: Dict[str, Any]):
        """发送告警"""
        if self.enabled:
            try:
                self.handler(alert)
            except Exception as e:
                logger.error(f"告警通道 {self.name} 发送失败: {e}")


class AlertManager:
    """告警管理器"""

    def __init__(self):
        self._channels: Dict[str, AlertChannel] = {}
        self._history: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._max_history = 1000

        # 注册默认通道
        self._register_default_channels()

    def _register_default_channels(self):
        """注册默认告警通道"""
        # 日志通道
        self.register_channel(AlertChannel(
            'log',
            self._log_handler
        ))

    def _log_handler(self, alert: Dict[str, Any]):
        """日志告警处理"""
        severity = alert.get('severity', AlertSeverity.INFO)
        message = alert.get('message', 'Unknown alert')
        source = alert.get('source', 'unknown')

        if severity == AlertSeverity.EMERGENCY:
            logger.critical(f"[ALERT] {source}: {message}")
        elif severity == AlertSeverity.CRITICAL:
            logger.error(f"[ALERT] {source}: {message}")
        elif severity == AlertSeverity.WARNING:
            logger.warning(f"[ALERT] {source}: {message}")
        else:
            logger.info(f"[ALERT] {source}: {message}")

    def register_channel(self, channel: AlertChannel):
        """注册告警通道"""
        self._channels[channel.name] = channel
        logger.info(f"注册告警通道: {channel.name}")

    def unregister_channel(self, name: str):
        """注销告警通道"""
        if name in self._channels:
            del self._channels[name]
            logger.info(f"注销告警通道: {name}")

    def send_alert(self, alert: Dict[str, Any]):
        """发送告警"""
        # 添加时间戳
        if 'timestamp' not in alert:
            alert['timestamp'] = datetime.now().isoformat()

        # 记录历史
        with self._lock:
            self._history.append(alert)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

        # 发送到所有通道
        for channel in self._channels.values():
            channel.send(alert)

    def get_history(self, limit: int = 100, severity: str = None) -> List[Dict[str, Any]]:
        """获取告警历史"""
        with self._lock:
            history = self._history
            if severity:
                history = [a for a in history if a.get('severity') == severity]
            return history[-limit:]

    def clear_history(self):
        """清除告警历史"""
        with self._lock:
            self._history.clear()


# 全局告警管理器实例
_alert_manager = None


def get_alert_manager() -> AlertManager:
    """获取全局告警管理器"""
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
    return _alert_manager


def send_alert(severity: str, message: str, source: str = 'system', **kwargs):
    """发送告警的快捷方法"""
    manager = get_alert_manager()
    manager.send_alert({
        'severity': severity,
        'message': message,
        'source': source,
        **kwargs,
    })
