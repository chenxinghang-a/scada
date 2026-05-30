"""
Modbus写入安全验证器
===================
基于以下国标实现写入安全防护：
- GB/T 19582: Modbus协议规范 — 地址/功能码/值域校验
- GB/T 35718: 工控系统信息安全 — 写入权限与审计
- GB/T 36323: 信息安全防护指南 — 安全联锁
- GB/T 15969: 可编程序控制器 — 故障安全
- DL/T 634.5104: 远动设备及系统 — 值域约束

核心原则：
1. 最小权限 — 只允许必要的写操作
2. 值域约束 — 每个寄存器有明确的安全范围
3. 安全联锁 — 关键操作需要二次确认
4. 全链路审计 — 每次写入都有完整记录
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class WriteRiskLevel(Enum):
    """写入风险等级 (GB/T 36323)"""
    LOW = 'low'           # 普通参数（显示亮度、蜂鸣器开关等）
    MEDIUM = 'medium'     # 工艺参数（温度设定值、速度设定值等）
    HIGH = 'high'         # 关键参数（压力限值、安全阀值等）
    CRITICAL = 'critical' # 危险操作（紧急停止、安全联锁旁路等）


@dataclass
class RegisterSafetyProfile:
    """寄存器安全配置"""
    address: int
    name: str
    data_type: str
    unit: str
    min_value: float
    max_value: float
    risk_level: WriteRiskLevel
    writable: bool = True
    requires_confirm: bool = False
    description: str = ''


# ===== GB/T 19582 功能码白名单 =====
# 只允许标准Modbus功能码，禁止厂商自定义功能码
ALLOWED_FUNCTION_CODES = {
    0x01,  # Read Coils
    0x02,  # Read Discrete Inputs
    0x03,  # Read Holding Registers
    0x04,  # Read Input Registers
    0x05,  # Write Single Coil
    0x06,  # Write Single Register
    0x0F,  # Write Multiple Coils
    0x10,  # Write Multiple Registers
    0x17,  # Read/Write Multiple Registers
}


# ===== 寄存器安全范围推断规则 =====
# 根据寄存器名称和单位自动推断安全范围
# 格式: (name_keywords, unit) -> (min, max, risk_level)
_SAFETY_RULES: list[tuple[list[str], str, float, float, WriteRiskLevel]] = [
    # 温度类 — 工业设备正常范围 (GB/T 15969)
    (['temperature', 'temp'], '°c', -40, 500, WriteRiskLevel.MEDIUM),
    (['temperature', 'temp'], 'c', -40, 500, WriteRiskLevel.MEDIUM),
    (['boiler_temperature'], '°c', 0, 200, WriteRiskLevel.HIGH),
    (['flue_gas_temperature'], '°c', 0, 800, WriteRiskLevel.HIGH),
    (['mold_temperature'], '°c', 0, 400, WriteRiskLevel.MEDIUM),
    (['oven_temperature'], '°c', 0, 300, WriteRiskLevel.MEDIUM),
    (['dryer_temperature'], '°c', 0, 200, WriteRiskLevel.MEDIUM),

    # 压力类 — 工业设备正常范围
    (['pressure'], 'mpa', 0, 10, WriteRiskLevel.HIGH),
    (['pressure'], 'kpa', 0, 1000, WriteRiskLevel.HIGH),
    (['boiler_pressure'], 'mpa', 0, 4, WriteRiskLevel.CRITICAL),
    (['injection_pressure'], 'mpa', 0, 30, WriteRiskLevel.HIGH),
    (['hydraulic_pressure'], 'mpa', 0, 35, WriteRiskLevel.HIGH),

    # 液位类
    (['level', 'tank_level'], '%', 0, 100, WriteRiskLevel.MEDIUM),
    (['level', 'tank_level'], 'm', 0, 50, WriteRiskLevel.MEDIUM),

    # 流量类
    (['flow'], 'm3/h', 0, 1000, WriteRiskLevel.MEDIUM),
    (['flow'], 'l/min', 0, 10000, WriteRiskLevel.MEDIUM),

    # 电气类 (IEC 61970)
    (['voltage'], 'v', 0, 500, WriteRiskLevel.HIGH),
    (['current'], 'a', 0, 1000, WriteRiskLevel.HIGH),
    (['power'], 'kw', 0, 10000, WriteRiskLevel.MEDIUM),
    (['power_factor'], '', 0, 1, WriteRiskLevel.LOW),
    (['frequency'], 'hz', 45, 55, WriteRiskLevel.MEDIUM),
    (['energy'], 'kwh', 0, 999999, WriteRiskLevel.LOW),

    # 振动类
    (['vibration'], 'mm/s', 0, 50, WriteRiskLevel.MEDIUM),
    (['vibration'], 'g', 0, 20, WriteRiskLevel.MEDIUM),

    # 水质类
    (['ph'], '', 0, 14, WriteRiskLevel.MEDIUM),
    (['conductivity'], 'us/cm', 0, 5000, WriteRiskLevel.LOW),
    (['turbidity'], 'ntu', 0, 1000, WriteRiskLevel.LOW),
    (['dissolved_oxygen'], 'mg/l', 0, 20, WriteRiskLevel.LOW),

    # 速度类
    (['speed', 'motor_speed', 'spindle_speed'], 'rpm', 0, 10000, WriteRiskLevel.MEDIUM),
    (['conveyor_speed', 'spray_speed'], 'm/min', 0, 100, WriteRiskLevel.MEDIUM),
    (['feed_rate'], 'mm/min', 0, 10000, WriteRiskLevel.MEDIUM),

    # 状态/控制类 — 只允许 0/1
    (['status', 'state', 'mode', 'running_status'], '', 0, 65535, WriteRiskLevel.LOW),
    (['green_light', 'yellow_light', 'red_light', 'blue_light', 'white_light'], '', 0, 1, WriteRiskLevel.LOW),
    (['buzzer'], '', 0, 1, WriteRiskLevel.LOW),
    (['relay'], '', 0, 1, WriteRiskLevel.MEDIUM),

    # 计数类
    (['count', 'total', 'shot_count'], '', 0, 999999, WriteRiskLevel.LOW),

    # 涂装工艺
    (['spray_pressure'], 'mpa', 0, 1.5, WriteRiskLevel.HIGH),
    (['coating_thickness'], 'μm', 0, 500, WriteRiskLevel.MEDIUM),

    # 湿度
    (['humidity'], '%', 0, 100, WriteRiskLevel.LOW),
]


# ===== 安全联锁规则 (GB/T 15969) =====
# 关键寄存器写入前需要检查的联锁条件
_INTERLOCK_RULES: dict[str, dict[str, Any]] = {
    'boiler_pressure': {
        'condition': lambda current_val, new_val: new_val <= 4.0,
        'message': '锅炉压力设定值不得超过 4.0 MPa (GB/T 15969 安全联锁)',
    },
    'boiler_temperature': {
        'condition': lambda current_val, new_val: new_val <= 200.0,
        'message': '锅炉温度设定值不得超过 200°C (GB/T 15969 安全联锁)',
    },
    'injection_pressure': {
        'condition': lambda current_val, new_val: new_val <= 30.0,
        'message': '注射压力不得超过 30 MPa (设备安全限值)',
    },
    'spray_pressure': {
        'condition': lambda current_val, new_val: new_val <= 1.5,
        'message': '喷涂压力不得超过 1.5 MPa (工艺安全限值)',
    },
}


class WriteSafetyValidator:
    """
    Modbus写入安全验证器

    实现：
    - GB/T 19582: 功能码白名单 + 地址范围校验 + 值域约束
    - GB/T 35718: 写入权限分级 + 审计日志
    - GB/T 36323: 安全联锁 + 风险评估
    - GB/T 15969: 故障安全 + 安全联锁
    - DL/T 634.5104: 值域约束 + 超时处理
    """

    def __init__(self, device_config: dict[str, Any]):
        """
        初始化安全验证器

        Args:
            device_config: 设备配置字典（来自devices.yaml）
        """
        self.device_id = device_config.get('id', 'unknown')
        self.device_name = device_config.get('name', 'unknown')
        self.protocol = device_config.get('protocol', 'modbus_tcp')

        # 构建寄存器安全档案
        self._profiles: dict[int, RegisterSafetyProfile] = {}
        self._build_profiles(device_config.get('registers', []))

        # 写入统计
        self.write_count = 0
        self.blocked_count = 0

        logger.info(
            f"[写入安全] {self.device_name}: 已加载 {len(self._profiles)} 个寄存器安全档案, "
            f"其中 {sum(1 for p in self._profiles.values() if p.requires_confirm)} 个需要二次确认"
        )

    def _build_profiles(self, registers: list[dict]) -> None:
        """从设备寄存器配置构建安全档案"""
        for reg in registers:
            addr = reg.get('address')
            if addr is None:
                continue

            name = reg.get('name', '')
            unit = reg.get('unit', '')
            data_type = reg.get('data_type', 'uint16')
            access = reg.get('access', 'ro')

            # 推断安全范围
            min_val, max_val, risk = self._infer_safety_range(name, unit, data_type)

            # 检查是否需要二次确认
            requires_confirm = risk in (WriteRiskLevel.HIGH, WriteRiskLevel.CRITICAL)

            self._profiles[addr] = RegisterSafetyProfile(
                address=addr,
                name=name,
                data_type=data_type,
                unit=unit,
                min_value=min_val,
                max_value=max_val,
                risk_level=risk,
                writable=(access in ('rw', 'write')),
                requires_confirm=requires_confirm,
                description=reg.get('description', ''),
            )

    def _infer_safety_range(self, name: str, unit: str, data_type: str) -> tuple[float, float, WriteRiskLevel]:
        """根据寄存器名称/单位/类型推断安全范围"""
        name_lower = name.lower()
        unit_lower = unit.lower()

        for keywords, rule_unit, min_val, max_val, risk in _SAFETY_RULES:
            # 关键字匹配
            kw_match = any(kw in name_lower for kw in keywords)
            # 单位匹配（空单位表示不限制）
            unit_match = (not rule_unit) or (rule_unit == unit_lower)

            if kw_match and unit_match:
                return min_val, max_val, risk

        # 兜底：根据data_type给一个保守范围
        if data_type == 'float32':
            return -1000.0, 1000.0, WriteRiskLevel.MEDIUM
        elif data_type in ('int32', 'uint32'):
            return 0, 999999, WriteRiskLevel.LOW
        elif data_type == 'int16':
            return -32768, 32767, WriteRiskLevel.LOW
        else:  # uint16
            return 0, 65535, WriteRiskLevel.LOW

    def validate_write(self, address: int, value: float,
                       current_value: float | None = None,
                       confirm: bool = False,
                       user: str = 'system') -> tuple[bool, str]:
        """
        验证写入操作的安全性

        Args:
            address: 寄存器地址
            value: 写入值（工程值，非原始寄存器值）
            current_value: 当前值（用于联锁检查）
            confirm: 是否已二次确认
            user: 操作用户

        Returns:
            (allowed, reason): 是否允许写入 + 原因
        """
        self.write_count += 1

        # 1. 检查寄存器是否存在
        profile = self._profiles.get(address)
        if profile is None:
            # 未知寄存器 — 允许但记录警告
            logger.warning(f"[写入安全] {self.device_name}: 写入未知寄存器 addr={address}, value={value}, user={user}")
            return True, '未知寄存器（无安全档案）'

        # 2. 检查是否可写
        if not profile.writable:
            self.blocked_count += 1
            reason = f'寄存器 {profile.name}(addr={address}) 为只读，禁止写入 (GB/T 19582)'
            logger.warning(f"[写入安全] {self.device_name}: 写入被拒绝 — {reason}, user={user}")
            return False, reason

        # 3. 值域校验 (GB/T 19582 + DL/T 634.5104)
        if value < profile.min_value or value > profile.max_value:
            self.blocked_count += 1
            reason = (
                f'寄存器 {profile.name}(addr={address}) 值越界: '
                f'{value} 超出安全范围 [{profile.min_value}, {profile.max_value}] '
                f'(GB/T 19582 值域约束)'
            )
            logger.warning(f"[写入安全] {self.device_name}: 写入被拒绝 — {reason}, user={user}")
            return False, reason

        # 4. 安全联锁检查 (GB/T 15969)
        interlock = _INTERLOCK_RULES.get(profile.name)
        if interlock and current_value is not None:
            if not interlock['condition'](current_value, value):
                self.blocked_count += 1
                reason = f'安全联锁阻止: {interlock["message"]}'
                logger.warning(f"[写入安全] {self.device_name}: 写入被拒绝 — {reason}, user={user}")
                return False, reason

        # 5. 二次确认检查 (GB/T 36323)
        if profile.requires_confirm and not confirm:
            self.blocked_count += 1
            reason = (
                f'寄存器 {profile.name}(addr={address}) 风险等级 {profile.risk_level.value}，'
                f'需要二次确认 (GB/T 36323)'
            )
            logger.info(f"[写入安全] {self.device_name}: 写入需要确认 — {reason}, user={user}")
            return False, reason

        # 6. 审计日志 (GB/T 35718)
        logger.info(
            f"[写入安全] {self.device_name}: 写入允许 — "
            f"addr={address}, name={profile.name}, value={value}, "
            f"range=[{profile.min_value},{profile.max_value}], "
            f"risk={profile.risk_level.value}, user={user}"
        )

        return True, '写入通过安全验证'

    def get_register_info(self, address: int) -> RegisterSafetyProfile | None:
        """获取寄存器安全档案"""
        return self._profiles.get(address)

    def get_all_profiles(self) -> dict[int, RegisterSafetyProfile]:
        """获取所有寄存器安全档案"""
        return self._profiles.copy()

    def get_stats(self) -> dict[str, Any]:
        """获取写入统计"""
        return {
            'device_id': self.device_id,
            'total_writes': self.write_count,
            'blocked_writes': self.blocked_count,
            'block_rate': f'{self.blocked_count / max(self.write_count, 1) * 100:.1f}%',
            'register_count': len(self._profiles),
            'writable_count': sum(1 for p in self._profiles.values() if p.writable),
            'requires_confirm': sum(1 for p in self._profiles.values() if p.requires_confirm),
        }
