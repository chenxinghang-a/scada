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
