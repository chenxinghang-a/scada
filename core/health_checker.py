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
        self._history_lock = threading.Lock()
        self.history = []
        self.max_history = 100
        # 当前正在执行检查的守护线程（用于检测上一次检查是否卡死）
        self._worker: Optional[threading.Thread] = None

    def _record_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """记录检查结果到状态与历史"""
        self.last_check = datetime.now()
        self.last_result = result

        with self._history_lock:
            self.history.append(result)
            if len(self.history) > self.max_history:
                self.history.pop(0)

        return result

    def run(self) -> Dict[str, Any]:
        """
        运行健康检查（带超时强制执行）

        超时必须有明确结果：检查项被标记为 unhealthy + TimeoutError，
        绝不允许静默忽略超时（否则健康检查自身会永久挂死）。

        Returns:
            检查结果
        """
        start_time = time.monotonic()

        # 上一次检查仍未返回（检查函数卡死）：不再叠加新线程，直接判定超时
        if self._worker is not None and self._worker.is_alive():
            return self._record_result({
                'status': HealthStatus.UNHEALTHY,
                'message': f'健康检查超时（{self.timeout}秒），上一次检查仍未结束',
                'details': {'error': 'TimeoutError'},
                'duration': 0.0,
                'timestamp': datetime.now().isoformat()
            })

        outcome: Dict[str, Any] = {}

        def _target():
            try:
                outcome['value'] = self.check_func()
            except BaseException as e:  # noqa: BLE001 - 检查函数任意异常都要转成结果
                outcome['error'] = e

        # 用守护线程执行：join(timeout) 到点即返回，守护线程不会阻塞进程退出。
        # 不能用 `with ThreadPoolExecutor(...)`：其 __exit__ 会 shutdown(wait=True)，
        # 卡死的检查函数会让超时形同虚设、run() 永久阻塞。
        self._worker = threading.Thread(
            target=_target, daemon=True, name=f'health-check-{self.name}')
        self._worker.start()
        self._worker.join(timeout=self.timeout)

        duration = time.monotonic() - start_time

        if self._worker.is_alive():
            # 超时：明确标记为不健康，不静默忽略
            return self._record_result({
                'status': HealthStatus.UNHEALTHY,
                'message': f'健康检查超时（{self.timeout}秒）',
                'details': {'error': 'TimeoutError'},
                'duration': duration,
                'timestamp': datetime.now().isoformat()
            })

        if 'error' in outcome:
            e = outcome['error']
            return self._record_result({
                'status': HealthStatus.UNHEALTHY,
                'message': str(e),
                'details': {'error': type(e).__name__},
                'duration': duration,
                'timestamp': datetime.now().isoformat()
            })

        check_result = outcome.get('value')
        result = {
            'status': HealthStatus.HEALTHY,
            'message': 'OK',
            'details': check_result if isinstance(check_result, dict) else {},
            'duration': duration,
            'timestamp': datetime.now().isoformat()
        }
        # 检查返回值是否标记了不健康
        if isinstance(check_result, dict) and check_result.get('status'):
            result['status'] = check_result['status']
            result['message'] = check_result.get('message', 'OK')

        return self._record_result(result)
    
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

        with check._history_lock:
            return list(check.history[-limit:])
    
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
            if cls._periodic_thread and cls._periodic_thread.is_alive():
                cls._periodic_thread.join(timeout=30)
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
                from core.module_registry import ModuleRegistry
                alarm_manager = ModuleRegistry.get_instance('alarm_manager')
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

        # 数据新鲜度 —— 补上「采集在跑、数据库能连，但数据就是进不了库」这类静默故障。
        # 原有的 database 检查只做 SELECT 1、collector 检查只看内存队列长度，
        # 二者在 2026-09-20 的落库停滞故障中全部返回 healthy（详见 _check_data_freshness）。
        if database:
            cls.register(
                'data_freshness',
                lambda: _check_data_freshness(database, data_collector),
                interval=30,
            )

        cls.register('disk', _check_disk_space, interval=60)
        cls.register('memory', _check_memory, interval=30)

        logger.info(f"默认健康检查已注册: database={database is not None}, "
                     f"devices={device_manager is not None}, collector={data_collector is not None}")


def _check_database(db):
    """检查数据库连接"""
    try:
        with db.get_connection() as conn:
            conn.execute("SELECT 1")
        return {'status': HealthStatus.HEALTHY, 'message': '数据库连接正常'}
    except Exception as e:
        return {'status': HealthStatus.UNHEALTHY, 'message': f'数据库连接失败: {e}'}


def _check_devices(dm):
    """检查设备状态"""
    try:
        status = dm.get_all_status()
        # get_all_status() may return list or dict
        if isinstance(status, dict):
            status_list = list(status.values())
        else:
            status_list = list(status)
        total = len(status_list)
        if total == 0:
            return {'status': HealthStatus.HEALTHY, 'message': '无设备配置'}
        connected = sum(1 for s in status_list if s.get('connected'))
        fault = sum(1 for s in status_list if s.get('status') == 'fault')

        if fault > 0:
            return {'status': HealthStatus.DEGRADED, 'message': f'{fault}/{total}设备故障', 'details': {'fault': fault}}
        elif connected < total * 0.5:
            return {'status': HealthStatus.DEGRADED, 'message': f'仅{connected}/{total}设备在线'}
        return {'status': HealthStatus.HEALTHY, 'message': f'{connected}/{total}设备在线'}
    except Exception as e:
        return {'status': HealthStatus.UNHEALTHY, 'message': f'设备检查失败: {e}'}


# 数据新鲜度阈值（秒）：超过 STALE_UNHEALTHY 说明落库链路已经断了
STALE_DEGRADED_SECONDS = 30
STALE_UNHEALTHY_SECONDS = 120
# 磁盘队列积压阈值
QUEUE_DEGRADED_BYTES = 10 * 1024 * 1024
QUEUE_UNHEALTHY_BYTES = 50 * 1024 * 1024


def _check_data_freshness(db, data_collector=None):
    """检查数据新鲜度与磁盘队列积压。

    为什么需要这一项（2026-09-20 实机故障）：
      采集器 running=True、`/api/health/status` 的 database 检查（只做 SELECT 1）
      通过、collector 检查只看**内存**队列长度（当时为 0）—— 于是 5 项检查全绿，
      而实际上落库已停滞数小时、磁盘队列涨到 160MB。
      纯连接性检查无法发现「链路活着但不干活」。

    本检查观察两个互补信号：
      1. 数据新鲜度：最新历史记录距今多久（直接反映数据是否真的入库）
      2. 磁盘队列积压：pending_data.jsonl 体积（消费停摆时会无界增长）
      外加一个交叉信号：磁盘有积压而内存队列为空 → 消费链路停摆。
    """
    from datetime import datetime as _dt

    problems = []
    status = HealthStatus.HEALTHY
    details = {}

    # --- 1) 数据新鲜度 ---
    try:
        with db.get_connection() as conn:
            row = conn.execute('SELECT MAX(timestamp) FROM history_data').fetchone()
        latest = row[0] if row else None
        if latest is None:
            # 空库不算故障（刚部署/刚清库），但也不能报 healthy 掩盖数据是否存在
            details['latest_data_age_seconds'] = None
            problems.append('历史表中没有任何数据')
            if status == HealthStatus.HEALTHY:
                status = HealthStatus.UNKNOWN
        else:
            if isinstance(latest, str):
                latest_dt = _dt.fromisoformat(latest.replace('Z', '+00:00').split('+')[0])
            else:
                latest_dt = latest
            age = (_dt.now() - latest_dt).total_seconds()
            details['latest_data_age_seconds'] = round(age, 1)
            details['latest_data_time'] = str(latest)
            if age > STALE_UNHEALTHY_SECONDS:
                status = HealthStatus.UNHEALTHY
                problems.append(
                    f'落库停滞：最新数据距今 {age:.0f}s（阈值 {STALE_UNHEALTHY_SECONDS}s）'
                )
            elif age > STALE_DEGRADED_SECONDS and status == HealthStatus.HEALTHY:
                status = HealthStatus.DEGRADED
                problems.append(f'数据新鲜度下降：最新数据距今 {age:.0f}s')
    except Exception as e:
        # 查不了库本身就是问题，不能当作正常
        status = HealthStatus.UNHEALTHY
        problems.append(f'数据新鲜度检查失败: {e}')

    # --- 2) 磁盘队列积压 ---
    persist_file = None
    try:
        persist_file = getattr(getattr(data_collector, 'data_queue', None), '_persist_file', None)
    except Exception:
        persist_file = None

    if persist_file is not None:
        try:
            from pathlib import Path as _Path

            pf = _Path(persist_file)
            if pf.exists():
                size = pf.stat().st_size
                details['queue_persist_bytes'] = size
                if size > QUEUE_UNHEALTHY_BYTES:
                    status = HealthStatus.UNHEALTHY
                    problems.append(f'磁盘队列积压严重: {size / 1048576:.1f}MB')
                elif size > QUEUE_DEGRADED_BYTES and status != HealthStatus.UNHEALTHY:
                    status = HealthStatus.DEGRADED
                    problems.append(f'磁盘队列积压: {size / 1048576:.1f}MB')
            else:
                details['queue_persist_bytes'] = 0
        except Exception as e:
            problems.append(f'队列积压检查失败: {e}')

    # --- 3) 交叉信号：磁盘有积压但内存队列为空 → 消费链路停摆 ---
    try:
        in_mem = data_collector.data_queue.qsize() if data_collector is not None else None
        details['queue_in_memory'] = in_mem
        if in_mem == 0 and details.get('queue_persist_bytes', 0) > QUEUE_DEGRADED_BYTES:
            problems.append('内存队列为空但磁盘队列有积压 —— 消费链路可能已停摆')
            if status == HealthStatus.HEALTHY:
                status = HealthStatus.DEGRADED
    except Exception:
        pass

    message = '; '.join(problems) if problems else '数据新鲜度与队列正常'
    return {'status': status, 'message': message, 'details': details}


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
