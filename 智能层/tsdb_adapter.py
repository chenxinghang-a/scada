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

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

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
                 config: Dict = None):
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
        self._sync_thread: Optional[threading.Thread] = None
        self._result_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # 设备注册表（需要同步的设备）
        self._registered_devices: Dict[str, Dict] = {}
        
        # 统计信息
        self.stats = {
            'sync_cycles': 0,
            'records_synced': 0,
            'results_written': 0,
            'errors': 0,
            'last_sync_time': None,
            'last_result_time': None
        }
        
        self._lock = threading.Lock()
        
        logger.info("TDengine智能层适配器初始化完成")
    
    def register_device(self, device_id: str, registers: List[Dict]):
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
                with self._lock:
                    self._registered_devices[device_id]['last_sync'] = end_time
                
            except Exception as e:
                logger.error(f"同步设备 {device_id} 数据失败: {e}")
                self.stats['errors'] += 1
        
        self.stats['sync_cycles'] += 1
        self.stats['last_sync_time'] = datetime.now().isoformat()
    
    def _feed_to_modules(self, device_id: str, register_name: str, data: List[Dict]):
        """将数据喂入各智能层模块"""
        
        for record in data:
            value = record.get('value')
            timestamp_str = record.get('timestamp')
            
            if value is None:
                continue
            
            # 解析时间戳
            if isinstance(timestamp_str, str):
                try:
                    timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                except:
                    timestamp = datetime.now()
            else:
                timestamp = datetime.now()
            
            # 预测性维护 — 喂入所有数值数据
            if self.predictive_maintenance:
                self.predictive_maintenance.feed_data(
                    device_id, register_name, float(value), timestamp
                )
            
            # SPC — 质量相关数据
            if self.spc_analyzer:
                spc_keywords = ['temperature', 'pressure', 'ph', 'quality', 'dimension',
                                'voltage', 'current', 'speed', 'flow', 'level',
                                'humidity', 'torque', 'frequency', 'thickness']
                if any(kw in register_name.lower() for kw in spc_keywords):
                    self.spc_analyzer.feed_data(device_id, register_name, float(value))
            
            # 能源管理 — 电力数据
            if self.energy_manager:
                if 'power' in register_name.lower() or 'watt' in register_name.lower():
                    self.energy_manager.feed_power_data(
                        device_id, float(value), timestamp=timestamp
                    )
                elif 'energy' in register_name.lower() or 'kwh' in register_name.lower():
                    self.energy_manager.feed_power_data(
                        device_id, 0, energy_kwh=float(value), timestamp=timestamp
                    )
            
            # OEE — 设备状态和产量
            if self.oee_calculator:
                if register_name == 'running_status':
                    status_map = {0: 'stopped', 1: 'running', 2: 'fault', 3: 'idle'}
                    status = status_map.get(int(float(value)), 'running')
                    self.oee_calculator.update_device_state(device_id, status)
                elif register_name in ('product_count', 'good_count', 'total_count'):
                    if register_name == 'product_count':
                        self.oee_calculator.record_production(device_id, count=int(float(value)))
                    elif register_name == 'good_count':
                        self.oee_calculator.record_production(device_id, good_count=int(float(value)))
    
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
                
                self.tdengine.write_oee(record)
                self.stats['results_written'] += 1
                
        except Exception as e:
            logger.error(f"写入OEE结果失败: {e}")
            self.stats['errors'] += 1
    
    def _write_predictive_results(self):
        """写入预测性维护结果"""
        try:
            health_scores = self.predictive_maintenance.get_health_scores()
            
            for key, score_data in health_scores.items():
                device_id = score_data.get('device_id')
                if not device_id:
                    continue
                
                failure_pred = score_data.get('failure_prediction')
                remaining_life = 0
                failure_probability = 0
                
                if failure_pred:
                    remaining_life = failure_pred.get('days_to_limit', 0) * 24  # 转换为小时
                    failure_probability = failure_pred.get('confidence', 0)
                
                record = PredictiveRecord(
                    device_id=device_id,
                    timestamp=datetime.now(),
                    health_score=score_data.get('health_score', 100),
                    failure_probability=failure_probability,
                    remaining_life=remaining_life,
                    anomaly_score=score_data.get('anomaly_count', 0),
                    trend=score_data.get('trend', {}).get('direction', 'stable')
                )
                
                self.tdengine.write_predictive(record)
                self.stats['results_written'] += 1
                
        except Exception as e:
            logger.error(f"写入预测性维护结果失败: {e}")
            self.stats['errors'] += 1
    
    def _write_energy_results(self):
        """写入能源数据"""
        try:
            # 获取所有设备的实时功率
            for device_id in self._registered_devices:
                power_data = self.energy_manager.realtime_power.get(device_id)
                if power_data:
                    record = EnergyRecord(
                        device_id=device_id,
                        timestamp=datetime.now(),
                        power=power_data.get('power_kw', 0),
                        energy=self.energy_manager.energy_accumulated.get(device_id, {}).get('energy_kwh', 0),
                        voltage=power_data.get('voltage', 0),
                        current=power_data.get('current', 0),
                        power_factor=power_data.get('power_factor', 1.0)
                    )
                    
                    self.tdengine.write_energy(record)
                    self.stats['results_written'] += 1
                    
        except Exception as e:
            logger.error(f"写入能源数据失败: {e}")
            self.stats['errors'] += 1
    
    def get_stats(self) -> Dict:
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
    
    def __init__(self, tdengine: TDengineClient):
        """
        初始化桥接器
        
        Args:
            tdengine: TDengine客户端
        """
        self.tdengine = tdengine
        self.logger = logging.getLogger("RealtimeDataBridge")
        
        # 批量写入缓冲
        from ..timeseries.data_models import TelemetryRecord
        self._buffer: List = []
        self._buffer_lock = threading.Lock()
        self._batch_size = 100
        self._flush_interval = 5.0
        
        # 运行状态
        self._running = False
        self._flush_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # 统计
        self.stats = {
            'records_received': 0,
            'records_written': 0,
            'errors': 0
        }
    
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
             timestamp: datetime = None, unit: str = "", protocol: str = "",
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
        from ..timeseries.data_models import TelemetryRecord
        
        record = TelemetryRecord(
            device_id=device_id,
            register_name=register_name,
            timestamp=timestamp or datetime.now(),
            value=float(value),
            quality=quality,
            unit=unit,
            protocol=protocol,
            gateway_id=gateway_id
        )
        
        with self._buffer_lock:
            self._buffer.append(record)
            self.stats['records_received'] += 1
        
        # 检查是否需要刷新
        if len(self._buffer) >= self._batch_size:
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
                self.stats['errors'] += 1
    
    def _flush_buffer(self):
        """刷新缓冲区"""
        with self._buffer_lock:
            if not self._buffer:
                return
            
            records = list(self._buffer)
            self._buffer.clear()
        
        try:
            self.tdengine.write_telemetry_batch(records)
            self.stats['records_written'] += len(records)
            self.logger.debug(f"写入 {len(records)} 条数据到TDengine")
        except Exception as e:
            self.logger.error(f"写入TDengine失败: {e}")
            self.stats['errors'] += 1
            
            # 将失败的数据放回缓冲区
            with self._buffer_lock:
                self._buffer.extend(records)
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        stats = self.stats.copy()
        stats['running'] = self._running
        stats['buffer_size'] = len(self._buffer)
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
