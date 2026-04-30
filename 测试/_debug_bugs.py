import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import requests

# Bug1: get latest data
print("=== Bug1: 获取最新数据 ===")
r = requests.get('http://127.0.0.1:5000/api/data/latest/temp_sensor_01', timeout=5)
print(f'Status: {r.status_code}')
print(f'Body: {r.json()}')

# Bug2: acknowledge alarm
print("\n=== Bug2: 确认报警 ===")
r = requests.get('http://127.0.0.1:5000/api/alarms?acknowledged=false&limit=1', timeout=5)
data = r.json()
alarms = data.get('alarms', [])
print(f'Unacked alarms: {len(alarms)}')
if alarms:
    a = alarms[0]
    alarm_id = a['alarm_id']
    device_id = a['device_id']
    register_name = a['register_name']
    print(f'Alarm: {alarm_id} / {device_id} / {register_name}')
    r2 = requests.post(f'http://127.0.0.1:5000/api/alarms/{alarm_id}/acknowledge',
        json={'device_id': device_id, 'register_name': register_name}, timeout=5)
    print(f'Ack status: {r2.status_code}')
    print(f'Ack body: {r2.json()}')
