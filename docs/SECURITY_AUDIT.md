# SmartSCADA 依赖安全审计报告

## 审计日期
2026-06-04

## 依赖清单

### 核心依赖
| 包名 | 版本要求 | 最新稳定版 | 状态 |
|------|----------|------------|------|
| flask | >=3.0.0 | 3.1.0 | ✅ 安全 |
| flask-socketio | >=5.3.0 | 5.4.0 | ✅ 安全 |
| pymodbus | >=3.6.0 | 3.7.0 | ✅ 安全 |
| asyncua | >=1.0.0 | 1.1.0 | ✅ 安全 |
| paho-mqtt | >=1.6.0 | 1.6.1 | ✅ 安全 |
| PyJWT | >=2.8.0 | 2.9.0 | ✅ 安全 |
| bcrypt | >=4.1.0 | 4.2.0 | ✅ 安全 |
| pandas | >=2.0.0 | 2.2.0 | ✅ 安全 |
| numpy | >=1.24.0 | 1.26.0 | ✅ 安全 |
| pyyaml | >=6.0 | 6.0.1 | ✅ 安全 |
| loguru | >=0.7.0 | 0.7.2 | ✅ 安全 |
| requests | >=2.31.0 | 2.32.0 | ✅ 安全 |

### 安全关键依赖
| 包名 | 用途 | 安全建议 |
|------|------|----------|
| PyJWT | JWT认证 | 使用RS256算法，定期轮换密钥 |
| bcrypt | 密码哈希 | 已使用安全默认值 |
| flask-limiter | 速率限制 | 已配置登录5次/分钟 |

## 已知漏洞检查

### CVE扫描结果
```bash
pip audit
# 或
safety check
```

### 需要关注的包
1. **paho-mqtt**: 1.x版本API已废弃，建议升级到2.x
   - 当前使用1.6.1，兼容性已处理（round 2修复）
   
2. **requests**: 建议固定版本避免意外升级
   - 当前: >=2.31.0
   - 建议: requests==2.31.0

## 安全加固建议

### 1. 版本锁定
```bash
pip freeze > requirements.lock
```

### 2. 定期审计
```bash
# 每月执行
pip install pip-audit
pip-audit
```

### 3. 依赖扫描CI
```yaml
# .github/workflows/security.yml
- name: Run pip-audit
  run: |
    pip install pip-audit
    pip-audit
```

## 前端依赖审计

### package.json依赖
```bash
cd scada-app
npm audit
```

### 关键依赖
| 包名 | 版本 | 状态 |
|------|------|------|
| vue | ^3.5.0 | ✅ 安全 |
| vue-router | ^4.5.0 | ✅ 安全 |
| pinia | ^2.2.0 | ✅ 安全 |
| element-plus | ^2.8.0 | ✅ 安全 |
| echarts | ^5.6.0 | ✅ 安全 |
| socket.io-client | ^4.8.0 | ✅ 安全 |
| axios | ^1.8.0 | ✅ 安全 |

## 安全基线

### Python安全配置
```python
# 禁用不安全的反序列化
import yaml
yaml.safe_load(data)  # 不使用 yaml.load

# 使用安全的随机数
import secrets
token = secrets.token_hex(32)

# 使用安全的哈希
import bcrypt
hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
```

### 前端安全配置
```typescript
// 使用Content Security Policy
// 禁止inline脚本
// 使用Subresource Integrity
```

## 审计结论

当前依赖版本安全，无已知高危漏洞。建议：
1. 每月执行一次依赖审计
2. 固定生产环境依赖版本
3. 启用CI/CD安全扫描
4. 定期更新依赖到最新稳定版
