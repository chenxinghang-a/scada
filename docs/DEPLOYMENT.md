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

> ⚠️ **生产环境（`SCADA_ENV=production`）的 fail-closed 口径（三项同款）**：
>
> | 配置项 | 弱值/缺失时的行为 |
> |---|---|
> | `SECRET_KEY` / `JWT_SECRET` | 缺失、为空、或 `change-me-in-production` / `CHANGE_ME` 等弱默认值 → **拒绝启动**（`RuntimeError`） |
> | `SCADA_ADMIN_PASSWORD` | **同上**（round 214 起与密钥复用**同一张弱值表** `_WEAK_SECRET_VALUES`；`admin123` / `admin_123` / `admin@123` 已入表） |
>
> **不要**留空或照抄模板值。生成方式：`python -c "import secrets;print(secrets.token_hex(32))"`。
> 开发环境（默认 `development`）不设密钥只会告警并随机生成，不影响本地跑；
> **桌面版（Electron 打包分发）不设 `SCADA_ENV`，走 development 分支，不受此限制**。
>
> ⚠️ **部署侧警示**：若你的编排/启动脚本设置了 `SCADA_ENV=production`，就必须**同时**提供
> `SECRET_KEY`、`JWT_SECRET`、`SCADA_ADMIN_PASSWORD` 三者（强随机值），否则进程**拒绝启动**——
> 这是有意设计：弱默认值视同未配置。CI/镜像构建冒烟时如不带这三个变量，请显式使用 development。

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

#### HA 链路验收（半端到端，单机可跑）

完整双机演练需要**两台机器或两个 netns** —— 本协议的心跳端口模型是
「发送目标端口 = 自己 bind 的端口」（`peer_port` 同一字段两用），同机双实例会 bind 冲突
（`0.0.0.0` 使 127.0.0.2 隔离也无效）。单机可跑的半端到端步骤：

1. 只起 `_listen_loop`（**不调** `start()`，避免心跳发送线程「自收」污染断言）；
2. **先发一个合法签名包并等到被接受** —— 证明监听真的 bind 上（否则负向断言会是空绿）；
3. 发错误密钥包 / 无 `signature` 包 → 断言 `heartbeats_received` 不增；
4. 接管链：`role=STANDBY` + `_started_at` 回拨 + `_send_heartbeat` 打桩为 no-op →
   真跑 `_heartbeat_loop` → 断言超时后晋升 `PRIMARY`；反向对照：心跳新鲜时不接管。

以上四条已固化为 `tests/test_ha_protocol.py`（真 UDP + 真线程 + 真 HMAC），
可直接 `pytest tests/test_ha_protocol.py` 验证部署环境。

### 3.2 设备控制的安全前置条件（fail-closed）

**真实设备模式**（`run.py --real`）下，控制写入有两道 fail-closed 前置条件，
任一不满足即**拒绝执行**（不会「无保护地放行」）：

1. **必须能加载到安全联锁规则** —— `配置/interlocks.yaml`，或设备配置里的 `interlocks` 段。
   一条都加载不到时，`validate_write()` 直接拒绝所有写操作。
   （零联锁 = 一条永不触发的保护，等于没有保护。）
2. **必须显式声明控制点位** —— 设备配置里的 `control:` 段，为 `start` / `stop` / `reset`
   给出 `type`（`coil`/`register`）与 `address`。
   未声明的设备在批量启停时被**拒绝**，**不会**回退到历史默认点位 `coil0` / `register100`
   —— 真实设备上猜点位就是误操作。

```yaml
# 配置/devices_real.yaml 片段
plc_reactor_01:
  protocol: modbus_tcp
  host: 192.168.1.10
  control:                       # ← 不写这段，批量启停会被拒绝
    start: {type: coil, address: 7,  value: true}
    stop:  {type: coil, address: 9,  value: false}
    reset: {type: register, address: 321, value: 0}
```

`control` 段支持三种写法（**两种入口都认**：`智能层/device_control.py` 的批量启停，
以及 `采集层/real_device_manager.py` 的单设备启停 / `/api/devices/{id}/start|stop`）：

```yaml
control:                                        # A：按动作声明（推荐）
  start: {type: coil, address: 7, value: true}
  stop:  {type: coil, address: 9, value: false}

control: {type: coil, address: 3,               # C：扁平共用点位
          start_value: true, stop_value: false}

control: {start_coil: 7, stop_coil: 9}          # B：历史写法（兼容保留）
control: {start_register: 321, stop_register: 322}
```

> ⚠️ **两种入口的「不配点位」行为现在一致**：都**拒绝执行**，
> 都不会回退到 `coil 0`。
> （2026-10-13 修：`real_device_manager` 原先在找不到点位时会**盲写 coil 0**，
> 而本仓注释已指出「地址 0 是 `relay_1` 输出线圈与 `di_1` 共用的地址」——
> 盲写就是误动作；且批量启停会因此**多写一次 coil 0**。）

模拟模式（`run.py` 无参 / `--simulator`）不受这两条限制：无物理风险，
仍可回退到默认点位，但会打 warning 说明用的是回退点位。

> **前端提示**：控制被拒时前端会显示具体原因（联锁缺失 / 急停中 / 无可用控制点位），
> 不再统一显示「指令已发送」。排查时也可直接看后端响应的 `reason` 字段，
> 或日志中的「无可用控制点位，拒绝写入」。

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
