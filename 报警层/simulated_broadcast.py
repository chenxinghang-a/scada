"""
模拟广播系统
完全独立的模拟实现，不依赖真实硬件
"""

# ============================================================================
# 接线状态：未接线（WIRED = False）
# ============================================================================
# 本模块在生产代码（run.py / 各业务层 / 其它模块）中**没有任何 import 引用**。
# 模块本身可用，但当前没有调用方 —— 它宣称的能力**当前并未生效**。
#
# 判定方式（可复现）：以 run.py / launcher.py / 各**独立入口模块**为根，
# 沿 import 图做可达性分析；本模块不在可达集内。
# 判据与守卫见 `tests/test_wiring_declaration.py`。
#
# 为什么保留而不删除：删掉即丢能力，且本模块可能仍有测试价值；
# 这里只把「没接线」**显式化、可追踪**，避免「代码在库里」被误读成「功能在跑」。
# 如果要真正启用它，请接线后**删掉本声明块**（守卫会据此发现状态变化）。
# ============================================================================

import logging
from typing import Any
from datetime import datetime

from .interfaces import IBroadcastSystem

logger = logging.getLogger(__name__)


class SimulatedBroadcastSystem(IBroadcastSystem):
    """
    模拟广播系统
    
    特点：
    - 完全独立，不依赖任何真实硬件
    - 所有广播输出到日志
    - 用于演示和测试环境
    - 可以独立运行，无需切换模式
    """

    def __init__(self, config: dict[str, Any] | None = None):
        """
        初始化模拟广播系统
        
        Args:
            config: 配置字典
        """
        self.config = config or {}
        self._enabled = self.config.get('enabled', True)
        
        # 广播区域
        self.areas = self.config.get('areas', ['车间A', '车间B', '仓库', '办公楼'])
        
        # 预设模板
        self.preset_templates = self.config.get('preset_templates', {
            'alarm_critical': '注意！{area}发生严重报警：{message}，请立即处置！',
            'alarm_warning': '提醒：{area}出现告警：{message}，请关注。',
            'evacuation': '请注意，{area}发生紧急状况，请沿疏散通道撤离！',
            'all_clear': '广播通知，{area}警报解除，恢复正常。',
        })
        
        # 广播历史
        self.history: list[dict[str, Any]] = []
        
        logger.info(f"[模拟] 广播系统初始化完成，可用区域: {self.areas}")

    @property
    def enabled(self) -> bool:
        """是否启用"""
        return self._enabled

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
        
        # 模拟广播
        logger.info(f"[模拟广播] [{level.upper()}] 区域: {target_area} | 来源: {source} | 内容: {text}")
        
        # 记录历史
        record = {
            'timestamp': datetime.now().isoformat(),
            'text': text,
            'level': level,
            'area': target_area,
            'source': source,
            'mode': 'simulated'
        }
        self.history.append(record)
        
        # 限制历史记录数量
        if len(self.history) > 1000:
            self.history = self.history[-500:]

        return {
            'success': True,
            'message': f'广播已发送到 {target_area}',
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
            'mode': 'simulated',
            'areas': self.areas,
            'history_count': len(self.history),
            'last_broadcast': self.history[-1] if self.history else None
        }
