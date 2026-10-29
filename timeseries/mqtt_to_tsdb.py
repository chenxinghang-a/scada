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
except ImportError:
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from gateway import MQTTSubscriber, DeviceTelemetry, DeviceStatus, AlarmMessage
from .tdengine_client import TDengineClient
from .data_models import (
    TelemetryRecord, AlarmRecord, OEERecord, EnergyRecord, PredictiveRecord
)


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

        # 运行状态
        self.running = False
        self._flush_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

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

    def start(self):
        """启动服务"""
        if self.running:
            self.logger.warning("服务已在运行")
            return

        self.logger.info("启动MQTT到TDengine数据写入服务...")

        # 连接TDengine
        if not self.tdengine.connect():
            self.logger.error("TDengine连接失败")
            return

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
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()

        self.logger.info("服务已启动")

    def stop(self):
        """停止服务"""
        if not self.running:
            return

        self.logger.info("停止服务...")
        self.running = False
        self._stop_event.set()

        # 刷新剩余数据
        self._flush_buffers()

        # 等待刷新线程结束
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=5)

        # 停止MQTT订阅
        self.subscriber.stop()

        # 断开TDengine
        self.tdengine.disconnect()

        self.logger.info("服务已停止")

    def _on_telemetry(self, telemetry: DeviceTelemetry):
        """处理遥测数据"""
        try:
            self.stats['telemetry_received'] += 1

            # 转换为TDengine记录
            timestamp = datetime.fromtimestamp(telemetry.Timestamp)

            for register_name, metric in telemetry.Metrics.items():
                record = TelemetryRecord(
                    device_id=telemetry.DeviceID,
                    register_name=register_name,
                    timestamp=timestamp,
                    value=metric['value'],
                    quality=metric.get('quality', 192),
                    unit=metric.get('unit', ''),
                    protocol=telemetry.Protocol,
                    gateway_id=telemetry.GatewayID
                )

                with self._buffer_lock:
                    # round 321：deque 满时会**静默挤掉最旧的一条**，
                    # 以前没人知道丢了多少 —— 这里显式计数。
                    if (self._telemetry_buffer.maxlen is not None
                            and len(self._telemetry_buffer) >= self._telemetry_buffer.maxlen):
                        self.stats['telemetry_dropped'] += 1
                    self._telemetry_buffer.append(record)

            # 检查是否需要刷新
            if len(self._telemetry_buffer) >= self.batch_size:
                self._flush_telemetry()

        except Exception as e:
            self.logger.error(f"处理遥测数据异常: {e}")
            self.stats['errors'] += 1

    def _on_alarm(self, alarm: AlarmMessage):
        """处理报警数据"""
        try:
            self.stats['alarms_received'] += 1

            record = AlarmRecord(
                alarm_id=alarm.AlarmID,
                device_id=alarm.DeviceID,
                timestamp=datetime.fromtimestamp(alarm.Timestamp),
                level=alarm.Level,
                alarm_type=alarm.Type,
                message=alarm.Message,
                value=alarm.Value,
                threshold=alarm.Threshold,
                acknowledged=alarm.Acknowledged
            )

            with self._buffer_lock:
                if (self._alarm_buffer.maxlen is not None
                        and len(self._alarm_buffer) >= self._alarm_buffer.maxlen):
                    self.stats['alarms_dropped'] += 1
                self._alarm_buffer.append(record)

            # 报警立即写入
            self._flush_alarms()

        except Exception as e:
            self.logger.error(f"处理报警数据异常: {e}")
            self.stats['errors'] += 1

    def _flush_loop(self):
        """定时刷新循环"""
        while self.running and not self._stop_event.is_set():
            try:
                self._stop_event.wait(self.flush_interval)
                if self.running:
                    self._flush_buffers()
            except Exception as e:
                self.logger.error(f"刷新异常: {e}")
                self.stats['errors'] += 1

    def _flush_buffers(self):
        """刷新所有缓冲区"""
        self._flush_telemetry()
        self._flush_alarms()
        self.stats['last_flush_time'] = datetime.now()

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
            # round 298：写失败必须回填并计数（原来返回值被丢弃，
            # 失败时 except 分支永远不进 —— 数据在 clear() 之后彻底消失）
            if self.tdengine.write_telemetry_batch(records):
                self.stats['telemetry_written'] += len(records)
            else:
                # round 321：**写失败必须回填**。
                # round 298 只补了"计数"（原来返回值被丢弃、except 永远不进），
                # 但 False 分支**没有把 records 放回缓冲区** ——
                # 注释写着"写失败必须回填并计数"，代码只做了后一半。
                # 后果：TDengine 短暂不可用（重启/网络抖动）期间，
                # 每 5 秒一批数据**静默消失**，而统计里只有 errors 涨。
                self.stats['errors'] += len(records)
                self.stats['telemetry_requeued'] += len(records)
                self.logger.warning(
                    "遥测批量写入失败（%d 条），已回填缓冲区重试", len(records))
                with self._buffer_lock:
                    self._telemetry_buffer.extend(records)
            self.logger.debug(f"写入 {len(records)} 条遥测数据")
        except Exception as e:
            self.logger.error(f"写入遥测数据失败: {e}")
            self.stats['errors'] += 1

            # 将失败的数据放回缓冲区
            with self._buffer_lock:
                self._telemetry_buffer.extend(records)

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
                    self.stats['errors'] += 1
                    failed.append(record)
                    continue
                self.stats['alarms_written'] += 1
            if failed:
                # round 321：报警比遥测更不能丢 —— 写失败的必须回填。
                # 原来 `continue` 直接把这条丢了（报警丢了等于"没发生过"）。
                self.stats['alarms_requeued'] += len(failed)
                self.logger.warning(
                    "报警写入失败 %d 条，已回填缓冲区重试", len(failed))
                with self._buffer_lock:
                    self._alarm_buffer.extend(failed)
            self.logger.debug(f"写入 {len(records)} 条报警数据")
        except Exception as e:
            self.logger.error(f"写入报警数据失败: {e}")
            self.stats['errors'] += 1

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
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
            self.tdengine.write_oee(record)
        except Exception as e:
            self.logger.error(f"写入OEE数据失败: {e}")


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
            self.tdengine.write_energy(record)
        except Exception as e:
            self.logger.error(f"写入能源数据失败: {e}")


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
            self.tdengine.write_predictive(record)
        except Exception as e:
            self.logger.error(f"写入预测性维护数据失败: {e}")


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
