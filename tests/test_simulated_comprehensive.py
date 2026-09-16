"""
模拟系统综合测试 - 提升覆盖率
覆盖: simulated_device_manager, enhanced_simulated_client全部协议,
      simulated_client, device_manager
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime


class TestSimulatedDeviceManager:
    """模拟设备管理器测试"""

    def test_init(self):
        """初始化"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        assert mgr.simulation_mode is True

    def test_add_device_modbus(self):
        """添加Modbus设备"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        result = mgr.add_device(config)
        assert result is True
        assert 'dev1' in mgr.devices

    def test_add_device_opcua(self):
        """添加OPC UA设备"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        config = {
            'id': 'opc1', 'name': 'OPC', 'protocol': 'opcua',
            'endpoint': 'opc.tcp://localhost:4840',
            'nodes': [{'node_id': 'ns=2;s=Temp', 'name': 'temp', 'unit': 'C'}]
        }
        result = mgr.add_device(config)
        assert result is True

    def test_add_device_mqtt(self):
        """添加MQTT设备"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        config = {
            'id': 'mqtt1', 'name': 'MQTT', 'protocol': 'mqtt',
            'host': '127.0.0.1', 'port': 1883,
            'topics': [{'topic': 'test/temp', 'name': 'temp', 'unit': 'C'}]
        }
        result = mgr.add_device(config)
        assert result is True

    def test_add_device_rest(self):
        """添加REST设备"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        config = {
            'id': 'rest1', 'name': 'REST', 'protocol': 'rest',
            'base_url': 'http://localhost/api',
            'endpoints': [{'name': 'temp', 'path': '/temp', 'unit': 'C'}]
        }
        result = mgr.add_device(config)
        assert result is True

    def test_add_device_mc(self):
        """添加MC协议设备"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        config = {
            'id': 'mc1', 'name': 'MC', 'protocol': 'mc',
            'host': '127.0.0.1', 'port': 5000,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        result = mgr.add_device(config)
        assert result is True

    def test_add_device_fins(self):
        """添加FINS协议设备"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        config = {
            'id': 'fins1', 'name': 'FINS', 'protocol': 'fins',
            'host': '127.0.0.1', 'port': 9600,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        result = mgr.add_device(config)
        assert result is True

    def test_add_device_unsupported(self):
        """添加不支持协议的设备"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        config = {'id': 'x', 'name': 'X', 'protocol': 'unknown'}
        result = mgr.add_device(config)
        # Should handle gracefully
        assert isinstance(result, bool)

    def test_remove_device(self):
        """删除设备"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        mgr.add_device(config)
        result = mgr.remove_device('dev1')
        assert result is True
        assert 'dev1' not in mgr.devices

    def test_remove_device_not_found(self):
        """删除不存在的设备"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        result = mgr.remove_device('nonexistent')
        assert isinstance(result, bool)

    def test_connect_device(self):
        """连接设备"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        mgr.add_device(config)
        result = mgr.connect_device('dev1')
        assert result is True

    def test_disconnect_device(self):
        """断开设备"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        mgr.add_device(config)
        mgr.connect_device('dev1')
        mgr.disconnect_device('dev1')
        # 原测试只调用不校验（纯调用 = 恒过）。disconnect_device 的副作用是
        # 把该设备客户端置为未连接，必须验证这个副作用真的发生了。
        client = mgr.get_client('dev1')
        assert client is not None, "断开后客户端不应被删除"
        assert client.connected is False, "disconnect_device 后客户端仍处于已连接状态"

    def test_get_device_status(self):
        """获取设备状态"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        mgr.add_device(config)
        status = mgr.get_device_status('dev1')
        assert 'device_id' in status or 'error' in status

    def test_get_device_status_not_found(self):
        """获取不存在设备的状态"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        status = mgr.get_device_status('nonexistent')
        assert 'error' in status

    def test_get_all_status(self):
        """获取所有设备状态"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        mgr.add_device(config)
        status_list = mgr.get_all_status()
        assert isinstance(status_list, list)

    def test_get_client(self):
        """获取客户端"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        mgr.add_device(config)
        client = mgr.get_client('dev1')
        assert client is not None, "已注册设备应能取到客户端"
        # 原断言只校验非 None，MagicMock/任意对象都能通过。
        # 这里校验返回的确实是该设备对应的模拟 Modbus 客户端。
        from 采集层.enhanced_simulated_client import EnhancedSimulatedModbusClient
        assert isinstance(client, EnhancedSimulatedModbusClient), \
            f"get_client 应返回 EnhancedSimulatedModbusClient，实际 {type(client).__name__}"
        assert client.device_id == 'dev1'

    def test_get_client_not_found(self):
        """获取不存在的客户端"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        client = mgr.get_client('nonexistent')
        assert client is None

    def test_get_all_devices(self):
        """获取所有设备配置"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        mgr.add_device(config)
        devices = mgr.get_all_devices()
        assert 'dev1' in devices

    def test_get_protocol_summary(self):
        """获取协议摘要"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        summary = mgr.get_protocol_summary()
        assert isinstance(summary, dict)

    def test_switch_simulation_mode(self):
        """切换模拟模式"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        result = mgr.switch_simulation_mode(True)
        assert isinstance(result, dict)

    def test_connect_all(self):
        """连接所有设备"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        mgr.add_device(config)
        results = mgr.connect_all()
        assert isinstance(results, dict)

    def test_disconnect_all(self):
        """断开所有设备"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        mgr = SimulatedDeviceManager()
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        mgr.add_device(config)
        mgr.connect_all()
        mgr.disconnect_all()
        # 原测试只调用不校验（纯调用 = 恒过）。disconnect_all 必须把每个
        # 已创建客户端的 connected 置为 False。
        assert mgr.clients, "connect_all 后应至少存在一个客户端"
        assert all(c.connected is False for c in mgr.clients.values()), \
            f"disconnect_all 后仍有客户端处于已连接: {[k for k, c in mgr.clients.items() if c.connected]}"


class TestEnhancedSimulatedClientAll:
    """增强版模拟客户端全部协议测试"""

    def test_enhanced_modbus_disconnect(self):
        """增强版Modbus断开"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedModbusClient
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = EnhancedSimulatedModbusClient(config)
        client.connect()
        client.disconnect()
        assert client.connected is False

    def test_enhanced_modbus_read_coils(self):
        """增强版Modbus读线圈"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedModbusClient
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'status', 'address': 100, 'type': 'bool', 'unit': ''}]
        }
        client = EnhancedSimulatedModbusClient(config)
        client.connect()
        result = client.read_coils(0, 1)
        # 原断言 `assert result is not None or True` 恒为真（末尾 `or True`），
        # 无论返回 None 还是任何垃圾值都通过 —— 等于没测。
        # read_coils 的契约（enhanced_simulated_client.py:323）：连接正常时返回
        # 长度为 count 的 bool 列表。
        assert isinstance(result, list), f"read_coils 应返回列表，实际 {result!r}"
        assert len(result) == 1, f"请求 1 个线圈应返回 1 个元素，实际 {result!r}"
        assert all(isinstance(v, bool) for v in result), \
            f"线圈元素应为 bool，实际 {[type(v).__name__ for v in result]}"

    def test_enhanced_modbus_write_coil(self):
        """增强版Modbus写线圈"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedModbusClient
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'status', 'address': 100, 'type': 'bool', 'unit': ''}]
        }
        client = EnhancedSimulatedModbusClient(config)
        client.connect()
        result = client.write_single_coil(0, True)
        assert result is True

    def test_enhanced_modbus_read_input_registers(self):
        """增强版Modbus读输入寄存器"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedModbusClient
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = EnhancedSimulatedModbusClient(config)
        client.connect()
        result = client.read_input_registers(0, 2)
        assert result is not None

    def test_enhanced_modbus_get_latest(self):
        """增强版Modbus获取最新数据"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedModbusClient
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = EnhancedSimulatedModbusClient(config)
        client.connect()
        # Read some data first
        client.read_holding_registers(0, 2)
        data = client.get_latest_data()
        assert isinstance(data, dict)

    def test_enhanced_opcua_disconnect(self):
        """增强版OPC UA断开"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedOPCUAClient
        config = {
            'id': 'opc1', 'name': 'OPC', 'protocol': 'opcua',
            'endpoint': 'opc.tcp://localhost:4840',
            'nodes': [{'node_id': 'ns=2;s=Temp', 'name': 'temp', 'unit': 'C'}]
        }
        client = EnhancedSimulatedOPCUAClient(config)
        client.connect()
        client.disconnect()
        assert client.connected is False

    def test_enhanced_opcua_get_stats(self):
        """增强版OPC UA统计"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedOPCUAClient
        config = {
            'id': 'opc1', 'name': 'OPC', 'protocol': 'opcua',
            'endpoint': 'opc.tcp://localhost:4840',
            'nodes': [{'node_id': 'ns=2;s=Temp', 'name': 'temp', 'unit': 'C'}]
        }
        client = EnhancedSimulatedOPCUAClient(config)
        client.connect()
        stats = client.get_stats()
        assert 'device_id' in stats

    def test_enhanced_mqtt_disconnect(self):
        """增强版MQTT断开"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedMQTTClient
        config = {
            'id': 'mqtt1', 'name': 'MQTT', 'protocol': 'mqtt',
            'host': '127.0.0.1', 'port': 1883,
            'topics': [{'topic': 'test/temp', 'name': 'temp', 'unit': 'C'}]
        }
        client = EnhancedSimulatedMQTTClient(config)
        client.connect()
        client.disconnect()
        assert client.connected is False

    def test_enhanced_mqtt_get_stats(self):
        """增强版MQTT统计"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedMQTTClient
        config = {
            'id': 'mqtt1', 'name': 'MQTT', 'protocol': 'mqtt',
            'host': '127.0.0.1', 'port': 1883,
            'topics': [{'topic': 'test/temp', 'name': 'temp', 'unit': 'C'}]
        }
        client = EnhancedSimulatedMQTTClient(config)
        client.connect()
        stats = client.get_stats()
        assert 'device_id' in stats

    def test_enhanced_mqtt_get_latest(self):
        """增强版MQTT获取最新数据"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedMQTTClient
        config = {
            'id': 'mqtt1', 'name': 'MQTT', 'protocol': 'mqtt',
            'host': '127.0.0.1', 'port': 1883,
            'topics': [{'topic': 'test/temp', 'name': 'temp', 'unit': 'C'}]
        }
        client = EnhancedSimulatedMQTTClient(config)
        client.connect()
        data = client.get_latest_data()
        assert isinstance(data, dict)

    def test_enhanced_rest_disconnect(self):
        """增强版REST断开"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedRESTClient
        config = {
            'id': 'rest1', 'name': 'REST', 'protocol': 'rest',
            'base_url': 'http://localhost/api',
            'endpoints': [{'name': 'temp', 'path': '/temp', 'unit': 'C'}]
        }
        client = EnhancedSimulatedRESTClient(config)
        client.connect()
        client.disconnect()
        assert client.connected is False

    def test_enhanced_rest_get_stats(self):
        """增强版REST统计"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedRESTClient
        config = {
            'id': 'rest1', 'name': 'REST', 'protocol': 'rest',
            'base_url': 'http://localhost/api',
            'endpoints': [{'name': 'temp', 'path': '/temp', 'unit': 'C'}]
        }
        client = EnhancedSimulatedRESTClient(config)
        client.connect()
        stats = client.get_stats()
        assert 'device_id' in stats

    def test_enhanced_rest_get_latest(self):
        """增强版REST获取最新数据"""
        from 采集层.enhanced_simulated_client import EnhancedSimulatedRESTClient
        config = {
            'id': 'rest1', 'name': 'REST', 'protocol': 'rest',
            'base_url': 'http://localhost/api',
            'endpoints': [{'name': 'temp', 'path': '/temp', 'unit': 'C'}]
        }
        client = EnhancedSimulatedRESTClient(config)
        client.connect()
        data = client.get_latest_data()
        assert isinstance(data, dict)


class TestSimulatedModbusClientExtended:
    """模拟Modbus客户端扩展测试"""

    def test_write_single_register(self):
        """写入单个寄存器"""
        from 采集层.simulated_client import SimulatedModbusClient
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = SimulatedModbusClient(config)
        client.connect()
        result = client.write_single_register(0, 100)
        assert result is True

    def test_write_single_coil(self):
        """写入单个线圈"""
        from 采集层.simulated_client import SimulatedModbusClient
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'status', 'address': 100, 'type': 'bool', 'unit': ''}]
        }
        client = SimulatedModbusClient(config)
        client.connect()
        result = client.write_single_coil(0, True)
        assert result is True

    def test_read_coils(self):
        """读线圈"""
        from 采集层.simulated_client import SimulatedModbusClient
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'status', 'address': 100, 'type': 'bool', 'unit': ''}]
        }
        client = SimulatedModbusClient(config)
        client.connect()
        result = client.read_coils(0, 1)
        # 原断言只校验非 None。read_coils 的契约是返回长度为 count 的 bool 列表，
        # 必须校验元素个数与类型，否则返回任意非 None 垃圾值也算通过。
        assert isinstance(result, list), f"read_coils 应返回列表，实际 {result!r}"
        assert len(result) == 1, f"请求 1 个线圈应返回 1 个元素，实际 {result!r}"
        assert all(isinstance(v, bool) for v in result), \
            f"线圈元素应为 bool，实际 {[type(v).__name__ for v in result]}"

    def test_read_discrete_inputs(self):
        """读离散输入"""
        from 采集层.simulated_client import SimulatedModbusClient
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'status', 'address': 100, 'type': 'bool', 'unit': ''}]
        }
        client = SimulatedModbusClient(config)
        client.connect()
        result = client.read_discrete_inputs(0, 1)
        assert isinstance(result, list), f"read_discrete_inputs 应返回列表，实际 {result!r}"
        assert len(result) == 1, f"请求 1 个离散输入应返回 1 个元素，实际 {result!r}"
        assert all(isinstance(v, bool) for v in result), \
            f"离散输入元素应为 bool，实际 {[type(v).__name__ for v in result]}"

    def test_read_input_registers(self):
        """读输入寄存器"""
        from 采集层.simulated_client import SimulatedModbusClient
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = SimulatedModbusClient(config)
        client.connect()
        result = client.read_input_registers(0, 2)
        assert isinstance(result, list), f"read_input_registers 应返回列表，实际 {result!r}"
        assert len(result) == 2, f"请求 2 个寄存器应返回 2 个元素，实际 {result!r}"
        assert all(isinstance(v, int) for v in result), \
            f"寄存器元素应为 int，实际 {[type(v).__name__ for v in result]}"

    def test_get_latest_data(self):
        """获取最新数据"""
        from 采集层.simulated_client import SimulatedModbusClient
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = SimulatedModbusClient(config)
        client.connect()
        data = client.get_latest_data()
        assert isinstance(data, dict)

    def test_add_data_callback(self):
        """添加数据回调"""
        from 采集层.simulated_client import SimulatedModbusClient
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = SimulatedModbusClient(config)
        client.connect()
        # 原测试只调用不校验（注释 "Should not raise" = 恒过）。
        # SimulatedModbusClient 是轮询型客户端，add_data_callback 继承自
        # BaseDeviceClient 的 no-op 实现（采集层/base_client.py:70）——
        # Modbus 没有推送回调。这里把这个契约断言出来，而不是空跑一遍。
        result = client.add_data_callback(lambda *args: None)
        assert result is None, "轮询型客户端的 add_data_callback 应为 no-op（返回 None）"
        assert not hasattr(client, '_data_callbacks'), \
            "SimulatedModbusClient 不应维护推送回调列表（Modbus 是轮询型）"
        assert client.connected is True, "添加回调不应改变连接状态"

    def test_get_stats(self):
        """获取统计信息"""
        from 采集层.simulated_client import SimulatedModbusClient
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = SimulatedModbusClient(config)
        client.connect()
        stats = client.get_stats()
        assert 'device_id' in stats

    def test_disconnect(self):
        """断开连接"""
        from 采集层.simulated_client import SimulatedModbusClient
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        client = SimulatedModbusClient(config)
        client.connect()
        client.disconnect()
        assert client.connected is False
