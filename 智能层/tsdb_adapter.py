"""
智能层TDengine适配器
====================

解决智能层模块（OEE/预测性维护/SPC/能源）与TDengine的数据流断层。

核心职责：
1. 从TDengine读取历史数据喂入智能层模块
2. 将智能层计算结果写回TDengine
3. 提供统一的数据访问接口

数据流：
TDengine(遥测) → 适配器 → OEE/预测性维护/SPC/能源
                         ← 计算结果 ←
TDengine(结果表) ← 适配器 ←
"""

import contextlib
import logging
import threading
import time
from datetime import datetime, timedelta

from core.safe_numeric import try_finite_float, try_int
from typing import Any

try:
    from ..timeseries.tdengine_client import TDengineClient
    from ..timeseries.data_models import (
        OEERecord, EnergyRecord, PredictiveRecord
    )
except ImportError:
    # 绝对导入 fallback（当项目根目录没有 __init__.py 时）
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from timeseries.tdengine_client import TDengineClient
    from timeseries.data_models import (
        OEERecord, EnergyRecord, PredictiveRecord
    )

logger = logging.getLogger(__name__)


class TSDBAdapter:
    """
    TDengine智能层适配器

    功能：
    1. 定期从TDengine读取遥测数据
    2. 喂入OEE/预测性维护/SPC/能源模块
    3. 将计算结果写回TDengine
    """

    def __init__(self, tdengine: TDengineClient, 
                 oee_calculator=None,
                 predictive_maintenance=None,
                 spc_analyzer=None,
                 energy_manager=None,
                 config: dict[str, Any] | None = None):
        """
        初始化适配器

        Args:
            tdengine: TDengine客户端
            oee_calculator: OEE计算器实例
            predictive_maintenance: 预测性维护实例
            spc_analyzer: SPC分析器实例
            energy_manager: 能源管理实例
            config: 配置字典
        """
        self.tdengine = tdengine
        self.oee_calculator = oee_calculator
        self.predictive_maintenance = predictive_maintenance
        self.spc_analyzer = spc_analyzer
        self.energy_manager = energy_manager
        self.config = config or {}

        # 数据同步间隔（秒）
        self.sync_interval = self.config.get('sync_interval', 10)

        # 结果写入间隔（秒）
        self.result_interval = self.config.get('result_interval', 60)

        # 运行状态
        self._running = False
        self._sync_thread: threading.Thread | None = None
        self._result_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # 设备注册表（需要同步的设备）
        self._registered_devices: dict[str, dict[str, Any]] = {}

        # 统计信息
        self.stats: dict[str, Any] = {
            'sync_cycles': 0,
            'records_synced': 0,
            'results_written': 0,
            'errors': 0,
            'last_sync_time': None,
            'last_result_time': None
        }

        self._lock = threading.Lock()

        logger.info("TDengine智能层适配器初始化完成")

    def forget_device(self, device_id: str) -> bool:
        """设备被删除时注销它的时序同步登记（round 310）。

        `_registered_devices` 以 `device_id` 为 key 且没有删除点，而
        `_sync_data()` 里 `for device_id in self._registered_devices:` 会
        **继续给一台已删除的设备拉数据**（查库得到空 → 仍走一轮同步），
        `_write_results()` 也会给它写结果行 —— 时序库里因此出现
        "已删除设备仍在产生数据"的记录，报表对不上设备清单。
        """
        # BUG-049：**必须持锁**。`_sync_data`（226-227 行写 last_sync）与
        # `_write_energy_results`（422 行遍历）都在别的线程里跑；
        # 这里原来是 check-then-act 无锁 pop —— 并发时那边会拿到已被删掉的 key。
        with self._lock:
            removed = self._registered_devices.pop(device_id, None) is not None
        if removed:
            logger.info("时序适配器已注销已删除设备: %s", device_id)
        return removed

    def register_device(self, device_id: str, registers: list[dict[str, Any]]):
        """
        注册设备及其寄存器

        Args:
            device_id: 设备ID
            registers: 寄存器列表 [{'name': 'temperature', 'unit': '°C'}, ...]
        """
        with self._lock:
            self._registered_devices[device_id] = {
                'registers': registers,
                'last_sync': None
            }
        logger.info(f"注册设备 {device_id}，{len(registers)} 个寄存器")

    def start(self):
        """启动适配器"""
        if self._running:
            logger.warning("适配器已在运行")
            return

        self._running = True
        self._stop_event.clear()

        # 启动数据同步线程
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._sync_thread.start()

        # 启动结果写入线程
        self._result_thread = threading.Thread(target=self._result_loop, daemon=True)
        self._result_thread.start()

        logger.info("TDengine智能层适配器已启动")

    def stop(self):
        """停止适配器"""
        if not self._running:
            return

        self._running = False
        self._stop_event.set()

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5)

        if self._result_thread and self._result_thread.is_alive():
            self._result_thread.join(timeout=5)

        logger.info("TDengine智能层适配器已停止")

    def _sync_loop(self):
        """数据同步主循环"""
        while self._running and not self._stop_event.is_set():
            try:
                self._sync_data()
                self._stop_event.wait(self.sync_interval)
            except Exception as e:
                logger.error(f"数据同步异常: {e}", exc_info=True)
                self.stats['errors'] += 1

    def _result_loop(self):
        """结果写入主循环"""
        while self._running and not self._stop_event.is_set():
            try:
                self._write_results()
                self._stop_event.wait(self.result_interval)
            except Exception as e:
                logger.error(f"结果写入异常: {e}", exc_info=True)
                self.stats['errors'] += 1

    def _sync_data(self):
        """从TDengine同步数据到智能层模块"""
        with self._lock:
            devices = dict(self._registered_devices)

        for device_id, device_info in devices.items():
            try:
                registers = device_info['registers']
                last_sync = device_info.get('last_sync')

                # 计算同步时间范围
                end_time = datetime.now()
                if last_sync:
                    start_time = last_sync
                else:
                    # 首次同步，获取最近1小时数据
                    start_time = end_time - timedelta(hours=1)

                for register in registers:
                    register_name = register['name']

                    # 从TDengine查询数据
                    data = self.tdengine.query_telemetry(
                        device_id, register_name, start_time, end_time, limit=1000
                    )

                    if not data:
                        continue

                    # 喂入各智能层模块
                    self._feed_to_modules(device_id, register_name, data)

                    self.stats['records_synced'] += len(data)

                # 更新最后同步时间
                # BUG-049：锁内也要用 `.get()`。本轮同步期间设备可能已被
                # `forget_device` 删除，下标赋值会抛 KeyError ——
                # 被下面的 except 吞成 errors+=1，每轮刷一条无意义错误日志。
                with self._lock:
                    entry = self._registered_devices.get(device_id)
                    if entry is not None:
                        entry['last_sync'] = end_time

            except Exception as e:
                logger.error(f"同步设备 {device_id} 数据失败: {e}")
                self.stats['errors'] += 1

        self.stats['sync_cycles'] += 1
        self.stats['last_sync_time'] = datetime.now().isoformat()

    def _feed_to_modules(self, device_id: str, register_name: str, data: list[dict[str, Any]]):
        """将数据喂入各智能层模块"""

        for record in data:
            value = record.get('value')
            timestamp_str = record.get('timestamp')

            num_val = None if isinstance(value, bool) else try_finite_float(value)
            if num_val is None:
                continue

            # 解析时间戳，并将带时区时间（如 Z/+00:00）转为本地无时区 datetime，避免与 datetime.now() 相减报 TypeError
            if isinstance(timestamp_str, str):
                try:
                    timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                except Exception as e:
                    logger.warning(
                        "tsdb_adapter: 时间戳无法解析，跳过该条 "
                        "(device=%s, register=%s, ts=%r): %s",
                        device_id, register_name, timestamp_str, e)
                    continue
            elif isinstance(timestamp_str, datetime):
                timestamp = timestamp_str
            else:
                logger.warning(
                    "tsdb_adapter: 时间戳类型不支持，跳过该条 "
                    "(device=%s, register=%s, ts=%r, type=%s)",
                    device_id, register_name, timestamp_str,
                    type(timestamp_str).__name__)
                continue

            if timestamp.tzinfo is not None:
                timestamp = timestamp.astimezone().replace(tzinfo=None)

            # 预测性维护 — 喂入所有有限数值数据
            if self.predictive_maintenance:
                self.predictive_maintenance.feed_data(
                    device_id, register_name, num_val, timestamp
                )

            reg_lower = register_name.lower()

            # SPC — 质量相关数据
            if self.spc_analyzer:
                spc_keywords = ['temperature', 'pressure', 'ph', 'quality', 'dimension',
                                'voltage', 'current', 'speed', 'flow', 'level',
                                'humidity', 'torque', 'frequency', 'thickness']
                if any(kw in reg_lower for kw in spc_keywords):
                    self.spc_analyzer.feed_data(device_id, register_name, num_val)

            # 能源管理 — 先分辨累积电量、水/气流量，再匹配有功功率（排除功率因数/无功/视在功率）
            if self.energy_manager:
                if 'water_flow' in reg_lower:
                    self.energy_manager.feed_water_data(
                        device_id, num_val, timestamp=timestamp
                    )
                elif 'gas_flow' in reg_lower:
                    self.energy_manager.feed_gas_data(
                        device_id, num_val, timestamp=timestamp
                    )
                elif 'energy' in reg_lower or 'kwh' in reg_lower:
                    self.energy_manager.feed_power_data(
                        device_id, 0, energy_kwh=num_val, timestamp=timestamp,
                        register_name=register_name,
                    )
                elif (
                    ('power' in reg_lower or 'watt' in reg_lower)
                    and 'power_factor' not in reg_lower
                    and 'reactive_power' not in reg_lower
                    and 'apparent_power' not in reg_lower
                ):
                    self.energy_manager.feed_power_data(
                        device_id, num_val, timestamp=timestamp
                    )

            # OEE — 设备状态和产量
            if self.oee_calculator:
                if register_name == 'running_status':
                    status_map = {0: 'stopped', 1: 'running', 2: 'fault', 3: 'idle'}
                    status = status_map.get(int(round(num_val)))
                    if status is None:
                        logger.warning(
                            "tsdb_adapter: 未知 running_status 值，本条不记录状态 "
                            "(device=%s, value=%r)", device_id, value)
                        continue
                    self.oee_calculator.update_device_state(device_id, status)
                elif register_name in ('product_count', 'good_count', 'total_count'):
                    int_val = try_int(int(round(num_val)))
                    if int_val is not None:
                        if register_name == 'product_count':
                            self.oee_calculator.record_production(device_id, count=int_val)
                        elif register_name == 'good_count':
                            self.oee_calculator.record_production(device_id, good_count=int_val)
                        elif register_name == 'total_count':
                            self.oee_calculator.record_production(device_id, count=int_val)

    def _write_results(self):
        """将智能层计算结果写回TDengine"""

        # 写入OEE结果
        if self.oee_calculator:
            self._write_oee_results()

        # 写入预测性维护结果
        if self.predictive_maintenance:
            self._write_predictive_results()

        # 写入能源数据
        if self.energy_manager:
            self._write_energy_results()

        self.stats['last_result_time'] = datetime.now().isoformat()

    def _write_oee_results(self):
        """写入OEE计算结果"""
        if not self.oee_calculator:
            return
        try:
            all_oee = self.oee_calculator.get_all_oee()

            for device_id, oee_data in all_oee.items():
                record = OEERecord(
                    device_id=device_id,
                    timestamp=datetime.now(),
                    availability=oee_data.get('availability', 0),
                    performance=oee_data.get('performance', 0),
                    quality_rate=oee_data.get('quality', 0),
                    oee=oee_data.get('oee', 0),
                    total_count=oee_data.get('total_count', 0),
                    good_count=oee_data.get('good_count', 0),
                    run_time=oee_data.get('actual_run_time', 0),
                    downtime=oee_data.get('downtime', 0)
                )

                # round 298：只有**真的写进去**才计数
                if self.tdengine.write_oee(record):
                    self.stats['results_written'] += 1
                else:
                    self.stats['errors'] += 1

        except Exception as e:
            logger.error(f"写入OEE结果失败: {e}")
            self.stats['errors'] += 1

    def _write_predictive_results(self):
        """写入预测性维护结果"""
        if not self.predictive_maintenance:
            return
        try:
            health_scores = self.predictive_maintenance.get_health_scores()

            # round 369：**一台设备一个子表，同一轮只能写一条**。
            # health_scores 的键是 f"{device_id}:{register_name}"，
            # 而 TDengine 侧只按 device_id 写 pred_{device_id} —— 同一毫秒内
            # 同一设备的多个寄存器记录时间戳相同，会**互相覆盖**，
            # 最后只剩一条（且不确定是哪一条）。
            # 取健康分最低（最危险）的那条作为该设备本轮的代表值。
            worst_by_device: dict[str, dict] = {}
            for _key, score_data in health_scores.items():
                device_id = score_data.get('device_id')
                if not device_id:
                    continue
                prev = worst_by_device.get(device_id)
                score = try_finite_float(score_data.get('health_score', 100))
                if score is None:
                    score = 100.0
                if prev is None:
                    worst_by_device[device_id] = score_data
                    continue
                prev_score = try_finite_float(prev.get('health_score', 100))
                if prev_score is None:
                    prev_score = 100.0
                if score < prev_score:
                    worst_by_device[device_id] = score_data

            for device_id, score_data in worst_by_device.items():
                failure_pred = score_data.get('failure_prediction')
                remaining_life = -1.0     # -1 = 未知（趋势平稳/远离阈值）
                failure_probability = 0

                if isinstance(failure_pred, dict):
                    # round 369：`days_to_limit` 可能是**键存在但值为 None**
                    # （参数趋势平稳时），`.get(k, 0)` 拿到的是 None ——
                    # `None * 24` 直接 TypeError，整个同步循环在第一个
                    # 平稳点位处崩掉。
                    days = try_finite_float(failure_pred.get('days_to_limit'))
                    remaining_life = (days * 24) if days is not None else -1.0
                    probability = try_finite_float(
                        failure_pred.get('confidence', 0) or 0)
                    failure_probability = (probability
                                           if probability is not None else 0.0)

                record = PredictiveRecord(
                    device_id=device_id,
                    timestamp=datetime.now(),
                    health_score=score_data.get('health_score', 100),
                    failure_probability=failure_probability,
                    remaining_life=remaining_life,
                    anomaly_score=score_data.get('anomaly_count', 0),
                    trend=(score_data.get('trend') or {}).get('direction', 'stable')
                )

                if self.tdengine.write_predictive(record):
                    self.stats['results_written'] += 1
                else:
                    self.stats['errors'] += 1

        except Exception as e:
            logger.error(f"写入预测性维护结果失败: {e}")
            self.stats['errors'] += 1

    def _write_energy_results(self):
        """写入能源数据"""
        if not self.energy_manager:
            return
        try:
            # 获取所有设备的实时功率
            # BUG-049：先在锁内取快照再遍历。原来直接迭代活字典，与
            # `register_device` / `forget_device` 并发时抛
            # `RuntimeError: dictionary changed size during iteration`，
            # 被外层 except 吞掉 → **这一轮能源结果整体丢失**，只记一条 errors。
            with self._lock:
                device_ids = list(self._registered_devices)
            for device_id in device_ids:
                em = self.energy_manager
                em_lock = getattr(em, '_lock', None)
                lock_ctx = (
                    em_lock
                    if em_lock is not None and type(em_lock).__module__ == '_thread'
                    else contextlib.nullcontext()
                )
                with lock_ctx:
                    rp = getattr(em, 'realtime_power', None)
                    ea = getattr(em, 'energy_accumulated', None)
                    raw_pd = rp.get(device_id) if hasattr(rp, 'get') else None
                    power_data = dict(raw_pd) if isinstance(raw_pd, dict) else None
                    raw_acc = ea.get(device_id, {}) if hasattr(ea, 'get') else {}
                    energy_val = raw_acc.get('energy_kwh', 0) if hasattr(raw_acc, 'get') else 0
                if power_data:
                    record = EnergyRecord(
                        device_id=device_id,
                        timestamp=datetime.now(),
                        power=power_data.get('power_kw', 0),
                        energy=energy_val,
                        voltage=power_data.get('voltage', 0),
                        current=power_data.get('current', 0),
                        power_factor=power_data.get('power_factor', 1.0)
                    )

                    if self.tdengine.write_energy(record):
                        self.stats['results_written'] += 1
                    else:
                        self.stats['errors'] += 1

        except Exception as e:
            logger.error(f"写入能源数据失败: {e}")
            self.stats['errors'] += 1

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        stats = self.stats.copy()
        stats['running'] = self._running
        stats['registered_devices'] = len(self._registered_devices)
        return stats


class RealtimeDataBridge:
    """
    实时数据桥接器

    将采集层的实时数据同时写入SQLite和TDengine。
    解决采集层只写SQLite的问题。
    """

    #: 内存回退缓冲的上限。
    #:
    #: ⚠️ 为什么必须有上限：TDengine 长时间不可用时，写失败的记录会被放回内存缓冲
    #: （见 `_requeue_to_memory`）。**无上限 = 内存无限增长 → 进程被 OOM 杀掉**，
    #: 而内存里的记录全都没了 —— 比「丢最旧」更糟。溢出时丢最旧并计数 + 告警，
    #: 与采集队列 `_enqueue_drop_oldest` 同一策略。
    DEFAULT_MAX_BUFFER_SIZE = 50000

    #: 连续失败多少次后熔断（round 291）。
    #:
    #: TDengine 的 REST 调用超时是 30s，而刷新周期是 5s —— 库挂掉时
    #: 每轮 flush 都会阻塞 30s，flush 线程被拖住、缓冲被填满、最后开始丢最旧。
    #: 熔断期间**不发起写**，记录留在缓冲里等下一轮，既不让线程卡死也不丢数据。
    CIRCUIT_FAIL_THRESHOLD = 3

    #: 熔断冷却时长（秒，**单调时钟**；初值用 -inf，绝不用 0.0）。
    CIRCUIT_COOLDOWN_S = 30.0

    def __init__(self, tdengine: TDengineClient, offline_buffer=None,
                 max_buffer_size: int | None = None):
        """
        初始化桥接器

        Args:
            tdengine: TDengine客户端
            offline_buffer: 断线缓存实例（可选，**鸭子类型**，只需实现
                ``buffer_records(kind, records)``）。

                ⚠️ **本仓库没有该实现**（2026-10-13 核实）：`timeseries/offline_buffer.py`
                并不存在 —— README 目录树里的那一条在 round 202 已按「文档腐烂」清理，
                但这里的调用点留了下来，单测又用 MagicMock 注入，于是看起来像「有断线缓存」。
                生产入口 `run.py` **也没有**注入它 → 运行时恒为 None → 写失败走内存回退。
                保留该参数是为了 ① 允许外部注入实现；② 单测可注入假对象验证分支。
                要真正启用「断线缓存」，需先实现该模块并在 `run.py` 注入。
            max_buffer_size: 内存回退缓冲上限（默认 ``DEFAULT_MAX_BUFFER_SIZE``）
        """
        self.tdengine = tdengine
        self.offline_buffer = offline_buffer
        self._max_buffer_size = max_buffer_size or self.DEFAULT_MAX_BUFFER_SIZE
        self.logger = logging.getLogger("RealtimeDataBridge")

        # 批量写入缓冲
        try:
            from ..timeseries.data_models import TelemetryRecord
        except ImportError:
            from timeseries.data_models import TelemetryRecord
        self._buffer: list[Any] = []
        self._buffer_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._batch_size = 100
        self._flush_interval = 5.0

        # 运行状态
        self._running = False
        self._flush_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # 统计
        self.stats: dict[str, Any] = {
            'records_received': 0,
            'records_written': 0,
            'errors': 0,
            # 写失败而被回填重试的条数（与 errors 区分：errors 是"失败轮次"）
            'records_requeued': 0,
            # 内存回退缓冲溢出被丢弃的条数（>0 说明 TDengine 长时间不可用且数据在丢）
            'dropped_buffer_full': 0,
        }

        # 熔断状态（round 291）。`-inf` = 从未熔断过；绝不能用 0.0 ——
        # 单调时钟在部分平台是"开机以来的秒数"，0.0 会让首次判定恒为"已过期"。
        self._fail_streak = 0
        self._circuit_open_until = float('-inf')
        self._last_write_error: str | None = None
        #: 单调时钟缝（测试要模拟"冷却期过去"只能替换它，不能 monkeypatch 全局
        #: `time.monotonic` —— 那会波及后台线程的 `Event.wait(timeout)`）。
        self._clock = time.monotonic

    def start(self):
        """启动桥接器"""
        if self._running:
            return

        self._running = True
        self._stop_event.clear()

        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()

        self.logger.info("实时数据桥接器已启动")

    def stop(self):
        """停止桥接器"""
        self._running = False
        self._stop_event.set()

        # 刷新剩余数据
        self._flush_buffer()

        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=5)

        self.logger.info("实时数据桥接器已停止")

    def feed(self, device_id: str, register_name: str, value: float,
             timestamp: datetime | None = None, unit: str = "", protocol: str = "",
             gateway_id: str = "", quality: int = 192):
        """
        喂入实时数据

        Args:
            device_id: 设备ID
            register_name: 寄存器名称
            value: 数据值
            timestamp: 时间戳
            unit: 单位
            protocol: 协议类型
            gateway_id: 网关ID
            quality: 数据质量
        """
        try:
            from ..timeseries.data_models import TelemetryRecord
        except ImportError:
            from timeseries.data_models import TelemetryRecord

        if isinstance(value, bool):
            self.logger.warning("RealtimeDataBridge 拒绝布尔值: %s/%s=%r", device_id, register_name, value)
            return
        num_val = try_finite_float(value)
        if num_val is None:
            self.logger.warning("RealtimeDataBridge 拒绝非有限值: %s/%s=%r", device_id, register_name, value)
            return
        q = 192 if isinstance(quality, bool) else (try_int(quality) if try_int(quality) is not None else 192)
        ts = timestamp or datetime.now()
        if isinstance(ts, datetime) and ts.tzinfo is not None:
            ts = ts.astimezone().replace(tzinfo=None)

        record = TelemetryRecord(
            device_id=device_id,
            register_name=register_name,
            timestamp=ts,
            value=num_val,
            quality=q,
            unit=unit,
            protocol=protocol,
            gateway_id=gateway_id
        )

        with self._buffer_lock:
            self._buffer.append(record)
            should_flush = len(self._buffer) >= self._batch_size
        with self._stats_lock:
            self.stats['records_received'] += 1

        # 检查是否需要刷新
        if should_flush:
            self._flush_buffer()

    def _flush_loop(self):
        """定时刷新循环"""
        while self._running and not self._stop_event.is_set():
            try:
                self._stop_event.wait(self._flush_interval)
                if self._running:
                    self._flush_buffer()
            except Exception as e:
                self.logger.error(f"刷新异常: {e}")
                with self._stats_lock:
                    self.stats['errors'] += 1

    def _requeue_to_memory(self, records) -> None:
        """把写失败的记录放回内存缓冲（**带上限**，溢出丢最旧并计数 + 告警）。"""
        dropped = 0
        with self._buffer_lock:
            combined = list(records) + self._buffer
            overflow = len(combined) - self._max_buffer_size
            if overflow > 0:
                del combined[:overflow]
                dropped = overflow
            self._buffer = combined

        if dropped:
            with self._stats_lock:
                self.stats['dropped_buffer_full'] += dropped
                total = self.stats['dropped_buffer_full']
            if total == dropped or (total - dropped) // 1000 != total // 1000:
                self.logger.error(
                    "内存回退缓冲已达上限 %d，丢弃最旧 %d 条（累计 %d 条）—— "
                    "TDengine 长时间不可用且数据正在丢失，请检查时序库状态",
                    self._max_buffer_size, dropped, total,
                )

    def _circuit_open(self) -> bool:
        """熔断是否处于打开状态（冷却期内不发起写）。"""
        with self._stats_lock:
            return self._clock() < self._circuit_open_until

    def _on_write_failed(self, records, error: str) -> None:
        """统一处理一次写失败：计数、熔断判定、回填重试。"""
        opened_now = False
        with self._stats_lock:
            self.stats['errors'] += 1
            self.stats['records_requeued'] += len(records)
            self._last_write_error = error
            self._fail_streak += 1
            streak = self._fail_streak
            if self._fail_streak >= self.CIRCUIT_FAIL_THRESHOLD and self._clock() >= self._circuit_open_until:
                self._circuit_open_until = self._clock() + self.CIRCUIT_COOLDOWN_S
                opened_now = True

        if opened_now:
            self.logger.error(
                "TDengine 连续写失败 %d 次 → 熔断 %.0fs（期间不发起写，数据留在内存缓冲，"
                "不丢弃；最后错误：%s）",
                streak, self.CIRCUIT_COOLDOWN_S, error,
            )

        if self.offline_buffer:
            try:
                serialized = [{
                    'device_id': r.device_id,
                    'register_name': r.register_name,
                    'timestamp': r.timestamp.isoformat(),
                    'value': r.value,
                    'quality': r.quality,
                    'unit': r.unit,
                    'protocol': r.protocol,
                    'gateway_id': r.gateway_id,
                } for r in records]
                self.offline_buffer.buffer_records('telemetry', serialized)
                self.logger.info(f"已转入离线缓存 {len(records)} 条")
                return
            except Exception as be:
                self.logger.error(f"离线缓存写入失败: {be}")

        self._requeue_to_memory(records)

    def _flush_buffer(self):
        """刷新缓冲区"""
        with self._buffer_lock:
            if not self._buffer:
                return

            records = list(self._buffer)
            self._buffer.clear()

        # 熔断冷却期内：不发起写，直接把本批放回缓冲（不丢），下一轮再试。
        if self._circuit_open():
            self._requeue_to_memory(records)
            return

        try:
            ok = self.tdengine.write_telemetry_batch(records)
        except Exception as e:
            self.logger.error(f"写入TDengine失败: {e}")
            self._on_write_failed(records, str(e))
            return

        if ok:
            with self._stats_lock:
                self.stats['records_written'] += len(records)
                self._fail_streak = 0
                self._circuit_open_until = float('-inf')
                self._last_write_error = None
            self.logger.debug(f"写入 {len(records)} 条数据到TDengine")
        else:
            self.logger.error("写入TDengine失败: 客户端返回失败（未真正写入）")
            self._on_write_failed(records, 'write_telemetry_batch 返回失败')

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        with self._buffer_lock:
            buf_size = len(self._buffer)
        with self._stats_lock:
            stats = self.stats.copy()
            stats['running'] = self._running
            stats['buffer_size'] = buf_size
            stats['fail_streak'] = self._fail_streak
            now = self._clock()
            stats['circuit_open'] = now < self._circuit_open_until
            remaining = self._circuit_open_until - now
            stats['circuit_remaining_seconds'] = round(max(0.0, remaining), 3)
            if self._last_write_error:
                stats['last_write_error'] = self._last_write_error
        return stats


# 测试代码
if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # 创建TDengine客户端
    tdengine = TDengineClient("localhost", 6041)

    if not tdengine.connect():
        print("TDengine连接失败")
        sys.exit(1)

    tdengine.init_tables()

    # 创建适配器
    adapter = TSDBAdapter(tdengine)

    # 注册设备
    adapter.register_device("CNC_001", [
        {'name': 'temperature', 'unit': '°C'},
        {'name': 'pressure', 'unit': 'MPa'},
        {'name': 'running_status', 'unit': 'enum'},
        {'name': 'product_count', 'unit': 'pcs'}
    ])

    # 启动适配器
    adapter.start()

    print("适配器已启动，按 Ctrl+C 停止...")

    try:
        while True:
            time.sleep(10)
            stats = adapter.get_stats()
            print(f"统计: {stats}")
    except KeyboardInterrupt:
        print("正在停止...")
    finally:
        adapter.stop()
