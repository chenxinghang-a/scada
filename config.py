"""
系统配置文件

安全说明:
- SECRET_KEY 和 JWT_SECRET 在生产环境中必须通过环境变量设置。
- 未设置时会生成随机密钥并打印警告（重启后密钥会变，导致已有会话/令牌失效）。
- 部署时请复制 .env.example 为 .env 并填入真实值。
"""

import os
import secrets
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _get_secret(name: str, env_var: str) -> str:
    """
    从环境变量获取密钥；未设置时生成随机值并警告。
    生产环境必须通过环境变量提供，否则重启后密钥会变化。
    """
    value = os.environ.get(env_var)
    if not value:
        value = secrets.token_hex(32)
        logger.warning(
            "%s not set via %s, using random key (will change on restart!)",
            name, env_var,
        )
    return value

# 项目根目录（通过 paths 模块统一管理）
try:
    import paths
    BASE_DIR = paths.PROJECT_ROOT
except ImportError:
    from pathlib import Path
    BASE_DIR = Path(__file__).resolve().parent

# Flask配置
class FlaskConfig:
    # 生产环境必须通过 SECRET_KEY 环境变量设置，否则使用随机值（重启失效）
    SECRET_KEY = _get_secret('SECRET_KEY', 'SECRET_KEY')
    DEBUG = os.environ.get('FLASK_DEBUG', '0') == '1'
    HOST = '127.0.0.1'  # 默认绑定本地，生产环境可改为0.0.0.0
    PORT = 5000          # 模拟模式
    REAL_PORT = 5001     # 真实模式（Chrome 拦截 6000/6666 等端口）

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
    LEVEL = os.environ.get('SCADA_LOG_LEVEL', 'INFO')

    # 日志文件路径
    LOG_DIR = os.environ.get('SCADA_LOG_DIR', str(BASE_DIR / 'logs'))

    # 是否输出JSON格式（用于SIEM集成）
    LOG_JSON = os.environ.get('SCADA_LOG_JSON', 'true').lower() == 'true'

    # 日志轮转大小
    LOG_ROTATION = os.environ.get('SCADA_LOG_ROTATION', '100 MB')

    # 日志保留时间
    LOG_RETENTION = os.environ.get('SCADA_LOG_RETENTION', '30 days')

# Web服务器配置（兼容别名）
WebConfig = FlaskConfig

# 导出配置
class ExportConfig:
    # 导出目录
    EXPORT_DIR = BASE_DIR / 'exports'

    # 支持的导出格式
    FORMATS = ['csv', 'excel', 'json']

# JWT认证配置
class AuthConfig:
    # JWT密钥
    # 生产环境必须通过 JWT_SECRET 环境变量设置，否则使用随机值（重启失效）
    JWT_SECRET = _get_secret('JWT_SECRET', 'JWT_SECRET')

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

    # 默认管理员密码（首次创建admin账户时使用，可通过环境变量覆盖）
    SCADA_ADMIN_PASSWORD = os.environ.get('SCADA_ADMIN_PASSWORD', 'admin123')

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

    # TLS/SSL配置
    TLS_ENABLED = os.environ.get('MQTT_TLS_ENABLED', 'false').lower() == 'true'
    CA_CERT = os.environ.get('MQTT_CA_CERT', '')
    CLIENT_CERT = os.environ.get('MQTT_CLIENT_CERT', '')
    CLIENT_KEY = os.environ.get('MQTT_CLIENT_KEY', '')
    TLS_INSECURE = os.environ.get('MQTT_TLS_INSECURE', 'false').lower() == 'true'

# 安全配置 - 等保2.0
class SecurityConfig:
    """安全配置 - 等保2.0 (GB/T 22239)"""
    # 是否启用安全响应头
    SECURITY_HEADERS = os.environ.get('SECURITY_HEADERS', 'true').lower() == 'true'
    # HSTS max-age（秒）
    HSTS_MAX_AGE = int(os.environ.get('HSTS_MAX_AGE', '31536000'))
    # CSP额外允许的脚本源（逗号分隔）
    CSP_EXTRA_SCRIPTS = [s.strip() for s in os.environ.get('CSP_EXTRA_SCRIPTS', '').split(',') if s.strip()]
    # 速率限制配置 (GB/T 22239 - 防暴力攻击和DDoS)
    RATE_LIMIT_ENABLED = os.environ.get('RATE_LIMIT_ENABLED', 'true').lower() == 'true'
    RATE_LIMIT_DEFAULT = os.environ.get('RATE_LIMIT_DEFAULT', '200 per minute')
    RATE_LIMIT_LOGIN = os.environ.get('RATE_LIMIT_LOGIN', '5 per minute')

# TDengine时序数据库配置
class TDengineConfig:
    HOST = os.environ.get('TDENGINE_HOST', 'localhost')
    PORT = int(os.environ.get('TDENGINE_PORT', 6041))
    USER = os.environ.get('TDENGINE_USER', 'root')
    PASSWORD = os.environ.get('TDENGINE_PASSWORD', '')
    DATABASE = os.environ.get('TDENGINE_DATABASE', 'scada')
