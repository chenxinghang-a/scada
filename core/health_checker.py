"""
健康检查器
监控各模块的健康状态
"""

import logging
import time
import threading
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class HealthStatus:
    """健康状态"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class HealthCheck:
    """健康检查项"""
    def __init__(self, name: str, check_func: Callable[[], Dict[str, Any]], 
                 interval: int = 60, timeout: int = 10):
        """
        初始化健康检查
        
        Args:
            name: 检查名称
            check_func: 检查函数，返回 {'status': str, 'message': str, 'details': dict}
            interval: 检查间隔（秒）
            timeout: 超时时间（秒）
        """
        self.name = name
        self.check_func = check_func
        self.interval = interval
        self.timeout = timeout
        self.last_check = None
        self.last_result = None
        self.history = []
        self.max_history = 100
    
    def run(self) -> Dict[str, Any]:
        """
        运行健康检查（带超时强制执行）

        Returns:
            检查结果
        """
        start_time = time.time()

        # 在子线程中执行检查函数，以便强制超时
        result_container = [None]
        exception_container = [None]

        def _target():
            try:
                result_container[0] = self.check_func()
            except Exception as e:
                exception_container[0] = e

        worker = threading.Thread(target=_target, daemon=True)
        worker.start()
        worker.join(timeout=self.timeout)

        duration = time.time() - start_time

        if worker.is_alive():
            # 超时：子线程仍在运行
            result = {
                'status': HealthStatus.UNHEALTHY,
                'message': f'健康检查超时（{self.timeout}秒）',
                'details': {'error': 'TimeoutError'},
                'duration': duration,
                'timestamp': datetime.now().isoformat()
            }
        elif exception_container[0] is not None:
            e = exception_container[0]
            result = {
                'status': HealthStatus.UNHEALTHY,
                'message': str(e),
                'details': {'error': type(e).__name__},
                'duration': duration,
                'timestamp': datetime.now().isoformat()
            }
        else:
            result = result_container[0]
            # 确保结果包含必要字段
            if 'status' not in result:
                result['status'] = HealthStatus.UNKNOWN
            if 'message' not in result:
                result['message'] = ''
            if 'details' not in result:
                result['details'] = {}

            result['duration'] = duration
            result['timestamp'] = datetime.now().isoformat()

        # 更新状态
        self.last_check = datetime.now()
        self.last_result = result

        # 记录历史
        self.history.append(result)
        if len(self.history) > self.max_history:
            self.history.pop(0)

        return result
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'name': self.name,
            'interval': self.interval,
            'timeout': self.timeout,
            'last_check': self.last_check.isoformat() if self.last_check else None,
            'last_result': self.last_result,
            'history_count': len(self.history)
        }


class HealthChecker:
    """
    健康检查器
    
    管理和运行所有健康检查
    """
    
    _lock = threading.Lock()
    _checks: Dict[str, HealthCheck] = {}
    _global_status = HealthStatus.UNKNOWN
    
    @classmethod
    def register(cls, name: str, check_func: Callable[[], Dict[str, Any]], 
                 interval: int = 60, timeout: int = 10):
        """
        注册健康检查
        
        Args:
            name: 检查名称
            check_func: 检查函数
            interval: 检查间隔（秒）
            timeout: 超时时间（秒）
        """
        with cls._lock:
            cls._checks[name] = HealthCheck(name, check_func, interval, timeout)
        logger.info(f"注册健康检查: {name}")
    
    @classmethod
    def unregister(cls, name: str):
        """
        取消注册健康检查
        
        Args:
            name: 检查名称
        """
        with cls._lock:
            if name in cls._checks:
                del cls._checks[name]
                logger.info(f"取消注册健康检查: {name}")
    
    @classmethod
    def check(cls, name: str = None) -> Dict[str, Any]:
        """
        运行健康检查
        
        Args:
            name: 检查名称（None则运行所有）
            
        Returns:
            检查结果
        """
        if name:
            with cls._lock:
                check = cls._checks.get(name)
            if not check:
                return {'status': HealthStatus.UNKNOWN, 'message': f'检查 {name} 未注册'}
            return check.run()

        # 快照当前检查列表（避免长时间持锁）
        with cls._lock:
            checks_snapshot = dict(cls._checks)

        # 运行所有检查
        results = {}
        overall_status = HealthStatus.HEALTHY

        for check_name, check in checks_snapshot.items():
            result = check.run()
            results[check_name] = result

            # 更新整体状态
            if result['status'] == HealthStatus.UNHEALTHY:
                overall_status = HealthStatus.UNHEALTHY
            elif result['status'] == HealthStatus.DEGRADED and overall_status != HealthStatus.UNHEALTHY:
                overall_status = HealthStatus.DEGRADED

        with cls._lock:
            cls._global_status = overall_status

        return {
            'status': overall_status,
            'checks': results,
            'timestamp': datetime.now().isoformat()
        }
    
    @classmethod
    def get_status(cls) -> Dict[str, Any]:
        """
        获取健康状态概览
        
        Returns:
            健康状态信息
        """
        with cls._lock:
            checks_status = {}
            for name, check in cls._checks.items():
                checks_status[name] = {
                    'status': check.last_result['status'] if check.last_result else HealthStatus.UNKNOWN,
                    'last_check': check.last_check.isoformat() if check.last_check else None
                }

            return {
                'global_status': cls._global_status,
                'checks': checks_status,
                'total_checks': len(cls._checks)
            }
    
    @classmethod
    def get_history(cls, name: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取检查历史
        
        Args:
            name: 检查名称
            limit: 返回数量限制
            
        Returns:
            检查历史列表
        """
        with cls._lock:
            check = cls._checks.get(name)
        if not check:
            return []

        return check.history[-limit:]
    
    @classmethod
    def clear(cls):
        """清除所有检查"""
        with cls._lock:
            cls._checks.clear()
            cls._global_status = HealthStatus.UNKNOWN
        logger.debug("清除所有健康检查")

    # ================================================================
    # 自动周期健康检查
    # ================================================================

    _periodic_thread: threading.Thread | None = None
    _stop_event: threading.Event | None = None

    @classmethod
    def start_periodic_checks(cls, interval: int = 30):
        """
        启动自动周期健康检查

        Args:
            interval: 检查间隔（秒），默认30秒
        """
        if hasattr(cls, '_periodic_thread') and cls._periodic_thread and cls._periodic_thread.is_alive():
            logger.warning("周期健康检查已在运行，忽略重复启动")
            return

        cls._stop_event = threading.Event()

        def _periodic_loop():
            while not cls._stop_event.wait(interval):
                try:
                    result = cls.check()
                    # 如果有新的不健康项，触发告警
                    checks = result.get('checks', {})
                    unhealthy = [name for name, r in checks.items() if r.get('status') == HealthStatus.UNHEALTHY]
                    degraded = [name for name, r in checks.items() if r.get('status') == HealthStatus.DEGRADED]
                    if unhealthy or degraded:
                        result['unhealthy_checks'] = unhealthy
                        result['degraded_checks'] = degraded
                        cls._emit_health_alert(result)
                except Exception as e:
                    logger.error(f"周期健康检查异常: {e}")

        cls._periodic_thread = threading.Thread(target=_periodic_loop, daemon=True, name="health-checker")
        cls._periodic_thread.start()
        logger.info(f"健康检查已启动，间隔 {interval}s")

    @classmethod
    def stop_periodic_checks(cls):
        """停止周期检查"""
        if hasattr(cls, '_stop_event') and cls._stop_event:
            cls._stop_event.set()
            cls._periodic_thread = None
            logger.info("周期健康检查已停止")

    @classmethod
    def _emit_health_alert(cls, result: dict):
        """健康降级时触发告警（最佳努力通知）"""
        try:
            degraded = result.get('degraded_checks', [])
            unhealthy = result.get('unhealthy_checks', [])

            if unhealthy:
                logger.error(f"系统不健康项: {unhealthy}")
            if degraded:
                logger.warning(f"系统降级项: {degraded}")

            # 尝试通过报警管理器发送告警
            try:
                from 报警层.alarm_manager import AlarmManager
                # 通过ModuleRegistry获取alarm_manager实例
                from core.module_registry import ModuleRegistry
                alarm_manager = ModuleRegistry.get('alarm_manager')
                if alarm_manager:
                    for check_name in unhealthy:
                        alarm_manager._emit_websocket_alarm({
                            'alarm_id': f'health_{check_name}',
                            'device_id': 'system',
                            'register_name': 'health',
                            'alarm_level': 'critical',
                            'alarm_message': f'健康检查不健康: {check_name}',
                            'threshold': 0,
                            'actual_value': 0,
                            'timestamp': datetime.now().isoformat(),
                            'area': 'system',
                            'dedup_key': f'health:{check_name}:unhealthy',
                        })
                    for check_name in degraded:
                        alarm_manager._emit_websocket_alarm({
                            'alarm_id': f'health_{check_name}',
                            'device_id': 'system',
                            'register_name': 'health',
                            'alarm_level': 'warning',
                            'alarm_message': f'健康检查降级: {check_name}',
                            'threshold': 0,
                            'actual_value': 0,
                            'timestamp': datetime.now().isoformat(),
                            'area': 'system',
                            'dedup_key': f'health:{check_name}:degraded',
                        })
            except Exception as inner_e:
                logger.debug(f"报警管理器通知跳过: {inner_e}")
        except Exception as e:
            logger.error(f"健康告警发送失败: {e}")

    # ================================================================
    # 内置健康检查
    # ================================================================

    @classmethod
    def register_default_checks(cls, database=None, device_manager=None, data_collector=None):
        """
        注册默认健康检查项

        Args:
            database: 数据库实例
            device_manager: 设备管理器实例
            data_collector: 数据采集器实例
        """
        if database:
            cls.register('database', lambda: _check_database(database), interval=30)

        if device_manager:
            cls.register('devices', lambda: _check_devices(device_manager), interval=15)

        if data_collector:
            cls.register('collector', lambda: _check_collector(data_collector), interval=10)

        cls.register('disk', _check_disk_space, interval=60)
        cls.register('memory', _check_memory, interval=30)

        logger.info(f"默认健康检查已注册: database={database is not None}, "
                     f"devices={device_manager is not None}, collector={data_collector is not None}")


def _check_database(db):
    """检查数据库连接"""
    try:
        conn = db.get_connection()
        conn.execute("SELECT 1")
        conn.close()
        return {'status': HealthStatus.HEALTHY, 'message': '数据库连接正常'}
    except Exception as e:
        return {'status': HealthStatus.UNHEALTHY, 'message': f'数据库连接失败: {e}'}


def _check_devices(dm):
    """检查设备状态"""
    try:
        status = dm.get_all_status()
        total = len(status)
        if total == 0:
            return {'status': HealthStatus.HEALTHY, 'message': '无设备配置'}
        connected = sum(1 for s in status.values() if s.get('connected'))
        fault = sum(1 for s in status.values() if s.get('status') == 'fault')

        if fault > 0:
            return {'status': HealthStatus.DEGRADED, 'message': f'{fault}/{total}设备故障', 'details': {'fault': fault}}
        elif connected < total * 0.5:
            return {'status': HealthStatus.DEGRADED, 'message': f'仅{connected}/{total}设备在线'}
        return {'status': HealthStatus.HEALTHY, 'message': f'{connected}/{total}设备在线'}
    except Exception as e:
        return {'status': HealthStatus.UNHEALTHY, 'message': f'设备检查失败: {e}'}


def _check_collector(dc):
    """检查数据采集器"""
    try:
        stats = dc.get_stats()
        running = stats.get('running', False)
        queue_size = stats.get('queue_size', 0)
        if not running:
            return {'status': HealthStatus.UNHEALTHY, 'message': '采集器未运行'}
        if queue_size > 10000:
            return {'status': HealthStatus.DEGRADED, 'message': f'数据队列积压: {queue_size}'}
        return {'status': HealthStatus.HEALTHY, 'message': f'采集正常, 队列: {queue_size}'}
    except Exception as e:
        return {'status': HealthStatus.UNHEALTHY, 'message': f'采集器检查失败: {e}'}


def _check_disk_space():
    """检查磁盘空间"""
    import shutil
    try:
        usage = shutil.disk_usage('.')
        free_gb = usage.free / (1024**3)
        if free_gb < 1:
            return {'status': HealthStatus.UNHEALTHY, 'message': f'磁盘空间不足: {free_gb:.1f}GB'}
        elif free_gb < 5:
            return {'status': HealthStatus.DEGRADED, 'message': f'磁盘空间偏低: {free_gb:.1f}GB'}
        return {'status': HealthStatus.HEALTHY, 'message': f'磁盘空间充足: {free_gb:.1f}GB'}
    except Exception as e:
        return {'status': HealthStatus.UNKNOWN, 'message': f'磁盘检查失败: {e}'}


def _check_memory():
    """检查内存使用"""
    try:
        import psutil
        mem = psutil.virtual_memory()
        if mem.percent > 95:
            return {'status': HealthStatus.UNHEALTHY, 'message': f'内存使用率: {mem.percent}%'}
        elif mem.percent > 85:
            return {'status': HealthStatus.DEGRADED, 'message': f'内存使用率偏高: {mem.percent}%'}
        return {'status': HealthStatus.HEALTHY, 'message': f'内存使用率: {mem.percent}%'}
    except ImportError:
        return {'status': HealthStatus.HEALTHY, 'message': 'psutil未安装，跳过内存检查'}
