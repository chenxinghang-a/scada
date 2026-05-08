# 工业4.0模块分析报告

## 一、模块概述

工业4.0智能层包含以下核心模块：

| 模块 | 文件 | 功能 | 状态 |
|------|------|------|------|
| 预测性维护 | `predictive_maintenance.py` | 设备健康预测、趋势分析、异常检测 | ✅ 正常 |
| OEE计算器 | `oee_calculator.py` | 设备综合效率计算、六大损失分析 | ✅ 正常 |
| SPC分析器 | `spc_analyzer.py` | 统计过程控制、控制图、过程能力 | ✅ 正常 |
| 能源管理 | `energy_manager.py` | 能耗监控、峰谷平分析、碳排放 | ✅ 正常 |
| 边缘决策 | `edge_decision.py` | 规则引擎、联锁控制、PID控制 | ✅ 正常 |

## 二、数据流分析

### 1. 数据采集流程

```
设备 → DataCollector → 数据队列 → 数据处理线程
                                    ↓
                              ┌─────┴─────┐
                              │           │
                         数据库存储    工业4.0模块分发
                                          ↓
                              ┌───────────┼───────────┐
                              │           │           │
                         预测性维护    OEE计算    SPC分析
                              │           │           │
                         能源管理    边缘决策    安全联锁
```

### 2. 数据分发逻辑

在 `data_collector.py` 的 `_process_data` 方法中：

```python
# 预测性维护 — 喂入所有数值数据
if self.predictive_maintenance:
    self.predictive_maintenance.feed_data(
        device_id, register_name, value, timestamp)

# 边缘决策引擎 — 更新数据快照
if self.edge_decision:
    self.edge_decision.update_data(
        f"{device_id}:{register_name}", value)

# 能源管理 — 电力数据喂入
if self.energy_manager:
    power_keywords = ['power', 'watt', 'kw', ...]
    energy_keywords = ['energy', 'kwh', 'mwh', ...]
    
    if any(kw in register_name.lower() for kw in power_keywords):
        self.energy_manager.feed_power_data(
            device_id, power_value, timestamp=timestamp)
    elif any(kw in register_name.lower() for kw in energy_keywords):
        self.energy_manager.feed_power_data(
            device_id, 0, energy_kwh=value, timestamp=timestamp)

# SPC — 质量相关数据喂入
if self.spc_analyzer:
    spc_keywords = ['temperature', 'pressure', 'ph', ...]
    if any(kw in register_name.lower() for kw in spc_keywords):
        self.spc_analyzer.feed_data(device_id, register_name, value)

# OEE — 设备状态和产量数据喂入
if self.oee_calculator:
    status_keywords = ['status', 'state', 'running', ...]
    count_keywords = ['count', 'product', 'shot', ...]
    
    if any(kw in register_name.lower() for kw in status_keywords):
        status_map = {0: 'stopped', 1: 'running', 2: 'fault', 3: 'idle'}
        status = status_map.get(int(value), 'running')
        self.oee_calculator.update_device_state(device_id, status)
    
    if any(kw in register_name.lower() for kw in count_keywords):
        self.oee_calculator.record_production(device_id, count=int(value))

# 安全联锁检查
if self.device_control:
    self.device_control.check_interlocks(
        device_id, register_name, value)
```

## 三、各模块详细分析

### 1. 预测性维护 (PredictiveMaintenance)

**功能**：
- 趋势分析：移动平均 + 线性回归预测未来值
- 异常检测：Z-Score + IQR双算法
- 设备健康评分：多维度加权评估 (0-100)
- 故障预测：基于趋势外推预测何时超限
- 维护建议：自动生成维护工单

**数据输入**：
```python
def feed_data(self, device_id: str, register_name: str,
              value: float, timestamp: datetime | None = None):
    """喂入实时数据（由DataCollector调用）"""
```

**健康评分算法**：
- 稳定性 (40分)：数据波动越小越好
- 趋势 (30分)：趋势越平稳越好
- 异常率 (30分)：异常点越少越好

**故障预测**：
- 基于线性回归趋势外推
- 预测何时触及阈值上限/下限
- 提供置信度评估

### 2. OEE计算器 (OEECalculator)

**功能**：
- 实时OEE计算（基于设备运行状态）
- OEE六大损失分析
- 班次/日/周/月OEE统计
- OEE趋势对比

**OEE公式**：
```
OEE = 可用率(A) × 性能率(P) × 质量率(Q)

A = 实际运行时间 / 计划生产时间
P = (总产量 × 理想节拍) / 实际运行时间
Q = 合格品数 / 总产量
```

**数据输入**：
```python
def update_device_state(self, device_id: str, status: str):
    """更新设备运行状态: 'running'|'stopped'|'fault'|'idle'"""

def record_production(self, device_id: str, count: int | None = None, 
                      good_count: int | None = None):
    """记录产量（支持绝对值和增量两种模式）"""
```

**六大损失分析**：
1. 故障损失 (Availability) — 设备故障停机
2. 换装调整损失 (Availability) — 换模/换线/调整
3. 空转短暂停机损失 (Performance) — 小停机/空转
4. 速度降低损失 (Performance) — 实际速度低于理论速度
5. 不良品损失 (Quality) — 废品/返工
6. 开机损失 (Quality) — 开机阶段的不良品

### 3. SPC分析器 (SPCAnalyzer)

**功能**：
- 控制图 — X̄-R图、X̄-S图
- 过程能力分析 — Cp、Cpk、Pp、Ppk
- Western Electric判异规则（4大判异准则）
- 过程稳定性评估

**数据输入**：
```python
def feed_data(self, device_id: str, register_name: str, value: float):
    """喂入质量数据"""
```

**判异规则**：
1. 规则1: 1点超出3σ控制限
2. 规则2: 连续9点在中心线同一侧
3. 规则3: 连续6点递增或递减
4. 规则4: 连续14点交替上下

**过程能力指数**：
- Cp = (USL - LSL) / (6σ) — 潜在能力
- Cpk = min((USL-μ)/3σ, (μ-LSL)/3σ) — 实际能力
- Pp = (USL - LSL) / (6σ_total) — 过程性能
- Ppk = min((USL-μ)/3σ_total, (μ-LSL)/3σ_total)

**能力等级**：
- Cpk ≥ 1.67: 特级(优秀)
- Cpk ≥ 1.33: 一级(良好)
- Cpk ≥ 1.00: 二级(合格)
- Cpk ≥ 0.67: 三级(不足)
- Cpk < 0.67: 四级(严重不足)

### 4. 能源管理 (EnergyManager)

**功能**：
- 实时能耗监控 — 电力/水/气/蒸汽分项计量
- 能耗统计 — 班次/日/周/月能耗汇总
- 峰谷平电价分析 — 分时电价成本核算
- 碳排放计算 — 基于国家排放因子
- 能效指标 — 单位产品能耗、万元产值能耗
- 能耗异常检测 — 突增/泄漏预警

**数据输入**：
```python
def feed_power_data(self, device_id: str, power_kw: float,
                     energy_kwh: float | None = None, 
                     timestamp: datetime | None = None):
    """喂入电力数据"""

def feed_water_data(self, device_id: str, flow_m3h: float, 
                    timestamp: datetime | None = None):
    """喂入水表数据"""

def feed_gas_data(self, device_id: str, flow_m3h: float, 
                  timestamp: datetime | None = None):
    """喂入气表数据"""
```

**分时电价配置**：
```python
tariff = {
    'peak': 1.2,      # 峰时电价 (8:00-11:00, 18:00-23:00)
    'flat': 0.7,       # 平时电价 (7:00-8:00, 11:00-18:00)
    'valley': 0.35,    # 谷时电价 (23:00-7:00)
}
```

**碳排放计算**：
- 碳排放因子：0.581 kgCO2/kWh（中国电网平均）
- 碳排放 = 电量 × 碳排放因子
- 等效植树 = 碳排放 / 21.77（1棵树年吸收21.77kg CO2）

### 5. 边缘决策引擎 (EdgeDecisionEngine)

**功能**：
- 规则引擎 — IF-THEN规则自动执行
- 联锁控制 — 安全联锁逻辑（急停、超限自动停机）
- 自适应调节 — PID控制回路
- 决策日志 — 所有自动决策可追溯

**数据输入**：
```python
def update_data(self, key: str, value: float):
    """更新数据快照，key格式: "device_id:register_name" """
```

**决策层级**：
1. 安全联锁（最高优先级，毫秒级响应）
2. 规则引擎（秒级响应）
3. 自适应调节（分钟级响应）

**规则类型**：
- 阈值比较：gt, lt, eq, gte, lte, between
- 逻辑组合：and, or

**动作类型**：
- write_register: 写入寄存器
- set_alarm: 触发报警
- callback: 调用回调函数

**PID控制**：
- 支持比例、积分、微分控制
- 带抗饱和积分限幅
- 可配置输出范围

## 四、数据收集正确性验证

### 1. 关键词匹配逻辑

**能源管理关键词**：
```python
power_keywords = ['power', 'watt', 'kw', 'active_power', 'reactive_power', 'apparent_power']
energy_keywords = ['energy', 'kwh', 'mwh', 'electricity', 'consumption']
```

**SPC关键词**：
```python
spc_keywords = [
    'temperature', 'pressure', 'ph', 'quality', 'dimension',
    'voltage', 'current', 'speed', 'flow', 'level',
    'humidity', 'torque', 'frequency', 'thickness',
    'viscosity', 'density', 'concentration', 'turbidity',
    'conductivity', 'oxygen', 'vibration', 'force',
    'position', 'distance', 'cycle_time', 'injection',
    'mold', 'dryer', 'distill', 'boiler', 'heat_exchanger',
    'spray', 'coating', 'sealing', 'conveyor'
]
```

**OEE关键词**：
```python
status_keywords = ['status', 'state', 'running', 'line_status', 'boiler_status', 'packing_status']
count_keywords = ['count', 'product', 'shot', 'label', 'palletizing', 'batch', 'painted', 'quantity']
good_keywords = ['good', 'ok', 'pass', 'qualified']
reject_keywords = ['reject', 'ng', 'defect', 'scrap']
```

### 2. 数据转换逻辑

**功率单位转换**：
```python
if 'w' in register_name.lower() and 'kw' not in register_name.lower():
    power_value = value / 1000  # W转kW
```

**设备状态映射**：
```python
status_map = {0: 'stopped', 1: 'running', 2: 'fault', 3: 'idle'}
status = status_map.get(int(value), 'running')
```

**产量增量计算**：
```python
delta = count - last.get('count', 0)
if delta < 0:
    delta = count  # 计数器归零
sd['total_count'] += max(0, delta)
```

## 五、潜在问题与改进建议

### 1. 关键词匹配问题

**问题**：关键词匹配可能产生误匹配
- 例如：`motor_speed` 会匹配到 `speed` 关键词，被SPC分析器误处理
- 例如：`water_pressure` 会匹配到 `pressure` 关键词，但不是质量数据

**建议**：
- 使用更精确的匹配规则（如正则表达式）
- 添加设备类型过滤
- 使用配置文件定义数据路由规则

### 2. 数据单位问题

**问题**：不同设备可能使用不同单位
- 温度：°C, °F, K
- 压力：Pa, kPa, MPa, bar, psi
- 流量：m³/h, L/min, gal/min

**建议**：
- 在设备配置中定义单位
- 数据采集时统一转换为标准单位
- 存储时记录原始单位

### 3. 数据质量检查

**问题**：缺乏数据质量检查
- 异常值过滤
- 数据完整性检查
- 时间戳同步

**建议**：
- 添加数据范围检查
- 实现数据插值补全
- 使用NTP同步时间戳

### 4. 模块间数据共享

**问题**：模块间数据独立，缺乏共享
- OEE需要设备状态，但SPC需要质量数据
- 能源管理需要功率数据，但OEE需要运行时间

**建议**：
- 实现统一的数据总线
- 使用事件驱动架构
- 建立数据仓库层

## 六、总结

工业4.0模块整体实现良好，具备以下特点：

1. **模块化设计**：每个模块独立，职责清晰
2. **实时数据处理**：支持流式数据输入
3. **多维度分析**：从健康、效率、质量、能源多个维度分析
4. **可扩展性**：支持自定义规则和阈值

**主要优势**：
- 完整的工业4.0功能覆盖
- 标准的算法实现（SPC、OEE、PID等）
- 良好的数据输入接口

**需要改进**：
- 关键词匹配精度
- 数据单位统一
- 数据质量检查
- 模块间数据共享
