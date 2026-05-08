import requests, json
base = 'http://localhost:5000'
r = requests.post(f'{base}/api/auth/login', json={'username':'admin','password':'admin123'})
h = {'Authorization': f"Bearer {r.json().get('token','')}"}
for ep in ['/api/industry40/health','/api/industry40/oee','/api/industry40/energy','/api/industry40/edge/status']:
    r = requests.get(f'{base}{ep}', headers=h)
    d = r.json()
    print(f"{ep}: {json.dumps(d, ensure_ascii=False)[:200]}")
