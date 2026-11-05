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

from core.safe_numeric import try_finite_float

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

        # 判异去重状态
        # 控制图每计算一次都会**重扫整个窗口**，若直接 extend，同一条违规会被
        # 每个计算周期重复计入，越跑越多（并且会在锁外写入）。
        # 这里用「绝对采样位置区间」做指纹：窗口滑动时，同一条物理违规的绝对
        # 区间不变 → 只计入一次；真正的新数据才产生新指纹。
        self._total_fed: dict[str, int] = defaultdict(int)
        self._violation_seen: dict[str, set[tuple]] = defaultdict(set)
        self._violation_fingerprint_cap = 4096  # 指纹集合上限（超出后清空重置）

        # 非有限值告警节流（清单 046）：key -> 上次告警时间戳
        self._nonfinite_warn_at: dict[str, float] = {}

        # 锁
        self._lock = threading.Lock()

        logger.info("SPC分析器初始化完成")

    def forget_device(self, device_id: str) -> bool:
        """设备被删除时清掉它的 SPC 控制限/规格限/违规记录（round 310）。

        ⚠️ 这三个容器的 key 是 **`device_id:register_name` 复合键**，不是裸
        device_id —— 直接 `pop(device_id)` 一个都删不掉（而且不会报错，
        是最容易写成"看起来修了其实没删"的形态）。这里按前缀匹配。

        设备删掉后：`get_violations()` / 控制图接口仍会返回它的数据，
        报表里出现不存在的设备；`_auto_set_spec_limits` 也不会再给它重算，
        旧控制限**永久冻结**。
        """
        prefix = f'{device_id}:'
        removed = False
        # round 369：**必须持锁**（与 feed_data / _run_analysis 共用这批容器）
        with self._lock:
            for container in (self.control_limits, self.spec_limits, self.violations):
                for key in [k for k in list(container) if k.startswith(prefix)]:
                    container.pop(key, None)
                    removed = True

            # round 355：**还有三个同样用复合键的容器被漏掉了**。
            # `data_buffers`（测量值缓冲，deque）与 `_total_fed`（喂入计数）
            # 不清 → 删掉的设备的样本继续占内存，且重新添加同名设备时
            # **首轮判异直接继承旧样本**（控制图一上来就报警或一上来就"样本充足"）；
            # `_violation_seen`（已报过的判异指纹）不清 → 同一违规在新设备上不再报。
            for container in (self.data_buffers, self._total_fed, self._violation_seen,
                              self._nonfinite_warn_at):
                for key in [k for k in list(container) if k.startswith(prefix)]:
                    container.pop(key, None)
                    removed = True

        if removed:
            logger.info("SPC 已清理已删除设备的控制限与缓冲: %s", device_id)
        return removed

    def feed_data(self, device_id: str, register_name: str, value: float) -> bool:
        """
        喂入质量数据

        Args:
            device_id: 设备ID
            register_name: 寄存器名称
            value: 测量值

        Returns:
            是否被接受（非有限值 / 非数值一律拒绝，不入缓冲区）
        """
        key = f"{device_id}:{register_name}"

        # 清单 046：bool / NaN / ±Inf 必须在入口挡住。
        # 缓冲区是**滑动窗口**，一个 NaN 进来后要等 maxlen 个正常点才被挤出去；
        # 期间 sum()/σ/控制限全变 NaN，而判异规则里全是 `x > cl` 这类比较 ——
        # NaN 与任何数比较都是 False，于是控制图会**静默显示"过程受控"**，
        # 比直接报错危险得多（Cpk 也会变成 NaN 写进看板）。
        # bool 单独挡：`float(True) == 1.0`，一个开关量混进温度序列会直接把
        # 均值拉到 0 附近（`try_finite_float` 本身不排斥 bool，与 `try_int` 同口径）。
        num = None if isinstance(value, bool) else try_finite_float(value)
        if num is None:
            now = time.time()
            last = self._nonfinite_warn_at.get(key, 0.0)
            if now - last >= 60.0:  # 同一 key 每分钟只记一条，避免污染源刷爆日志
                self._nonfinite_warn_at[key] = now
                logger.warning(
                    "SPC 拒绝非有限测量值 %s=%r（register=%s），本次不入缓冲",
                    device_id, value, register_name,
                )
            return False

        with self._lock:
            self.data_buffers[key].append(num)
            self._total_fed[key] += 1
            # 自动设置默认规格限（如果未配置）
            if key not in self.spec_limits:
                self._auto_set_spec_limits(key, register_name)
        return True

    def set_spec_limits(self, key: str, usl: float | None = None, lsl: float | None = None,
                         target: float | None = None):
        """
        设置规格限

        Args:
            key: "device_id:register_name"
            usl: 上规格限 (Upper Specification Limit)
            lsl: 下规格限 (Lower Specification Limit)
            target: 目标值

        Raises:
            ValueError: 传入的限值不是有限数值（NaN/Inf 会污染 Cpk 与 PPM）
        """
        # 清单 046：这里原来直接落库，且用 `target or ...` / `usl and lsl` 做
        # 真值判断 —— 两个独立问题：
        # 1) `lsl=0.0`（电压下限、振动下限这类**最常见**的配置）是 falsy，
        #    目标值算不出来、`usl and lsl` 也判成"没配全"，target 变 None；
        # 2) usl/lsl/target 若为 NaN/Inf（YAML 的 `.nan`/`.inf` 是规范内字面量），
        #    `usl - lsl` 直接是 NaN，Cpk/PPM 全部静默变 NaN。
        usl_v = self._spec_value(usl, 'usl')
        lsl_v = self._spec_value(lsl, 'lsl')
        target_v = self._spec_value(target, 'target')

        if target_v is None and usl_v is not None and lsl_v is not None:
            target_v = (usl_v + lsl_v) / 2
        if usl_v is not None and lsl_v is not None and usl_v <= lsl_v:
            raise ValueError(f'Invalid spec limits: usl({usl_v}) 必须大于 lsl({lsl_v})')

        self.spec_limits[key] = {
            'usl': usl_v,
            'lsl': lsl_v,
            'target': target_v,
        }
        logger.info(f"SPC规格限设置 {key}: USL={usl_v}, LSL={lsl_v}, Target={target_v}")

    @staticmethod
    def _spec_value(value: float | None, name: str) -> float | None:
        """规格限入参归一化：None 原样返回，非有限值抛 ValueError。"""
        if value is None:
            return None
        num = try_finite_float(value)
        if num is None:
            raise ValueError(f'Invalid {name}: must be a finite number (收到 {value!r})')
        return num

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

    # ==================== 判异结果记录 ====================

    def _violation_fingerprint(self, violation: dict[str, Any],
                                base_index: int) -> tuple:
        """
        生成判异的稳定指纹（rule + 绝对采样位置区间）

        窗口每滑一格，窗口内下标整体 -1，若用下标做指纹会持续"变新" → 重复计入。
        因此这里把窗口下标换算成绝对采样序号：

            绝对序号 = max(0, 已喂入总数 - 当前窗口长度) + 窗口内下标

        同一条物理违规在滑窗过程中绝对区间不变，指纹稳定。
        """
        abs_start = base_index + violation.get('index', 0)
        span = violation.get('span', 1)
        return (violation.get('rule'), abs_start, abs_start + span - 1)

    def _align_window(self, raw_data: list[float], fed: int,
                      subgroup_size: int) -> tuple[list[float], int]:
        """把缓冲区起点对齐到「按绝对位置切分」的子组边界。

        round 300：缓冲区是定长 deque，每喂 1 个点就从头部挤掉 1 个；而
        `_form_subgroups` 恒从 `data[0]` 按 n 切 —— 于是窗口每滑 1 格，
        所有子组的**内容边界**整体错位，同一个物理子组在两轮里根本不是同一个东西。

        指纹只能靠「绝对采样序号」稳：把开头不足一个子组的残片丢掉，
        让 `aligned[0]` 的绝对位置是 n 的倍数，子组序号才有稳定身份。

        返回 (对齐后的数据, 第一个子组的绝对子组序号)。

        原来的写法是 `base_index = 已喂入点数 - 子组数`：**点数**与**子组数**
        两个单位直接相减，且没做对齐。默认 n=5 时每来 1 个新点 base_index +1、
        而子组边界要 5 个点才移一格 —— 同一条物理违规每次采样都换指纹，
        重复计入，100 条上限被它自己刷满，真正的违规反而被挤出去。
        """
        dropped = max(0, fed - len(raw_data))       # 已被挤出的点数
        pad = (-dropped) % subgroup_size            # 让 aligned[0] 落在子组边界
        aligned = raw_data[pad:] if pad else raw_data
        base = (dropped + pad) // subgroup_size     # 首个子组的绝对序号
        return aligned, base

    def _snapshot(self, key: str) -> tuple[list[float], int]:
        """取缓冲区快照，并在读侧再剔除一次非有限值（清单 046）。

        `feed_data` 已经挡了 NaN/Inf，但缓冲区还有别的写入路径：历史数据
        回填、`tsdb_adapter` 的批量导入、测试/运维脚本直接 append。读侧不设防
        的话，一个 NaN 就能让整窗控制限变成 NaN，判异规则因 `NaN > x == False`
        静默失效（显示"受控"），Cpk 也变成 NaN 写进看板。
        """
        with self._lock:
            raw = list(self.data_buffers.get(key, []))
            fed = self._total_fed.get(key, 0)
        clean = []
        for x in raw:
            if isinstance(x, bool):  # True/1 会被当成 1.0 混进均值
                continue
            v = try_finite_float(x)
            if v is not None:
                clean.append(v)
        return clean, fed

    def _record_violations(self, key: str, violations: list[dict[str, Any]],
                            device_id: str, register_name: str,
                            base_index: int) -> list[dict[str, Any]]:
        """
        记录本轮判异结果 —— **必须在 self._lock 内调用**。

        只写入指纹未出现过的判异（去重），并返回本轮新增的部分。
        这样 `get_violations()` 不会因为反复重扫窗口而线性膨胀。

        `base_index` 必须是**第一个子组的绝对子组序号**（由 `_align_window`
        给出），不是窗口长度、更不是已喂入点数 —— 单位错会让指纹每轮变一次。
        """
        if not violations:
            return []

        seen = self._violation_seen[key]
        if len(seen) > self._violation_fingerprint_cap:
            seen.clear()

        now_iso = datetime.now().isoformat()
        fresh = []
        for v in violations:
            fp = self._violation_fingerprint(v, base_index)
            if fp in seen:
                continue
            seen.add(fp)
            v['device_id'] = device_id
            v['register_name'] = register_name
            v['timestamp'] = now_iso
            v['abs_index'] = fp[1]
            fresh.append(v)

        if fresh:
            self.violations[key].extend(fresh)
            # 限制每个key最多保留100条
            if len(self.violations[key]) > 100:
                self.violations[key] = self.violations[key][-100:]

        return fresh

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
        n = self.subgroup_size

        # 清单 046：R 图的 A2/D3/D4 系数只到 n=10。超过时不能靠
        # `a2_table.get(n, 0.577)` 静默套用 n=5 的系数（控制限过窄 → 正常波动
        # 被成批判成异常），直接走 X̄-S 图（A3/B3/B4 覆盖 n=2~25）。
        # 返回结构里的 chart_type 会如实写成 'X̄-S'。
        if n > 10:
            logger.info("子组大小 %d > 10，X̄-R 图系数无定义，改用 X̄-S 图: %s", n, key)
            return self.calculate_xbar_s_chart(device_id, register_name)

        raw_data, fed = self._snapshot(key)

        if len(raw_data) < n * 2:
            return None

        # 分组：必须先对齐到**绝对子组边界**，否则窗口每滑 1 点、
        # 子组内容就整体错位，指纹随之每轮变一次（round 300，见 _align_window）
        aligned, base_subgroup = self._align_window(raw_data, fed, n)
        subgroups = self._form_subgroups(aligned)
        if len(subgroups) < 2:
            return None

        # 计算子组均值和极差
        xbar_values = [sum(g) / len(g) for g in subgroups]
        r_values = [max(g) - min(g) for g in subgroups]

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
        # d3 是 R 图**下限**系数：n<=6 时 d3=0（此时下限为 0 是标准做法），
        # n>=7 时 d3>0，下限必须是正数。
        # 原先 d3_table 定义了却从未使用、r_lcl 直接硬编码为 0 —— 于是 n>=7 时
        # 「极差过小」这类异常永远检不出来：数据被抹平、测量分辨率不足、
        # 采样被平滑处理，在 R 图上都表现为 R 偏小。
        d3_table = {2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0.076, 8: 0.136, 9: 0.184, 10: 0.223}
        d4_table = {2: 3.267, 3: 2.574, 4: 2.282, 5: 2.114,
                    6: 2.004, 7: 1.924, 8: 1.864, 9: 1.816, 10: 1.777}
        d3 = d3_table.get(n, 0)
        d4 = d4_table.get(n, 2.114)

        r_ucl = d4 * r_bar
        r_lcl = max(0.0, d3 * r_bar)

        # 判异检测
        violations = self._check_violations(xbar_values, xbar_bar, xbar_ucl, xbar_lcl)

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

        # 判异结果去重 + 控制限缓存：同一把锁内原子完成
        with self._lock:
            self._record_violations(key, violations, device_id, register_name,
                                    base_subgroup)
            self.control_limits[key] = result

        return result

    def calculate_xbar_s_chart(self, device_id: str, register_name: str) -> dict[str, Any] | None:
        """
        X̄-S控制图计算

        适用于子组大小 n>10
        """
        key = f"{device_id}:{register_name}"
        n = self.subgroup_size

        raw_data, fed = self._snapshot(key)

        if len(raw_data) < n * 2:
            return None

        # 对齐到绝对子组边界（round 300，理由见 _align_window）
        aligned, base_subgroup = self._align_window(raw_data, fed, n)
        subgroups = self._form_subgroups(aligned)
        if len(subgroups) < 2:
            return None

        xbar_values = [sum(g) / len(g) for g in subgroups]
        s_values = [self._std_dev(g) for g in subgroups]

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

        result = {
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

        # 清单 046：判异去重 + 控制限缓存，与 X̄-R 图在同一把锁内原子完成。
        # 原来只记录判异、**不写 control_limits** → `calculate_capability`
        # 取不到 's_chart'，组内标准差只能退化成总体 σ，
        # 于是 n>10（只能走 S 图）时 Cp/Cpk 与 Pp/Ppk 完全同值，能力被高估。
        with self._lock:
            self._record_violations(key, violations, device_id, register_name,
                                    base_subgroup)
            self.control_limits[key] = result

        return result

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

        # 清单 046：`config['spec_limits']` 是**配置直读**的（YAML 的 `.nan`/`.inf`
        # 是规范内字面量），不经过 set_spec_limits 的校验。这里再收一次口，
        # 否则 usl-lsl 为 NaN → Cp/Cpk/PPM 全部 NaN 并原样写进看板。
        usl = try_finite_float(spec['usl'])
        lsl = try_finite_float(spec['lsl'])
        if usl is None or lsl is None or usl <= lsl:
            logger.warning("SPC 规格限非法，跳过过程能力计算: %s (USL=%r, LSL=%r)",
                           key, spec.get('usl'), spec.get('lsl'))
            return None

        # `target` 允许缺省；缺省（或旧版本写入的 None）时按 (USL+LSL)/2 兜底。
        target = try_finite_float(spec.get('target'))
        if target is None:
            target = (usl + lsl) / 2

        raw_data, _fed = self._snapshot(key)

        if len(raw_data) < 20:
            return None

        n = len(raw_data)
        mean = sum(raw_data) / n

        # 总体标准差
        variance = sum((x - mean) ** 2 for x in raw_data) / (n - 1)
        sigma_total = math.sqrt(variance) if variance > 0 else 0

        # 组内标准差（从控制图获取）
        # 清单 046：X̄-S 图现在会写入 control_limits，这里必须一并识别 ——
        # 否则 n>10 只能走 S 图时永远命中不到 'r_chart'，σ_within 退化成
        # σ_total，Cp/Cpk 与 Pp/Ppk 变成同一组数（能力被系统性高估）。
        cl_data = self.control_limits.get(key, {})
        if 'r_chart' in cl_data:
            r_bar = try_finite_float(cl_data['r_chart'].get('cl'))
            d2_table = {2: 1.128, 3: 1.693, 4: 2.059, 5: 2.326,
                        6: 2.534, 7: 2.704, 8: 2.847, 9: 2.970, 10: 3.078}
            d2 = d2_table.get(self.subgroup_size, 2.326)
            sigma_within = r_bar / d2 if (r_bar is not None and d2 > 0) else sigma_total
        elif 's_chart' in cl_data:
            s_bar = try_finite_float(cl_data['s_chart'].get('cl'))
            c4 = self._c4(self.subgroup_size)
            sigma_within = s_bar / c4 if (s_bar is not None and c4 > 0) else sigma_total
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
        Western Electric判异规则检测（完整8条）

        规则1: 1点超出3σ控制限
        规则2: 连续9点在中心线同一侧
        规则3: 连续6点递增或递减
        规则4: 连续14点交替上下
        规则5: 连续3点中有2点超出2σ
        规则6: 连续5点中有4点超出1σ
        规则7: 连续15点在1σ以内（中心线附近）
        规则8: 连续8点超出1σ（在中心线两侧）
        """
        violations = []
        n = len(points)

        if n < 2:
            return violations

        sigma = (ucl - cl) / 3 if (ucl - cl) > 0 else 1
        one_sigma_up = cl + sigma
        one_sigma_down = cl - sigma
        two_sigma_up = cl + 2 * sigma
        two_sigma_down = cl - 2 * sigma

        # 规则1: 超出控制限
        for i, p in enumerate(points):
            if p > ucl or p < lcl:
                violations.append({
                    'rule': 1,
                    'description': f'点{i+1}超出3σ控制限',
                    'value': round(p, 4),
                    'index': i,
                    'span': 1,
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
                    'span': 9,
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
                    'span': 6,
                    'severity': 'warning',
                })
            elif all(segment[j] > segment[j+1] for j in range(5)):
                violations.append({
                    'rule': 3,
                    'description': f'点{i+1}~{i+6}连续6点递减',
                    'index': i,
                    'span': 6,
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
                        'span': 14,
                        'severity': 'warning',
                    })

        # 规则5: 连续3点中有2点超出2σ
        for i in range(n - 2):
            segment = points[i:i+3]
            count_outside = sum(1 for p in segment if p > two_sigma_up or p < two_sigma_down)
            if count_outside >= 2:
                violations.append({
                    'rule': 5,
                    'description': f'点{i+1}~{i+3}连续3点中有2点超出2σ',
                    'index': i,
                    'span': 3,
                    'severity': 'warning',
                })

        # 规则6: 连续5点中有4点超出1σ
        for i in range(n - 4):
            segment = points[i:i+5]
            count_outside = sum(1 for p in segment if p > one_sigma_up or p < one_sigma_down)
            if count_outside >= 4:
                violations.append({
                    'rule': 6,
                    'description': f'点{i+1}~{i+5}连续5点中有4点超出1σ',
                    'index': i,
                    'span': 5,
                    'severity': 'warning',
                })

        # 规则7: 连续15点在1σ以内
        for i in range(n - 14):
            segment = points[i:i+15]
            if all(one_sigma_down <= p <= one_sigma_up for p in segment):
                violations.append({
                    'rule': 7,
                    'description': f'点{i+1}~{i+15}连续15点在1σ以内（过度集中）',
                    'index': i,
                    'span': 15,
                    'severity': 'info',
                })

        # 规则8: 连续8点超出1σ
        for i in range(n - 7):
            segment = points[i:i+8]
            if all(p > one_sigma_up or p < one_sigma_down for p in segment):
                violations.append({
                    'rule': 8,
                    'description': f'点{i+1}~{i+8}连续8点超出1σ',
                    'index': i,
                    'span': 8,
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

    @staticmethod
    def _c4(n: int) -> float:
        """c4 系数：把子组标准差 s̄ 换算成组内 σ（σ̂ = s̄ / c4）。

        n=2~25 用标准表；超范围时按 Γ 函数精确式计算
        `c4 = sqrt(2/(n-1)) · Γ(n/2) / Γ((n-1)/2)`。
        """
        table = {
            2: 0.7979, 3: 0.8862, 4: 0.9213, 5: 0.9400, 6: 0.9515,
            7: 0.9594, 8: 0.9650, 9: 0.9693, 10: 0.9727, 11: 0.9754,
            12: 0.9776, 13: 0.9794, 14: 0.9810, 15: 0.9823, 16: 0.9835,
            17: 0.9845, 18: 0.9854, 19: 0.9862, 20: 0.9869, 21: 0.9876,
            22: 0.9882, 23: 0.9887, 24: 0.9892, 25: 0.9896,
        }
        if n in table:
            return table[n]
        if n < 2:
            return 0.0
        return math.sqrt(2.0 / (n - 1)) * math.exp(
            math.lgamma(n / 2) - math.lgamma((n - 1) / 2))

    # ==================== 查询接口 ====================

    def get_control_chart(self, device_id: str, register_name: str) -> dict[str, Any] | None:
        """获取控制图数据（按子组大小自动选图）。

        清单 046：原来**无条件**调 `calculate_xbar_r_chart`，而 R 图的
        A2/D3/D4 系数只定义到 n=10。子组 >10 时 `a2_table.get(n, 0.577)`
        会静默套用 n=5 的系数 —— 控制限偏窄，正常波动被成批判成异常。
        X̄-S 图的 A3/B3/B4 覆盖到 n=25，是这一区间的标准选择。
        """
        if self.subgroup_size > 10:
            return self.calculate_xbar_s_chart(device_id, register_name)
        return self.calculate_xbar_r_chart(device_id, register_name)

    def get_capability(self, device_id: str, register_name: str) -> dict[str, Any] | None:
        """获取过程能力数据"""
        return self.calculate_capability(device_id, register_name)

    def get_violations(self, device_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """获取判异结果（去重后的唯一违规，按发生顺序）"""
        with self._lock:
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
