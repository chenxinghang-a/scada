"""全面检查SCADA系统bug"""
import requests
from datetime import datetime, timedelta

base = 'http://127.0.0.1:5000'

print("=" * 60)
print("SCADA系统Bug检查")
print("=" * 60)

# 1. 检查所有API端点
print("\n[1. API端点测试]")
endpoints = [
    ('GET', '/api/devices'),
    ('GET', '/api/devices/temp_sensor_01'),
    ('GET', '/api/data/realtime?limit=5'),
    ('GET', '/api/data/latest/temp_sensor_01'),
    ('GET', '/api/data/history/temp_sensor_01/temperature'),
    ('GET', '/api/alarms'),
    ('GET', '/api/alarms/active'),
    ('GET', '/api/system/status'),
    ('GET', '/api/system/database'),
]
for method, url in endpoints:
    try:
        r = requests.get(base + url, timeout=5)
        status = 'OK' if r.status_code == 200 else f'FAIL({r.status_code})'
        data = r.json()
        if 'error' in data:
            status += f' ERR:{data["error"]}'
        print(f'  {status} {url}')
    except Exception as e:
        print(f'  ERR  {url}: {e}')

# 2. 检查数据质量
print("\n[2. 数据质量检查]")
r = requests.get(f'{base}/api/data/realtime?limit=20')
data = r.json()['data']
issues = []
for item in data:
    v = item['value']
    name = item['register_name']
    unit = item.get('unit', '')
    device = item['device_id']

    if name == 'temperature' and (v < -50 or v > 100):
        issues.append(f'  WARN: {device}/{name}={v}{unit} 温度异常!')
    elif name == 'pressure' and (v < 0 or v > 10):
        issues.append(f'  WARN: {device}/{name}={v}{unit} 压力异常!')
    elif name == 'voltage' and (v < 100 or v > 300):
        issues.append(f'  WARN: {device}/{name}={v}{unit} 电压异常!')
    elif name == 'current' and (v < 0 or v > 200):
        issues.append(f'  WARN: {device}/{name}={v}{unit} 电流异常!')

if issues:
    for i in issues[:5]:
        print(i)
else:
    print('  数据质量正常')

# 3. 检查报警
print("\n[3. 报警检查]")
r = requests.get(f'{base}/api/alarms?limit=10')
alarms = r.json().get('alarms', [])
print(f'  报警数: {len(alarms)}')
for a in alarms[:5]:
    print(f'  [{a["alarm_level"]}] {a["alarm_message"]} 值={a["actual_value"]:.2f}')

# 4. 检查历史数据
print("\n[4. 历史数据检查]")
r = requests.get(f'{base}/api/data/history/temp_sensor_01/temperature?interval=1min')
hist = r.json()
print(f'  历史记录数: {len(hist.get("data", []))}')

# 5. 检查导出
print("\n[5. 导出功能检查]")
data = {
    'start_time': (datetime.now() - timedelta(hours=1)).isoformat(),
    'end_time': datetime.now().isoformat(),
    'format': 'csv'
}
r = requests.post(f'{base}/api/export/device/temp_sensor_01', json=data, timeout=10)
print(f'  设备导出: {r.json()}')
r = requests.post(f'{base}/api/export/alarms', json={'format': 'csv'}, timeout=10)
print(f'  报警导出: {r.json()}')

# 6. 检查页面
print("\n[6. 页面测试]")
pages = ['/', '/devices', '/history', '/alarms', '/config']
for page in pages:
    r = requests.get(f'{base}{page}', timeout=5)
    print(f'  {page}: {"OK" if r.status_code == 200 else f"FAIL({r.status_code})"}')

# 7. 检查WebSocket
print("\n[7. WebSocket检查]")
try:
    r = requests.get(f'{base}/socket.io/?EIO=4&transport=polling', timeout=5)
    print(f'  WebSocket端点: {"OK" if r.status_code == 200 else f"FAIL({r.status_code})"}')
except Exception as e:
    print(f'  WebSocket端点: ERR - {e}')

print("\n" + "=" * 60)
print("检查完成!")
print("=" * 60)
