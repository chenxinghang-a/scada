import requests
import json

try:
    r = requests.get('http://localhost:5000/')
    print(f'首页状态码: {r.status_code}')
    
    r = requests.get('http://localhost:5000/api/devices')
    print(f'设备API状态码: {r.status_code}')
    if r.status_code == 200:
        data = r.json()
        devices = data.get('devices', [])
        print(f'设备数量: {len(devices)}')
        for d in devices:
            print(f'  - {d.get("device_id")}: connected={d.get("connected")}')
    
    r = requests.get('http://localhost:5000/api/data/realtime')
    print(f'实时数据API状态码: {r.status_code}')
    if r.status_code == 200:
        data = r.json()
        records = data.get('data', [])
        print(f'数据记录数: {len(records)}')
        for rec in records[:5]:
            print(f'  - {rec.get("device_id")}/{rec.get("register_name")}: {rec.get("value")} {rec.get("unit", "")}')
    
    r = requests.get('http://localhost:5000/api/system/status')
    print(f'系统状态API状态码: {r.status_code}')
    if r.status_code == 200:
        data = r.json()
        db_stats = data.get('database', {})
        collector_stats = data.get('collector', {})
        print(f'数据库: realtime={db_stats.get("realtime_records", 0)}, history={db_stats.get("history_records", 0)}')
        print(f'采集器: running={collector_stats.get("running")}, total={collector_stats.get("total_collections", 0)}, success={collector_stats.get("successful_collections", 0)}')
    
    print('\nAPI测试完成！')
except Exception as e:
    print(f'API测试失败: {e}')
