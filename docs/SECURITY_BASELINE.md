# SmartSCADA 安全基线配置

## 概述

本文档定义SmartSCADA系统的安全基线配置，确保系统符合工业安全标准。

---

## 1. 操作系统安全基线

### Windows Server 2019+
```powershell
# 禁用不必要的服务
Disable-WindowsOptionalFeature -Online -FeatureName SMB1Protocol
Disable-WindowsOptionalFeature -Online -FeatureName TelnetClient

# 启用防火墙
Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled True

# 配置审计策略
auditpol /set /category:"Logon/Logoff" /success:enable /failure:enable
auditpol /set /category:"Object Access" /success:enable /failure:enable
```

### Linux (如果使用)
```bash
# 禁用不必要的服务
systemctl disable telnet.socket
systemctl disable rsh.socket

# 配置防火墙
ufw enable
ufw default deny incoming
ufw allow 5000/tcp  # SmartSCADA API
ufw allow 443/tcp   # HTTPS
```

---

## 2. Python安全配置

### 环境变量安全
```bash
# .env 文件权限（Linux）
chmod 600 .env

# Windows
icacls .env /inheritance:r /grant:r %USERNAME%:F
```

### Python安全模块
```python
# 使用安全的随机数
import secrets
token = secrets.token_hex(32)

# 使用安全的哈希
import bcrypt
hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12))

# 使用安全的JSON解析
import json
data = json.loads(json_str)  # 不使用yaml.load
```

---

## 3. Flask安全配置

### 安全头设置
```python
@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['Content-Security-Policy'] = "default-src 'self'"
    return response
```

### CORS配置
```python
from flask_cors import CORS

CORS(app, resources={
    r"/api/*": {
        "origins": ["http://localhost:5173", "https://yourdomain.com"],
        "methods": ["GET", "POST", "PUT", "DELETE"],
        "allow_headers": ["Content-Type", "Authorization"],
        "supports_credentials": True
    }
})
```

### 速率限制
```python
from flask_limiter import Limiter

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per minute"],
    storage_uri="memory://"
)

# 登录接口更严格限制
@limiter.limit("5 per minute")
def login():
    pass
```

---

## 4. 数据库安全配置

### SQLite安全
```python
# 启用WAL模式
conn.execute('PRAGMA journal_mode=WAL')

# 设置忙等待超时
conn.execute('PRAGMA busy_timeout=30000')

# 启用外键约束
conn.execute('PRAGMA foreign_keys=ON')

# 设置安全删除
conn.execute('PRAGMA secure_delete=ON')
```

### 数据库加密（可选）
```python
# 使用SQLCipher加密数据库
import sqlcipher3
conn = sqlcipher3.connect('scada.db')
conn.execute(f"PRAGMA key='{encryption_key}'")
```

---

## 5. JWT安全配置

### Token配置
```python
import jwt
from datetime import datetime, timedelta

# Token有效期
ACCESS_TOKEN_EXPIRY = timedelta(hours=1)
REFRESH_TOKEN_EXPIRY = timedelta(days=7)

# Token生成
def generate_token(user_id, role):
    payload = {
        'user_id': user_id,
        'role': role,
        'exp': datetime.utcnow() + ACCESS_TOKEN_EXPIRY,
        'iat': datetime.utcnow(),
        'jti': secrets.token_hex(16)  # 唯一标识
    }
    return jwt.encode(payload, JWT_SECRET, algorithm='HS256')
```

### Token刷新
```python
def refresh_token(refresh_token):
    try:
        payload = jwt.decode(refresh_token, JWT_SECRET, algorithms=['HS256'])
        # 检查是否在黑名单中
        if is_token_blacklisted(payload['jti']):
            return None
        return generate_token(payload['user_id'], payload['role'])
    except jwt.ExpiredSignatureError:
        return None
```

---

## 6. 密码安全策略

### 密码复杂度要求
```python
import re

def validate_password(password):
    """验证密码复杂度"""
    if len(password) < 8:
        return False, "密码长度至少8位"
    if not re.search(r'[A-Z]', password):
        return False, "密码必须包含大写字母"
    if not re.search(r'[a-z]', password):
        return False, "密码必须包含小写字母"
    if not re.search(r'[0-9]', password):
        return False, "密码必须包含数字"
    return True, "密码符合要求"
```

### 密码哈希
```python
import bcrypt

def hash_password(password):
    """哈希密码"""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(rounds=12))

def verify_password(password, hashed):
    """验证密码"""
    return bcrypt.checkpw(password.encode('utf-8'), hashed)
```

---

## 7. 网络安全配置

### TLS配置
```python
import ssl

# 创建SSL上下文
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.minimum_version = ssl.TLSVersion.TLSv1_2
context.maximum_version = ssl.TLSVersion.TLSv1_3

# 加载证书
context.load_cert_chain('certs/server.crt', 'certs/server.key')

# 禁用弱密码
context.set_ciphers('ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384')
```

### 防火墙规则
```powershell
# Windows防火墙
New-NetFirewallRule -DisplayName "SmartSCADA" -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow
New-NetFirewallRule -DisplayName "SmartSCADA HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
```

---

## 8. 日志安全配置

### 日志轮转
```python
from loguru import logger

logger.add(
    "logs/scada_{time:YYYY-MM-DD}.log",
    rotation="100 MB",
    retention="30 days",
    compression="gz",
    encoding="utf-8",
    enqueue=True
)
```

### 审计日志
```python
# 独立审计日志
logger.add(
    "logs/audit_{time:YYYY-MM-DD}.log",
    level="WARNING",
    rotation="50 MB",
    retention="90 days",
    compression="gz"
)
```

---

## 9. 备份安全配置

### 备份加密
```bash
# GPG加密备份
gpg --encrypt --recipient admin@company.com backup.db

# 解密备份
gpg --decrypt backup.db.gpg > backup.db
```

### 备份验证
```bash
# 验证备份完整性
sqlite3 backup.db "PRAGMA integrity_check;"
```

---

## 10. 监控安全配置

### Prometheus安全
```yaml
# prometheus.yml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'smartscada'
    static_configs:
      - targets: ['localhost:5000']
    basic_auth:
      username: 'prometheus'
      password: 'secure_password'
```

### 告警安全
```yaml
# alertmanager.yml
route:
  group_by: ['alertname']
  group_wait: 10s
  group_interval: 10s
  repeat_interval: 1h
  receiver: 'web.hook'

receivers:
  - name: 'web.hook'
    webhook_configs:
      - url: 'http://localhost:5001/alert'
```

---

## 合规检查清单

### 每日检查
- [ ] 检查登录失败日志
- [ ] 检查异常访问
- [ ] 检查系统资源使用
- [ ] 检查备份状态

### 每周检查
- [ ] 检查安全更新
- [ ] 检查用户权限
- [ ] 检查日志轮转
- [ ] 检查证书有效期

### 每月检查
- [ ] 执行安全扫描
- [ ] 审查用户账户
- [ ] 测试备份恢复
- [ ] 更新安全策略

### 每季度检查
- [ ] 渗透测试
- [ ] 合规审计
- [ ] 应急演练
- [ ] 安全培训
