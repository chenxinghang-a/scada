"""
Pytest shared fixtures for SCADA system tests
"""

import pytest
import sys
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def pytest_configure(config):
    """把 pytest 临时根指向**每次运行都不同**的目录，实现"零删除"。

    背景（本机环境特有，CI 上无此问题）：
        WorkBuddy 注入了批量删除保护 shim。pytest 默认行为有两次删除会踩中：
        1. 会话开始时清理 `basetemp`（决定性地：如果 basetemp 已存在，pytest 会
           先删掉它再重建）。走 shim 的"移回收站"路径时，对 `\\\\?\\` 前缀路径
           报 `OSError [Errno 53] 找不到网络路径` → `SAFE_DELETE_FAIL_CLOSED`
           拒绝 → 所有用例在 setup 阶段 ERROR。
        2. 默认 basetemp 在系统 TEMP 下做"保留最近 3 个 run"的滚动清理，
           量可达上万文件 → 触发 `SAFE_DELETE_BULK_CONFIRM_REQUIRED` 弹窗。

    解法：目录名带 **PID**，保证每次进程启动时 basetemp 都是全新的、不存在的
    路径 —— pytest 无需清任何东西，两个坑同时消失。

    目录位置选**仓库内**（而不是系统 TEMP）有两个原因：
    - 便于事后排查（临时文件就在眼皮底下）
    - 避免污染系统 TEMP，也不会和用户其他项目的 pytest 临时目录混在一起

    这些目录已加进 .gitignore（`.pytest_tmp-*`）。**不清理是刻意的** ——
    清理就是删除，就会再次踩保护。它们只是临时文件，磁盘占用很小
    （单次全量测试约几十 MB），需要时可手工清（按 ≤50 个/批）。
    """
    # 用户显式传了 --basetemp 就尊重用户选择，不覆盖
    if config.option.basetemp:
        return
    tmp_root = Path(PROJECT_ROOT) / f'.pytest_tmp-{os.getpid()}'
    config.option.basetemp = str(tmp_root)


def _quarantine_smoke_test_dbs(data_dir: Path):
    """将冒烟测试数据库移出工作目录，避免测试清理触发批量删除保护。"""
    if not data_dir.exists():
        return
    for db_file in data_dir.glob("smoke_test_*.db*"):
        try:
            target = db_file.with_name(f".stale_{db_file.name}")
            suffix = 1
            while target.exists():
                target = db_file.with_name(f".stale_{suffix}_{db_file.name}")
                suffix += 1
            db_file.rename(target)
        except (PermissionError, OSError):
            pass


@pytest.fixture(autouse=True)
def isolate_queue_persistence(tmp_path):
    """把 DiskBackedQueue 的持久化目录指向**本测试专属**的临时目录。

    默认路径是仓库内的 `data/queue/pending_data.jsonl`，所有测试共用一个文件：
    某个测试 `put()` 进去的数据会被下一个构造 `DataCollector` 的测试
    `_recover_from_disk()` 恢复并 `unlink()`。

    ⚠️ 这个 fixture 原本是 **session 级**的（全会话共用一个临时目录），
    结果只是把「共用一个文件」从仓库搬到了临时目录 —— 同样的污染、
    同样的累计 unlink，仍然触发环境的批量删除保护
    （表现为 `test_data_collector.py` 里 `TestDynamicInterval` /
    `TestDispatchIntelligence` 等大批用例以 SystemExit 失败，是假失败）。

    改为**函数级**：每个测试一个全新目录，任何测试都不会读到别人的残留文件，
    `unlink()` 次数也随之降到 0~1 次。
    """
    old = os.environ.get("SCADA_QUEUE_PERSIST_DIR")
    os.environ["SCADA_QUEUE_PERSIST_DIR"] = str(tmp_path / "queue_persist")
    yield
    if old is None:
        os.environ.pop("SCADA_QUEUE_PERSIST_DIR", None)
    else:
        os.environ["SCADA_QUEUE_PERSIST_DIR"] = old


@pytest.fixture(scope="session", autouse=True)
def cleanup_smoke_test_dbs():
    """会话开始/结束时隔离残留的smoke_test数据库文件。"""
    data_dir = Path(PROJECT_ROOT) / "data"
    _quarantine_smoke_test_dbs(data_dir)
    yield
    _quarantine_smoke_test_dbs(data_dir)


#: 会被接口写回、但属于**受版本控制的配置文件**。
#: 测试打到这些接口时会把改动持久化进仓库，导致每次跑完测试工作区都变脏。
_POLLUTABLE_CONFIGS = (
    "配置/system.yaml",   # POST /api/system/simulation-mode 会整份重写
    "配置/alarms.yaml",   # 报警规则增删改会整份重写
    "配置/devices.yaml",
    "配置/devices_simulated.yaml",
    "配置/energy.yaml",
)


@pytest.fixture(scope="session", autouse=True)
def restore_polluted_configs():
    """会话结束时把被测试写脏的配置文件恢复原状。

    背景：`展示层/api/api_system.py` 的 `POST /api/system/simulation-mode` 会把
    `simulation_mode` 写进 `配置/system.yaml`，`api_alarms.py` 的规则增删改同理。
    测试打这些接口时改动会落到**受版本控制的真实配置文件**上 ——
    跑完一次全量测试，工作区就多出若干 `M 配置/*.yaml`，很容易被误提交。

    这里只做「快照 → 会话结束还原」，不改变任何生产行为。
    之所以用 session 粒度：2000+ 个测试逐条还原开销不划算，
    而这些配置在单次会话内被读到中间态的风险很低（现有测试只断言字段存在）。
    """
    snapshots = {}
    for rel in _POLLUTABLE_CONFIGS:
        p = Path(PROJECT_ROOT) / rel
        if p.is_file():
            snapshots[p] = p.read_bytes()

    yield

    restored = []
    for p, original in snapshots.items():
        try:
            if p.read_bytes() != original:
                p.write_bytes(original)
                restored.append(str(p.relative_to(PROJECT_ROOT)))
        except OSError:
            pass
    if restored:
        print(f"\n[conftest] 已还原被测试写脏的配置文件: {', '.join(restored)}")


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset all class-based singletons between tests"""
    from core.di_container import DIContainer
    from core.event_bus import EventBus
    from core.config_manager import ConfigManager
    from core.health_checker import HealthChecker
    from core.module_registry import ModuleRegistry

    yield

    DIContainer.clear_all()
    EventBus.clear_all()
    ConfigManager.clear()
    HealthChecker.clear()
    ModuleRegistry.clear()


@pytest.fixture
def app():
    """Create a minimal Flask test app with mocked SCADA components"""
    from flask import Flask
    from 展示层.api import register_api_blueprints

    flask_app = Flask(__name__, template_folder='../模板', static_folder='../静态资源')
    flask_app.config['SECRET_KEY'] = 'test-secret-key'
    flask_app.config['TESTING'] = True

    # Mock database
    mock_db = MagicMock()
    mock_db.get_database_stats.return_value = {
        'realtime_count': 0, 'history_count': 0, 'alarm_count': 0
    }
    mock_db.get_alarm_records.return_value = []
    mock_db.get_active_alarms.return_value = []

    # Mock device manager
    mock_device_mgr = MagicMock()
    mock_device_mgr.get_all_status.return_value = []
    mock_device_mgr.simulation_mode = True

    # Mock alarm manager
    mock_alarm_mgr = MagicMock()
    mock_alarm_mgr.get_active_alarms.return_value = []
    mock_alarm_mgr.get_alarm_statistics.return_value = {
        'total': 0, 'active': 0, 'acknowledged': 0
    }

    # Mock data collector
    mock_collector = MagicMock()
    mock_collector.get_stats.return_value = {'total_readings': 0}

    # Mock auth manager
    mock_auth = MagicMock()

    # Attach mocks to app
    flask_app.database = mock_db
    flask_app.device_manager = mock_device_mgr
    flask_app.alarm_manager = mock_alarm_mgr
    flask_app.data_collector = mock_collector
    flask_app.auth_manager = mock_auth

    # Register API blueprints
    register_api_blueprints(flask_app)

    yield flask_app


@pytest.fixture
def auth_headers(app):
    """Generate valid JWT auth headers for testing"""
    import jwt
    from datetime import datetime, timedelta, timezone
    from config import AuthConfig

    now = datetime.now(timezone.utc)
    payload = {
        'username': 'testuser',
        'role': 'admin',
        'type': 'access',
        'iat': now,
        'exp': now + timedelta(hours=AuthConfig.JWT_EXPIRATION_HOURS)
    }
    token = jwt.encode(payload, AuthConfig.JWT_SECRET, algorithm=AuthConfig.JWT_ALGORITHM)

    # Make auth_manager.verify_token() return a valid user for this token
    app.auth_manager.verify_token.return_value = {
        'username': 'testuser',
        'role': 'admin',
        'display_name': 'Test User',
        'permissions': ['read', 'write', 'delete', 'manage_users', 'manage_devices',
                        'acknowledge_alarms', 'export_data', 'system_config']
    }

    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def client(app):
    """Flask test client"""
    return app.test_client()


@pytest.fixture
def db():
    """In-memory SQLite test database with SCADA schema"""
    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_path = tmp.name
    tmp.close()

    conn = sqlite3.connect(tmp_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS realtime_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            register_name TEXT NOT NULL,
            value REAL,
            unit TEXT,
            timestamp DATETIME NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(device_id, register_name)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            register_name TEXT NOT NULL,
            value REAL,
            unit TEXT,
            timestamp DATETIME NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alarm_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alarm_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            register_name TEXT NOT NULL,
            alarm_level TEXT NOT NULL,
            alarm_message TEXT,
            threshold REAL,
            actual_value REAL,
            timestamp DATETIME NOT NULL,
            trigger_count INTEGER DEFAULT 1,
            last_trigger_time DATETIME,
            last_value REAL,
            acknowledged BOOLEAN DEFAULT 0,
            acknowledged_at DATETIME,
            acknowledged_by TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS device_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            status TEXT NOT NULL,
            message TEXT,
            timestamp DATETIME NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()

    yield tmp_path

    # Cleanup
    try:
        os.unlink(tmp_path)
    except OSError:
        pass


@pytest.fixture
def device_config():
    """Sample device configuration for testing"""
    return {
        'devices': [
            {
                'id': 'test_pump_01',
                'name': 'Test Pump',
                'protocol': 'modbus_tcp',
                'host': '127.0.0.1',
                'port': 502,
                'slave_id': 1,
                'registers': [
                    {'name': 'flow_rate', 'address': 0, 'type': 'float', 'unit': 'm3/h'},
                    {'name': 'pressure', 'address': 2, 'type': 'float', 'unit': 'MPa'},
                    {'name': 'status', 'address': 100, 'type': 'bool', 'unit': ''},
                ]
            },
            {
                'id': 'test_motor_01',
                'name': 'Test Motor',
                'protocol': 'modbus_tcp',
                'host': '127.0.0.1',
                'port': 502,
                'slave_id': 2,
                'registers': [
                    {'name': 'speed', 'address': 0, 'type': 'uint16', 'unit': 'RPM'},
                    {'name': 'current', 'address': 1, 'type': 'float', 'unit': 'A'},
                    {'name': 'temperature', 'address': 3, 'type': 'float', 'unit': 'C'},
                ]
            },
        ]
    }


@pytest.fixture
def alarm_config():
    """Sample alarm configuration for testing"""
    return {
        'rules': [
            {
                'id': 'alarm_high_temp',
                'name': 'High Temperature',
                'device_id': 'test_motor_01',
                'register_name': 'temperature',
                'condition': 'greater_than',
                'threshold': 80.0,
                'level': 'warning',
                'message': 'Motor temperature exceeds 80C',
                'enabled': True,
                'severity': 3,
                'likelihood': 3,
            },
            {
                'id': 'alarm_critical_temp',
                'name': 'Critical Temperature',
                'device_id': 'test_motor_01',
                'register_name': 'temperature',
                'condition': 'greater_than',
                'threshold': 100.0,
                'level': 'critical',
                'message': 'Motor temperature exceeds 100C - DANGER',
                'enabled': True,
                'severity': 5,
                'likelihood': 3,
            },
            {
                'id': 'alarm_low_pressure',
                'name': 'Low Pressure',
                'device_id': 'test_pump_01',
                'register_name': 'pressure',
                'condition': 'less_than',
                'threshold': 0.5,
                'level': 'warning',
                'message': 'Pump pressure below 0.5 MPa',
                'enabled': True,
                'severity': 2,
                'likelihood': 4,
            },
        ]
    }


@pytest.fixture
def auth_manager():
    """Create a real AuthManager with a temp SQLite database for unit testing"""
    import sqlite3
    from 用户层.auth import AuthManager

    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_path = tmp.name
    tmp.close()

    class TestDatabase:
        def __init__(self, path):
            self._path = path
        def get_connection(self):
            conn = sqlite3.connect(self._path)
            conn.row_factory = sqlite3.Row
            return conn

    db = TestDatabase(tmp_path)
    mgr = AuthManager(db)

    yield mgr

    try:
        os.unlink(tmp_path)
    except OSError:
        pass
