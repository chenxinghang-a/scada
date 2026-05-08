from typing import Any
import requests

try:
    r = requests.get('http://localhost:5000/api/system/status', timeout=5)
    if r.status_code == 200:
        data = r.json()
        print('系统运行正常')
        devices = data.get('devices', [])
        db = data.get('database', {})
        collector = data.get('collector', {})
        print(f'设备: {len(devices)} 个')
        print(f'数据: {db.get("realtime_records", 0)} 条')
        print(f'采集: {collector.get("total_collections", 0)} 次')
    else:
        print(f'API返回错误: {r.status_code}')
except Exception as e:
    print(f'系统未运行: {e}')
