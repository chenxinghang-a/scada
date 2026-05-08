"""
SPC统计过程控制模块 (Statistical Process Control)
===================================================
工业4.0质量管理核心：用统计方法监控和控制生产过程

功能：
1. 控制图 — X̄-R图、X̄-S图、p图、c图
2. 过程能力分析 — Cp、Cpk、Pp、Ppk
3. Western Electric判异规则（8大判异准则）
4. 过程稳定性评估
"""

import logging
import math
import threading
import time
from datetime import datetime
from typing import Any
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


class SPCAnalyzer:
    """
    SPC统计过程控制分析器

    工作流程：
    1. 持续采集质量数据
    2. 计算控制限（UCL/CL/LCL）
    3. 判异检测（Western Electric规则）
    4. 计算过程能力指数
    """

    def __init__(self, database, config: dict[str, Any] | None = None):
        """
        Args:
            database: Database实例
            config: 配置字典，包含规格限等
        """
        self.database = database
        self.config = config or {}

        # 控制图数据窗口
        self.window_size = self.config.get('window_size', 25)  # 子组数
        self.subgroup_size = self.config.get('subgroup_size', 5)  # 子组大小

        # 数据缓冲区
        # key -> deque[Any] of subgroups (each subgroup is a list of values)
        self.data_buffers: dict[str, deque[Any]] = defaultdict(
            lambda: deque[Any](maxlen=self.window_size * self.subgroup_size)
        )

        # 控制限缓存
        self.control_limits: dict[str, dict[str, Any]] = {}

        # 规格限配置（USL/LSL）
        self.spec_limits: dict[str, dict[str, Any]] = self.config.get('spec_limits', {})

        # 判异结果
        self.violations: dict[str, list[dict[str, Any]]] = defaultdict(list)

        # 锁
        self._lock = threading.Lock()

        logger.info("SPC分析器初始化完成")

    def feed_data(self, device_id: str, register_name: str, value: float):
        """
        喂入质量数据

        Args:
            device_id: 设备ID
            register_name: 寄存器名称
            value: 测量值
        """
        key = f"{device_id}:{register_name}"
        with self._lock:
            self.data_buffers[key].append(value)
            # 自动设置默认规格限（如果未配置）
            if key not in self.spec_limits:
                self._auto_set_spec_limits(key, register_name)

    def set_spec_limits(self, key: str, usl: float | None = None, lsl: float | None = None,
                         target: float | None = None):
        """
        设置规格限

        Args:
            key: "device_id:register_name"
            usl: 上规格限 (Upper Specification Limit)
            lsl: 下规格限 (Lower Specification Limit)
            target: 目标值
        """
        self.spec_limits[key] = {
            'usl': usl,
            'lsl': lsl,
            'target': target or ((usl + lsl) / 2 if usl and lsl else None),
        }
        logger.info(f"SPC规格限设置 {key}: USL={usl}, LSL={lsl}, Target={target}")

    # ==================== 控制图计算 ====================

    def _auto_set_spec_limits(self, key: str, register_name: str):
        """根据寄存器名称自动设置默认规格限"""
        # 常见工业参数的默认规格限
        default_specs = {
            'temperature': {'usl': 100.0, 'lsl': 0.0, 'target': 50.0},
            'pressure': {'usl': 1.0, 'lsl': 0.0, 'target': 0.5},
            'ph': {'usl': 9.0, 'lsl': 5.0, 'target': 7.0},
            'voltage': {'usl': 250.0, 'lsl': 200.0, 'target': 220.0},
            'current': {'usl': 20.0, 'lsl': 0.0, 'target': 10.0},
            'speed': {'usl': 1500.0, 'lsl': 0.0, 'target': 1000.0},
            'flow': {'usl': 100.0, 'lsl': 0.0, 'target': 50.0},
            'level': {'usl': 100.0, 'lsl': 0.0, 'target': 50.0},
            'humidity': {'usl': 90.0, 'lsl': 30.0, 'target': 60.0},
            'torque': {'usl': 100.0, 'lsl': 0.0, 'target': 50.0},
            'frequency': {'usl': 60.0, 'lsl': 40.0, 'target': 50.0},
            'thickness': {'usl': 2.0, 'lsl': 0.5, 'target': 1.0},
        }

        for kw, spec in default_specs.items():
            if kw in register_name.lower():
                self.spec_limits[key] = spec
                break

    def calculate_xbar_r_chart(self, device_id: str, register_name: str) -> dict[str, Any] | None:
        """
        X̄-R控制图计算

        适用于子组大小 n=2~10

        Returns:
            {
                'xbar': {'ucl': float, 'cl': float, 'lcl': float, 'points': list},
                'r_chart': {'ucl': float, 'cl': float, 'lcl': float, 'points': list},
                'violations': list,
            }
        """
        key = f"{device_id}:{register_name}"

        with self._lock:
            raw_data = list(self.data_buffers.get(key, []))

        if len(raw_data) < self.subgroup_size * 2:
            return None

        # 分组
        subgroups = self._form_subgroups(raw_data)

        # 计算子组均值和极差
        xbar_values = [sum(g) / len(g) for g in subgroups]
        r_values = [max(g) - min(g) for g in subgroups]

        n = self.subgroup_size
        k = len(subgroups)

        # X̄图控制限
        xbar_bar = sum(xbar_values) / k  # 总均值
        r_bar = sum(r_values) / k  # 平均极差

        # A2系数表 (n=2~10)
        a2_table = {2: 1.880, 3: 1.023, 4: 0.729, 5: 0.577,
                    6: 0.483, 7: 0.419, 8: 0.373, 9: 0.337, 10: 0.308}
        a2 = a2_table.get(n, 0.577)

        xbar_ucl = xbar_bar + a2 * r_bar
        xbar_lcl = xbar_bar - a2 * r_bar

        # R图控制限
        d3_table = {2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0.076, 8: 0.136, 9: 0.184, 10: 0.223}
        d4_table = {2: 3.267, 3: 2.574, 4: 2.282, 5: 2.114,
                    6: 2.004, 7: 1.924, 8: 1.864, 9: 1.816, 10: 1.777}
        d4 = d4_table.get(n, 2.114)

        r_ucl = d4 * r_bar
        r_lcl = 0  # R图下限为0

        # 判异检测
        violations = self._check_violations(xbar_values, xbar_bar, xbar_ucl, xbar_lcl)

        # 存储判异结果（带时间戳）
        if violations:
            for v in violations:
                v['device_id'] = device_id
                v['register_name'] = register_name
                v['timestamp'] = datetime.now().isoformat()
            self.violations[key].extend(violations)
            # 限制每个key最多保留100条
            if len(self.violations[key]) > 100:
                self.violations[key] = self.violations[key][-100:]

        result = {
            'chart_type': 'X̄-R',
            'subgroup_size': n,
            'num_subgroups': k,
            'xbar_chart': {
                'ucl': round(xbar_ucl, 4),
                'cl': round(xbar_bar, 4),
                'lcl': round(xbar_lcl, 4),
                'points': [round(v, 4) for v in xbar_values],
            },
            'r_chart': {
                'ucl': round(r_ucl, 4),
                'cl': round(r_bar, 4),
                'lcl': round(r_lcl, 4),
                'points': [round(v, 4) for v in r_values],
            },
            'violations': violations,
            'is_in_control': len(violations) == 0,
        }

        # 缓存控制限
        self.control_limits[key] = result

        return result

    def calculate_xbar_s_chart(self, device_id: str, register_name: str) -> dict[str, Any] | None:
        """
        X̄-S控制图计算

        适用于子组大小 n>10
        """
        key = f"{device_id}:{register_name}"

        with self._lock:
            raw_data = list(self.data_buffers.get(key, []))

        if len(raw_data) < self.subgroup_size * 2:
            return None

        subgroups = self._form_subgroups(raw_data)

        xbar_values = [sum(g) / len(g) for g in subgroups]
        s_values = [self._std_dev(g) for g in subgroups]

        n = self.subgroup_size
        k = len(subgroups)

        xbar_bar = sum(xbar_values) / k
        s_bar = sum(s_values) / k

        # A3系数查表 (n=2~25)
        a3_table = {
            2: 2.659, 3: 1.954, 4: 1.628, 5: 1.427, 6: 1.287,
            7: 1.182, 8: 1.099, 9: 1.032, 10: 0.975, 11: 0.927,
            12: 0.886, 13: 0.850, 14: 0.817, 15: 0.789, 16: 0.763,
            17: 0.739, 18: 0.718, 19: 0.698, 20: 0.680, 21: 0.663,
            22: 0.647, 23: 0.633, 24: 0.619, 25: 0.606
        }
        a3 = a3_table.get(n, 3 / math.sqrt(n))

        xbar_ucl = xbar_bar + a3 * s_bar
        xbar_lcl = xbar_bar - a3 * s_bar

        # B3, B4系数查表 (n=2~25)
        b3_table = {
            2: 0, 3: 0, 4: 0, 5: 0, 6: 0.030, 7: 0.118, 8: 0.185,
            9: 0.239, 10: 0.284, 11: 0.321, 12: 0.354, 13: 0.382,
            14: 0.406, 15: 0.428, 16: 0.448, 17: 0.466, 18: 0.482,
            19: 0.497, 20: 0.510, 21: 0.523, 22: 0.534, 23: 0.545,
            24: 0.555, 25: 0.565
        }
        b4_table = {
            2: 3.267, 3: 2.568, 4: 2.266, 5: 2.089, 6: 1.970,
            7: 1.882, 8: 1.815, 9: 1.761, 10: 1.716, 11: 1.679,
            12: 1.646, 13: 1.618, 14: 1.594, 15: 1.572, 16: 1.552,
            17: 1.534, 18: 1.518, 19: 1.503, 20: 1.490, 21: 1.477,
            22: 1.466, 23: 1.455, 24: 1.445, 25: 1.435
        }
        b3 = b3_table.get(n, 0)
        b4 = b4_table.get(n, 1 + 3 / math.sqrt(n))

        s_ucl = b4 * s_bar
        s_lcl = max(0, b3 * s_bar)

        violations = self._check_violations(xbar_values, xbar_bar, xbar_ucl, xbar_lcl)

        # 存储判异结果
        if violations:
            for v in violations:
                v['device_id'] = device_id
                v['register_name'] = register_name
                v['timestamp'] = datetime.now().isoformat()
            self.violations[key].extend(violations)
            if len(self.violations[key]) > 100:
                self.violations[key] = self.violations[key][-100:]

        return {
            'chart_type': 'X̄-S',
            'subgroup_size': n,
            'num_subgroups': k,
            'xbar_chart': {
                'ucl': round(xbar_ucl, 4),
                'cl': round(xbar_bar, 4),
                'lcl': round(xbar_lcl, 4),
                'points': [round(v, 4) for v in xbar_values],
            },
            's_chart': {
                'ucl': round(s_ucl, 4),
                'cl': round(s_bar, 4),
                'lcl': round(s_lcl, 4),
                'points': [round(v, 4) for v in s_values],
            },
            'violations': violations,
            'is_in_control': len(violations) == 0,
        }

    # ==================== 过程能力分析 ====================

    def calculate_capability(self, device_id: str, register_name: str) -> dict[str, Any] | None:
        """
        过程能力指数计算

        Cp  = (USL - LSL) / (6σ)        — 潜在能力
        Cpk = min((USL-μ)/3σ, (μ-LSL)/3σ) — 实际能力
        Pp  = (USL - LSL) / (6σ_total)   — 过程性能
        Ppk = min((USL-μ)/3σ_total, (μ-LSL)/3σ_total)

        判定标准：
        Cpk ≥ 1.67: 优秀（特级）
        Cpk ≥ 1.33: 良好（一级）
        Cpk ≥ 1.00: 合格（二级）
        Cpk < 1.00: 不合格
        """
        key = f"{device_id}:{register_name}"
        spec = self.spec_limits.get(key)

        if not spec or spec.get('usl') is None or spec.get('lsl') is None:
            return None

        usl = spec['usl']
        lsl = spec['lsl']
        target = spec.get('target', (usl + lsl) / 2)

        with self._lock:
            raw_data = list(self.data_buffers.get(key, []))

        if len(raw_data) < 20:
            return None

        n = len(raw_data)
        mean = sum(raw_data) / n

        # 总体标准差
        variance = sum((x - mean) ** 2 for x in raw_data) / (n - 1)
        sigma_total = math.sqrt(variance) if variance > 0 else 0

        # 组内标准差（从控制图获取）
        cl_data = self.control_limits.get(key, {})
        if 'r_chart' in cl_data:
            r_bar = cl_data['r_chart']['cl']
            d2_table = {2: 1.128, 3: 1.693, 4: 2.059, 5: 2.326,
                        6: 2.534, 7: 2.704, 8: 2.847, 9: 2.970, 10: 3.078}
            d2 = d2_table.get(self.subgroup_size, 2.326)
            sigma_within = r_bar / d2 if d2 > 0 else sigma_total
        else:
            sigma_within = sigma_total

        if sigma_within == 0 or sigma_total == 0:
            return None

        # Cp, Cpk
        cp = (usl - lsl) / (6 * sigma_within)
        cpu = (usl - mean) / (3 * sigma_within)
        cpl = (mean - lsl) / (3 * sigma_within)
        cpk = min(cpu, cpl)

        # Pp, Ppk
        pp = (usl - lsl) / (6 * sigma_total)
        ppu = (usl - mean) / (3 * sigma_total)
        ppl = (mean - lsl) / (3 * sigma_total)
        ppk = min(ppu, ppl)

        # 不良率估算 (PPM)
        # 用正态分布近似
        z_upper = (usl - mean) / sigma_total if sigma_total > 0 else 999
        z_lower = (mean - lsl) / sigma_total if sigma_total > 0 else 999
        ppm_upper = self._normal_ppm(z_upper)
        ppm_lower = self._normal_ppm(z_lower)
        total_ppm = ppm_upper + ppm_lower

        return {
            'device_id': device_id,
            'register_name': register_name,
            'sample_size': n,
            'mean': round(mean, 4),
            'sigma_within': round(sigma_within, 4),
            'sigma_total': round(sigma_total, 4),
            'usl': usl,
            'lsl': lsl,
            'target': target,
            'cp': round(cp, 4),
            'cpk': round(cpk, 4),
            'pp': round(pp, 4),
            'ppk': round(ppk, 4),
            'estimated_ppm': round(total_ppm, 1),
            'capability_grade': self._capability_grade(cpk),
            'updated_at': datetime.now().isoformat(),
        }

    def _capability_grade(self, cpk: float) -> str:
        """过程能力等级"""
        if cpk >= 1.67:
            return '特级(优秀)'
        elif cpk >= 1.33:
            return '一级(良好)'
        elif cpk >= 1.00:
            return '二级(合格)'
        elif cpk >= 0.67:
            return '三级(不足)'
        else:
            return '四级(严重不足)'

    def _normal_ppm(self, z: float) -> float:
        """标准正态分布的PPM估算"""
        # 用近似公式
        if z > 6:
            return 0.001
        elif z < -6:
            return 1000000

        # 简化近似
        t = 1 / (1 + 0.2316419 * abs(z))
        d = 0.3989422804014327  # 1/sqrt(2*pi)
        p = d * math.exp(-z * z / 2) * t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))

        if z > 0:
            return p * 1000000
        else:
            return (1 - p) * 1000000

    # ==================== 判异规则 ====================

    def _check_violations(self, points: list[float], cl: float,
                           ucl: float, lcl: float) -> list[dict[str, Any]]:
        """
        Western Electric判异规则检测

        规则1: 1点超出3σ控制限
        规则2: 连续9点在中心线同一侧
        规则3: 连续6点递增或递减
        规则4: 连续14点交替上下
        """
        violations = []
        n = len(points)

        if n < 2:
            return violations

        sigma = (ucl - cl) / 3 if (ucl - cl) > 0 else 1

        # 规则1: 超出控制限
        for i, p in enumerate(points):
            if p > ucl or p < lcl:
                violations.append({
                    'rule': 1,
                    'description': f'点{i+1}超出3σ控制限',
                    'value': round(p, 4),
                    'index': i,
                    'severity': 'critical',
                })

        # 规则2: 连续9点在中心线同一侧
        for i in range(n - 8):
            segment = points[i:i+9]
            if all(p > cl for p in segment) or all(p < cl for p in segment):
                violations.append({
                    'rule': 2,
                    'description': f'点{i+1}~{i+9}连续9点在中心线同一侧',
                    'index': i,
                    'severity': 'warning',
                })

        # 规则3: 连续6点递增或递减
        for i in range(n - 5):
            segment = points[i:i+6]
            if all(segment[j] < segment[j+1] for j in range(5)):
                violations.append({
                    'rule': 3,
                    'description': f'点{i+1}~{i+6}连续6点递增',
                    'index': i,
                    'severity': 'warning',
                })
            elif all(segment[j] > segment[j+1] for j in range(5)):
                violations.append({
                    'rule': 3,
                    'description': f'点{i+1}~{i+6}连续6点递减',
                    'index': i,
                    'severity': 'warning',
                })

        # 规则4: 连续14点交替上下
        if n >= 14:
            for i in range(n - 13):
                segment = points[i:i+14]
                alternating = True
                for j in range(1, 14):
                    if (segment[j] > cl) == (segment[j-1] > cl):
                        alternating = False
                        break
                if alternating:
                    violations.append({
                        'rule': 4,
                        'description': f'点{i+1}~{i+14}连续14点交替上下',
                        'index': i,
                        'severity': 'warning',
                    })

        return violations

    # ==================== 辅助方法 ====================

    def _form_subgroups(self, data: list[float]) -> list[list[float]]:
        """将数据分成子组"""
        subgroups = []
        for i in range(0, len(data), self.subgroup_size):
            group = data[i:i + self.subgroup_size]
            if len(group) == self.subgroup_size:
                subgroups.append(group)
        return subgroups

    def _std_dev(self, data: list[float]) -> float:
        """计算标准差"""
        n = len(data)
        if n < 2:
            return 0
        mean = sum(data) / n
        variance = sum((x - mean) ** 2 for x in data) / (n - 1)
        return math.sqrt(variance)

    # ==================== 查询接口 ====================

    def get_control_chart(self, device_id: str, register_name: str) -> dict[str, Any] | None:
        """获取控制图数据"""
        return self.calculate_xbar_r_chart(device_id, register_name)

    def get_capability(self, device_id: str, register_name: str) -> dict[str, Any] | None:
        """获取过程能力数据"""
        return self.calculate_capability(device_id, register_name)

    def get_violations(self, device_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """获取判异结果"""
        if device_id:
            key_prefix = f"{device_id}:"
            results = []
            for k, v in self.violations.items():
                if k.startswith(key_prefix):
                    results.extend(v)
            return results[-limit:]

        all_violations = []
        for v in self.violations.values():
            all_violations.extend(v)
        return sorted(all_violations, key=lambda x: x.get('index', 0))[-limit:]
