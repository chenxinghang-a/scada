"""
设备模拟器测试工具
用于测试数据采集、报警、控制等功能
运行: python tools/device_simulator.py
"""

import json
import time
import random
import threading
from datetime import datetime
from pathlib import Path


class DeviceSimulator:
    """设备模拟器"""

    def __init__(self, config_path: str = '配置/devices.yaml'):
        self.config_path = config_path
        self.devices = {}
        self.running = False
        self.threads = []

    def load_config(self):
        """加载设备配置"""
        import yaml
        config_file = Path(self.config_path)
        if not config_file.exists():
            print(f"配置文件不存在: {self.config_path}")
            return False

        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}

        self.devices = {}
        for device in config.get('devices', []):
            device_id = device.get('id', device.get('device_id'))
            if device_id:
                self.devices[device_id] = device

        print(f"加载了 {len(self.devices)} 个设备配置")
        return True

    def simulate_device(self, device_id: str, device_config: dict):
        """模拟单个设备数据采集"""
        registers = device_config.get('registers', [])
        interval = device_config.get('collection_interval', 5)

        print(f"  启动设备模拟: {device_id} ({len(registers)} 个寄存器, 间隔 {interval}s)")

        while self.running:
            try:
                for reg in registers:
                    name = reg.get('name', '')
                    data_type = reg.get('data_type', 'int16')

                    # 生成模拟值
                    if 'temp' in name.lower():
                        value = 25.0 + random.uniform(-5, 15)
                    elif 'pressure' in name.lower():
                        value = 5.0 + random.uniform(-2, 5)
                    elif 'flow' in name.lower():
                        value = 50.0 + random.uniform(-20, 30)
                    elif 'level' in name.lower():
                        value = 60.0 + random.uniform(-30, 30)
                    elif 'speed' in name.lower() or 'rpm' in name.lower():
                        value = 1500 + random.randint(-500, 500)
                    elif 'vibration' in name.lower():
                        value = 2.0 + random.uniform(0, 3)
                    elif data_type == 'float32':
                        value = random.uniform(0, 100)
                    else:
                        value = random.randint(0, 100)

                    # 模拟数据质量
                    quality = 'good' if random.random() > 0.05 else 'uncertain'

                    data_point = {
                        'device_id': device_id,
                        'register_name': name,
                        'value': round(value, 2),
                        'unit': reg.get('unit', ''),
                        'quality': quality,
                        'timestamp': datetime.now().isoformat(),
                    }

                    # 这里可以发送到数据队列或API
                    # print(f"    {device_id}/{name}: {value:.2f} {reg.get('unit', '')}")

                time.sleep(interval)

            except Exception as e:
                print(f"  设备 {device_id} 模拟异常: {e}")
                time.sleep(1)

    def start(self):
        """启动所有设备模拟"""
        if not self.load_config():
            return False

        self.running = True
        self.threads = []

        for device_id, device_config in self.devices.items():
            thread = threading.Thread(
                target=self.simulate_device,
                args=(device_id, device_config),
                daemon=True
            )
            thread.start()
            self.threads.append(thread)

        print(f"\n✅ 已启动 {len(self.threads)} 个设备模拟器")
        return True

    def stop(self):
        """停止所有设备模拟"""
        self.running = False
        for thread in self.threads:
            thread.join(timeout=5)
        print("\n🛑 所有设备模拟器已停止")

    def run_interactive(self):
        """交互式运行"""
        print("=" * 60)
        print("SCADA 设备模拟器测试工具")
        print("=" * 60)

        if not self.start():
            return

        print("\n命令:")
        print("  status  - 显示设备状态")
        print("  config  - 重新加载配置")
        print("  quit    - 退出")
        print()

        try:
            while True:
                cmd = input("> ").strip().lower()

                if cmd == 'quit' or cmd == 'exit':
                    break
                elif cmd == 'status':
                    print(f"\n设备数量: {len(self.devices)}")
                    print(f"模拟线程: {len(self.threads)}")
                    for device_id in self.devices:
                        print(f"  - {device_id}")
                elif cmd == 'config':
                    self.stop()
                    self.start()
                elif cmd:
                    print(f"未知命令: {cmd}")

        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


class AlarmSimulator:
    """报警模拟器"""

    def __init__(self):
        self.alarms = []

    def generate_random_alarm(self):
        """生成随机报警"""
        devices = ['motor_01', 'pump_01', 'fan_01', 'tank_01', 'boiler_01']
        registers = ['temperature', 'pressure', 'vibration', 'level', 'flow']
        levels = ['warning', 'critical', 'info']
        conditions = ['greater_than', 'less_than']

        device = random.choice(devices)
        register = random.choice(registers)
        level = random.choice(levels)
        condition = random.choice(conditions)

        if register == 'temperature':
            threshold = random.uniform(60, 100)
            value = threshold + random.uniform(-10, 20)
        elif register == 'pressure':
            threshold = random.uniform(5, 15)
            value = threshold + random.uniform(-3, 5)
        elif register == 'vibration':
            threshold = random.uniform(3, 8)
            value = threshold + random.uniform(-2, 4)
        else:
            threshold = random.uniform(50, 90)
            value = threshold + random.uniform(-20, 30)

        alarm = {
            'device_id': device,
            'register_name': register,
            'level': level,
            'condition': condition,
            'threshold': round(threshold, 2),
            'actual_value': round(value, 2),
            'timestamp': datetime.now().isoformat(),
            'message': f'{device} {register} {condition} {threshold:.1f}',
        }

        self.alarms.append(alarm)
        return alarm

    def run_alarm_simulation(self, interval: float = 10.0, max_alarms: int = 100):
        """运行报警模拟"""
        print(f"开始报警模拟 (间隔 {interval}s, 最多 {max_alarms} 个)")

        for i in range(max_alarms):
            alarm = self.generate_random_alarm()
            print(f"[{i+1}] {alarm['level'].upper()}: {alarm['message']}")
            time.sleep(interval)

        print(f"报警模拟完成，共生成 {len(self.alarms)} 个报警")


def main():
    """主函数"""
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == '--alarm':
        # 报警模拟模式
        simulator = AlarmSimulator()
        interval = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
        max_alarms = int(sys.argv[3]) if len(sys.argv) > 3 else 100
        simulator.run_alarm_simulation(interval, max_alarms)
    else:
        # 设备模拟模式
        simulator = DeviceSimulator()
        simulator.run_interactive()


if __name__ == '__main__':
    main()
