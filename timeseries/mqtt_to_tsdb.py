"""
MQTT到TDengine数据写入服务

将MQTT接收到的标准化数据自动写入TDengine时序数据库。

数据流：
MQTT Broker → MQTTSubscriber → 本服务 → TDengine

使用方式：
    from timeseries import TDengineClient
    from gateway import MQTTSubscriber
    from timeseries.mqtt_to_tsdb import MQTTToTSDBService

    # 创建客户端
    tdengine = TDengineClient("localhost", 6041)
    subscriber = MQTTSubscriber("localhost", 1883)

    # 创建服务
    service = MQTTToTSDBService(subscriber, tdengine)
    service.start()
"""

import logging
from datetime import datetime
from typing import Any
from collections import deque
import threading

try:
    from ..gateway import MQTTSubscriber, DeviceTelemetry, DeviceStatus, AlarmMessage
    from ..core.safe_numeric import try_finite_float, try_int
except ImportError:
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from gateway import MQTTSubscriber, DeviceTelemetry, DeviceStatus, AlarmMessage
    from core.safe_numeric import try_finite_float, try_int
from .tdengine_client import TDengineClient
from .data_models import (
    TelemetryRecord, AlarmRecord, OEERecord, EnergyRecord, PredictiveRecord
)


def _coerce_timestamp(raw_ts: Any) -> datetime:
    """将输入时间戳安全转换为 datetime；拒绝 bool、NaN/Inf 及非法类型。"""
    if isinstance(raw_ts, datetime):
        return raw_ts
    if isinstance(raw_ts, bool):
        raise ValueError(f'时间戳不能为 bool: {raw_ts!r}')
    ts_num = try_finite_float(raw_ts)
    if ts_num is None:
        raise ValueError(f'时间戳必须为有限数字或 datetime: {raw_ts!r}')
    try:
        return datetime.fromtimestamp(ts_num)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f'时间戳超出范围: {raw_ts!r} ({exc})') from exc


class MQTTToTSDBService:
    """
    MQTT到TDengine数据写入服务

    功能：
    1. 订阅MQTT主题
    2. 接收标准化数据
    3. 转换为TDengine数据模型
    4. 批量写入TDengine
    """

    def __init__(self, subscriber: MQTTSubscriber, tdengine: TDengineClient,
                 batch_size: int = 100, flush_interval: float = 5.0):
        """
        初始化服务

        Args:
            subscriber: MQTT订阅客户端
            tdengine: TDengine客户端
            batch_size: 批量写入大小
            flush_interval: 刷新间隔（秒）
        """
        self.subscriber = subscriber
        self.tdengine = tdengine
        self.batch_size = batch_size
        self.flush_interval = flush_interval

        self.logger = logging.getLogger("MQTTToTSDB")

        # 数据缓冲区
        self._telemetry_buffer: deque[Any] = deque(maxlen=10000)
        self._alarm_buffer: deque[Any] = deque(maxlen=1000)
        self._buffer_lock = threading.Lock()
        #: 统计计数在「MQTT 回调线程 / 刷新线程 / 主线程」三处自增，
        #: `+=` 不是原子操作，必须串行化，否则会静默丢计数。
        self._stats_lock = threading.Lock()

        # 运行状态
        self.running = False
        self._flush_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._flush_wake = threading.Event()

        # 统计信息
        self.stats: dict[str, Any] = {
            'telemetry_received': 0,
            'alarms_received': 0,
            'telemetry_written': 0,
            'alarms_written': 0,
            'errors': 0,
            # round 321：两类"数据没了但没人知道"的计数
            #   requeued —— 写失败被放回缓冲区重试的条数（说明 TDengine 在抖）
            #   dropped  —— 缓冲区满（deque maxlen）被挤掉的条数（**永久丢失**）
            'telemetry_requeued': 0,
            'alarms_requeued': 0,
            'telemetry_dropped': 0,
            'alarms_dropped': 0,
            'last_flush_time': None
        }

        # 设置回调
        self.subscriber.on_telemetry = self._on_telemetry
        self.subscriber.on_alarm = self._on_alarm

    def start(self) -> bool:
        """启动服务。

        Returns:
            bool: 真的起来了（TDengine 连上 + 订阅/刷新线程已启动）才为 True。
            原先失败只打日志返回 None，调用方无从得知启动失败。
        """
        if self.running:
            self.logger.warning("服务已在运行")
            return True

        self.logger.info("启动MQTT到TDengine数据写入服务...")

        # 连接TDengine
        if not self.tdengine.connect():
            self.logger.error("TDengine连接失败，服务未启动")
            return False

        # 初始化表
        self.tdengine.init_tables()

        # 订阅MQTT主题
        self.subscriber.subscribe_telemetry()
        self.subscriber.subscribe_alarms()

        # 启动MQTT订阅
        self.subscriber.start()

        # 启动刷新线程
        self.running = True
        self._stop_event.clear()
        self._flush_wake.clear()
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()

        self.logger.info("服务已启动")
        return True

    def stop(self):
        """停止服务"""
        if not self.running:
            return

        self.logger.info("停止服务...")
        self.running = False
        self._stop_event.set()
        self._flush_wake.set()

        # 先停订阅，否则 flush 与 stop 之间到达的数据会滞留在缓冲区被丢弃
        self.subscriber.stop()

        # 等待刷新线程结束
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=5)

        # 订阅已停，最后再刷一次
        self._flush_buffers()

        # 断开TDengine
        self.tdengine.disconnect()

        self.logger.info("服务已停止")

    def _on_telemetry(self, telemetry: DeviceTelemetry):
        """处理遥测数据"""
        try:
            self._bump('telemetry_received', 1)

            timestamp = _coerce_timestamp(telemetry.Timestamp)
            metrics = getattr(telemetry, 'Metrics', None)
            if not isinstance(metrics, dict):
                raise ValueError(f'telemetry.Metrics 非字典: {type(metrics).__name__}')

            records: list[TelemetryRecord] = []
            for register_name, metric in metrics.items():
                if not isinstance(metric, dict) or 'value' not in metric:
                    self._bump('errors', 1)
                    self.logger.warning("跳过格式非法的遥测指标 %s/%r: %r",
                                        telemetry.DeviceID, register_name, metric)
                    continue
                raw_val = metric['value']
                num_val = None if isinstance(raw_val, bool) else try_finite_float(raw_val)
                if num_val is None:
                    self._bump('errors', 1)
                    self.logger.warning("跳过非有限遥测值 %s/%s: %r",
                                        telemetry.DeviceID, register_name, raw_val)
                    continue
                raw_q = metric.get('quality', 192)
                q = 192 if raw_q is None else (None if isinstance(raw_q, bool) else try_int(raw_q))
                if q is None:
                    self._bump('errors', 1)
                    self.logger.warning("跳过非法质量码 %s/%s: %r",
                                        telemetry.DeviceID, register_name, raw_q)
                    continue
                records.append(TelemetryRecord(
                    device_id=telemetry.DeviceID,
                    register_name=register_name,
                    timestamp=timestamp,
                    value=num_val,
                    quality=q,
                    unit=metric.get('unit', ''),
                    protocol=telemetry.Protocol,
                    gateway_id=telemetry.GatewayID
                ))

            if not records:
                return

            with self._buffer_lock:
                for record in records:
                    # round 321：deque 满时会**静默挤掉最旧的一条**，
                    # 以前没人知道丢了多少 —— 这里显式计数。
                    if (self._telemetry_buffer.maxlen is not None
                            and len(self._telemetry_buffer) >= self._telemetry_buffer.maxlen):
                        self._bump('telemetry_dropped', 1)
                    self._telemetry_buffer.append(record)
                should_flush = len(self._telemetry_buffer) >= self.batch_size

            # 在运行中的后台模式下唤醒刷新线程异步落库，避免在 Paho MQTT 网络回调线程同步阻塞
            if should_flush:
                if self.running and self._flush_thread and self._flush_thread.is_alive():
                    self._flush_wake.set()
                else:
                    self._flush_telemetry()

        except Exception as e:
            self.logger.error(f"处理遥测数据异常: {e}")
            self._bump('errors', 1)

    def _on_alarm(self, alarm: AlarmMessage):
        """处理报警数据"""
        try:
            self._bump('alarms_received', 1)

            val = None if isinstance(alarm.Value, bool) else try_finite_float(alarm.Value)
            thr = None if isinstance(alarm.Threshold, bool) else try_finite_float(alarm.Threshold)
            if val is None or thr is None:
                raise ValueError(f'报警数值非有限: value={alarm.Value!r}, threshold={alarm.Threshold!r}')

            record = AlarmRecord(
                alarm_id=alarm.AlarmID,
                device_id=alarm.DeviceID,
                timestamp=_coerce_timestamp(alarm.Timestamp),
                level=alarm.Level,
                alarm_type=alarm.Type,
                message=alarm.Message,
                value=val,
                threshold=thr,
                acknowledged=bool(alarm.Acknowledged)
            )

            with self._buffer_lock:
                if (self._alarm_buffer.maxlen is not None
                        and len(self._alarm_buffer) >= self._alarm_buffer.maxlen):
                    self._bump('alarms_dropped', 1)
                self._alarm_buffer.append(record)

            # 报警立即写入（后台运行时唤醒刷新线程，避免卡死 MQTT 回调）
            if self.running and self._flush_thread and self._flush_thread.is_alive():
                self._flush_wake.set()
            else:
                self._flush_alarms()

        except Exception as e:
            self.logger.error(f"处理报警数据异常: {e}")
            self._bump('errors', 1)

    def _flush_loop(self):
        """定时刷新循环"""
        while self.running and not self._stop_event.is_set():
            try:
                self._flush_wake.wait(self.flush_interval)
                self._flush_wake.clear()
                if self.running:
                    self._flush_buffers()
            except Exception as e:
                self.logger.error(f"刷新异常: {e}")
                self._bump('errors', 1)

    def _flush_buffers(self):
        """刷新所有缓冲区"""
        self._flush_telemetry()
        self._flush_alarms()
        with self._stats_lock:
            self.stats['last_flush_time'] = datetime.now()

    def _requeue_failed(self, buf: deque[Any], items: list[Any], dropped_key: str) -> None:
        """将写失败记录按原序放回缓冲区头部；超限时保留较新记录并累加丢弃计数。"""
        with self._buffer_lock:
            maxlen = buf.maxlen
            combined = items + list(buf)
            if maxlen is not None and len(combined) > maxlen:
                overflow = len(combined) - maxlen
                self._bump(dropped_key, overflow)
                combined = combined[overflow:]
            buf.clear()
            buf.extend(combined)

    def _flush_telemetry(self):
        """刷新遥测数据缓冲区"""
        with self._buffer_lock:
            if not self._telemetry_buffer:
                return

            # 取出所有数据
            records = list(self._telemetry_buffer)
            self._telemetry_buffer.clear()

        try:
            # 批量写入
            if self.tdengine.write_telemetry_batch(records):
                self._bump('telemetry_written', len(records))
            else:
                self._bump('errors', len(records))
                self._bump('telemetry_requeued', len(records))
                self.logger.warning(
                    "遥测批量写入失败（%d 条），已回填缓冲区重试", len(records))
                self._requeue_failed(self._telemetry_buffer, records, 'telemetry_dropped')
                return
            self.logger.debug(f"写入 {len(records)} 条遥测数据")
        except Exception as e:
            self.logger.error(f"写入遥测数据失败: {e}")
            self._bump('errors', 1)
            self._bump('telemetry_requeued', len(records))
            self._requeue_failed(self._telemetry_buffer, records, 'telemetry_dropped')

    def _flush_alarms(self):
        """刷新报警数据缓冲区"""
        with self._buffer_lock:
            if not self._alarm_buffer:
                return

            records = list(self._alarm_buffer)
            self._alarm_buffer.clear()

        try:
            failed = []
            for record in records:
                if not self.tdengine.write_alarm(record):
                    self._bump('errors', 1)
                    failed.append(record)
                    continue
                self._bump('alarms_written', 1)
            if failed:
                self._bump('alarms_requeued', len(failed))
                self.logger.warning(
                    "报警写入失败 %d 条，已回填缓冲区重试", len(failed))
                self._requeue_failed(self._alarm_buffer, failed, 'alarms_dropped')
                return
            self.logger.debug(f"写入 {len(records)} 条报警数据")
        except Exception as e:
            self.logger.error("写入报警数据失败（%d 条），已回填缓冲区: %s",
                              len(records), e)
            self._bump('errors', 1)
            self._bump('alarms_requeued', len(records))
            self._requeue_failed(self._alarm_buffer, records, 'alarms_dropped')

    def _bump(self, key: str, amount: int = 1) -> None:
        """线程安全地累加统计计数。"""
        with self._stats_lock:
            self.stats[key] += amount

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        with self._stats_lock:
            stats = self.stats.copy()
        stats['running'] = self.running
        stats['telemetry_buffer_size'] = len(self._telemetry_buffer)
        stats['alarm_buffer_size'] = len(self._alarm_buffer)
        return stats


class OEEDataWriter:
    """
    OEE数据写入器

    将OEE计算结果写入TDengine。
    """

    def __init__(self, tdengine: TDengineClient):
        self.tdengine = tdengine
        self.logger = logging.getLogger("OEEDataWriter")

    def write_oee(self, device_id: str, availability: float, performance: float,
                  quality_rate: float, oee: float, total_count: int = 0,
                  good_count: int = 0, run_time: float = 0, downtime: float = 0):
        """写入OEE数据"""
        record = OEERecord(
            device_id=device_id,
            timestamp=datetime.now(),
            availability=availability,
            performance=performance,
            quality_rate=quality_rate,
            oee=oee,
            total_count=total_count,
            good_count=good_count,
            run_time=run_time,
            downtime=downtime
        )

        try:
            # round 372：**返回值必须检查** —— `write_*` 失败是返回 False
            # 而不是抛异常，原来这里只 catch 异常，TDengine 挂了也静默。
            if not self.tdengine.write_oee(record):
                self.logger.error("写入OEE数据失败：TDengine 写入返回 False")
                return False
            return True
        except Exception as e:
            self.logger.error(f"写入OEE数据失败: {e}")
            return False


class EnergyDataWriter:
    """
    能源数据写入器

    将能源管理数据写入TDengine。
    """

    def __init__(self, tdengine: TDengineClient):
        self.tdengine = tdengine
        self.logger = logging.getLogger("EnergyDataWriter")

    def write_energy(self, device_id: str, power: float, energy: float,
                     voltage: float = 0, current: float = 0, power_factor: float = 1.0):
        """写入能源数据"""
        record = EnergyRecord(
            device_id=device_id,
            timestamp=datetime.now(),
            power=power,
            energy=energy,
            voltage=voltage,
            current=current,
            power_factor=power_factor
        )

        try:
            if not self.tdengine.write_energy(record):
                self.logger.error("写入能源数据失败：TDengine 写入返回 False")
                return False
            return True
        except Exception as e:
            self.logger.error(f"写入能源数据失败: {e}")
            return False


class PredictiveDataWriter:
    """
    预测性维护数据写入器

    将预测性维护结果写入TDengine。
    """

    def __init__(self, tdengine: TDengineClient):
        self.tdengine = tdengine
        self.logger = logging.getLogger("PredictiveDataWriter")

    def write_predictive(self, device_id: str, health_score: float,
                         failure_probability: float, remaining_life: float,
                         anomaly_score: float = 0, trend: str = "stable"):
        """写入预测性维护数据"""
        record = PredictiveRecord(
            device_id=device_id,
            timestamp=datetime.now(),
            health_score=health_score,
            failure_probability=failure_probability,
            remaining_life=remaining_life,
            anomaly_score=anomaly_score,
            trend=trend
        )

        try:
            if not self.tdengine.write_predictive(record):
                self.logger.error("写入预测性维护数据失败：TDengine 写入返回 False")
                return False
            return True
        except Exception as e:
            self.logger.error(f"写入预测性维护数据失败: {e}")
            return False


# 测试代码
if __name__ == "__main__":
    import time

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # 创建客户端
    tdengine = TDengineClient("localhost", 6041)
    subscriber = MQTTSubscriber("localhost", 1883)

    # 创建服务
    service = MQTTToTSDBService(subscriber, tdengine)

    try:
        # 启动服务
        service.start()

        print("服务已启动，等待数据... (按 Ctrl+C 停止)")

        while True:
            time.sleep(10)
            stats = service.get_stats()
            print(f"统计: {stats}")

    except KeyboardInterrupt:
        print("正在停止...")
    finally:
        service.stop()
