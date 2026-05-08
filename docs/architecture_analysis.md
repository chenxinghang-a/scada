# 工业SCADA系统架构分析与改进方案

## 一、当前架构问题总结

### 1. 假接口问题（共15处）

| 模块 | 问题描述 | 严重程度 |
|------|----------|----------|
| 短信通知 | `send_sms()` 直接返回True，无实际发送逻辑 | 高 |
| 数据归档 | `compress_data()` 只查询不压缩 | 中 |
| 工业4.0总览 | 返回硬编码零值，无法区分"未启用"和"数据为零" | 高 |
| 能源管理 | 5个API在模块未启用时返回硬编码零值 | 高 |
| 边缘决策 | 返回假状态数据 | 高 |
| 设备控制 | 紧急停机、联锁、健康状态返回假数据 | **严重** |
| 网关模块 | 只有Modbus实现，其他协议为空壳 | 中 |
| 广播系统 | `SimulatedBroadcastSystem` 只记录日志 | 低 |
| 报警输出 | `SimulatedAlarmOutput` 只记录日志 | 低 |
| 预测性维护 | 模块未启用时返回假数据 | 中 |
| OEE计算 | 模块未启用时返回假数据 | 中 |
| 历史趋势 | 部分查询返回空数据 | 低 |
| 设备模板 | 预设模板硬编码在代码中 | 低 |
| 用户权限 | 部分权限检查是假的 | 中 |
| 数据导出 | 部分格式导出是空实现 | 低 |

### 2. 无用设置问题（共8处）

| 配置项 | 问题描述 |
|--------|----------|
| `AlarmConfig.EMAIL_ENABLED` | 配置了但邮件发送功能未实现 |
| `AlarmConfig.SMTP_*` | SMTP配置存在但从未使用 |
| `DatabaseConfig.COMPRESSION_INTERVAL` | 配置了但压缩功能是假的 |
| `ExportConfig.FORMATS` | 配置了支持格式但部分格式未实现 |
| `BroadcastConfig.PRESET_TEMPLATES` | 模板配置了但模拟模式不使用 |
| `AlarmOutputConfig.DO_MAPPING` | DO映射配置了但模拟模式不使用 |
| `MQTTConfig.*` | MQTT配置存在但部分功能未实现 |
| `AuthConfig.JWT_REFRESH_DAYS` | 刷新令牌功能未实现 |

### 3. 架构设计问题

1. **模块耦合度高**：`run.py` 直接创建所有组件，缺乏依赖注入
2. **错误处理不一致**：部分API返回假数据，部分返回错误码
3. **配置管理混乱**：YAML配置和Python配置类并存
4. **模拟/真实模式分离不彻底**：部分模块仍需判断模式
5. **缺乏健康检查**：无法知道哪些模块真正可用
6. **缺乏模块状态管理**：无法动态启用/禁用模块

---

## 二、SCADA最佳实践参考（ISA-18.2）

### 1. 报警管理最佳实践

- **报警哲学文档**：定义什么是报警，谁有权创建
- **报警合理化**：每个报警必须有明确的操作员响应
- **优先级分布**：80%低优先级，15%中优先级，5%高优先级
- **高级报警技术**：死区、延迟、状态抑制、动态限值
- **报警KPI监控**：每小时报警数、峰值报警率、常驻报警数

### 2. 系统架构最佳实践

- **分层架构**：现场设备层 → RTU/PLC层 → 通信网络层 → 监控中心层
- **冗余设计**：关键系统双机热备
- **实时性保障**：数据采集延迟 < 100ms
- **安全防护**：网络分段、入侵检测、零信任架构

### 3. 数据管理最佳实践

- **时序数据库**：使用TDengine/InfluxDB存储历史数据
- **数据压缩**：有损/无损压缩算法
- **数据归档**：冷热数据分离
- **数据备份**：定期备份，异地容灾

---

## 三、改进架构设计

### 1. 新架构图

```
┌─────────────────────────────────────────────────────────────┐
│                      用户界面层 (HMI)                        │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐       │
│  │ 仪表盘   │ │ 报警管理 │ │ 设备控制 │ │ 历史趋势 │       │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘       │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      API网关层 (Gateway)                     │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  认证授权 │ 限流熔断 │ 请求路由 │ 健康检查 │ 模块状态 │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      业务逻辑层 (Services)                   │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐       │
│  │ 数据采集 │ │ 报警服务 │ │ 设备控制 │ │ 历史服务 │       │
│  │ Service  │ │ Service  │ │ Service  │ │ Service  │       │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘       │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐       │
│  │ 能源服务 │ │ OEE服务  │ │ 预测维护 │ │ 边缘决策 │       │
│  │ Service  │ │ Service  │ │ Service  │ │ Service  │       │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘       │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      核心抽象层 (Core)                       │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  依赖注入容器 │ 模块注册表 │ 配置管理 │ 事件总线     │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      设备抽象层 (Device)                     │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐       │
│  │ Modbus   │ │ OPC-UA   │ │ MQTT     │ │ REST     │       │
│  │ Client   │ │ Client   │ │ Client   │ │ Client   │       │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘       │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      数据存储层 (Storage)                    │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐       │
│  │ SQLite   │ │ TDengine │ │ Redis    │ │ 文件系统 │       │
│  │ (配置)   │ │ (时序)   │ │ (缓存)   │ │ (日志)   │       │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘       │
└─────────────────────────────────────────────────────────────┘
```

### 2. 核心改进点

#### 2.1 依赖注入容器

```python
class DIContainer:
    """依赖注入容器"""
    _services = {}
    _singletons = {}
    
    @classmethod
    def register(cls, name, factory, singleton=False):
        cls._services[name] = {'factory': factory, 'singleton': singleton}
    
    @classmethod
    def resolve(cls, name):
        if name in cls._singletons:
            return cls._singletons[name]
        
        service = cls._services.get(name)
        if not service:
            raise KeyError(f"Service {name} not registered")
        
        instance = service['factory']()
        if service['singleton']:
            cls._singletons[name] = instance
        return instance
```

#### 2.2 模块注册表

```python
class ModuleRegistry:
    """模块注册表"""
    _modules = {}
    
    @classmethod
    def register(cls, name, module_class, config=None):
        cls._modules[name] = {
            'class': module_class,
            'config': config,
            'instance': None,
            'status': 'registered'
        }
    
    @classmethod
    def initialize(cls, name):
        module = cls._modules.get(name)
        if module:
            module['instance'] = module['class'](module['config'])
            module['status'] = 'initialized'
    
    @classmethod
    def get_status(cls, name=None):
        if name:
            return cls._modules.get(name, {}).get('status')
        return {k: v['status'] for k, v in cls._modules.items()}
```

#### 2.3 统一错误处理

```python
class ServiceResponse:
    """统一服务响应"""
    def __init__(self, success, data=None, error=None, code=200):
        self.success = success
        self.data = data
        self.error = error
        self.code = code
    
    def to_dict(self):
        if self.success:
            return {'success': True, 'data': self.data}
        return {'success': False, 'error': self.error}
```

#### 2.4 模块健康检查

```python
class HealthChecker:
    """模块健康检查"""
    @staticmethod
    def check_module(module_name):
        module = ModuleRegistry._modules.get(module_name)
        if not module or not module['instance']:
            return {'status': 'unavailable', 'message': '模块未初始化'}
        
        try:
            if hasattr(module['instance'], 'health_check'):
                return module['instance'].health_check()
            return {'status': 'healthy'}
        except Exception as e:
            return {'status': 'unhealthy', 'error': str(e)}
```

---

## 四、实施计划

### 阶段一：核心架构重构（1-2周）

1. 实现依赖注入容器
2. 实现模块注册表
3. 重构 `run.py` 使用新架构
4. 统一错误处理

### 阶段二：假接口修复（2-3周）

1. 修复短信通知接口（集成阿里云短信）
2. 修复数据归档接口（实现真正的压缩）
3. 修复工业4.0 API（返回模块状态而非硬编码）
4. 修复设备控制API（返回真实状态）
5. 实现健康检查API

### 阶段三：功能完善（3-4周）

1. 实现报警KPI监控
2. 实现报警合理化工具
3. 实现数据备份恢复
4. 实现模块动态启停

### 阶段四：测试与优化（1-2周）

1. 单元测试覆盖
2. 集成测试
3. 性能优化
4. 文档完善

---

## 五、预期效果

1. **消除假接口**：所有API返回真实数据或明确的"未启用"状态
2. **统一错误处理**：所有API遵循统一的响应格式
3. **模块化架构**：支持动态启用/禁用模块
4. **健康监控**：实时了解系统各模块状态
5. **符合ISA-18.2**：报警管理符合国际标准
