# 国标合规映射表

本文档将系统实现映射到相关国家标准条款，便于论文审查和合规审计。

## GB/T 19582 — 基于 Modbus 协议的工业自动化网络规范

| 条款 | 要求 | 实现 | 文件:行 |
|------|------|------|---------|
| 功能码白名单 | 只允许标准 Modbus 功能码，禁止厂商自定义功能码 | `ALLOWED_FUNCTION_CODES` 集合定义允许的功能码 (0x01-0x17) | `core/write_safety.py:52-62` |
| 地址范围校验 | 寄存器地址必须在设备声明的范围内 | `validate_write()` 检查地址是否在 `_profiles` 中 | `core/write_safety.py:267-278` |
| 值域约束 | 写入值必须在寄存器安全范围内 | `validate_write()` 检查 `min_value <= value <= max_value` | `core/write_safety.py:281-289` |
| 只读寄存器保护 | 只读寄存器禁止写入 | `RegisterSafetyProfile.writable` 字段控制 | `core/write_safety.py:274-278` |
| FC03 批量读取限制 | 单次最多读 125 个寄存器 | `_collect_modbus()` 自动分段读取，块边界预留重叠区 | `采集层/data_collector.py:608-636` |
| 读取地址范围校验 | 读取地址必须在合法范围 | `_validate_address_range()` 检查 0-65535 | `采集层/modbus_client.py:136-155` |
| 写入安全验证 | 写入前必须通过安全检查 | `WriteSafetyValidator` 集成到 `ModbusClient` | `采集层/modbus_client.py:81-83` |
| 单寄存器写入校验 | FC06 写入前地址/值域校验 | `write_register()` 调用 `_write_safety.validate_write()` | `采集层/modbus_client.py:555` |
| 多寄存器写入校验 | FC16 逐寄存器安全检查 | `write_registers()` 循环调用 `validate_write()` | `采集层/modbus_client.py:713` |

## GB/T 35718 — 工业控制系统信息安全

| 条款 | 要求 | 实现 | 文件:行 |
|------|------|------|---------|
| 令牌撤销机制 | JWT 令牌必须支持即时撤销 | `jwt_blacklist` 表 + `blacklist_token()` 方法 | `用户层/auth.py:126-142` |
| 令牌黑名单检查 | 验证令牌时必须检查黑名单 | `verify_token()` 查询 `jwt_blacklist` 表 | `用户层/auth.py:335-340` |
| 密码变更时间戳 | 记录密码最后修改时间 | `password_changed_at` 列 | `用户层/auth.py:97-101` |
| 密码变更后令牌失效 | 修改密码后旧令牌必须立即失效 | `change_password()` 调用 `_blacklist_user_tokens()` | `用户层/auth.py:532-533` |
| 刷新令牌密码检查 | 刷新令牌时检查密码是否已变更 | `refresh_token()` 比较 `password_changed_at` 和 `token_iat` | `用户层/auth.py:468-475` |
| 写入权限分级 | 不同风险等级的写入需要不同权限 | `WriteRiskLevel` 枚举 (LOW/MEDIUM/HIGH/CRITICAL) | `core/write_safety.py:28-33` |
| 写入审计日志 | 每次写入操作必须记录完整审计信息 | `validate_write()` 记录 addr、name、value、range、risk、user | `core/write_safety.py:311-316` |
| 操作日志记录 | 用户操作必须可审计 | `operation_logs` 表 + `_log_operation()` | `用户层/auth.py:103-114` |
| TLS 通信加密 | 工控系统通信必须加密 | TLS/HTTPS 配置和证书生成 | `config.py:218-221`, `core/generate_certs.py:4` |
| 登出令牌撤销 | 登出时必须撤销当前令牌 | `logout()` 调用 `blacklist_token()` | `展示层/api/api_auth.py:82-89` |

## GB/T 22239 — 信息安全技术 网络安全等级保护基本要求（等保 2.0）

| 条款 | 要求 | 实现 | 文件:行 |
|------|------|------|---------|
| 密码强度 | 密码必须满足复杂度要求 | `_validate_password_strength()` 检查长度、大小写、数字 | `用户层/auth.py:159-169` |
| 账户锁定 | 登录失败次数过多必须锁定 | `login()` 5 次失败后锁定 30 分钟 | `用户层/auth.py:258-273` |
| 速率限制 | 对异常访问行为进行限制 | `flask_limiter` 限制 200 次/分钟 | `core/rate_limiter.py:1-20` |
| 登录速率限制 | 登录接口单独限制 | 登录接口限制 5 次/分钟 | `config.py:214` |
| 安全响应头 | 必须设置安全 HTTP 头 | `X-Frame-Options`、`X-Content-Type-Options`、CSP 等 | `展示层/routes.py:80-90` |
| 页面级认证 | 所有页面必须经过认证 | `@page_auth_required` 装饰器 | `展示层/routes.py:111-115` |
| 结构化审计日志 | 安全审计日志必须可机器解析 | JSON 格式结构化日志 | `core/structured_logging.py:1-4` |
| 首次登录改密 | 首次登录必须强制修改密码 | `must_change_password` 标志 + `force_change_password()` | `用户层/auth.py:537-595` |
| 角色权限控制 | 不同角色具有不同权限 | `ROLES` 字典定义 4 级角色权限 | `用户层/auth.py:19-36` |
| 权限装饰器 | API 接口必须检查权限 | `@jwt_required`、`@role_required`、`@permission_required` | `用户层/auth.py:720-805` |

## GB/T 37980 — 工业互联网综合标准化

| 条款 | 要求 | 实现 | 文件:行 |
|------|------|------|---------|
| CSRF 防护 | 防止跨站请求伪造 | Double Submit Cookie 模式 | `core/csrf_protection.py:1-30` |
| CSRF 令牌端点 | 提供令牌获取接口 | `/api/csrf-token` 端点 | `展示层/routes.py:216` |
| TLS/HTTPS | 通信必须加密 | TLS 配置和自签名证书生成 | `config.py:218-221`, `core/generate_certs.py:4` |
| 安全联锁 | 关键操作需要安全联锁检查 | `_INTERLOCK_RULES` 定义联锁条件 | `core/write_safety.py:134-153` |
| 二次确认 | 高风险操作需要二次确认 | `requires_confirm` 字段 + `confirm` 参数 | `core/write_safety.py:300-308` |

## GB/T 15969 — 可编程序控制器

| 条款 | 要求 | 实现 | 文件:行 |
|------|------|------|---------|
| 故障安全 | 设备故障时必须进入安全状态 | `DeviceState.FAULT` 状态处理 | `采集层/device_behavior_simulator.py:903-913` |
| 安全联锁 | 关键参数写入需要联锁检查 | `boiler_pressure`、`boiler_temperature` 联锁规则 | `core/write_safety.py:134-153` |
| 温度安全范围 | 工业设备温度必须在安全范围 | 温度类寄存器安全范围 -40~500°C | `core/write_safety.py:70-77` |
| 压力安全范围 | 压力必须在安全范围 | 锅炉压力上限 4.0 MPa | `core/write_safety.py:82` |

## GB/T 36323 — 信息安全技术 工业控制系统安全管理指南

| 条款 | 要求 | 实现 | 文件:行 |
|------|------|------|---------|
| 写入风险评估 | 写入操作必须评估风险等级 | `WriteRiskLevel` 四级风险评估 | `core/write_safety.py:28-33` |
| 风险分级管理 | 不同风险等级采取不同措施 | HIGH/CRITICAL 需要二次确认 | `core/write_safety.py:208-209` |
| 安全档案 | 每个寄存器必须有安全配置 | `RegisterSafetyProfile` 数据类 | `core/write_safety.py:36-48` |

## GB/T 33008 — 工业控制系统网络安全基本要求

| 条款 | 要求 | 实现 | 文件:行 |
|------|------|------|---------|
| 令牌安全传递 | JWT 令牌通过 HttpOnly Cookie 传递 | `set_cookie('token', ..., httponly=True)` | `展示层/api/api_auth.py:66-76` |
| 登出安全 | 登出时必须撤销令牌 | `logout()` 调用 `blacklist_token()` | `展示层/api/api_auth.py:82-89` |

## DL/T 634.5104 — 远动设备及系统（IEC 60870-5-104）

| 条款 | 要求 | 实现 | 文件:行 |
|------|------|------|---------|
| 值域约束 | 遥调命令值必须在允许范围内 | `validate_write()` 值域校验 | `core/write_safety.py:281-289` |
| 超时处理 | 通信超时必须正确处理 | 连接超时和读取超时配置 | `采集层/modbus_client.py:50-55` |
