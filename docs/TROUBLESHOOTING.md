# SmartSCADA 故障排查手册

## 常见问题

### 1. 服务无法启动

#### 问题: 端口被占用
```
OSError: [WinError 10048] 通常每个套接字地址只允许使用一次
```

**解决**:
```bash
# 查找占用端口的进程
netstat -ano | findstr :5000

# 终止进程
taskkill /PID <进程ID> /F
```

#### 问题: Python模块导入失败
```
ModuleNotFoundError: No module named 'xxx'
```

**解决**:
```bash
pip install -r requirements.txt
```

#### 问题: 数据库锁定
```
sqlite3.OperationalError: database is locked
```

**解决**:
1. 检查是否有其他进程占用数据库
2. 删除 `data/scada.db-journal` 和 `data/scada.db-wal`
3. 重启服务

---

### 2. 设备连接失败

#### 问题: Modbus连接超时
```
Modbus连接超时: 192.168.1.100:502
```

**排查步骤**:
1. 检查网络连通性: `ping 192.168.1.100`
2. 检查端口是否开放: `telnet 192.168.1.100 502`
3. 检查防火墙设置
4. 检查设备是否在线

#### 问题: OPC UA连接失败
```
OPC UA连接异常: Endpoint must be a string
```

**解决**:
1. 检查端点URL格式: `opc.tcp://192.168.1.100:4840`
2. 检查OPC UA服务器是否运行
3. 检查安全策略配置

#### 问题: MQTT连接被拒绝
```
MQTT连接被拒绝: Connection refused
```

**解决**:
1. 检查Broker地址和端口
2. 检查用户名密码
3. 检查客户端ID是否冲突

---

### 3. 数据采集异常

#### 问题: 数据不更新
```
设备 xxx 最后更新时间超过60秒
```

**排查步骤**:
1. 检查设备连接状态
2. 检查采集器是否运行: `GET /api/health/status`
3. 检查断路器状态
4. 查看日志中的错误信息

#### 问题: 数据值异常
```
温度值显示为 -999 或 0
```

**排查步骤**:
1. 检查寄存器地址是否正确
2. 检查数据类型是否匹配
3. 检查字节序配置
4. 使用模拟模式验证

---

### 4. 报警系统问题

#### 问题: 报警不触发
```
温度超过阈值但无报警
```

**排查步骤**:
1. 检查报警规则是否启用
2. 检查设备ID和寄存器名称是否匹配
3. 检查去重配置（冷却窗口）
4. 检查报警管理器状态

#### 问题: 报警重复触发
```
同一报警频繁弹窗
```

**解决**:
1. 调整去重配置:
   - `emit_cooldown_seconds`: 冷却窗口（默认30秒）
   - `acknowledge_suppress_seconds`: 确认后抑制时间（默认300秒）

#### 问题: 报警确认失败
```
报警确认返回 success: false
```

**排查步骤**:
1. 检查用户权限（需要admin/engineer/operator角色）
2. 检查alarm_id是否正确
3. 检查报警是否已被确认

---

### 5. 前端问题

#### 问题: 页面空白
```
浏览器控制台报错
```

**排查步骤**:
1. 检查后端是否运行
2. 检查API地址配置
3. 清除浏览器缓存
4. 检查网络连接

#### 问题: 实时数据不更新
```
仪表盘数据停留在旧值
```

**排查步骤**:
1. 检查WebSocket连接（浏览器开发者工具）
2. 检查JWT Token是否有效
3. 检查网络代理设置

#### 问题: 图表不显示
```
ECharts图表区域空白
```

**解决**:
1. 检查数据是否有值
2. 检查图表容器尺寸
3. 刷新页面

---

### 6. 性能问题

#### 问题: API响应慢
```
API请求耗时超过5秒
```

**排查步骤**:
1. 检查数据库大小: `GET /api/system/database`
2. 检查历史数据量
3. 执行数据归档
4. 检查服务器资源使用

#### 问题: 内存占用高
```
Python进程内存超过1GB
```

**解决**:
1. 检查是否有内存泄漏
2. 减少设备数量或采集频率
3. 重启服务

#### 问题: CPU使用率高
```
CPU使用率持续超过80%
```

**排查步骤**:
1. 检查设备数量
2. 检查采集间隔
3. 检查WebSocket推送频率
4. 考虑使用模拟模式

---

## 日志分析

### 日志位置
- 应用日志: `logs/scada_YYYY-MM-DD.log`
- 审计日志: `logs/audit_YYYY-MM-DD.log`
- 服务日志: `logs/service_stdout.log`

### 日志级别
- DEBUG: 调试信息
- INFO: 一般信息
- WARNING: 警告
- ERROR: 错误

### 关键日志模式

#### 设备连接
```
[INFO] 设备 xxx 已连接
[WARNING] 设备 xxx 连接失败
[ERROR] 设备 xxx 断开连接
```

#### 报警触发
```
[WARNING] 报警触发: xxx - 设备/寄存器 = 值
[INFO] 报警确认: xxx
[INFO] 报警清除: xxx
```

#### 数据采集
```
[DEBUG] 设备 xxx 采集成功
[WARNING] 设备 xxx 采集失败，重试中
[ERROR] 设备 xxx 断路器打开
```

---

## 诊断命令

### 健康检查
```bash
curl http://localhost:5000/api/health/status
```

### 系统状态
```bash
curl http://localhost:5000/api/system/status
```

### 设备列表
```bash
curl http://localhost:5000/api/devices
```

### 报警统计
```bash
curl http://localhost:5000/api/alarms/statistics
```

### 数据库统计
```bash
curl http://localhost:5000/api/system/database
```

---

## 恢复流程

### 数据库损坏
1. 停止服务
2. 备份损坏的数据库
3. 从备份恢复: `cp backup.db data/scada.db`
4. 重启服务

### 配置文件损坏
1. 停止服务
2. 从备份恢复配置文件
3. 重启服务

### 紧急停机
1. 访问控制页面
2. 点击"紧急停机"按钮
3. 确认停机操作

---

## 联系支持

如遇到无法解决的问题，请提供：
1. 错误日志
2. 系统状态截图
3. 设备配置
4. 操作步骤
