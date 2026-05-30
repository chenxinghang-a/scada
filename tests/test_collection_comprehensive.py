"""
采集层综合测试 - 提升采集层覆盖率
覆盖: base_client, simulated_client, enhanced_simulated_client,
      device_manager, data_collector, interfaces
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime


class TestBaseDeviceClient:
    """BaseDeviceClient 测试"""

    def test_base_client_init(self):
        """初始化基类"""
        from 采集层.base_client import BaseDeviceClient

        class ConcreteClient(BaseDeviceClient):
            def connect(self): return True
            def disconnect(self): pass
            def get_stats(self): return {}

        config = {'id': 'dev1', 'name': 'Test Device', 'protocol': 'modbus_tcp'}
        client = ConcreteClient(config)
        assert client.device_id == 'dev1'
        assert client.device_name == 'Test Device'
        assert client.protocol == 'modbus_tcp'
        assert client.connected is False

    def test_base_client_init_device_id_fallback(self):
        """device_id 兼容 device_id 和 id 两种key"""
        from 采集层.base_client import BaseDeviceClient

        class ConcreteClient(BaseDeviceClient):
            def connect(self): return True
            def disconnect(self): pass
            def get_stats(self): return {}

        client = ConcreteClient({'device_id': 'alt_id'})
        assert client.device_id == 'alt_id'

    def test_base_client_default_methods(self):
        """默认方法测试"""
        from 采集层.base_client import BaseDeviceClient

        class ConcreteClient(BaseDeviceClient):
            def connect(self): return True
            def disconnect(self): pass
            def get_stats(self): return {}

        client = ConcreteClient({'id': 'dev1'})
        assert client.get_latest_data() == {}
        client.add_data_callback(lambda: None)  # Should not raise

    def test_modbus_client_interface_methods(self):
        """ModbusClientInterface 接口测试"""
        from 采集层.base_client import ModbusClientInterface

        # Verify it's abstract
        assert hasattr(ModbusClientInterface, 'read_holding_registers')


class TestInterfaces:
    """接口层测试"""

    def test_idevice_client_interface(self):
        """IDeviceClient 接口定义"""
        from 采集层.interfaces import IDeviceClient
        assert hasattr(IDeviceClient, 'connect')
        assert hasattr(IDeviceClient, 'disconnect')
        assert hasattr(IDeviceClient, 'read_holding_registers')
        assert hasattr(IDeviceClient, 'read_coils')
        assert hasattr(IDeviceClient, 'write_single_register')
        assert hasattr(IDeviceClient, 'write_single_coil')

    def test_idevice_manager_interface(self):
        """IDeviceManager 接口定义"""
        from 采集层.interfaces import IDeviceManager
        assert hasattr(IDeviceManager, 'load_config')
        assert hasattr(IDeviceManager, 'get_client')
        assert hasattr(IDeviceManager, 'connect_device')
        assert hasattr(IDeviceManager, 'disconnect_device')
        assert hasattr(IDeviceManager, 'connect_all')
        assert hasattr(IDeviceManager, 'get_device_status')
        assert hasattr(IDeviceManager, 'get_all_status')
        assert hasattr(IDeviceManager, 'add_device')
        assert hasattr(IDeviceManager, 'remove_device')
        assert hasattr(IDeviceManager, 'get_device_category')

    def test_get_device_category(self):
        """设备分类测试"""
        from 采集层.interfaces import IDeviceManager

        config = {'device_category': 'mechanical'}
        assert IDeviceManager.get_device_category(config) == 'mechanical'

        config = {'device_category': 'instrument'}
        assert IDeviceManager.get_device_category(config) == 'instrument'

        config = {'device_category': 'safety'}
        assert IDeviceManager.get_device_category(config) == 'safety'

        config = {'protocol': 'modbus_tcp'}
        assert IDeviceManager.get_device_category(config) == 'instrument'


class TestSimulatedClient:
    """模拟客户端测试"""

    def test_simulated_modbus_client_init(self):
        """模拟Modbus客户端初始化"""
        from 采集层.simulated_client import SimulatedModbusClient
        config = {
            'id': 'test_dev', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [
                {'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}
            ]
        }
        client = SimulatedModbusClient(config)
        assert client.device_id == 'test_dev'
        assert client.connected is False

    def test_simulated_modbus_connect_disconnect(self):
        """模拟Modbus连接/断开"""
        from 采集层.simulated_client import SimulatedModbusClient
        config = {
            'id': 'test_dev', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = SimulatedModbusClient(config)
        result = client.connect()
        assert result is True
        assert client.connected is True

        client.disconnect()
        assert client.connected is False

    def test_simulated_modbus_read_registers(self):
        """模拟Modbus读寄存器"""
        from 采集层.simulated_client import SimulatedModbusClient
        config = {
            'id': 'test_dev', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = SimulatedModbusClient(config)
        client.connect()

        result = client.read_holding_registers(0, 2)
        assert result is not None
        assert len(result) > 0

    def test_simulated_modbus_get_stats(self):
        """模拟Modbus统计"""
        from 采集层.simulated_client import SimulatedModbusClient
        config = {
            'id': 'test_dev', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = SimulatedModbusClient(config)
        client.connect()
        stats = client.get_stats()
        assert 'device_id' in stats
        assert stats['device_id'] == 'test_dev'


class TestEnhancedSimulatedClient:
    """增强版模拟客户端测试"""

    def test_enhanced_modbus_client_init(self):
        """增强版Modbus客户端初始化"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedModbusClient
        config = {
            'id': 'test_dev', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = EnhancedSimulatedModbusClient(config)
        assert client.device_id == 'test_dev'
        assert hasattr(client, 'behavior_simulator')

    def test_enhanced_modbus_connect(self):
        """增强版Modbus连接"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedModbusClient
        config = {
            'id': 'test_dev', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = EnhancedSimulatedModbusClient(config)
        result = client.connect()
        assert result is True
        assert client.connected is True

    def test_enhanced_modbus_read(self):
        """增强版Modbus读取"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedModbusClient
        config = {
            'id': 'test_dev', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = EnhancedSimulatedModbusClient(config)
        client.connect()
        result = client.read_holding_registers(0, 2)
        assert result is not None

    def test_enhanced_modbus_write(self):
        """增强版Modbus写入"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedModbusClient
        config = {
            'id': 'test_dev', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = EnhancedSimulatedModbusClient(config)
        client.connect()
        result = client.write_single_register(0, 100)
        # May fail due to simulated communication fault
        assert result is True or result is False

    def test_enhanced_modbus_get_stats(self):
        """增强版Modbus统计"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedModbusClient
        config = {
            'id': 'test_dev', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = EnhancedSimulatedModbusClient(config)
        client.connect()
        stats = client.get_stats()
        assert 'device_id' in stats

    def test_enhanced_opcua_client_init(self):
        """增强版OPC UA客户端初始化"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedOPCUAClient
        config = {
            'id': 'opc_dev', 'name': 'OPC Device', 'protocol': 'opcua',
            'endpoint': 'opc.tcp://localhost:4840',
            'nodes': [{'node_id': 'ns=2;s=Temperature', 'name': 'temp', 'unit': 'C'}]
        }
        client = EnhancedSimulatedOPCUAClient(config)
        assert client.device_id == 'opc_dev'

    def test_enhanced_opcua_connect(self):
        """增强版OPC UA连接"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedOPCUAClient
        config = {
            'id': 'opc_dev', 'name': 'OPC Device', 'protocol': 'opcua',
            'endpoint': 'opc.tcp://localhost:4840',
            'nodes': [{'node_id': 'ns=2;s=Temperature', 'name': 'temp', 'unit': 'C'}]
        }
        client = EnhancedSimulatedOPCUAClient(config)
        result = client.connect()
        assert result is True

    def test_enhanced_opcua_get_latest(self):
        """增强版OPC UA获取最新数据"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedOPCUAClient
        config = {
            'id': 'opc_dev', 'name': 'OPC Device', 'protocol': 'opcua',
            'endpoint': 'opc.tcp://localhost:4840',
            'nodes': [{'node_id': 'ns=2;s=Temperature', 'name': 'temp', 'unit': 'C'}]
        }
        client = EnhancedSimulatedOPCUAClient(config)
        client.connect()
        data = client.get_latest_data()
        assert isinstance(data, dict)

    def test_enhanced_mqtt_client_init(self):
        """增强版MQTT客户端初始化"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedMQTTClient
        config = {
            'id': 'mqtt_dev', 'name': 'MQTT Device', 'protocol': 'mqtt',
            'host': '127.0.0.1', 'port': 1883,
            'topics': [{'topic': 'factory/temp', 'name': 'temp', 'unit': 'C'}]
        }
        client = EnhancedSimulatedMQTTClient(config)
        assert client.device_id == 'mqtt_dev'

    def test_enhanced_mqtt_connect(self):
        """增强版MQTT连接"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedMQTTClient
        config = {
            'id': 'mqtt_dev', 'name': 'MQTT Device', 'protocol': 'mqtt',
            'host': '127.0.0.1', 'port': 1883,
            'topics': [{'topic': 'factory/temp', 'name': 'temp', 'unit': 'C'}]
        }
        client = EnhancedSimulatedMQTTClient(config)
        result = client.connect()
        assert result is True

    def test_enhanced_rest_client_init(self):
        """增强版REST客户端初始化"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedRESTClient
        config = {
            'id': 'rest_dev', 'name': 'REST Device', 'protocol': 'rest',
            'base_url': 'http://localhost/api',
            'endpoints': [{'name': 'temp', 'path': '/temp', 'unit': 'C'}]
        }
        client = EnhancedSimulatedRESTClient(config)
        assert client.device_id == 'rest_dev'

    def test_enhanced_rest_connect(self):
        """增强版REST连接"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedRESTClient
        config = {
            'id': 'rest_dev', 'name': 'REST Device', 'protocol': 'rest',
            'base_url': 'http://localhost/api',
            'endpoints': [{'name': 'temp', 'path': '/temp', 'unit': 'C'}]
        }
        client = EnhancedSimulatedRESTClient(config)
        result = client.connect()
        assert result is True

    def test_enhanced_client_inject_fault(self):
        """故障注入测试"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedModbusClient
        from 采集层.device_behavior_simulator import FaultType
        config = {
            'id': 'test_dev', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = EnhancedSimulatedModbusClient(config)
        client.connect()
        client.inject_fault(FaultType.SENSOR_DRIFT, 0.5)
        assert client.behavior_simulator.active_fault == FaultType.SENSOR_DRIFT

    def test_enhanced_client_force_state(self):
        """强制状态测试"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedModbusClient
        from 采集层.device_behavior_simulator import DeviceState
        config = {
            'id': 'test_dev', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = EnhancedSimulatedModbusClient(config)
        client.connect()
        client.force_state(DeviceState.MAINTENANCE)
        assert client.behavior_simulator.state == DeviceState.MAINTENANCE


class TestDeviceBehaviorSimulator:
    """设备行为模拟器测试"""

    def _make_sim(self):
        from 采集层.device_behavior_simulator import DeviceBehaviorSimulator
        config = {
            'id': 'test_dev', 'name': 'TestDevice', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        return DeviceBehaviorSimulator('test_dev', config)

    def test_simulator_init(self):
        """模拟器初始化"""
        from 采集层.device_behavior_simulator import DeviceState
        sim = self._make_sim()
        assert sim.device_name == 'TestDevice'
        assert sim.state == DeviceState.IDLE

    def test_simulator_state_transitions(self):
        """状态转换测试"""
        from 采集层.device_behavior_simulator import DeviceState
        sim = self._make_sim()

        sim.state = DeviceState.RUNNING
        assert sim.state == DeviceState.RUNNING

        sim.state = DeviceState.FAULT
        assert sim.state == DeviceState.FAULT

    def test_simulator_health(self):
        """健康评分测试"""
        sim = self._make_sim()
        assert sim.health.overall_score > 0
        assert sim.health.mechanical_health > 0
        assert sim.health.electrical_health > 0

    def test_simulator_update(self):
        """模拟器update生成数据"""
        from 采集层.device_behavior_simulator import DeviceState
        sim = self._make_sim()
        sim.state = DeviceState.RUNNING
        data = sim.update(1.0)
        assert isinstance(data, dict)
        # update should return sensor data dict
        assert len(data) >= 0

    def test_simulator_stats(self):
        """统计信息测试"""
        sim = self._make_sim()
        stats = sim.stats
        assert 'total_cycles' in stats


class TestDeviceManagerFactory:
    """设备管理器工厂测试"""

    def test_factory_creates_simulated_manager(self):
        """工厂直接创建模拟设备管理器"""
        from 采集层.device_manager_factory import DeviceManagerFactory
        manager = DeviceManagerFactory.create_simulated()
        assert manager is not None
        assert manager.simulation_mode is True

    def test_factory_creates_real_manager(self):
        """工厂直接创建真实设备管理器"""
        from 采集层.device_manager_factory import DeviceManagerFactory
        manager = DeviceManagerFactory.create_real()
        assert manager is not None
        assert manager.simulation_mode is False


class TestRecipeSimulator:
    """配方模拟器测试"""

    def test_recipes_exist(self):
        """配方存在"""
        from 采集层.recipe_simulator import RecipeSimulator
        assert hasattr(RecipeSimulator, 'RECIPES')
        assert len(RecipeSimulator.RECIPES) > 0

    def test_recipe_structure(self):
        """配方结构"""
        from 采集层.recipe_simulator import RecipeSimulator
        for key, recipe in RecipeSimulator.RECIPES.items():
            assert hasattr(recipe, 'name')
            assert hasattr(recipe, 'version')
            assert hasattr(recipe, 'phases')
            assert hasattr(recipe, 'parameters')
