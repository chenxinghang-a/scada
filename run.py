"""
工业数据采集与监控系统启动脚本
"""

import sys
import os

# 解决PyInstaller打包后中文乱码问题
if sys.platform == 'win32':
    os.system('chcp 65001 >nul 2>&1')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')

import logging
import threading
from pathlib import Path
from datetime import datetime

# 初始化项目路径（必须在所有 import 之前）
import paths
paths.setup()

SYSTEM_START_TIME = datetime.now()

# 配置结构化日志（等保2.0 GB/T 22239）
from core.structured_logging import setup_logging, get_logger
from config import LogConfig
setup_logging(
    log_dir=str(LogConfig.LOG_DIR),
    log_level=LogConfig.LEVEL,
    json_format=LogConfig.LOG_JSON,
    rotation=LogConfig.LOG_ROTATION,
    retention=LogConfig.LOG_RETENTION,
)
logger = get_logger(__name__)

# 配置Schema验证
from core.config_validator import validate_startup_configs


def main():
    """主函数"""
    try:
        logger.info("正在启动工业数据采集与监控系统...")

        # 启动时验证配置文件Schema
        validate_startup_configs()

        from 存储层.database import Database
        from 采集层.real_device_manager import RealDeviceManager
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        from 报警层.alarm_manager import AlarmManager
        from 采集层.data_collector import DataCollector
        from 展示层.routes import create_app

        # 判断运行模式
        if '--simulator' in sys.argv:
            simulation_mode = False
            config_path = paths.get_config_path('devices_modbus_sim.yaml')
            db_path = paths.get_db_path('simulator')
            logger.info("模拟器模式：连接外部 Modbus TCP 模拟器（真实协议）")
        elif '--real' in sys.argv:
            simulation_mode = False
            config_path = paths.get_config_path('devices_real.yaml')
            db_path = paths.get_db_path('real')
            # 生产配置不得静默回退（审计 P0：生产配置自动回退 = 0）。
            # devices_real.yaml 为空（devices: []）或文件缺失时，必须明确报错并退出，
            # 绝不允许悄悄改用混合了真实 IP 的 devices.yaml。
            # 显式逃生口：--allow-fallback 允许临时回退，但必须在日志中高亮警告。
            import yaml as _yaml
            _real_cfg_file = paths.resolve(config_path)
            _allow_fallback = '--allow-fallback' in sys.argv
            _real_devices = []
            if _real_cfg_file.exists():
                try:
                    with open(_real_cfg_file, encoding='utf-8') as _f:
                        _real_cfg = _yaml.safe_load(_f) or {}
                    _real_devices = _real_cfg.get('devices') or []
                except Exception as _e:
                    logger.error("解析 %s 失败: %s", config_path, _e)
                    print(f"ERROR: 解析真实设备清单 {config_path} 失败: {_e}")
                    sys.exit(1)
            else:
                logger.error("真实设备清单文件不存在: %s", config_path)
                print(f"ERROR: 真实设备清单 {config_path} 不存在。")

            if not _real_devices:
                if _allow_fallback:
                    _fallback = paths.get_config_path('devices.yaml')
                    logger.warning(
                        "!! 生产配置回退已触发 !! devices_real.yaml 为空，"
                        "因 --allow-fallback 显式回退到 devices.yaml（混合清单，含真实 IP，仅限临时排障）")
                    config_path = _fallback
                else:
                    logger.error(
                        "真实设备清单 %s 为空（devices: []），已禁止静默回退到 devices.yaml",
                        config_path)
                    print(
                        "ERROR: 真实设备清单为空，请先按 文档/ 的 schema 填写 "
                        f"{config_path}；\n"
                        "       如需临时用混合配置（含真实 IP，存在误连风险）请显式加 --allow-fallback")
                    sys.exit(1)
            logger.info("真实设备模式：使用真实设备配置")
        else:
            simulation_mode = True
            config_path = paths.get_config_path('devices_simulated.yaml')
            db_path = paths.get_db_path('simulated')
            logger.info("模拟模式：使用模拟设备配置")

        # 初始化数据库
        logger.info("初始化数据库...")
        database = Database(db_path)

        # 补齐缺失索引（P1-6）。必须放在建库之后、开始采集之前：
        # 采集一旦跑起来，history_data/alarm_records 就会持续写入，
        # 事后补索引要在大表上重建、代价高得多。
        # ensure_indexes 是幂等的——已有索引原样跳过，单条失败不影响其它索引。
        from core.index_bootstrap import ensure_indexes
        index_result = ensure_indexes(database)
        logger.info(
            "数据库索引就绪: 新建 %d 个, 已存在 %d 个, 失败 %d 个",
            len(index_result['created']),
            len(index_result['already_existed']),
            len(index_result['failed']),
        )

        # 运维清理工具接线（round 162）。`core/ops_tools.py` 的全局单例 `data_cleaner`
        # 默认 `_db_path = None`，而 `set_db_path()` 此前**全仓库无任何调用方** ——
        # 结果是管理端 `POST /api/ops/cleanup/history` 100% 失败：`sqlite3.connect(None)`
        # 抛 TypeError，被 `except Exception` 收成 `{'status': 'error'}`，
        # 再被 `success_response` 包成 HTTP 200 → 界面显示"清理成功"，实际一行没删。
        # 与 ensure_indexes 同理，必须放在建库之后。
        from core.ops_tools import data_cleaner
        data_cleaner.set_db_path(str(db_path))
        logger.info("运维清理工具已接线: %s", db_path)

        # 初始化设备管理器（根据模式选择不同的管理器）
        logger.info("加载设备配置...")
        if simulation_mode:
            device_manager = SimulatedDeviceManager(config_path)
        else:
            device_manager = RealDeviceManager(config_path)

        # 初始化报警管理器（含声光报警器+广播系统）
        logger.info("加载报警配置...")
        from 报警层.alarm_output import AlarmOutput
        from 报警层.broadcast_system import BroadcastSystem

        # 从alarms.yaml读取灯控设备配置
        import yaml
        alarms_config_path = paths.resolve('配置/alarms.yaml')
        alarm_output_cfg = {'enabled': True, 'simulation': simulation_mode}
        if alarms_config_path.exists():
            with open(alarms_config_path, 'r', encoding='utf-8') as f:
                alarms_yaml = yaml.safe_load(f) or {}
            ao_cfg = alarms_yaml.get('alarm_output', {})
            if ao_cfg:
                # 使用Patlite信号灯塔作为主输出
                tower = ao_cfg.get('signal_tower', {})
                alarm_output_cfg.update({
                    'modbus': {
                        'host': tower.get('host', '192.168.1.70'),
                        'port': tower.get('port', 502),
                        'slave_id': tower.get('slave_id', 1),
                    },
                    'do_mapping': tower.get('do_mapping', {
                        'red_light': 0, 'yellow_light': 1,
                        'green_light': 2, 'buzzer': 5
                    }),
                })
                logger.info(f"灯控设备: {tower.get('device_id')} @ {tower.get('host')}")
        alarm_output = AlarmOutput(alarm_output_cfg)
        broadcast_system = BroadcastSystem({
            'enabled': True,
            'simulation': simulation_mode,
            'areas': ['车间A', '车间B', '仓库', '办公楼'],
            'default_area': 'all',
            'preset_templates': {
                'alarm_critical': '注意！{area}发生严重报警：{message}，请立即处置！',
                'alarm_warning': '提醒：{area}出现告警：{message}，请关注。',
                'evacuation': '请注意，{area}发生紧急状况，请沿疏散通道撤离！',
                'all_clear': '广播通知，{area}警报解除，恢复正常。',
            }
        })
        alarm_manager = AlarmManager(database, '配置/alarms.yaml',
                                     alarm_output=alarm_output,
                                     broadcast_system=broadcast_system)

        # 初始化数据采集器（注入智能层模块）
        logger.info("初始化数据采集器...")

        # 初始化工业4.0智能层
        logger.info("初始化智能层模块...")
        from 智能层.predictive_maintenance import PredictiveMaintenance
        from 智能层.oee_calculator import OEECalculator
        from 智能层.spc_analyzer import SPCAnalyzer
        from 智能层.energy_manager import EnergyManager
        from 智能层.edge_decision import EdgeDecisionEngine
        from 智能层.device_control import DeviceControlSafety
        from 智能层.vibration_analyzer import VibrationAnalyzer

        predictive_maintenance = PredictiveMaintenance(database)
        oee_calculator = OEECalculator(database)
        spc_analyzer = SPCAnalyzer(database)
        energy_manager = EnergyManager(database)
        edge_decision = EdgeDecisionEngine(database)
        device_control = DeviceControlSafety(database, device_manager, alarm_manager)
        vibration_analyzer = VibrationAnalyzer(database)

        # 初始化TDengine适配器（可选，需要TDengine服务）
        realtime_bridge = None
        tsdb_adapter = None
        try:
            from 智能层.tsdb_adapter import TSDBAdapter, RealtimeDataBridge
            from timeseries.tdengine_client import TDengineClient

            # 这里原有一行 `import os`。它是**函数内**的 import，会让 Python 把
            # 整个 main() 里的 `os` 都当成局部变量 —— 于是本行**之前**任何使用
            # os 的代码都会抛
            #   UnboundLocalError: cannot access local variable 'os'
            # 模块级第 6 行已有 import os，这里删掉即可。
            td_host = os.environ.get('TDENGINE_HOST', 'localhost')
            td_port = int(os.environ.get('TDENGINE_PORT', 6041))
            tdengine = TDengineClient(td_host, td_port)
            if tdengine.connect():
                tdengine.init_tables()

                # 创建实时数据桥接器
                realtime_bridge = RealtimeDataBridge(tdengine)

                # 创建智能层适配器
                tsdb_adapter = TSDBAdapter(
                    tdengine,
                    oee_calculator=oee_calculator,
                    predictive_maintenance=predictive_maintenance,
                    spc_analyzer=spc_analyzer,
                    energy_manager=energy_manager
                )

                logger.info("TDengine适配器初始化成功")
            else:
                logger.warning("TDengine连接失败，跳过TDengine集成")
        except Exception as e:
            logger.warning(f"TDengine初始化失败（可选组件）: {e}")

        # 注册边缘决策的动作回调
        def edge_set_alarm(message, level='warning'):
            """边缘决策报警回调"""
            from datetime import datetime
            logger.warning(f"[边缘决策报警] [{level.upper()}] {message}")
            # 记录到数据库
            try:
                database.insert_alarm(
                    alarm_id=f'edge_{int(datetime.now().timestamp())}',
                    device_id='edge_decision',
                    register_name='auto',
                    alarm_level=level,
                    alarm_message=message,
                    threshold=0,
                    actual_value=0,
                    timestamp=datetime.now()
                )
            except Exception as e:
                logger.error(f"边缘决策报警记录失败: {e}")

        edge_decision.register_action('set_alarm', edge_set_alarm)

        data_collector = DataCollector(
            device_manager, database, alarm_manager,
            predictive_maintenance=predictive_maintenance,
            oee_calculator=oee_calculator,
            spc_analyzer=spc_analyzer,
            energy_manager=energy_manager,
            edge_decision=edge_decision,
            device_control=device_control,
            realtime_bridge=realtime_bridge,
            vibration_analyzer=vibration_analyzer,
        )

        # 创建Flask应用
        logger.info("创建Web应用...")
        app = create_app(database, device_manager, alarm_manager, data_collector,
                         predictive_maintenance=predictive_maintenance,
                         oee_calculator=oee_calculator,
                         spc_analyzer=spc_analyzer,
                         energy_manager=energy_manager,
                         edge_decision=edge_decision,
                         device_control=device_control,
                         vibration_analyzer=vibration_analyzer)

        # 将启动时间传递给app
        app.system_start_time = SYSTEM_START_TIME

        # 初始化CSRF防护 (GB/T 37980)
        from core.csrf_protection import csrf
        csrf.init_app(app)

        # 注册健康检查（内置检查 + 周期自动扫描）
        logger.info("注册健康检查...")
        from core.health_checker import HealthChecker
        HealthChecker.register_default_checks(
            database=database,
            device_manager=device_manager,
            data_collector=data_collector,
        )
        HealthChecker.start_periodic_checks(interval=30)

        # 把核心模块实例注册进模块注册表。
        # 此前生产环境从未注册过任何模块 → /modules API 恒返回空；
        # chaos_engineering / health_checker 里的 get_instance('alarm_manager'
        # | 'database' | 'data_collector') 每次抛 KeyError 被吞掉，
        # 导致这些检查实际上退化成常量。
        logger.info("注册核心模块到模块注册表...")
        from core.module_registry import ModuleRegistry
        ModuleRegistry.register_instance('database', database)
        ModuleRegistry.register_instance('device_manager', device_manager)
        ModuleRegistry.register_instance('alarm_manager', alarm_manager)
        ModuleRegistry.register_instance('data_collector', data_collector)
        logger.info(f"模块注册表已就绪: {len(ModuleRegistry.get_status())} 个模块")

        # 初始化高可用管理器
        logger.info("初始化高可用管理器...")
        # 同前：删掉函数内的 `import os`（模块级已有），
        # 否则它会遮蔽模块级的 os，让 main() 里所有 os 用法变成局部变量。
        from core.ha_manager import HAManager, HARole
        ha_manager = HAManager(
            node_id=os.environ.get('HA_NODE_ID', f'node-{os.getpid()}'),
            priority=int(os.environ.get('HA_PRIORITY', '100')),
            heartbeat_interval=float(os.environ.get('HA_HEARTBEAT_INTERVAL', '2')),
            heartbeat_timeout=float(os.environ.get('HA_HEARTBEAT_TIMEOUT', '10')),
            peer_address=os.environ.get('HA_PEER_ADDRESS', ''),
            peer_port=int(os.environ.get('HA_PEER_PORT', '9999')),
        )
        ha_manager.set_on_role_change(lambda old, new: logger.info(f"HA角色: {old.value} -> {new.value}"))
        ha_manager.start()
        app.ha_manager = ha_manager

        # ---- 后台连接设备 + 启动采集（不阻塞Web服务启动） ----
        def _background_start():
            """后台线程：连接设备 → 启动采集。

            整体包在 try/except 中：此前一旦 device_manager.start_reconnect_loop()
            等方法缺失或抛错，线程会静默崩溃，data_collector/报警/智能层全都不会启动，
            外部无任何提示。现在任何异常都会被明确记录，并写进 app.background_start_error
            以便运维查询（如 /api/system/status 读取）。
            """
            try:
                # 连接所有设备（带20s超时，避免阻塞太久）
                logger.info("后台连接设备...")
                connection_results = device_manager.connect_all(timeout=20)
                for device_id, success in connection_results.items():
                    status = "成功" if success else "失败"
                    logger.info(f"  设备 {device_id}: {status}")

                # 启动断线自动重连（每30秒检查一次）
                device_manager.start_reconnect_loop(interval=30)

                # 启动数据采集
                logger.info("启动数据采集...")
                data_collector.start()

                # 启动智能层模块
                logger.info("启动智能层模块...")
                predictive_maintenance.start()
                oee_calculator.start()
                energy_manager.start()
                edge_decision.start()
                vibration_analyzer.start()

                # 注册边缘决策PID输出回调
                def edge_write_register(device_id, address, value):
                    """边缘决策PID输出回调：实际写入设备"""
                    try:
                        device_manager.adjust_device(device_id, str(address), float(value))
                    except Exception as e:
                        logger.error(f"边缘决策写入失败: {e}")

                edge_decision.register_action('write_register', edge_write_register)

                # 启动TDengine适配器（如果可用）
                if realtime_bridge:
                    realtime_bridge.start()
                    logger.info("TDengine实时数据桥接器已启动")
                if tsdb_adapter:
                    devices = device_manager.get_all_devices()
                    for dev_id, dev_config in devices.items():
                        registers = dev_config.get('registers', [])
                        if registers:
                            tsdb_adapter.register_device(dev_id, registers)
                    tsdb_adapter.start()
                    logger.info("TDengine智能层适配器已启动")

                # 后台维护任务：数据归档 + WAL checkpoint + 各类缓存/黑名单清理。
                # 统一交给 core.maintenance（基于 core.scheduled_tasks.task_manager），
                # 取代原先手写的 while True: sleep(86400) 裸线程 —— 那种写法没有
                # 停止路径，而且令牌黑名单/缓存等清理根本没被调度过，会无界增长。
                from core.maintenance import start_maintenance
                maintenance_started = start_maintenance(
                    database=database, auth_manager=getattr(app, 'auth_manager', None))
                logger.info(f"后台维护任务已启动: {', '.join(maintenance_started) or '（无）'}")

                # 注入WebSocket推送函数到报警管理器
                from 展示层.websocket import emit_alarm, emit_broadcast
                alarm_manager.set_websocket_emit(emit_alarm)
                broadcast_system.add_callback(lambda msg: emit_broadcast(msg))

                logger.info("后台采集服务已就绪")
            except Exception as _bg_err:
                # 关键：禁止把异常吞成静默。明确记录 + 把失败状态写到 app 上。
                logger.error("后台采集服务启动失败: %s", _bg_err, exc_info=True)
                try:
                    app.background_start_error = {
                        'error': str(_bg_err),
                        'type': type(_bg_err).__name__,
                        'timestamp': datetime.now().isoformat(),
                    }
                except Exception:
                    # 写状态失败也不应掩盖原始异常
                    pass

        bg_thread = threading.Thread(target=_background_start, daemon=True, name="bg_device_connect")
        bg_thread.start()

        # ---- 模拟模式初始化器（主线程创建，不依赖后台连接） ----
        simulation_initializer = None
        if simulation_mode:
            logger.info("模拟模式：初始化工业4.0模块数据...")
            from core.simulation_initializer import initialize_simulation_data
            simulation_initializer = initialize_simulation_data(
                device_manager, oee_calculator, predictive_maintenance,
                spc_analyzer, energy_manager
            )
            app.simulation_initializer = simulation_initializer

        # 立即启动Web服务（不等待设备连接）

        # 先确定实际监听地址（模拟=5000，真实/模拟器=5001），供横幅与 runtime.json 使用
        from config import WebConfig, SecurityConfig
        host = WebConfig.HOST
        port = WebConfig.REAL_PORT if not simulation_mode else WebConfig.PORT

        # 写入运行时端口文件，供 Electron 发现真实端口（避免 5000/5001 错配）。
        # 格式：{"port": int, "host": str, "pid": int, "mode": "real"|"simulated", "started_at": ISO}
        # 路径：<DATA_DIR>/runtime.json（paths.RUNTIME_JSON_PATH）。
        # Electron 通过 <backend_dir>/data/runtime.json 读取；读不到则回退 5000 并探测 5000/5001。
        try:
            import json as _json
            _runtime = {
                'port': int(port),
                'host': host,
                'pid': os.getpid(),
                'mode': 'real' if not simulation_mode else 'simulated',
                'started_at': datetime.now().isoformat(),
            }
            _runtime_path = paths.RUNTIME_JSON_PATH
            _runtime_path.parent.mkdir(parents=True, exist_ok=True)
            with open(_runtime_path, 'w', encoding='utf-8') as _rf:
                _json.dump(_runtime, _rf, ensure_ascii=False, indent=2)
            logger.info("运行时端口文件已写入: %s (port=%s, mode=%s)",
                        _runtime_path, port, _runtime['mode'])
        except Exception as _re:
            logger.warning("写入运行时端口文件失败（Electron 将回退 5000）: %s", _re)

        # 启动Web服务
        mode_str = "真实设备模式" if not simulation_mode else "模拟模式：使用仿真数据"
        logger.info("启动Web服务...")
        logger.info("=" * 50)
        logger.info("工业数据采集与监控系统已启动")
        logger.info(f"访问地址: http://{host}:{port}")
        logger.info(f"（{mode_str}）")
        logger.info("=" * 50)

        # 使用routes.py中已创建的SocketIO实例（init_socketio在create_app中已调用）
        from 展示层.websocket import socketio

        # TLS/HTTPS 支持 (GB/T 35718 + GB/T 37980)
        ssl_context = None
        if SecurityConfig.TLS_ENABLED:
            import ssl
            cert_file = SecurityConfig.TLS_CERT_FILE
            key_file = SecurityConfig.TLS_KEY_FILE
            if os.path.exists(cert_file) and os.path.exists(key_file):
                ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ssl_context.load_cert_chain(cert_file, key_file)
                ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
                logger.info(f"TLS 已启用: cert={cert_file}, min_version=TLSv1.2")
            else:
                logger.warning(f"TLS 证书文件不存在: cert={cert_file}, key={key_file}")
                logger.warning("回退到 HTTP 模式，请生成证书: python -m core.generate_certs")

        if socketio is None:
            logger.warning("SocketIO未初始化，使用普通Flask服务器")
            app.run(host=host, port=port, debug=False, ssl_context=ssl_context)
        else:
            socketio.run(app, host=host, port=port, debug=False,
                        allow_unsafe_werkzeug=True, ssl_context=ssl_context)

    except KeyboardInterrupt:
        logger.info("系统正在关闭...")
        if 'data_collector' in locals():
            data_collector.stop()
        if 'tsdb_adapter' in locals() and tsdb_adapter:
            tsdb_adapter.stop()
        if 'realtime_bridge' in locals() and realtime_bridge:
            realtime_bridge.stop()
        if 'alarm_output' in locals():
            alarm_output.disconnect()
        if 'broadcast_system' in locals():
            broadcast_system.disconnect()
        if 'device_manager' in locals():
            try:
                device_manager.stop_reconnect_loop()
            except Exception:
                pass
            device_manager.disconnect_all()
        if 'alarm_manager' in locals():
            # 停止升级/洪水定时器 + 配置热重载线程（stop() 幂等）
            alarm_manager.stop()
        if 'ha_manager' in locals():
            ha_manager.stop()
        if 'HealthChecker' in locals():
            HealthChecker.stop_periodic_checks()
        # 停止后台维护任务（幂等）
        from core.maintenance import stop_maintenance
        stop_maintenance()
        logger.info("系统已关闭")

    except Exception as e:
        logger.error(f"系统启动失败: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
