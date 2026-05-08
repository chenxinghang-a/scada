"""
工业数据采集与监控系统 - 全面Bug测试
测试所有功能模块，发现潜在问题
"""

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

import requests
import time
import json
from datetime import datetime, timedelta

BASE_URL = 'http://127.0.0.1:5000'

class BugTester:
    def __init__(self):
        self.bugs = []
        self.passed = 0
        self.failed = 0

    def test(self, name, func):
        """运行测试"""
        try:
            result = func()
            if result:
                self.passed += 1
                print(f"  [PASS] {name}")
            else:
                self.failed += 1
                self.bugs.append(name)
                print(f"  [FAIL] {name}")
        except Exception as e:
            self.failed += 1
            self.bugs.append(f"{name}: {str(e)}")
            print(f"  [FAIL] {name}: {e}")

    def test_api_endpoints(self):
        """测试所有API端点"""
        print("\n" + "="*60)
        print("【API端点测试】")
        print("="*60)

        endpoints = [
            ('GET', '/api/devices'),
            ('GET', '/api/devices/temp_sensor_01'),
            ('GET', '/api/devices/nonexistent_device'),
            ('GET', '/api/data/realtime'),
            ('GET', '/api/data/realtime?limit=10'),
            ('GET', '/api/data/realtime?device_id=temp_sensor_01'),
            ('GET', '/api/data/latest/temp_sensor_01'),
            ('GET', '/api/data/latest/nonexistent'),
            ('GET', '/api/data/history/temp_sensor_01/temperature'),
            ('GET', '/api/alarms'),
            ('GET', '/api/alarms?limit=5'),
            ('GET', '/api/alarms?device_id=temp_sensor_01'),
            ('GET', '/api/alarms?alarm_level=critical'),
            ('GET', '/api/alarms/active'),
            ('GET', '/api/alarms/statistics'),
            ('GET', '/api/system/status'),
            ('GET', '/api/system/database'),
        ]

        for method, url in endpoints:
            def test_func(u=url):
                r = requests.get(f'{BASE_URL}{u}', timeout=5)
                return r.status_code in [200, 404]
            self.test(f"{method} {url}", test_func)

    def test_device_operations(self):
        """测试设备操作"""
        print("\n" + "="*60)
        print("【设备操作测试】")
        print("="*60)

        # 获取设备列表
        def test_get_devices():
            r = requests.get(f'{BASE_URL}/api/devices', timeout=5)
            data = r.json()
            return 'devices' in data and len(data['devices']) > 0
        self.test("获取设备列表", test_get_devices)

        # 获取单个设备
        def test_get_device():
            r = requests.get(f'{BASE_URL}/api/devices/temp_sensor_01', timeout=5)
            data = r.json()
            return 'device_id' in data
        self.test("获取单个设备", test_get_device)

        # 获取不存在的设备
        def test_get_nonexistent():
            r = requests.get(f'{BASE_URL}/api/devices/nonexistent', timeout=5)
            return r.status_code == 404
        self.test("获取不存在的设备", test_get_nonexistent)

        # 断开设备
        def test_disconnect():
            r = requests.post(f'{BASE_URL}/api/devices/temp_sensor_01/disconnect', timeout=5)
            data = r.json()
            return data.get('success') == True
        self.test("断开设备", test_disconnect)

        # 连接设备
        def test_connect():
            r = requests.post(f'{BASE_URL}/api/devices/temp_sensor_01/connect', timeout=5)
            data = r.json()
            return data.get('success') == True
        self.test("连接设备", test_connect)

    def test_data_queries(self):
        """测试数据查询"""
        print("\n" + "="*60)
        print("【数据查询测试】")
        print("="*60)

        # 实时数据
        def test_realtime():
            r = requests.get(f'{BASE_URL}/api/data/realtime?limit=10', timeout=5)
            data = r.json()
            return 'data' in data
        self.test("获取实时数据", test_realtime)

        # 最新数据
        def test_latest():
            r = requests.get(f'{BASE_URL}/api/data/latest/temp_sensor_01', timeout=5)
            data = r.json()
            return 'temperature' in data or 'error' in data
        self.test("获取最新数据", test_latest)

        # 历史数据
        def test_history():
            r = requests.get(f'{BASE_URL}/api/data/history/temp_sensor_01/temperature?interval=1min', timeout=5)
            data = r.json()
            return 'data' in data
        self.test("获取历史数据", test_history)

        # 带时间范围的历史数据
        def test_history_range():
            end = datetime.now().isoformat()
            start = (datetime.now() - timedelta(hours=1)).isoformat()
            r = requests.get(f'{BASE_URL}/api/data/history/temp_sensor_01/temperature?start_time={start}&end_time={end}', timeout=5)
            data = r.json()
            return 'data' in data
        self.test("带时间范围的历史数据", test_history_range)

    def test_alarm_operations(self):
        """测试报警操作"""
        print("\n" + "="*60)
        print("【报警操作测试】")
        print("="*60)

        # 获取报警列表
        def test_get_alarms():
            r = requests.get(f'{BASE_URL}/api/alarms?limit=10', timeout=5)
            data = r.json()
            return 'alarms' in data
        self.test("获取报警列表", test_get_alarms)

        # 获取活动报警
        def test_active_alarms():
            r = requests.get(f'{BASE_URL}/api/alarms/active', timeout=5)
            data = r.json()
            return 'alarms' in data
        self.test("获取活动报警", test_active_alarms)

        # 获取报警统计
        def test_alarm_stats():
            r = requests.get(f'{BASE_URL}/api/alarms/statistics', timeout=5)
            data = r.json()
            return 'total_active_alarms' in data
        self.test("获取报警统计", test_alarm_stats)

        # 确认报警（如果有未确认的）
        def test_acknowledge():
            r = requests.get(f'{BASE_URL}/api/alarms?acknowledged=false&limit=1', timeout=5)
            data = r.json()
            if data.get('alarms') and len(data['alarms']) > 0:
                alarm = data['alarms'][0]
                r2 = requests.post(f'{BASE_URL}/api/alarms/{alarm["alarm_id"]}/acknowledge', 
                    json={'device_id': alarm['device_id'], 'register_name': alarm['register_name']},
                    timeout=5)
                return r2.json().get('success') == True
            return True  # 没有未确认的报警也算通过
        self.test("确认报警", test_acknowledge)

    def test_export_functions(self):
        """测试导出功能"""
        print("\n" + "="*60)
        print("【导出功能测试】")
        print("="*60)

        end = datetime.now().isoformat()
        start = (datetime.now() - timedelta(hours=1)).isoformat()

        # 设备数据导出
        def test_device_export():
            r = requests.post(f'{BASE_URL}/api/export/device/temp_sensor_01', 
                json={'start_time': start, 'end_time': end, 'format': 'csv'},
                timeout=10)
            return r.json().get('success') == True
        self.test("设备数据导出(CSV)", test_device_export)

        # 报警数据导出
        def test_alarm_export():
            r = requests.post(f'{BASE_URL}/api/export/alarms', 
                json={'format': 'csv'},
                timeout=10)
            return r.json().get('success') == True
        self.test("报警数据导出(CSV)", test_alarm_export)

    def test_web_pages(self):
        """测试Web页面"""
        print("\n" + "="*60)
        print("【Web页面测试】")
        print("="*60)

        pages = [
            ('/', '仪表盘'),
            ('/devices', '设备管理'),
            ('/history', '历史数据'),
            ('/alarms', '报警管理'),
            ('/config', '系统配置'),
        ]

        for url, name in pages:
            def test_func(u=url):
                r = requests.get(f'{BASE_URL}{u}', timeout=5)
                return r.status_code == 200
            self.test(f"页面: {name} ({url})", test_func)

    def test_edge_cases(self):
        """测试边界情况"""
        print("\n" + "="*60)
        print("【边界情况测试】")
        print("="*60)

        # 空参数
        def test_empty_params():
            r = requests.get(f'{BASE_URL}/api/data/realtime?limit=', timeout=5)
            return r.status_code in [200, 400]
        self.test("空参数处理", test_empty_params)

        # 无效limit
        def test_invalid_limit():
            r = requests.get(f'{BASE_URL}/api/data/realtime?limit=abc', timeout=5)
            return r.status_code in [200, 400]
        self.test("无效limit参数", test_invalid_limit)

        # 负数limit
        def test_negative_limit():
            r = requests.get(f'{BASE_URL}/api/data/realtime?limit=-1', timeout=5)
            return r.status_code in [200, 400]
        self.test("负数limit参数", test_negative_limit)

        # 超大limit
        def test_large_limit():
            r = requests.get(f'{BASE_URL}/api/data/realtime?limit=999999', timeout=5)
            return r.status_code == 200
        self.test("超大limit参数", test_large_limit)

        # 无效时间格式
        def test_invalid_time():
            r = requests.get(f'{BASE_URL}/api/data/history/temp_sensor_01/temperature?start_time=invalid', timeout=5)
            return r.status_code in [200, 400, 500]
        self.test("无效时间格式", test_invalid_time)

        # 不存在的设备
        def test_nonexistent_device():
            r = requests.get(f'{BASE_URL}/api/data/latest/nonexistent', timeout=5)
            return r.status_code == 404
        self.test("不存在的设备", test_nonexistent_device)

        # 不存在的寄存器
        def test_nonexistent_register():
            r = requests.get(f'{BASE_URL}/api/data/history/temp_sensor_01/nonexistent', timeout=5)
            return r.status_code in [200, 404]
        self.test("不存在的寄存器", test_nonexistent_register)

    def test_concurrent_requests(self):
        """测试并发请求"""
        print("\n" + "="*60)
        print("【并发请求测试】")
        print("="*60)

        import concurrent.futures

        def make_request():
            r = requests.get(f'{BASE_URL}/api/data/realtime?limit=5', timeout=5)
            return r.status_code == 200

        def test_concurrent():
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(make_request) for _ in range(20)]
                results = [f.result() for f in concurrent.futures.as_completed(futures)]
                return all(results)
        self.test("20个并发请求", test_concurrent)

    def run_all_tests(self):
        """运行所有测试"""
        print("="*60)
        print("工业数据采集与监控系统 - 全面Bug测试")
        print(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("="*60)

        self.test_api_endpoints()
        self.test_device_operations()
        self.test_data_queries()
        self.test_alarm_operations()
        self.test_export_functions()
        self.test_web_pages()
        self.test_edge_cases()
        self.test_concurrent_requests()

        # 汇总
        print("\n" + "="*60)
        print("【测试汇总】")
        print("="*60)
        print(f"通过: {self.passed}")
        print(f"失败: {self.failed}")
        print(f"总计: {self.passed + self.failed}")
        print(f"通过率: {self.passed/(self.passed+self.failed)*100:.1f}%")

        if self.bugs:
            print("\n[发现的问题]")
            for bug in self.bugs:
                print(f"  - {bug}")
        else:
            print("\n[PASS] 未发现Bug！")

        return self.bugs


if __name__ == '__main__':
    tester = BugTester()
    bugs = tester.run_all_tests()
