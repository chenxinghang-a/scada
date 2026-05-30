# Industrial SCADA System v3.0

工业级数据采集与监控系统 -- 符合中国国标等保2.0 (GB/T 22239)

## 项目概述

本系统是一套完整的工业SCADA (Supervisory Control And Data Acquisition) 平台，用于实时采集、存储、可视化和管理工业设备数据。系统遵循中国国家标准设计，涵盖GB/T 32919工控安全、GB/T 19582 Modbus协议规范、GB/T 35718工控信息安全等多项国标要求。

### 核心能力

- **多协议采集**: Modbus TCP/RTU、OPC UA、S7、FINS、MC、REST HTTP、IEC 104
- **实时数据流**: 设备 -> 采集层 -> 数据库 -> WebSocket -> 浏览器，端到端延迟 <1s
- **智能分析**: OEE综合效率、SPC统计过程控制、预测性维护、振动分析、能源管理、边缘决策
- **工业输出**: 声光报警器(Modbus DO控制)、PA广播系统(MQTT)、TDengine时序存储
- **安全合规**: JWT+RBAC认证、CSRF防护、速率限制、安全响应头、审计日志、TLS支持
- **高可用**: 主备HA节点、自动故障转移、离线数据缓冲

### 实时数据流架构

```
工业设备层 (PLC/传感器/执行器)
    |
    | Modbus TCP/RTU / OPC UA / S7 / FINS / MC / REST / IEC 104
    v
数据采集层 (DataCollector + DeviceManager)
    |
    | 写入 SQLite (realtime_data + history_data)
    | 桥接 TDengine (可选, 高吞吐时序存储)
    v
数据存储层 (SQLite + TDengine)
    |
    | Flask-SocketIO 推送 WebSocket 事件
    v
Web展示层 (Dashboard / 数据大屏 / 报警面板)
    |
    | 报警触发
    v
报警输出层 (声光报警器 Modbus DO / PA广播 MQTT / 邮件通知)
    |
    v
智能分析层 (OEE / SPC / 预测性维护 / 振动分析 / 能源管理 / 边缘决策)
```

## 目录结构

```
industrial_scada/
├── run.py                       # 主启动脚本 (模拟/模拟器/真实三种模式)
├── launcher.py                  # 启动器 (自动开浏览器)
├── config.py                    # 全局配置 (Flask/DB/Modbus/Alarm/MQTT/JWT/Security/TDengine)
├── paths.py                     # 统一路径管理
├── build.py                     # PyInstaller打包脚本
│
├── 采集层/                       # 数据采集模块
│   ├── base_client.py           # 采集客户端基类
│   ├── simulated_device_manager.py  # 模拟设备管理器
│   ├── device_manager_factory.py    # 设备管理器工厂
│   ├── modbus_client.py         # Modbus TCP/RTU 客户端
│   ├── opcua_client.py          # OPC UA 客户端
│   ├── s7_client.py             # S7协议客户端
│   ├── fins_client.py           # FINS协议客户端 (Omron)
│   ├── mc_client.py             # MC协议客户端 (Mitsubishi)
│   ├── rest_client.py           # REST HTTP客户端
│   ├── data_collector.py        # 数据采集调度器
│   └── recipe_simulator.py      # 配方模拟器
│
├── 存储层/                       # 数据存储模块
│   ├── database.py              # SQLite数据库 (连接池/WAL/UPSERT/归档/备份)
│   ├── data_archive.py          # 数据归档策略
│   └── data_export.py           # CSV/Excel/JSON导出
│
├── 展示层/                       # Web可视化模块
│   ├── routes.py                # Flask路由 + SocketIO初始化
│   ├── api/                     # REST API (模块化Blueprint)
│   │   ├── api_metrics.py       # Prometheus指标端点
│   │   └── swagger.py           # API文档 (flask-restx)
│   └── websocket.py             # WebSocket实时推送
│
├── 报警层/                       # 报警管理模块
│   ├── alarm_manager.py         # 报警管理器 (阈值检测/去重/升级)
│   ├── alarm_rules.py           # 报警规则引擎
│   ├── alarm_statistics.py      # 报警统计分析
│   ├── alarm_output.py          # 声光报警器输出 (Modbus DO)
│   ├── broadcast_system.py      # PA广播系统 (MQTT)
│   ├── notification.py          # 通知服务 (邮件/短信)
│   └── interfaces.py            # 输出接口抽象
│
├── 智能层/                       # 工业4.0智能分析
│   ├── oee_calculator.py        # OEE综合效率计算
│   ├── spc_analyzer.py          # SPC统计过程控制
│   ├── predictive_maintenance.py # 预测性维护
│   ├── vibration_analyzer.py    # 振动分析
│   ├── energy_manager.py        # 能源管理
│   ├── edge_decision.py         # 边缘决策引擎 (PID控制)
│   ├── device_control.py        # 设备控制安全管理 (安全联锁)
│   └── tsdb_adapter.py          # TDengine适配器
│
├── 用户层/                       # 用户认证与权限
│   ├── auth.py                  # JWT认证 + RBAC权限
│   └── audit_logger.py          # 审计日志
│
├── core/                        # 核心基础设施
│   ├── structured_logging.py    # 结构化日志 (JSON/SIEM集成)
│   ├── health_checker.py        # 健康检查 (自动周期扫描)
│   ├── ha_manager.py            # 高可用管理 (主备切换)
│   ├── rate_limiter.py          # 速率限制 (防暴力/DDoS)
│   ├── csrf_protection.py       # CSRF防护
│   ├── config_validator.py      # 配置Schema验证
│   └── service_response.py      # 统一响应格式
│
├── gateway/                     # 协议网关 (独立部署)
│   ├── modbus_gateway.py        # Modbus网关 (TCP/RTU -> MQTT)
│   ├── opcua_gateway.py         # OPC UA网关
│   ├── s7_gateway.py            # S7网关
│   ├── iec104_gateway.py        # IEC 104网关
│   └── run_gateway.py           # 网关启动脚本
│
├── timeseries/                  # 时序数据库模块
│   ├── tdengine_client.py       # TDengine客户端
│   ├── mqtt_to_tsdb.py          # MQTT -> TDengine桥接
│   ├── offline_buffer.py        # 离线数据缓冲
│   └── query_builder.py         # 时序查询构建器
│
├── 模板/                         # HTML模板 (Jinja2)
│   ├── base.html                # 基础布局模板
│   ├── login.html               # 登录页面
│   ├── dashboard.html           # 仪表盘 (KPI卡片/设备网格/报警面板/趋势图)
│   ├── screen.html              # 数据大屏 (全屏投屏展示)
│   ├── history.html             # 历史数据查询
│   ├── alarms.html              # 报警管理
│   ├── devices.html             # 设备管理
│   ├── config.html              # 系统配置
│   ├── users.html               # 用户管理
│   ├── control.html             # 设备控制
│   ├── industry40.html          # 工业4.0智能分析
│   └── alarm_output.html        # 报警输出配置
│
├── 静态资源/                     # 前端静态文件
│   ├── css/
│   │   ├── style.css            # 全局样式
│   │   ├── dashboard.css        # 仪表盘样式
│   │   └── screen.css           # 数据大屏样式
│   └── js/
│       ├── main.js              # 全局JS工具
│       ├── dashboard.js         # 仪表盘逻辑 (WebSocket/ECharts)
│       └── screen.js            # 数据大屏逻辑
│
├── 配置/                         # YAML配置文件
│   ├── devices.yaml             # 设备定义 (主配置)
│   ├── devices_simulated.yaml   # 模拟模式设备配置
│   ├── devices_modbus_sim.yaml  # Modbus模拟器设备配置
│   ├── devices_real.yaml        # 真实设备配置
│   ├── alarms.yaml              # 报警规则配置
│   ├── system.yaml              # 系统参数配置
│   ├── energy.yaml              # 能源管理配置
│   └── simulation_presets.yaml  # 模拟预设方案
│
├── tests/                       # 测试套件 (1653 tests, 71% coverage)
│   ├── test_api.py              # API测试
│   ├── test_core.py             # 核心模块测试
│   ├── test_alarm.py            # 报警测试
│   ├── test_auth_enhanced.py    # 认证增强测试
│   ├── test_spc_analyzer.py     # SPC分析测试
│   ├── test_oee_calculator.py   # OEE计算测试
│   ├── test_vibration_analyzer.py # 振动分析测试
│   ├── test_energy_manager.py   # 能源管理测试
│   ├── test_edge_decision.py    # 边缘决策测试
│   ├── test_metrics.py          # Prometheus指标测试
│   └── ...                      # 更多测试文件
│
├── 测试/                         # 集成测试
│   ├── test_system.py           # 系统集成测试
│   ├── load_test.py             # 负载测试
│   └── 全面Bug测试.py            # 全面Bug回归测试
│
├── docker-compose.yml           # Docker编排 (SCADA + EMQX + TDengine + nginx + Grafana)
├── requirements.txt             # Python依赖
└── .env                         # 环境变量 (本地开发)
```

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| 后端框架 | Python 3.12+ / Flask 3.0 | Web服务 + REST API |
| 实时通信 | Flask-SocketIO / WebSocket | 设备数据实时推送到浏览器 |
| 数据库 | SQLite (WAL模式) | 主存储, 连接池复用, UPSERT |
| 时序数据库 | TDengine 3.3 (可选) | 高吞吐时序数据, 超级表 |
| MQTT Broker | EMQX 5.6 | 设备消息中间件, 网关数据汇聚 |
| 协议通信 | pymodbus / asyncua / paho-mqtt | Modbus/OPC UA/MQTT |
| 认证 | PyJWT + bcrypt | JWT令牌 + RBAC权限 |
| 前端 | ECharts 5.4 / Socket.IO 4.5 | 图表可视化 / 实时推送 |
| 监控 | Prometheus + Grafana | 指标采集 + 可视化面板 |
| 反向代理 | nginx 1.25 | 静态文件 + 负载均衡 |
| 容器化 | Docker Compose | 一键部署全栈 |
| 打包 | PyInstaller | Windows桌面部署 |

## 快速开始

### 方式一: 本地开发 (推荐)

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 复制环境变量
cp .env.example .env
# 编辑 .env 设置 SECRET_KEY 和 JWT_SECRET

# 3. 启动 (三种模式任选)
python run.py              # 模拟模式 (默认, 无需真实设备)
python run.py --simulator  # 模拟器模式 (连接外部Modbus TCP模拟器)
python run.py --real       # 真实模式 (连接真实PLC设备)

# 4. 访问系统
# 模拟模式: http://localhost:5000
# 真实模式: http://localhost:5001
# 默认账号: admin / admin123
```

### 方式二: 启动器 (自动开浏览器)

```bash
python launcher.py              # 模拟模式
python launcher.py --real       # 真实模式
```

### 方式三: Docker Compose (全栈部署)

```bash
# 启动全部服务: SCADA + EMQX + TDengine + nginx + Grafana
docker-compose up -d

# 访问地址
# SCADA系统:    http://localhost:80    (nginx反向代理)
# EMQX控制台:   http://localhost:18083 (MQTT Broker管理)
# Grafana面板:  http://localhost:3000  (admin/admin123)
# TDengine:     localhost:6030         (原生) / localhost:6041 (REST)
```

## 环境变量

### 安全密钥 (必须设置)

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SECRET_KEY` | 随机生成 (重启失效) | Flask会话密钥, 生产环境必须设置固定值 |
| `JWT_SECRET` | 随机生成 (重启失效) | JWT签名密钥, 生产环境必须设置固定值 |
| `SCADA_ADMIN_PASSWORD` | `admin123` | 首次创建admin账户的默认密码 |
| `CSRF_SECRET` | 随机生成 | CSRF令牌HMAC密钥 |

### 数据库与时序存储

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TDENGINE_HOST` | `localhost` | TDengine服务器地址 |
| `TDENGINE_PORT` | `6041` | TDengine REST API端口 |
| `TDENGINE_USER` | `root` | TDengine用户名 |
| `TDENGINE_PASSWORD` | (空) | TDengine密码 |
| `TDENGINE_DATABASE` | `scada` | TDengine数据库名 |

### MQTT通信

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MQTT_BROKER` | `localhost` | MQTT Broker地址 (EMQX) |
| `MQTT_PORT` | `1883` | MQTT端口 |
| `MQTT_TLS_ENABLED` | `false` | 启用MQTT TLS加密 |
| `MQTT_CA_CERT` | (空) | CA证书路径 |
| `MQTT_CLIENT_CERT` | (空) | 客户端证书路径 |
| `MQTT_CLIENT_KEY` | (空) | 客户端私钥路径 |
| `MQTT_TLS_INSECURE` | `false` | 跳过证书验证 (仅测试) |

### PA广播系统

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PA_BROKER` | `localhost` | 广播MQTT Broker地址 |
| `PA_MQTT_PORT` | `1883` | 广播MQTT端口 |

### 安全与合规 (等保2.0)

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SECURITY_HEADERS` | `true` | 启用安全响应头 (CSP/HSTS/X-Frame等) |
| `HSTS_MAX_AGE` | `31536000` | HSTS最大有效期 (秒) |
| `CSP_EXTRA_SCRIPTS` | (空) | CSP额外允许的脚本源 (逗号分隔) |
| `RATE_LIMIT_ENABLED` | `true` | 启用速率限制 |
| `RATE_LIMIT_DEFAULT` | `200 per minute` | 默认API速率限制 |
| `RATE_LIMIT_LOGIN` | `5 per minute` | 登录接口速率限制 |
| `CSRF_ENABLED` | `true` | 启用CSRF防护 |
| `SCADA_TLS_ENABLED` | `false` | 启用HTTPS/TLS |
| `SCADA_TLS_CERT` | `certs/server.crt` | TLS证书文件路径 |
| `SCADA_TLS_KEY` | `certs/server.key` | TLS私钥文件路径 |

### 高可用 (HA)

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HA_NODE_ID` | `node-{pid}` | 节点唯一标识 |
| `HA_PRIORITY` | `100` | 节点优先级 (数值越高越优先成为主节点) |
| `HA_HEARTBEAT_INTERVAL` | `2` | 心跳间隔 (秒) |
| `HA_HEARTBEAT_TIMEOUT` | `10` | 心跳超时 (秒) |
| `HA_PEER_ADDRESS` | (空) | 对端节点地址 (留空禁用HA) |
| `HA_PEER_PORT` | `9999` | 对端节点端口 |

### 日志与调试

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SCADA_LOG_LEVEL` | `INFO` | 日志级别 (DEBUG/INFO/WARNING/ERROR) |
| `SCADA_LOG_DIR` | `logs/` | 日志文件目录 |
| `SCADA_LOG_JSON` | `true` | JSON格式日志 (SIEM集成) |
| `SCADA_LOG_ROTATION` | `100 MB` | 日志轮转大小 |
| `SCADA_LOG_RETENTION` | `30 days` | 日志保留时间 |
| `FLASK_DEBUG` | `0` | Flask调试模式 (1=启用) |

### Docker Compose 专用

| 变量 | docker-compose默认值 | 说明 |
|------|---------------------|------|
| `SCADA_MODE` | `simulated` | 运行模式 |
| `GF_SECURITY_ADMIN_USER` | `admin` | Grafana管理员用户名 |
| `GF_SECURITY_ADMIN_PASSWORD` | `admin123` | Grafana管理员密码 |
| `GF_INSTALL_PLUGINS` | `tdengine-datasource` | Grafana插件 |

## 浏览器端功能 (Dashboard)

### 仪表盘页面 (`/dashboard`)

**顶部栏**
- 系统标题显示
- 模拟模式徽章 (模拟/真实模式标识)
- 实时时钟 (HH:MM:SS)

**KPI指标卡片行** (6个卡片)
- **OEE综合效率**: 实时OEE值, 目标 >=85%
- **设备状态**: 在线/总数 比值
- **活动报警**: 当前报警数量, 未确认数
- **采集吞吐**: 每分钟采集条数
- **数据质量**: 采集成功率百分比
- **运行时间**: 系统持续运行时长, 当前运行模式

**设备卡片网格**
- 每个设备一张卡片, 显示设备ID/名称/状态
- 实时更新各寄存器值 (WebSocket推送)
- 设备在线/离线/报警状态指示灯
- 点击设备卡片可展开详情

**报警面板**
- 实时报警滚动列表
- 报警级别徽章: CRIT (严重) / HIGH (高) / MED (中)
- 报警闪烁横幅 (严重报警时顶部红色横幅)
- 报警确认操作

**实时趋势图**
- ECharts折线图, 实时更新
- 设备选择下拉框
- 多寄存器叠加显示
- 自动滚动时间窗口

**底部状态栏**
- 系统运行状态指示灯 (绿/黄/红)
- 数据库统计信息
- 当前登录用户信息

### 数据大屏页面 (`/screen`)

全屏投屏展示页面, 三栏布局, 适合大屏幕/投影仪展示:

**左栏**
- **设备状态概览**: 设备总数/在线/离线/报警 KPI + 饼图
- **OEE综合效率**: ECharts仪表盘图
- **能源消耗趋势**: 能源折线图

**中栏 (主区域)**
- **顶部KPI卡片**: 今日采集量、采集频率、运行时间、数据质量
- **实时数据趋势**: 主趋势图 (设备选择 + 多寄存器)

**右栏**
- **实时报警滚动**: 报警列表 + 计数徽章
- **SPC质量监控**: SPC控制图 (Xbar-R)
- **设备健康度**: 预测性维护健康度图

**特性**
- WebSocket实时更新, 3秒轮询兜底
- JWT认证, 401自动跳转登录
- XSS安全转义 (escapeHtml)
- 深色主题, 自适应分辨率

### 其他页面

| 页面 | 路径 | 功能 |
|------|------|------|
| 登录 | `/login` | JWT认证登录, 支持记住密码 |
| 历史数据 | `/history` | 时间范围查询, 1min/5min/1h/1d聚合, CSV导出 |
| 报警管理 | `/alarms` | 报警列表/筛选/确认/统计 |
| 设备管理 | `/devices` | 设备列表/状态/寄存器查看 |
| 系统配置 | `/config` | 报警阈值/采集频率/保留策略配置 |
| 用户管理 | `/users` | 用户CRUD/RBAC权限管理 |
| 设备控制 | `/control` | 远程设备控制 (带安全联锁验证) |
| 工业4.0 | `/industry40` | OEE/SPC/预测性维护/能源/振动分析面板 |
| 报警输出 | `/alarm_output` | 声光报警器/广播系统配置 |

## 运行模式

### 模拟模式 (默认)

```bash
python run.py
```

- 使用 `SimulatedDeviceManager`, 无需真实设备
- 设备数据自动生成 (正弦波 + 随机噪声 + 故障模拟)
- 配置文件: `配置/devices_simulated.yaml`
- 数据库: `data/scada_simulated.db`
- 端口: 5000

### 模拟器模式

```bash
python run.py --simulator
```

- 连接外部Modbus TCP模拟器 (如ModRSsim2)
- 使用真实Modbus协议通信, 但设备是模拟的
- 配置文件: `配置/devices_modbus_sim.yaml`
- 数据库: `data/scada_simulator.db`

### 真实设备模式

```bash
python run.py --real
```

- 连接真实PLC/传感器设备
- 配置文件: `配置/devices_real.yaml`
- 数据库: `data/scada_real.db`
- 端口: 5001

## 协议网关 (独立部署)

网关模块可独立部署在边缘节点, 负责协议转换并上报到MQTT Broker:

```bash
# Modbus网关
python -m gateway.run_gateway --protocol modbus --config 配置/gateway_modbus.yaml

# OPC UA网关
python -m gateway.run_gateway --protocol opcua --config 配置/gateway_opcua.yaml

# S7网关
python -m gateway.run_gateway --protocol s7 --config 配置/gateway_s7.yaml

# IEC 104网关
python -m gateway.run_gateway --protocol iec104 --config 配置/gateway_iec104.yaml
```

网关将设备数据转换为统一物模型 (ThingModel), 通过MQTT发布到 `scada/{device_id}/telemetry` 主题。

## Git推仓流程

```bash
# 1. 代码写完后暂存
git add -A

# 2. 提交
git commit -m "feat: 描述你的改动"

# 3. 推送到GitHub
git push origin main
```

- GitHub仓库: `https://github.com/chenxinghang-a/scada.git`
- 推仓工具: `C:\Users\cxx\WorkBuddy\Claw\tools\mingit\cmd\git.exe`
- 代理: `git config http.proxy http://127.0.0.1:7890`

## Agent启动指南

### 从源码启动Agent

```bash
# 1. 进入项目目录
cd C:\Users\cxx\WorkBuddy\Claw\industrial_scada

# 2. 安装依赖 (首次)
pip install -r requirements.txt

# 3. 启动 (模拟模式, 最快上手)
python run.py

# 4. 浏览器访问
# http://localhost:5000
# 账号: admin / admin123
```

### 从打包版启动

```bash
# 双击 dist/SCADA.exe 或命令行运行
dist/SCADA.exe           # 模拟模式
dist/SCADA.exe --real    # 真实模式
```

### Docker启动

```bash
docker-compose up -d
# 访问 http://localhost
```

## 国标合规

本系统遵循以下中国国家标准:

| 标准编号 | 名称 | 合规项 |
|---------|------|--------|
| GB/T 22239-2019 | 等保2.0 | 速率限制、安全响应头、审计日志、访问控制 |
| GB/T 32919 | 工控系统安全 | XSS防护、CSP策略、信息泄露防护、会话管理 |
| GB/T 33008 | 工控终端安全 | 健康检查API认证、登出记录、SRI完整性 |
| GB/T 19582 | Modbus协议规范 | 寄存器地址验证、功能码覆盖、异常码分类、字节序 |
| GB/T 15969 | PLC标准 | 安全联锁、审计追踪、故障降级 |
| GB/T 35718 | 工控信息安全 | JWT黑名单撤销、密码修改撤销令牌、JTI唯一标识 |
| GB/T 36323 | 信息安全防护 | 访问控制、安全审计 |
| GB/T 37980 | 工业互联网安全 | CSRF防护、HMAC-SHA256 token、TLS支持 |

## 许可证

MIT License
