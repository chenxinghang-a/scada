# Industrial SCADA 审计缺陷修复清单（Round 369 之后）

> 依据：`industrialscadacodeauditreport (1).pdf`（v1.3.1091 / Round 369 二次审计）
> 仓库：`C:\Users\cxx\WorkBuddy\Claw\industrial_scada`
> 改动规模：62 个文件，+2242 / -576 行（含 7 个测试/守卫同步）

## 一、P0 严重（已修）

| 编号 | 修复内容 | 关键文件 |
|---|---|---|
| BUG-01 | `docker-compose.yml` 补齐必填 `SCADA_ADMIN_PASSWORD`（前次已修） | docker-compose.yml |
| BUG-02 | 写回读三态审计（success / unverified / verify_failed）；`adjust` 失败不再回 200；`/write-coil`、`/adjust` 接入幂等防重；幂等窗口改单调时钟 | 智能层/device_control.py、展示层/api/api_control.py |
| BUG-03 | `reset_emergency_stop` 逐台校验 + 失败保留 + 声光塔按活跃报警复位（前次已修） | 智能层/device_control.py |
| BUG-04 | `batch_control`：reset 同口径联锁拦截；`device_ids=None` 时按设备粒度阻断而非全厂连坐；批量写走安全通道；**取消 start/stop 的二次下发**并校验返回值 | 智能层/device_control.py、采集层/real_device_manager.py、采集层/simulated_device_manager.py |
| BUG-05 | FINS/MC 补齐 `write_single_register` / `write_single_coil`；`read_int32`/`read_float32` 委托按字节序解码（补码符号扩展） | 采集层/fins_client.py、采集层/mc_client.py |
| BUG-06 | HA 心跳：`state` 纳入 HMAC；时间戳新鲜度校验；每对端最大时间戳防重放 | core/ha_manager.py |
| BUG-07 | 签名装饰器先取密钥（生产缺失 → 503 fail-closed）、**先验签后登记 nonce**、nonce 缓存硬上限 5 万 + 单调时钟 | core/request_signature.py |
| BUG-08 | `update_user` 禁止禁用/降权 admin；改密拒绝新旧相同；撤销令牌兼容 body token；刷新令牌黑名单键统一；WebSocket 拦截未完成强制改密 | 用户层/auth.py、展示层/websocket.py |

## 二、P1 高危（已修）

| 编号 | 修复内容 | 关键文件 |
|---|---|---|
| BUG-09 | 报警规则 POST/PUT 加 `_state_lock`；改绑点位（device/register/condition）或禁用时收尾旧活跃报警；通知配置落盘后重建内存通知器 | 展示层/api/api_alarms.py |
| BUG-10 | DNP3 `send_control` / IEC104 `_send_i_frame` 未连接返回 False，改 `sendall` | gateway/dnp3_gateway.py、gateway/iec104_gateway.py |
| BUG-11 | IEC104 ASDU 支持 SQ=1 顺序寻址、完整 24 位 IOA、APDU 长度下限与坏帧统计 | gateway/iec104_gateway.py |
| BUG-12 | Modbus 网关 uint32/int32 按 byte_order 解码；OPC UA 区分 Float/Double/UInt16/UInt32；S7 补 bool 写入 | gateway/*.py |
| BUG-13 | 模拟 REST 网关实现 POST/PUT 写入（原硬编码 405） | 模拟服务端/rest_gateway.py |
| BUG-14 | 迁移器核验 TDengine 写返回值，按实际写入条数记账 | timeseries/migration.py |
| BUG-15 | 边缘决策/PID 检查回调返回 False（不再只捕异常） | 智能层/edge_decision.py |
| BUG-16 | `days_to_limit=None` 不再 TypeError；按设备聚合最低健康分，避免同毫秒覆盖 | 智能层/tsdb_adapter.py |
| BUG-17 | 设备锁惰性创建加锁（setdefault），全局锁改 RLock | 智能层/device_control.py |
| BUG-18 | 定时任务拒绝重叠执行；耗时改单调时钟 | core/scheduled_tasks.py |
| BUG-19 | 控制 API 幂等窗口单调时钟、adjust 失败 403、写路径统一防重 | 展示层/api/api_control.py |
| BUG-20 | 广播喊话失败返回 500 并记失败日志 | 展示层/api/api_alarms.py |
| BUG-37 | 真实/模拟管理器 `reload_config` 断开并移除已删设备客户端、触发 `notify_device_removed` | 采集层/real_device_manager.py、simulated_device_manager.py |
| BUG-38 | 增强模拟客户端按 byte_order 编解码 32 位值；停机分支按点位宽度步进；线圈回读返回写入值 | 采集层/enhanced_simulated_client.py、base_client.py |
| BUG-39 | 真实管理器 float32 按字节序写；模拟管理器 adjust 真正写入并同步设定点 | 采集层/real_device_manager.py、simulated_device_manager.py |

## 三、P2 中危（已修）

| 编号 | 修复内容 | 关键文件 |
|---|---|---|
| BUG-21 | Modbus 重连退避/缓存年龄、真实设备退避/connect_all 超时、设备状态缓存改单调时钟 | 采集层/modbus_client.py、real_device_manager.py、device_manager.py |
| BUG-22 | WebSocket `_known_users` 用单调时钟并在断开时刷新 | 展示层/websocket.py |
| BUG-23 | 数据库指标连接 finally 关闭；缓存 TTL 单调时钟 | 展示层/api/api_performance.py |
| BUG-24 | QueryBuilder 表名/列名/聚合函数/排序/interval/fill 白名单校验 | timeseries/query_builder.py |
| BUG-25 | 历史清理改分块删除并失效统计缓存 | 存储层/database.py |
| BUG-26 | CSV 导出汇总全部行键集合；非法时间参数 400；区分「无数据 404」与「写盘失败 500」 | 存储层/data_export.py、展示层/api/api_data.py |
| BUG-27 | 预测维护入口过滤 bool/NaN/Inf | 智能层/predictive_maintenance.py |
| BUG-28 | 健康摘要补 `degraded` 键 | 智能层/device_control.py |
| BUG-29 | 故障注入到期用单调时钟 | core/fault_injection.py |
| BUG-30 | 生产环境 `CSRF_SECRET` 缺失/弱值 → 拒绝启动 | config.py |
| BUG-40 | 行为模拟器写设定点乘 scale/offset；清理重复字典键 | 采集层/device_behavior_simulator.py |
| BUG-41 | MQTT 用客户端绑定设备 ID、按 topics_config 取点位名/单位、单值分支补 retain | 采集层/mqtt_client.py |
| BUG-42 | 真实/模拟管理器配置保存改原子写 `dump_yaml` | 采集层/real_device_manager.py、simulated_device_manager.py |
| BUG-43 | 200 响应的 `X-RateLimit-Reset` 改绝对时刻 | core/user_rate_limiter.py |
| BUG-44 | 前端补全局 `logout()`；CSV 公式注入与 RFC4180 转义 | 静态资源/js/main.js、dashboard.js |
| BUG-45 | `start.bat` 改纯 ASCII 内联启动（原 `call 启动.bat` 在 UTF-8 代码页下必失败） | start.bat |
| BUG-46 | 联锁旁路定时器句柄登记、手动/删除时取消、孤儿定时器校验 request_id、有效期从审批通过起算 | 智能层/device_control.py |
| BUG-47 | 连接池占用中拒绝并发复用（不再把非线程安全客户端交给两个线程） | core/connection_pool.py |
| BUG-48 | 6 个智能模块 `forget_device` 加锁 | 智能层/*.py |
| BUG-49 | 广播熔断状态走 `_circuit_is_open()`，计数加锁 | 报警层/broadcast_system.py |
| BUG-50 | REST 采集禁用系统代理；轮询改 Event.wait 可即时退出 | 采集层/rest_client.py |
| BUG-51 | 预设设备添加失败/采集未启动返回非 200 | 展示层/api/api_devices.py |
| BUG-52 | 设备删除/配置重载清理协议标签缓存 | 采集层/data_collector.py |
| BUG-53 | 模拟 MQTT Broker 报文越界保护 + 可选凭据校验 | 模拟服务端/mqtt_broker.py |
| BUG-54 | 异步任务队列耗时长单调时钟；`queued_task` 只把「入队失败」判 503 | core/request_queue.py |
| BUG-55 | 熔断器状态时间已分离（mono 判定 / wall 展示），无监听器持锁回调 | core/circuit_breaker.py（复核，无需改） |
| BUG-56 | 升级键各段转义冒号（消除拼接碰撞） | 报警层/alarm_escalation.py |
| BUG-57 | WAL 队列重写改原子替换（临时文件 + fsync + os.replace） | 采集层/data_collector.py |
| BUG-58 | 诊断包临时目录 finally 清理；耗时单调时钟 | core/ops_tools.py |
| BUG-59 | 班次切换保留 ideal_cycle_time 并重置 `since` | 智能层/oee_calculator.py |
| BUG-60 | 性能指标文件原子裁剪 + 进程内互斥 | 展示层/api/api_performance.py |

## 四、P3 清理项（本轮补齐）

| 编号 | 处理结果 | 关键文件 |
|---|---|---|
| BUG-31 | **复核为非缺陷**：`DatabaseConfig.DB_PATH` / `LogConfig.LOG_DIR` / `ExportConfig.EXPORT_DIR` 是冻结产物布局断言的**单一真源**（`tests/test_frozen_layout_paths.py`、`tests/test_env_vars_wired.py` 读它们断言与 `paths.*` 一致），删掉等于拆掉守卫；`AuthConfig._DEV_DEFAULT_ADMIN_PW` 在 `config.py` 内部被弱口令检测读取（module 级第 340/342 行），并非零读取。已在 `test_config_readers_gate.py` 的白名单里写明理由。 | config.py |
| BUG-32 | **复核为已修复**：`.env.example` 已声明全部 57 个 `${DEVICE_*}`（含 `DEVICE_REST_ADMIN_PASSWORD`），`tools/scan_doc_env_vars.py` 报「配置引用但无声明 0 条」。 | .env.example |
| BUG-33 | **已修**：`jwt_blacklist.blacklisted_at`、`operation_logs.timestamp`、`users.created_at/updated_at` 改为**显式写本地时间**；DDL 默认值由 `CURRENT_TIMESTAMP`（UTC）改为 `datetime('now','localtime')`；`blacklist_token` 的写入与 `cleanup_expired_blacklist` 的 7 天比较同口径。存量库仍用 `tools/migrate_created_at_to_local.py` 迁移。 | 用户层/auth.py、存储层/database.py |
| BUG-34 | **已修**：`RELEASE_NOTES.md` 头/版本号/日期对齐 `VERSION=1.3.1091`，补本轮修复摘要与最新测试数；`CHANGELOG.md` 新增 `[1.3.1091]` 条目并加「按发布时间倒序、3.0.0 是历史编号」的排序说明。 | RELEASE_NOTES.md、CHANGELOG.md |
| BUG-35 | **已修**：扫描器跳过 `.venv`/`dist`/`node_modules`/缓存目录；凭证检测改为「命名捕获 + 值像不像凭证」判定（`TOKEN_EXPIRED = 'E2003'` 不再误报）；弱口令检查先剥注释（`docker-compose.yml` 注释里的 admin123 不再误报）；`debug=True` 只认代码、跳过含引号的行（不再自己报自己）；日志检查改 AST（只报真的把令牌变量传进 logger 的调用）。实测 quick 扫描 **0 发现 / 100 分**；full 扫描仅剩 33 条「SQL 标识符拼接」需人工复核项。 | tools/security_scan.py |
| BUG-36 | **部分接线**：把 3 个高价值孤岛接到生产路径 —— `DeviceControlSafety.reload_interlocks()` → `POST /api/system/interlocks/reload`（改联锁阈值不再需要重启）；`DataExport.list_exports()` / `delete_export()` → `GET|DELETE /api/export/files[/<name>]`（导出文件可清理，路径穿越由 `_resolve_target` 兜底）。剩余 ~160 个为协议适配器读写方法、测试专用助手与对外 API 面，删除属产品决策，扫描器已支持 `# noqa: dead-api` 逐个标注。 | 展示层/api/api_system.py、api_data.py、tests/test_interlock_reload_api.py |
| BUG-05 补充 | 欧姆龙/三菱的 float32 写入仍按 `>f`/`<f` 编码（原实现即如此），本轮只补齐寄存器/线圈写与解码；如需四种字节序写 float32，应统一走 `encode_float32_registers` | |

## 五、部署注意

1. **HA 心跳协议变更**：`state` 已纳入 HMAC 签名，新旧版本节点不能混跑（需同时升级）。
2. **生产签名密钥**：`API_SIGNATURE_SECRET` 缺失时，带 `@signature_required` 的接口在生产环境返回 503（当前仓库内暂无路由使用该装饰器，接入前需先配置密钥）。
3. **`CSRF_SECRET`**：生产环境必须通过环境变量提供强随机值，否则启动即 `RuntimeError`（与 `SECRET_KEY`/`JWT_SECRET` 同口径）。
4. **`start.bat`** 现在是纯 ASCII 内联启动，不再依赖中文文件名的 `启动.bat`。

## 六、验证

- 全量回归（`pytest tests`，`PYTHONUTF8=1`）：
  - 第一次：`25 failed / 1934 passed`（停止于 maxfail）
  - 第二次：`6 failed / 4522 passed / 11 skipped`
  - 第三次：`1 failed / 4527 passed / 11 skipped`（失败项为 `tsdb_adapter` 缺顶层导入，已修）
  - P3 轮次：`1 failed / 4530 passed / 11 skipped`（失败项是新增端点的契约分类，已在 `UNUSED_BACKEND_PREFIX_RULES` 登记，复跑通过）
  - 最终：**4531 passed / 11 skipped / 0 failed**
- 两个环境性失败：`test_env_vars_wired` 在非 UTF-8 控制台下对中文路径断言失败（设 `PYTHONUTF8=1` 即通过），与代码无关。
- 安全扫描：`tools/security_scan.py quick` → **0 发现 / 100 分**；`full` → 0 严重 / 0 高危以外的误报，仅剩 33 条「SQL 标识符拼接」人工复核项（均为 `f'... FROM {table}'` 形式的表名插值）。
- 同步更新/新增的守卫单测：设备生命周期通知计数、写审计三态、MQTT、报警升级键、REST 模拟网关、连接池、数值守卫、队列持久化（禁 unlink）、联锁热重载接口等。
