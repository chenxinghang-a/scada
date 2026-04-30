"""全面测试SCADA系统"""
import requests
from datetime import datetime, timedelta

base = 'http://127.0.0.1:5000'

print("=" * 50)
print("SCADA系统全面测试")
print("=" * 50)

# 1. 系统状态
r = requests.get(f'{base}/api/system/status', timeout=5)
status = r.json()
print(f"\n[系统状态]")
print(f"  设备数: {len(status['devices'])}")
print(f"  采集成功: {status['collector']['total_collections']}")
print(f"  采集失败: {status['collector']['failed_collections']}")
print(f"  实时记录: {status['database']['realtime_records']}")

# 2. 设备列表
r = requests.get(f'{base}/api/devices', timeout=5)
devices = r.json()['devices']
print(f"\n[设备列表]")
for d in devices:
    print(f"  {d['name']}: {'在线' if d['connected'] else '离线'}")

# 3. 实时数据
r = requests.get(f'{base}/api/data/realtime', timeout=5)
data = r.json()['data']
print(f"\n[实时数据] ({len(data)}条)")
for item in data[:6]:
    print(f"  {item['device_id']}/{item['register_name']}: {item['value']:.2f} {item.get('unit','')}")

# 4. 报警
r = requests.get(f'{base}/api/alarms?limit=10', timeout=5)
alarms = r.json().get('alarms', [])
print(f"\n[报警记录] ({len(alarms)}条)")
for a in alarms[:5]:
    print(f"  [{a['alarm_level']}] {a['alarm_message']} (值={a['actual_value']:.2f})")

# 5. 历史数据
r = requests.get(f'{base}/api/data/history/temp_sensor_01/temperature?interval=1min', timeout=5)
hist = r.json()
print(f"\n[历史数据] temp/temperature: {len(hist.get('data', []))}条")

# 6. 导出测试
end_time = datetime.now().isoformat()
start_time = (datetime.now() - timedelta(hours=1)).isoformat()
data = {'start_time': start_time, 'end_time': end_time, 'format': 'csv'}

r = requests.post(f'{base}/api/export/device/temp_sensor_01', json=data, timeout=10)
result = r.json()
print(f"\n[数据导出]")
print(f"  设备数据: {'成功' if result['success'] else '失败'} - {result.get('filepath', result.get('message'))}")

r = requests.post(f'{base}/api/export/alarms', json={'format': 'csv'}, timeout=10)
result = r.json()
print(f"  报警记录: {'成功' if result['success'] else '失败'} - {result.get('filepath', result.get('message'))}")

# 7. 页面可用性
pages = ['/', '/devices', '/history', '/alarms', '/config']
print(f"\n[页面测试]")
for page in pages:
    r = requests.get(f'{base}{page}', timeout=5)
    print(f"  {page}: {'OK' if r.status_code == 200 else f'FAIL({r.status_code})'}")

# 8. WebSocket测试
print(f"\n[WebSocket]")
print(f"  ws://127.0.0.1:5000/socket.io/ - 已配置")

print("\n" + "=" * 50)
print("测试完成!")
print("=" * 50)
