"""
模拟客户端全部协议测试 - 提升采集层/simulated_client.py 覆盖率
"""
import pytest
from unittest.mock import MagicMock


class TestSimulatedOPCUAClient:
    """模拟OPC UA客户端测试"""

    def _make_client(self):
        from 采集层.simulated_client import SimulatedOPCUAClient
        config = {
            'id': 'opc1', 'name': 'OPC Device', 'protocol': 'opcua',
            'endpoint': 'opc.tcp://localhost:4840',
            'nodes': [
                {'node_id': 'ns=2;s=Temperature', 'name': 'temperature', 'unit': 'C'},
                {'node_id': 'ns=2;s=Pressure', 'name': 'pressure', 'unit': 'MPa'},
            ]
        }
        return SimulatedOPCUAClient(config)

    def test_init(self):
        """初始化"""
        client = self._make_client()
        assert client.device_id == 'opc1'

    def test_connect(self):
        """连接"""
        client = self._make_client()
        result = client.connect()
        assert result is True
        assert client.connected is True

    def test_disconnect(self):
        """断开"""
        client = self._make_client()
        client.connect()
        client.disconnect()
        assert client.connected is False

    def test_get_latest_data(self):
        """获取最新数据"""
        client = self._make_client()
        client.connect()
        data = client.get_latest_data()
        assert isinstance(data, dict)
        assert 'temperature' in data

    def test_get_stats(self):
        """获取统计"""
        client = self._make_client()
        client.connect()
        stats = client.get_stats()
        assert 'device_id' in stats


class TestSimulatedMQTTClient:
    """模拟MQTT客户端测试"""

    def _make_client(self):
        from 采集层.simulated_client import SimulatedMQTTClient
        config = {
            'id': 'mqtt1', 'name': 'MQTT Sensor', 'protocol': 'mqtt',
            'host': '127.0.0.1', 'port': 1883,
            'topics': [
                {'topic': 'factory/workshop_a/temperature', 'name': 'temperature', 'unit': 'C'},
                {'topic': 'factory/workshop_a/humidity', 'name': 'humidity', 'unit': '%RH'},
            ]
        }
        return SimulatedMQTTClient(config)

    def test_init(self):
        """初始化"""
        client = self._make_client()
        assert client.device_id == 'mqtt1'

    def test_connect(self):
        """连接"""
        client = self._make_client()
        result = client.connect()
        assert result is True

    def test_disconnect(self):
        """断开"""
        client = self._make_client()
        client.connect()
        client.disconnect()
        assert client.connected is False

    def test_get_latest_data(self):
        """获取最新数据"""
        client = self._make_client()
        client.connect()
        data = client.get_latest_data()
        assert isinstance(data, dict)

    def test_get_stats(self):
        """获取统计"""
        client = self._make_client()
        client.connect()
        stats = client.get_stats()
        assert 'device_id' in stats

    def test_add_data_callback(self):
        """添加数据回调"""
        client = self._make_client()
        client.connect()
        client.add_data_callback(lambda *args: None)


class TestSimulatedRESTClient:
    """模拟REST客户端测试"""

    def _make_client(self):
        from 采集层.simulated_client import SimulatedRESTClient
        config = {
            'id': 'rest1', 'name': 'REST Gateway', 'protocol': 'rest',
            'base_url': 'http://192.168.1.250/api',
            'endpoints': [
                {'name': 'temperature', 'path': '/sensors/temp', 'method': 'GET', 'unit': 'C'},
                {'name': 'humidity', 'path': '/sensors/humi', 'method': 'GET', 'unit': '%RH'},
            ]
        }
        return SimulatedRESTClient(config)

    def test_init(self):
        """初始化"""
        client = self._make_client()
        assert client.device_id == 'rest1'

    def test_connect(self):
        """连接"""
        client = self._make_client()
        result = client.connect()
        assert result is True

    def test_disconnect(self):
        """断开"""
        client = self._make_client()
        client.connect()
        client.disconnect()
        assert client.connected is False

    def test_get_latest_data(self):
        """获取最新数据"""
        client = self._make_client()
        client.connect()
        data = client.get_latest_data()
        assert isinstance(data, dict)

    def test_get_stats(self):
        """获取统计"""
        client = self._make_client()
        client.connect()
        stats = client.get_stats()
        assert 'device_id' in stats


class TestSimulatedClientModbusExtended:
    """模拟Modbus客户端扩展测试"""

    def _make_client(self):
        from 采集层.simulated_client import SimulatedModbusClient
        config = {
            'id': 'dev1', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [
                {'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'},
                {'name': 'pressure', 'address': 2, 'type': 'float', 'unit': 'MPa'},
                {'name': 'status', 'address': 100, 'type': 'bool', 'unit': ''},
            ]
        }
        return SimulatedModbusClient(config)

    def test_read_multiple_registers(self):
        """读多个寄存器"""
        client = self._make_client()
        client.connect()
        result = client.read_holding_registers(0, 4)
        assert result is not None
        assert len(result) > 0

    def test_read_discrete_inputs(self):
        """读离散输入"""
        client = self._make_client()
        client.connect()
        result = client.read_discrete_inputs(0, 4)
        assert result is not None

    def test_read_input_registers(self):
        """读输入寄存器"""
        client = self._make_client()
        client.connect()
        result = client.read_input_registers(0, 2)
        assert result is not None

    def test_write_single_register(self):
        """写单个寄存器"""
        client = self._make_client()
        client.connect()
        result = client.write_single_register(0, 42)
        assert result is True

    def test_write_single_coil(self):
        """写单个线圈"""
        client = self._make_client()
        client.connect()
        result = client.write_single_coil(0, True)
        assert result is True

    def test_get_latest_data(self):
        """获取最新数据"""
        client = self._make_client()
        client.connect()
        # Read some data first
        client.read_holding_registers(0, 4)
        data = client.get_latest_data()
        assert isinstance(data, dict)

    def test_get_stats(self):
        """获取统计"""
        client = self._make_client()
        client.connect()
        stats = client.get_stats()
        assert 'device_id' in stats
        assert stats['device_id'] == 'dev1'

    def test_connect_disconnect_cycle(self):
        """连接/断开循环"""
        client = self._make_client()
        for _ in range(3):
            client.connect()
            assert client.connected is True
            client.disconnect()
            assert client.connected is False
