# SCADA v3.0.0 工业级改造 - 自主工作队列

## 规则
- 每项：读代码 → 改代码 → 跑测试 → 修到全过 → commit → push
- 测试命令：C:\Users\cxx\AppData\Local\Programs\Python\Python313\python.exe -m pytest tests/ -q --tb=short
- Git: C:\Users\cxx\WorkBuddy\Claw\tools\mingit\cmd\git.exe
- 项目路径：C:\Users\cxx\WorkBuddy\Claw\industrial_scada
- 当前测试：575 passed

## 已完成

### 批次1：安全加固 ✅
- [x] TDengine SQL注入修复
- [x] 安全响应头（CSP/X-Frame-Options/HSTS）
- [x] 页面级认证（JWT cookie）
- [x] API端点认证（15+端点）

### 批次2：架构改进 ✅
- [x] IEC 60870-5-104协议网关
- [x] Modbus字节序配置（ABCD/BADC/CDAB/DCBA）
- [x] 结构化日志（loguru JSON）
- [x] 配置schema验证（jsonschema）

### 批次3：测试与监控 ✅
- [x] 575个测试（core/api/alarm/byte_order/config/device/data/oee/edge/auth/metrics/predictive/audit/module/spc/vibration/energy）
- [x] Prometheus指标导出（/metrics）
- [x] 健康检查增强（自动周期+告警集成）
- [x] 安全联锁完善（多人审批+超时恢复）

### 批次4：高可用 ✅
- [x] 数据持久化保障（DiskBackedQueue）
- [x] 审计日志增强（SHA256+备份）
- [x] OpenAPI/Swagger文档

### 前端修复 ✅
- [x] WebSocket认证（传token）
- [x] WebSocket房间订阅
- [x] 重复socket连接
- [x] loadData竞态条件（generation counter）
- [x] 时间戳碰撞（毫秒精度）
- [x] 告警列表双源冲突
- [x] apiRequest错误处理
- [x] 三重冗余告警请求
- [x] 健康图假数据
- [x] 内存泄漏（buffer限制）

### 模拟器修复 ✅
- [x] state_duration永远为0
- [x] BADC/CDAB字节序相同
- [x] E-STOP冻结值key类型
- [x] total_collections重复计数
- [x] REST write死代码
- [x] coil值不一致
- [x] 硬编码dt
- [x] scale不一致
- [x] OPC UA线程安全
- [x] status模式太确定

### 测试修复 ✅
- [x] 575/575全通过
- [x] energy_manager死锁修复
- [x] vibration_analyzer FFT结果丢失修复
- [x] energy累加错误修复

## 待完成

### 优先级高
- [ ] 连接池管理：Modbus/OPC UA连接复用
- [ ] 前端设备值实时显示（当前显示--）
- [ ] 模拟器参数关联（V*I*cos(phi)、温度→压力物理模型）

### 优先级中
- [ ] 告警抑制/洪水检测
- [ ] 批量过程模拟（配方驱动）
- [ ] 数据质量标志（good/uncertain/bad）
- [ ] 1/f噪声模型

### 优先级低
- [ ] DNP3协议支持
- [ ] 功能安全SIL认证
- [ ] 双机热备
