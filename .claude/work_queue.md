# SCADA v3.0.0 工业级改造 - 自主工作队列

## 规则
- 每项：读代码 → 改代码 → 跑测试 → 修到87/87全过 → commit → push
- 测试命令：C:\Users\cxx\AppData\Local\Programs\Python\Python313\python.exe -m pytest tests/ -v --tb=short
- Git: C:\Users\cxx\WorkBuddy\Claw\tools\mingit\cmd\git.exe
- 项目路径：C:\Users\cxx\WorkBuddy\Claw\industrial_scada

## 队列（按优先级）

### 批次1：安全加固
1. [ ] 默认admin强制首次登录改密 + 密码复杂度(>=8位+大小写+数字)
2. [ ] TDengine SQL注入修复：f-string → 参数化查询
3. [ ] 安全响应头：CSP, X-Frame-Options, X-Content-Type-Options, HSTS
4. [ ] 页面级认证：所有HTML页面路由检查JWT(除login页)

### 批次2：架构改进
5. [ ] IEC 60870-5-104协议网关(模拟器+客户端+物模型转换)
6. [ ] Modbus字节序配置：支持ABCD/BADC/CDAB/DCBA
7. [ ] 结构化日志：loguru配置JSON格式输出
8. [ ] 配置schema验证：用jsonschema验证YAML配置

### 批次3：测试与监控
9. [ ] 扩展测试覆盖：采集层/智能层/用户层单元测试(目标>80%)
10. [ ] Prometheus指标导出：/metrics端点，采集/告警/连接数指标
11. [ ] 健康检查增强：自动周期检查+告警集成
12. [ ] 安全联锁完善：interlock多人审批+超时自动恢复

### 批次4：高可用
13. [ ] 数据持久化保障：队列落盘+崩溃恢复
14. [ ] 连接池管理：Modbus/OPC UA连接复用
15. [ ] 审计日志增强：完整SHA256+远程备份+防篡改
16. [ ] API文档自动生成：OpenAPI/Swagger
