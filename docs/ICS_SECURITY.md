# SmartSCADA 工控安全加固指南

## 概述

本文档基于GB/T 35718-2017《信息安全技术 工业控制系统安全防护指南》，定义SmartSCADA系统的工控安全加固措施。

---

## 1. 网络安全加固

### 1.1 网络分区
```
┌─────────────────────────────────────────────────────────┐
│  企业网（ERP/MES）                                       │
│    ↓ 防火墙                                             │
│  DMZ区（Web服务器/API网关）                               │
│    ↓ 工业防火墙                                          │
│  监控网（SCADA服务器/HMI）                                │
│    ↓ 工业防火墙                                          │
│  控制网（PLC/DCS/RTU）                                   │
│    ↓ 物理隔离                                            │
│  现场网（传感器/执行器）                                   │
└─────────────────────────────────────────────────────────┘
```

### 1.2 访问控制策略
```python
# 工业防火墙规则示例
FIREWALL_RULES = {
    'allow_scada_to_plc': {
        'source': '192.168.1.0/24',  # SCADA网段
        'dest': '192.168.2.0/24',    # PLC网段
        'port': [502, 4840],         # Modbus, OPC UA
        'action': 'ALLOW'
    },
    'deny_all': {
        'source': 'any',
        'dest': 'any',
        'action': 'DENY'
    }
}
```

### 1.3 网络监控
```python
# 异常流量检测
def detect_anomaly_traffic():
    """检测工业网络异常流量"""
    normal_patterns = {
        'modbus': {'pps': 100, 'bytes_per_sec': 10000},
        'opcua': {'pps': 50, 'bytes_per_sec': 5000},
    }

    for protocol, pattern in normal_patterns.items():
        current = get_traffic_stats(protocol)
        if current['pps'] > pattern['pps'] * 3:
            alert_anomaly_traffic(protocol, current)
```

---

## 2. 主机安全加固

### 2.1 操作系统加固
```powershell
# Windows Server加固脚本

# 1. 禁用不必要的服务
$services = @(
    'RemoteRegistry',
    'W3SVC',
    'IISADMIN',
    'SNMPTRAP'
)
foreach ($svc in $services) {
    Stop-Service -Name $svc -Force -ErrorAction SilentlyContinue
    Set-Service -Name $svc -StartupType Disabled
}

# 2. 启用审计策略
auditpol /set /category:"System" /success:enable /failure:enable
auditpol /set /category:"Logon/Logoff" /success:enable /failure:enable
auditpol /set /category:"Object Access" /success:enable /failure:enable

# 3. 配置密码策略
net accounts /minpwlen:8 /maxpwage:90 /minpwage:1 /uniquepw:5

# 4. 启用防火墙
Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled True
```

### 2.2 Python环境加固
```bash
# 创建虚拟环境隔离
python -m venv scada_env
source scada_env/bin/activate  # Linux
scada_env\Scripts\activate     # Windows

# 固定依赖版本
pip install -r requirements.txt
pip freeze > requirements.lock

# 定期更新安全补丁
pip install --upgrade pip
pip install --upgrade setuptools
```

---

## 3. 应用安全加固

### 3.1 身份认证加固
```python
# 多因素认证（可选）
import pyotp

def generate_mfa_secret():
    """生成MFA密钥"""
    return pyotp.random_base32()

def verify_mfa_token(secret, token):
    """验证MFA令牌"""
    totp = pyotp.TOTP(secret)
    return totp.verify(token)
```

### 3.2 会话管理加固
```python
# 会话超时配置
SESSION_CONFIG = {
    'access_token_lifetime': timedelta(hours=1),
    'refresh_token_lifetime': timedelta(days=7),
    'max_concurrent_sessions': 3,
    'session_timeout_minutes': 30,
}

# 会话固定攻击防护
def regenerate_session_id(user_id):
    """登录后重新生成会话ID"""
    old_token = get_current_token()
    blacklist_token(old_token)
    return generate_new_token(user_id)
```

### 3.3 输入验证加固
```python
import re
from functools import wraps

def validate_device_id(device_id):
    """验证设备ID格式"""
    pattern = r'^[a-zA-Z0-9_-]{1,50}$'
    if not re.match(pattern, device_id):
        raise ValueError(f"无效的设备ID: {device_id}")
    return device_id

def validate_register_name(name):
    """验证寄存器名称"""
    pattern = r'^[a-zA-Z0-9_]{1,100}$'
    if not re.match(pattern, name):
        raise ValueError(f"无效的寄存器名称: {name}")
    return name
```

---

## 4. 数据安全加固

### 4.1 数据分类
```python
DATA_CLASSIFICATION = {
    'public': ['device_name', 'protocol'],
    'internal': ['device_config', 'alarm_rules'],
    'confidential': ['user_credentials', 'encryption_keys'],
    'restricted': ['audit_logs', 'security_config'],
}
```

### 4.2 数据加密
```python
from cryptography.fernet import Fernet
import base64

class DataEncryptor:
    """数据加密器"""

    def __init__(self, key=None):
        self.key = key or Fernet.generate_key()
        self.cipher = Fernet(self.key)

    def encrypt(self, data: str) -> str:
        """加密数据"""
        return self.cipher.encrypt(data.encode()).decode()

    def decrypt(self, encrypted: str) -> str:
        """解密数据"""
        return self.cipher.decrypt(encrypted.encode()).decode()
```

### 4.3 数据脱敏
```python
def mask_sensitive_data(data: dict, fields: list) -> dict:
    """脱敏敏感数据"""
    masked = data.copy()
    for field in fields:
        if field in masked:
            value = str(masked[field])
            if len(value) > 4:
                masked[field] = value[:2] + '*' * (len(value) - 4) + value[-2:]
    return masked
```

---

## 5. 控制安全加固

### 5.1 写入安全验证
```python
# 安全联锁检查
def check_interlock(device_id, register, value):
    """检查安全联锁"""
    interlocks = get_device_interlocks(device_id)

    for interlock in interlocks:
        if interlock['register'] == register:
            if interlock['condition'] == 'greater_than':
                if value > interlock['limit']:
                    raise SecurityError(
                        f"安全联锁阻止写入: {register} = {value} > {interlock['limit']}"
                    )
    return True
```

### 5.2 操作审计加固
```python
# 完整操作审计
def audit_operation(user, action, target, value, result):
    """记录完整操作审计"""
    audit_record = {
        'timestamp': datetime.now().isoformat(),
        'user': user,
        'action': action,
        'target': target,
        'value': value,
        'result': result,
        'ip_address': request.remote_addr,
        'user_agent': request.user_agent.string,
        'session_id': get_session_id(),
    }

    # 计算校验和防篡改
    audit_record['checksum'] = compute_checksum(audit_record)

    # 写入审计日志
    write_audit_log(audit_record)
```

---

## 6. 通信安全加固

### 6.1 TLS配置加固
```python
import ssl

def create_secure_ssl_context():
    """创建安全的SSL上下文"""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

    # 最低TLS版本
    context.minimum_version = ssl.TLSVersion.TLSv1_2

    # 禁用弱密码
    context.set_ciphers(
        'ECDHE-ECDSA-AES256-GCM-SHA384:'
        'ECDHE-RSA-AES256-GCM-SHA384:'
        'ECDHE-ECDSA-AES128-GCM-SHA256:'
        'ECDHE-RSA-AES128-GCM-SHA256'
    )

    # 启用证书验证
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True

    return context
```

### 6.2 Modbus安全加固
```python
# Modbus访问控制
MODBUS_ACCESS_CONTROL = {
    'allowed_functions': [3, 4, 6, 16],  # 读保持寄存器、读输入寄存器、写单个、写多个
    'denied_functions': [1, 2, 5, 15],   # 禁用线圈操作（如有需要可开启）
    'max_registers_per_request': 125,
    'rate_limit': '100/minute',
}
```

---

## 7. 监控安全加固

### 7.1 安全事件监控
```python
SECURITY_EVENTS = {
    'login_failed': {'severity': 'warning', 'threshold': 5, 'window': 300},
    'permission_denied': {'severity': 'warning', 'threshold': 3, 'window': 60},
    'suspicious_traffic': {'severity': 'critical', 'threshold': 1, 'window': 60},
    'config_change': {'severity': 'info', 'threshold': 1, 'window': 0},
}
```

### 7.2 入侵检测
```python
def detect_intrusion():
    """检测入侵行为"""
    # 1. 检测暴力破解
    failed_logins = get_failed_logins(window=300)
    if len(failed_logins) > 10:
        alert_brute_force(failed_logins)

    # 2. 检测异常访问
    suspicious_ips = detect_suspicious_ips()
    if suspicious_ips:
        alert_suspicious_access(suspicious_ips)

    # 3. 检测配置篡改
    if detect_config_tampering():
        alert_config_tampering()
```

---

## 8. 合规检查清单

### GB/T 35718 检查项
- [x] 网络分区隔离
- [x] 访问控制策略
- [x] 身份认证机制
- [x] 数据加密传输
- [x] 操作审计日志
- [x] 安全联锁机制
- [x] 入侵检测系统
- [x] 应急响应预案

### 定期检查
- [ ] 每月安全扫描
- [ ] 每季度渗透测试
- [ ] 每年合规审计
- [ ] 持续安全监控

---

## 9. 安全加固工具

### 推荐工具
- Nmap: 网络扫描
- Wireshark: 流量分析
- Nessus: 漏洞扫描
- OSSEC: 入侵检测
- Fail2Ban: 暴力破解防护

### 自动化脚本
```bash
# 安全加固检查脚本
python tools/security_check.py

# 漏洞扫描脚本
python tools/vulnerability_scan.py

# 合规检查脚本
python tools/compliance_check.py
```
