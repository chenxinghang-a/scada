"""
系统配置文件
"""

import os
from pathlib import Path

# 项目根目录
BASE_DIR = Path(__file__).parent

# Flask配置
class FlaskConfig:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'industrial-scada-secret-key')
    DEBUG = True
    HOST = '127.0.0.1'  # 默认绑定本地，生产环境可改为0.0.0.0
    PORT = 5000

# 数据库配置
class DatabaseConfig:
    # SQLite数据库路径
    DB_PATH = BASE_DIR / 'data' / 'scada.db'

    # 数据保留天数
    RETENTION_DAYS = 30

    # 数据压缩间隔（小时）
    COMPRESSION_INTERVAL = 24

# Modbus配置
class ModbusConfig:
    # 默认采集间隔（秒）
    DEFAULT_INTERVAL = 5

    # 连接超时（秒）
    CONNECTION_TIMEOUT = 10

    # 重试次数
    MAX_RETRIES = 3

    # 重试间隔（秒）
    RETRY_INTERVAL = 5

# 报警配置
class AlarmConfig:
    # 报警检查间隔（秒）
    CHECK_INTERVAL = 10

    # 报警记录保留天数
    RETENTION_DAYS = 90

    # 邮件通知配置
    EMAIL_ENABLED = False
    SMTP_SERVER = 'smtp.example.com'
    SMTP_PORT = 587
    SMTP_USERNAME = ''
    SMTP_PASSWORD = ''


# 报警输出配置（声光报警器 + 广播系统）
class AlarmOutputConfig:
    # 声光报警器（Modbus DO控制灯塔+蜂鸣器）
    ENABLED = True
    SIMULATION = True  # True=模拟模式(日志输出), False=硬件模式(Modbus DO)
    DO_MAPPING = {
        'red_light': 0,    # DO0 = 红灯
        'yellow_light': 1, # DO1 = 黄灯
        'green_light': 2,  # DO2 = 绿灯
        'buzzer': 3,       # DO3 = 蜂鸣器
    }
    MODBUS_HOST = '192.168.1.100'
    MODBUS_PORT = 502
    MODBUS_SLAVE_ID = 1


# 工业广播系统配置
class BroadcastConfig:
    ENABLED = True
    SIMULATION = True
    MQTT_BROKER = os.environ.get('PA_BROKER', 'localhost')
    MQTT_PORT = int(os.environ.get('PA_MQTT_PORT', 1883))
    TOPIC_PREFIX = 'pa/'  # 广播MQTT主题前缀
    AREAS = ['车间A', '车间B', '仓库', '办公楼']
    PRESET_TEMPLATES = {
        'alarm_critical': '注意！{area}发生严重报警：{message}，请立即处置！',
        'alarm_warning': '提醒：{area}出现告警：{message}，请关注。',
        'evacuation': '请注意，{area}发生紧急状况，请沿疏散通道撤离！',
        'all_clear': '广播通知，{area}警报解除，恢复正常。',
    }

# 日志配置
class LogConfig:
    # 日志级别
    LEVEL = 'DEBUG'

    # 日志文件路径
    LOG_DIR = BASE_DIR / 'logs'

    # 日志保留天数
    RETENTION_DAYS = 30

# 导出配置
class ExportConfig:
    # 导出目录
    EXPORT_DIR = BASE_DIR / 'exports'

    # 支持的导出格式
    FORMATS = ['csv', 'excel', 'json']

# JWT认证配置
class AuthConfig:
    # JWT密钥
    JWT_SECRET = os.environ.get('JWT_SECRET', 'industrial-scada-jwt-secret-2026')

    # JWT算法
    JWT_ALGORITHM = 'HS256'

    # 访问令牌过期时间（小时）
    JWT_EXPIRATION_HOURS = 24

    # 刷新令牌过期时间（天）
    JWT_REFRESH_DAYS = 7

    # 最大登录失败次数
    MAX_LOGIN_ATTEMPTS = 5

    # 账户锁定时间（分钟）
    LOCKOUT_MINUTES = 30

    # 密码最小长度
    MIN_PASSWORD_LENGTH = 6

# MQTT配置
class MQTTConfig:
    # MQTT Broker地址
    BROKER_HOST = os.environ.get('MQTT_BROKER', 'localhost')

    # MQTT端口
    BROKER_PORT = int(os.environ.get('MQTT_PORT', 1883))

    # 客户端ID前缀
    CLIENT_ID_PREFIX = 'scada_'

    # QoS级别
    QOS = 1

    # 主题前缀
    TOPIC_PREFIX = 'scada/'
