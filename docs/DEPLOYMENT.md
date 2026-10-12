# SmartSCADA 部署指南

## 系统要求

### 硬件要求
- CPU: 4核以上
- 内存: 8GB以上
- 硬盘: 100GB以上（SSD推荐）
- 网络: 千兆网卡

### 软件要求
- 操作系统: Windows 10/11 或 Windows Server 2019+
- Python: 3.12+（`pyproject.toml` 的 `requires-python = ">=3.12"`；此处原写 3.11+ 与 README/Dockerfile 不一致）
- Node.js: 18+（前端构建）

---

## 快速部署

### 1. 克隆项目
```bash
git clone https://github.com/chenxinghang-a/scada.git
cd scada
```

### 2. 安装Python依赖
```bash
pip install -r requirements.txt
```

### 3. 配置环境变量
复制 `.env.example` 为 `.env` 并修改：
```bash
cp .env.example .env
```

关键配置项：
```env
# 必须修改
SECRET_KEY=your-secret-key-here
JWT_SECRET=your-jwt-secret-here
SCADA_ADMIN_PASSWORD=your-admin-password

# 可选配置
SCADA_TLS_ENABLED=true
SCADA_TLS_CERT=certs/server.crt
SCADA_TLS_KEY=certs/server.key
```

> ⚠️ **生产环境（`SCADA_ENV=production`）下 `SECRET_KEY` / `JWT_SECRET` 缺失、为空、
> 或仍是 `change-me-in-production` / `CHANGE_ME` 之类的弱默认值时，进程会直接
> **拒绝启动**（抛 `RuntimeError`）。**不要**留空或照抄模板值。
> 生成方式：`python -c "import secrets;print(secrets.token_hex(32))"`。
> 开发环境（默认 `development`）不设密钥只会告警并随机生成，不影响本地跑。

### 3.1 高可用（主备）部署

启用 HA 需要两个节点，并**必须**满足：

```env
# 两节点都设，且**值必须完全相同**
HA_SHARED_SECRET=<强随机串，两节点一致>
# 对端地址（A 填 B 的 IP，B 填 A 的 IP）
HA_PEER_ADDRESS=192.168.1.11
HA_PEER_PORT=9999
HA_NODE_ID=node-a          # 两节点不能重名
HA_PRIORITY=100            # 数值大者优先当主
```

- **`HA_SHARED_SECRET` 是硬性要求**：配置了 `HA_PEER_ADDRESS` 却没给密钥，
  进程会在构造 `HAManager` 时**直接报错**，不会启动。
- **两节点密钥必须一致**。密钥不一致时心跳 HMAC 互验失败、双方各自判定对端失联，
  结果是**双主**（两台同时采集并写 PLC）—— 这是最危险的失效模式。
  启动日志会打印**密钥指纹**（SHA-256 前 8 位），两节点比对即可确认：

  ```
  HA 共享密钥指纹: 3f9a1c02 —— 主备两节点必须一致
  ```

- **角色决定采集**：只有 `PRIMARY` 启动数据采集；`STANDBY` 只监听、不采集不写。
  切换（主故障/恢复）时采集器自动启停，无需人工干预。
- 单机部署（不设 `HA_PEER_ADDRESS`）不需要 `HA_SHARED_SECRET`，节点直接以 PRIMARY 运行。

### 4. 初始化数据库
```bash
python -c "from 存储层.database import Database; db = Database(); db._init_database()"
```

### 5. 启动服务
```bash
python run.py
```

访问 http://localhost:5000

---

## 生产环境部署

### 方案1: Windows服务（推荐）

1. 安装NSSM（Non-Sucking Service Manager）
2. 以管理员权限运行：
```bash
tools\install_service.bat
```

服务会自动启动，崩溃后5秒自动重启。

### 方案2: PyInstaller打包

1. 安装PyInstaller
```bash
pip install pyinstaller
```

2. 执行打包
```bash
pyinstaller scada-backend.spec --noconfirm
```

3. 产物位置
```
dist/scada-backend/
  ├── scada-backend.exe
  └── _internal/
```

4. 复制到前端项目
```bash
cp dist/scada-backend/scada-backend.exe ../scada-app/backend/
cp -r dist/scada-backend/_internal ../scada-app/backend/
```

### 方案3: Docker部署

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY . .
RUN pip install -r requirements.txt

EXPOSE 5000
CMD ["python", "run.py"]
```

```bash
docker build -t smartscada .
docker run -p 5000:5000 -v ./data:/app/data smartscada
```

---

## 前端部署

### 开发模式
```bash
cd scada-app
npm install
npm run dev
```

### 生产构建
```bash
npm run build
```

### Electron打包
```bash
npm run electron:build
```

产物位置：`release/SmartSCADA Setup.exe`

---

## 配置说明

### 设备配置 (配置/devices.yaml)
```yaml
devices:
  - id: motor_01
    name: 电机01
    protocol: modbus_tcp
    host: 192.168.1.100
    port: 502
    slave_id: 1
    collection_interval: 5
    registers:
      - name: temperature
        description: 温度
        address: 100
        data_type: float32
        unit: °C
```

### 报警配置 (配置/alarms.yaml)
```yaml
rules:
  - id: high_temp
    name: 高温报警
    device_id: motor_01
    register_name: temperature
    condition: greater_than
    threshold: 80
    level: warning
    enabled: true
```

---

## TLS/HTTPS配置

### 生成证书
```bash
python -m core.generate_certs
```

### 启用TLS
在 `.env` 中设置：
```env
SCADA_TLS_ENABLED=true
SCADA_TLS_CERT=certs/server.crt
SCADA_TLS_KEY=certs/server.key
```

---

## 数据库维护

### 备份
```bash
python -c "
from 存储层.database import Database
db = Database()
db.backup('backup_$(date +%Y%m%d).db')
"
```

### 归档
系统每24小时自动归档7天前的数据。

手动归档：
```bash
python -c "
from 存储层.database import Database
db = Database()
result = db.archive_old_data(archive_days=7, delete_days=30)
print(f'归档: {result}')
"
```

### WAL Checkpoint
系统每24小时自动执行WAL checkpoint。

手动执行：
```bash
python -c "
from 存储层.database import Database
db = Database()
db.wal_checkpoint()
"
```

---

## 监控

### 健康检查
```bash
curl http://localhost:5000/api/health/status
```

### Prometheus指标
```bash
curl http://localhost:5000/metrics
```

### 日志
- 应用日志: `logs/scada_YYYY-MM-DD.log`
- 审计日志: `logs/audit_YYYY-MM-DD.log`
- 服务日志: `logs/service_stdout.log`, `logs/service_stderr.log`

---

## 故障排查

见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
