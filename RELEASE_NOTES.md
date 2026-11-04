# SmartSCADA v1.3.1091 Release Notes

## 版本信息
- 版本号: 1.3.1091（唯一版本源：根目录 `VERSION`）
- 发布日期: 2026-11-04
- 代码行数: ~30,000行
- 测试用例: 276 个测试文件（详见 README 与 coverage.xml；历史文档中的 1,847/1,780 为过时口径）

## 本轮修复（Round 369 二次审计）

> 完整清单见 `文档/审计修复清单_Round369.md`。以下只列对部署/运维有感知的项。

- **PLC 写入闭环**：回读验证失败不再返回 `success=True`；`/write-register`、`/write-coil`、`/adjust` 统一幂等防重，失败返回非 200。
- **批量控制**：`batch_control` 的 reset 与单台同口径联锁拦截；`device_ids=None` 时只阻断联锁命中的设备，不再整厂连坐；取消 start/stop 的二次下发。
- **采集层**：FINS/MC 补齐寄存器/线圈写入与补码解码；真实/模拟管理器热重载会真正断开并通知已删设备；模拟器 32 位值按 `byte_order` 编解码。
- **协议网关**：DNP3/IEC104 未连接返回失败；IEC104 支持 SQ=1 与 24 位 IOA；Modbus/OPC UA/S7 编解码修正；模拟 REST 网关支持 POST/PUT。
- **安全**：HA 心跳 state 纳入 HMAC 并做防重放；签名装饰器先验签后登记 nonce（生产缺密钥 fail-closed）；`auth` 禁止管理员自我禁用/降权，改密撤销旧令牌、WebSocket 拦截未完成强制改密；生产 `CSRF_SECRET` 弱值拒绝启动。
- **存储/导出**：历史清理改分块删除；CSV 导出修正列集合、RFC4180 转义与公式注入；QueryBuilder 增加标识符白名单。
- **时间基准**：剩余时长比较全部改 `time.monotonic()`；SQLite 时间戳统一写本地时间（存量库用 `tools/migrate_created_at_to_local.py` 迁移）。
- **运维**：`start.bat` 改为纯 ASCII 内联启动；诊断包临时目录无条件清理；设备配置保存改原子写。

## 主要改进

### 架构优化
- 模块化API设计，统一错误处理
- 数据库连接池优化，支持自动扩缩容
- WebSocket连接管理，支持心跳保活
- 配置文件热重载，无需重启

### 安全加固
- JWT认证体系，支持Token刷新
- 角色权限控制（RBAC）
- API限流防护
- SQL注入防护
- XSS防护
- CSRF防护

### 性能优化
- 设备状态查询缓存（TTL 5s）
- 历史数据分页+索引优化
- WebSocket推送频率自适应
- ECharts大数据量采样渲染
- API响应gzip压缩

### 可靠性提升
- 设备断线自动重连（30秒间隔）
- 数据采集故障降级（模拟数据兜底）
- 报警系统冗余（WebSocket断开→轮询降级）
- 数据库WAL自动checkpoint
- 进程崩溃自动恢复

### 可观测性
- 分布式追踪支持
- 结构化日志增强
- 性能指标自动采集
- 告警规则自动生成
- 运行时诊断工具

### 数据治理
- 数据质量监控
- 数据脱敏工具
- 数据生命周期管理
- 数据血缘追踪
- 数据一致性检查

### 运维自动化
- 自动化部署脚本
- 健康检查端点增强
- 日志分析自动化
- 性能基线建立
- 容量规划工具

## 测试覆盖
- 测试套件: 276 个测试文件（tests/ 下 test_*.py）
- 集成测试: 覆盖核心 API 与端到端链路（Modbus / OPC UA / REST 均用真协议冒烟）
- 安全测试: 渗透测试用例
- 性能测试: 压力测试用例
- 覆盖率门槛: 60%（见 .github/workflows/ci.yml 的 --cov-fail-under=60）
- 当前状态: **4531 passed / 11 skipped / 0 failed**（2026-11-04 全量实跑，`PYTHONUTF8=1`）

## 已知问题
- 无阻塞性缺陷。此前记录在此的「43 个用例失败 / 23 个错误」已全部清理。
- 安全扫描结果以 `tools/security_scan.py` 的当次输出为准（不在此处维护固定条数）。

## 部署说明
详见 `docs/DEPLOYMENT.md`

## 更新日志
详见 `CHANGELOG.md`
