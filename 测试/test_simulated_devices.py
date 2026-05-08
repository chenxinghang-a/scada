"""
测试模拟设备数据生成
验证所有18台设备在模拟模式下能正确生成数据
"""

import sys
import yaml
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from 采集层.simulated_client import (
    SimulatedModbusClient, SimulatedOPCUAClient,
    SimulatedMQTTClient, SimulatedRESTClient, _find_rule
)


def test_find_rule_coverage():
    """测试所有设备寄存器是否都能找到匹配规则"""
    config_path = Path(__file__).parent.parent / '配置' / 'devices.yaml'
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    devices = config.get('devices', [])
    total_registers = 0
    matched_registers = 0
    unmatched = []

    for device in devices:
        device_id = device.get('id')
        device_name = device.get('name')
        protocol = device.get('protocol')

        # Modbus设备
        for reg in device.get('registers', []):
            total_registers += 1
            name = reg.get('name', '')
            unit = reg.get('unit', '')
            data_type = reg.get('data_type', 'uint16')
            rule = _find_rule(name, unit, data_type)
            if rule:
                matched_registers += 1
            else:
                unmatched.append(f"{device_id}/{name} [{unit}] ({data_type})")

        # OPC UA设备
        for node in device.get('nodes', []):
            total_registers += 1
            name = node.get('name', '')
            unit = node.get('unit', '')
            rule = _find_rule(name, unit, 'float32')
            if rule:
                matched_registers += 1
            else:
                unmatched.append(f"{device_id}/{name} [{unit}]")

        # MQTT设备
        for topic in device.get('topics', []):
            total_registers += 1
            name = topic.get('name', '')
            unit = topic.get('unit', '')
            rule = _find_rule(name, unit, 'float32')
            if rule:
                matched_registers += 1
            else:
                unmatched.append(f"{device_id}/{name} [{unit}]")

        # REST设备
        for ep in device.get('endpoints', []):
            total_registers += 1
            name = ep.get('name', '')
            unit = ep.get('unit', '')
            rule = _find_rule(name, unit, 'float32')
            if rule:
                matched_registers += 1
            else:
                unmatched.append(f"{device_id}/{name} [{unit}]")

    print(f"\n{'='*60}")
    print(f"规则匹配测试结果")
    print(f"{'='*60}")
    print(f"总寄存器数: {total_registers}")
    print(f"匹配成功: {matched_registers}")
    print(f"匹配失败: {len(unmatched)}")

    if unmatched:
        print(f"\n未匹配的寄存器（将使用智能推断）:")
        for item in unmatched:
            print(f"  - {item}")
    else:
        print(f"\n所有寄存器都能找到匹配规则！")

    return len(unmatched) == 0


def test_modbus_device(device_config):
    """测试单个Modbus设备的模拟数据生成"""
    device_id = device_config.get('id')
    device_name = device_config.get('name')

    print(f"\n设备: {device_id} ({device_name})")
    print(f"  协议: {device_config.get('protocol')}")

    client = SimulatedModbusClient(device_config)
    client.connect()

    success_count = 0
    fail_count = 0

    for reg in device_config.get('registers', []):
        address = reg['address']
        data_type = reg.get('data_type', 'uint16')
        length = 2 if data_type in ('float32', 'int32', 'uint32') else 1

        raw = client.read_holding_registers(address, length)
        if raw is not None:
            success_count += 1
            # 尝试解码
            if data_type == 'float32' and len(raw) >= 2:
                value = client.decode_float32(raw)
            elif data_type == 'int32' and len(raw) >= 2:
                value = client.decode_int32(raw)
            elif data_type == 'uint32' and len(raw) >= 2:
                value = client.decode_uint32(raw)
            elif data_type == 'int16':
                value = client.decode_int16(raw[0])
            else:
                value = client.decode_uint16(raw[0])

            # 应用缩放
            scale = reg.get('scale', 1.0)
            offset = reg.get('offset', 0)
            actual_value = value * scale + offset

            print(f"    {reg['name']:30s} = {actual_value:10.2f} {reg.get('unit', '')}")
        else:
            fail_count += 1
            print(f"    {reg['name']:30s} = 读取失败")

    client.disconnect()
    return success_count, fail_count


def test_opcua_device(device_config):
    """测试OPC UA设备的模拟数据生成"""
    device_id = device_config.get('id')
    device_name = device_config.get('name')

    print(f"\n设备: {device_id} ({device_name})")
    print(f"  协议: {device_config.get('protocol')}")

    client = SimulatedOPCUAClient(device_config)
    client.connect()

    data = client.get_latest_data()
    for name, info in data.items():
        print(f"    {name:30s} = {info['value']:10.2f} {info.get('unit', '')}")

    client.disconnect()
    return len(data), 0


def test_mqtt_device(device_config):
    """测试MQTT设备的模拟数据生成"""
    device_id = device_config.get('id')
    device_name = device_config.get('name')

    print(f"\n设备: {device_id} ({device_name})")
    print(f"  协议: {device_config.get('protocol')}")

    client = SimulatedMQTTClient(device_config)
    client.connect()

    data = client.get_latest_data()
    for name, info in data.items():
        print(f"    {name:30s} = {info['value']:10.2f} {info.get('unit', '')}")

    client.disconnect()
    return len(data), 0


def test_rest_device(device_config):
    """测试REST设备的模拟数据生成"""
    device_id = device_config.get('id')
    device_name = device_config.get('name')

    print(f"\n设备: {device_id} ({device_name})")
    print(f"  协议: {device_config.get('protocol')}")

    client = SimulatedRESTClient(device_config)
    client.connect()

    data = client.get_latest_data()
    for name, info in data.items():
        print(f"    {name:30s} = {info['value']:10.2f} {info.get('unit', '')}")

    client.disconnect()
    return len(data), 0


def test_all_devices():
    """测试所有设备"""
    config_path = Path(__file__).parent.parent / '配置' / 'devices.yaml'
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    devices = config.get('devices', [])

    print(f"\n{'='*60}")
    print(f"模拟设备数据生成测试")
    print(f"共 {len(devices)} 台设备")
    print(f"{'='*60}")

    total_success = 0
    total_fail = 0

    for device in devices:
        protocol = device.get('protocol')

        if protocol in ('modbus_tcp', 'modbus_rtu'):
            s, f = test_modbus_device(device)
        elif protocol == 'opcua':
            s, f = test_opcua_device(device)
        elif protocol == 'mqtt':
            s, f = test_mqtt_device(device)
        elif protocol == 'rest':
            s, f = test_rest_device(device)
        else:
            print(f"\n未知协议: {protocol}")
            continue

        total_success += s
        total_fail += f

    print(f"\n{'='*60}")
    print(f"测试总结")
    print(f"{'='*60}")
    print(f"成功: {total_success}")
    print(f"失败: {total_fail}")
    print(f"成功率: {total_success/(total_success+total_fail)*100:.1f}%")

    return total_fail == 0


if __name__ == '__main__':
    print("开始测试...")

    # 测试规则覆盖率
    rule_ok = test_find_rule_coverage()

    # 测试所有设备
    device_ok = test_all_devices()

    if rule_ok and device_ok:
        print("\n所有测试通过！")
        sys.exit(0)
    else:
        print("\n部分测试失败！")
        sys.exit(1)
