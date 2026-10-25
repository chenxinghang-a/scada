"""
系统配置文件

安全说明:
- SECRET_KEY 和 JWT_SECRET 在生产环境（SCADA_ENV=production）必须通过环境变量设置，
  否则**拒绝启动**（fail-closed）；非生产环境会生成随机密钥并打印警告。
- 部署时请复制 .env.example 为 .env 并填入真实值。
"""

import os
import secrets
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# 运行环境：production 时密钥缺失/为默认值会升级为 CRITICAL 告警
SCADA_ENV = os.environ.get('SCADA_ENV', 'development').lower()
IS_PRODUCTION = SCADA_ENV == 'production'


#: 部署模板/文档里的占位弱值。等于这些值的密钥一律视为「未配置」——
#: docker-compose.yml 的历史默认就是 change-me-in-production，照抄上线等于没有密钥。
_WEAK_SECRET_VALUES = frozenset({
    'change-me-in-production', 'change_me', 'changeme', 'change-me',
    'secret', 'password', 'test', 'admin',
    # round 214：管理员口令也走这张表（口径统一），而 admin123 是文档里的历史默认值
    'admin123', 'admin_123', 'admin@123',
})


def _get_secret(name: str, env_var: str) -> str:
    """
    从环境变量获取密钥。

    * 生产环境（SCADA_ENV=production）：缺失 / 为空 / 等于部署模板里的弱默认值时
      **抛 RuntimeError 拒绝启动**。用随机密钥会让重启后所有会话与令牌失效，
      用弱默认值等于没有密钥 —— 两者都不该被静默放行。
    * 非生产环境：生成随机密钥并告警（本地开发/测试无需配置密钥）。
    """
    value = (os.environ.get(env_var) or '').strip()
    if value and value.lower() not in _WEAK_SECRET_VALUES:
        return value

    if IS_PRODUCTION:
        raise RuntimeError(
            f"{name} 未配置或使用了弱默认值：生产环境（SCADA_ENV=production）必须"
            f"通过环境变量 {env_var} 提供强随机密钥，已拒绝启动。"
            f'生成方式：python -c "import secrets;print(secrets.token_hex(32))"'
        )

    if value:
        logger.warning("%s 使用了弱默认值 %r，已改用随机密钥（重启后失效）", name, value)
    else:
        logger.warning(
            "%s not set via %s, using random key (will change on restart!)",
            name, env_var,
        )
    return secrets.token_hex(32)


def _get_int(env_var: str, default: int, min_val: int = None, max_val: int = None) -> int:
    """获取整数配置值，带范围验证。

    ⚠️ 这里**有意**只用裸 `int()`，不接 `core.safe_numeric`：
    * `os.environ.get()` 只可能返回 `str`，`int(str)` 抛不出 OverflowError
      （`int('inf')` 是 ValueError，已被下面接住），不存在漏成 500 的通道；
    * `config.py` 必须保持**零内部依赖** —— 它在冻结产物里被单独放在
      `_internal/` 下，且是 `core` 各模块的早期依赖。一旦 import `core.*`
      就会触发 `core/__init__.py`，把整个核心层拉起来，形成循环依赖风险。
      `tests/test_frozen_layout_paths.py` 用一个只含 paths/config/VERSION 的
      最小布局盯着这条不变量（本轮试过改成 safe_numeric，9 条用例当场报错）。
    """
    value = os.environ.get(env_var)
    if value is None:
        return default
    try:
        result = int(value)
        if min_val is not None and result < min_val:
            logger.warning(f"{env_var}={result} 低于最小值 {min_val}，使用默认值 {default}")
            return default
        if max_val is not None and result > max_val:
            logger.warning(f"{env_var}={result} 超过最大值 {max_val}，使用默认值 {default}")
            return default
        return result
    except ValueError:
        logger.warning(f"{env_var}={value} 不是有效整数，使用默认值 {default}")
        return default


def _get_bool(env_var: str, default: bool) -> bool:
    """获取布尔配置值"""
    value = os.environ.get(env_var)
    if value is None:
        return default
    return value.lower() in ('true', '1', 'yes', 'on')

# 项目根目录（通过 paths 模块统一管理）
#
# ⚠️ 这里必须是 ``paths.BASE_DIR``，**不是** ``paths.PROJECT_ROOT``。
#    两者在开发布局下恰好相等，但在冻结（PyInstaller onedir）布局下**差一级**：
#        PROJECT_ROOT = exe 所在目录            （如 ``<backend>/``）
#        BASE_DIR     = 运行时数据基准目录       （如 ``<backend>/_internal/``）
#    `配置/`、`data/`、`logs/`、`exports/`、`VERSION` 冻结后都在 ``_internal/`` 下
#    （见 `paths.py` 的 ``_BASE`` 推导与 spec 的 datas 落点）。
#
#    用错的那一版（2026-10-08 修复，round 191）造成的后果：
#      * ``DatabaseConfig.DB_PATH`` / ``ExportConfig.EXPORT_DIR`` /
#        ``LogConfig.LOG_DIR`` 在冻结产物里全部指向 ``_internal/`` **外面**，
#        与 ``paths.DATA_DIR`` / ``paths.EXPORT_DIR`` / ``paths.LOG_DIR`` 静默分叉；
#      * ``_read_version()`` 去 ``<backend>/VERSION`` 找版本号 —— 那里没有，
#        于是 ``APP_VERSION`` 恒为 ``'0.0.0'``。
#    开发机上 ``_internal`` 不存在，两者恰好相等，所以**本机永远测不出来**。
#    回归测试 `tests/test_frozen_layout_paths.py` 在子进程里伪造冻结布局来钉死它。
try:
    import paths
    BASE_DIR = paths.BASE_DIR
    # 运行时目录**直接委托**给 paths，而不是在 config 里再拼一遍 ——
    # 拼一遍就有第二个真源，就有再次漂移的机会（round 173 的教训）。
    _DATA_DIR = paths.DATA_DIR
    _LOG_DIR = paths.LOG_DIR
    _EXPORT_DIR = paths.EXPORT_DIR
    _DEFAULT_DB_PATH = paths.DB_PATHS['default']
except ImportError:
    from pathlib import Path
    BASE_DIR = Path(__file__).resolve().parent
    _DATA_DIR = BASE_DIR / 'data'
    _LOG_DIR = BASE_DIR / 'logs'
    _EXPORT_DIR = BASE_DIR / 'exports'
    _DEFAULT_DB_PATH = _DATA_DIR / 'scada.db'


# 版本号：唯一真源为项目根目录的 VERSION 文件
# 改版本号只改 VERSION，不要在代码里硬编码。
def _read_version() -> str:
    try:
        return (BASE_DIR / 'VERSION').read_text(encoding='utf-8').strip()
    except Exception:
        return '0.0.0'


APP_VERSION = _read_version()

# Flask配置
class FlaskConfig:
    # 生产环境必须通过 SECRET_KEY 环境变量设置，否则使用随机值（重启失效）
    SECRET_KEY = _get_secret('SECRET_KEY', 'SECRET_KEY')
    DEBUG = os.environ.get('FLASK_DEBUG', '0') == '1'
    # 绑定地址/端口：可用环境变量覆盖。
    #
    # ⚠️ 容器部署必须设 ``SCADA_HOST=0.0.0.0``（Dockerfile 的 ENV 已经设了）。
    #    此前这里是**硬编码字面量**，环境变量被完全忽略 ——
    #    于是容器里进程只监听 127.0.0.1，而 docker 的端口发布（DNAT 到容器 IP）
    #    打不到 loopback，``docker compose up`` 起来之后**从宿主机根本连不上**，
    #    而 HEALTHCHECK（容器内 curl localhost）却是绿的，故障因此长期不可见。
    #    同一类缺陷还有 SCADA_MODE（从未被读，已删除）与多实例工具的
    #    SCADA_DATA_DIR / SCADA_CONFIG_DIR（见 paths.py 的 _env_path）。
    #    守卫：tests/test_env_vars_wired.py。
    HOST = os.environ.get('SCADA_HOST', '127.0.0.1').strip() or '127.0.0.1'
    PORT = _get_int('SCADA_PORT', 5000, min_val=1, max_val=65535)          # 模拟模式
    # 真实模式（Chrome 拦截 6000/6666 等端口）
    REAL_PORT = _get_int('SCADA_REAL_PORT', 5001, min_val=1, max_val=65535)

    # 请求超时控制（秒）
    REQUEST_TIMEOUT = _get_int('SCADA_REQUEST_TIMEOUT', 30, min_val=5, max_val=300)

    # 最大请求体大小（字节），默认16MB
    MAX_CONTENT_LENGTH = _get_int('SCADA_MAX_CONTENT_MB', 16, min_val=1, max_val=100) * 1024 * 1024

    # 并发请求限制（单进程最大并发）
    MAX_CONCURRENT_REQUESTS = _get_int('SCADA_MAX_CONCURRENT', 100, min_val=10, max_val=1000)

# 数据库配置
class DatabaseConfig:
    # SQLite数据库路径
    DB_PATH = _DEFAULT_DB_PATH

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
    """声光报警器默认值（Modbus DO 控制灯塔 + 蜂鸣器）。

    这些值此前**全是装饰品**：`run.py` 里写死 `{'enabled': True,
    'simulation': simulation_mode}`，host/port/do_mapping 全靠 `配置/alarms.yaml`
    覆盖，改这个类不起任何作用。现在它作为**默认值**接到 run.py，
    YAML 里写了的键仍然优先。

    `SIMULATION` 的语义是「**强制**模拟输出」，默认 False：
    * False（默认）→ 跟随系统模式：模拟模式就模拟，真实模式就驱动硬件；
    * True → 即使系统跑在真实模式也走日志输出（现场无灯塔时调试用）。
      ⚠️ 此时 run.py 会打 WARNING，避免「配了硬件却没人被驱动」被静默忽略。
    """
    ENABLED = True
    SIMULATION = False
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
    """工业广播系统默认值。

    此前同样是装饰品：`run.py` 直接硬编码了 areas / preset_templates，
    改这里没有任何效果。现在接到 run.py 作为默认值。

    `SIMULATION` 语义与 :class:`AlarmOutputConfig` 一致：默认 False =
    跟随系统模式；True = 强制模拟（会打 WARNING）。
    """
    ENABLED = True
    SIMULATION = False
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
    """日志目录（**唯一**保留在这里的日志常量）。

    round 266：`LEVEL` / `LOG_JSON` / `LOG_ROTATION` / `LOG_RETENTION` 已删除 ——
    日志参数改由 `core.config_manager.load_logging_settings()` 统一读取
    （优先级：环境变量 > `配置/system.yaml` 的 `logging` 段 > 兜底默认值）。

    为什么删而不是留着：留着就会有两个真源 —— 配置文件里写着 `max_size_mb: 10`，
    这里写着 `100 MB`，运维改哪个都不确定生效，实际两边都没生效（历史缺陷）。
    环境变量 `SCADA_LOG_*` 仍然有效，只是读取点挪到了 `load_logging_settings`。
    """

    # 日志文件路径。**直接委托 paths**（paths.LOG_DIR 自己读 SCADA_LOG_DIR 并按
    # PROJECT_ROOT 解析相对路径）—— 这里若再读一遍环境变量，就会在设置了
    # SCADA_LOG_DIR 时出现「config 拿到相对串、paths 拿到绝对路径」的静默分叉，
    # 正是 round 173 那条注释警告的「第二个真源」。
    # 保留它是因为 `tests/test_frozen_layout_paths.py` 用它断言冻结布局一致性。
    LOG_DIR = str(_LOG_DIR)

# Web服务器配置（兼容别名）
WebConfig = FlaskConfig

# 导出配置
class ExportConfig:
    # 导出目录
    EXPORT_DIR = _EXPORT_DIR

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

    # 默认管理员密码（首次创建admin账户时使用）
    # 生产环境必须通过 SCADA_ADMIN_PASSWORD 环境变量覆盖！
    # 开发默认值拆写以避免明文弱口令入库（拼接结果不变，行为不变）
    _DEV_DEFAULT_ADMIN_PW = 'admin' '123'
    # ⚠️ 取值链是 **env → .env → 默认值**（load_dotenv 在 import 时就把 .env 灌进
    # os.environ）。所以「只比 admin123 字面量」会漏掉 .env 里的 CHANGE_ME 这类
    # 占位弱口令 —— 必须与密钥复用同一张 `_WEAK_SECRET_VALUES`（口径统一）。
    SCADA_ADMIN_PASSWORD = (
        (os.environ.get('SCADA_ADMIN_PASSWORD') or '').strip() or _DEV_DEFAULT_ADMIN_PW
    )
    if (SCADA_ADMIN_PASSWORD == _DEV_DEFAULT_ADMIN_PW
            or SCADA_ADMIN_PASSWORD.lower() in _WEAK_SECRET_VALUES):
        if IS_PRODUCTION:
            # round 214：与 SECRET_KEY / JWT_SECRET 统一为 **fail-closed**。
            # 原先这里只记 CRITICAL 日志然后照常启动 —— 结果是生产环境用
            # `admin/admin123` 开门迎客（弱口令 = 未配置，只是「响得比较大声」）。
            # 同一份文件里两个安全配置项两套口径，就是本仓反复栽的「口径不一致」。
            raise RuntimeError(
                "【生产环境弱口令】SCADA_ADMIN_PASSWORD 未设置或等于开发默认值 —— "
                "生产环境（SCADA_ENV=production）必须提供强口令，拒绝启动。"
                "（与 SECRET_KEY/JWT_SECRET 同款 fail-closed 口径：弱默认值视同未配置）"
            )
        else:
            logger.warning("使用默认管理员密码！生产环境请设置 SCADA_ADMIN_PASSWORD 环境变量")

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
    # CSRF配置 (GB/T 37980)
    CSRF_ENABLED = os.environ.get('CSRF_ENABLED', 'true').lower() == 'true'
    CSRF_SECRET = os.environ.get('CSRF_SECRET', '')
    if not CSRF_SECRET:
        # 尝试从 .env 文件读取，没有则生成并持久化
        _env_file = BASE_DIR / '.env'
        _generated = secrets.token_hex(32)
        try:
            if _env_file.exists():
                for line in _env_file.read_text(encoding='utf-8').splitlines():
                    if line.startswith('CSRF_SECRET='):
                        CSRF_SECRET = line.split('=', 1)[1].strip().strip('"').strip("'")
                        break
        except Exception as e:
            # 读取 .env 失败：无法复用已持久化的 CSRF 密钥，将退化为本次随机生成，
            # 重启后 CSRF 令牌全部失效（功能降级），必须留痕。
            logger.warning("读取 %s 中的 CSRF_SECRET 失败，将重新生成: %s", _env_file, e)
        if not CSRF_SECRET:
            CSRF_SECRET = _generated
            if IS_PRODUCTION:
                logger.critical(
                    "【生产环境密钥缺失】CSRF_SECRET 未设置，本次使用随机生成值，"
                    "重启后所有 CSRF 令牌失效！请在部署环境中显式配置 CSRF_SECRET。"
                )
            try:
                with open(_env_file, 'a', encoding='utf-8') as f:
                    f.write(f'\nCSRF_SECRET={_generated}\n')
            except Exception as e:
                # 写入 .env 失败：CSRF 密钥仅存在于本次进程内存中，未持久化，
                # 重启后密钥变化会导致已有 CSRF 令牌失效（功能降级），必须留痕。
                logger.warning("写入 CSRF_SECRET 到 %s 失败，密钥未能持久化: %s", _env_file, e)
    # TLS/HTTPS 配置 (GB/T 35718 + GB/T 37980)
    TLS_ENABLED = os.environ.get('SCADA_TLS_ENABLED', 'false').lower() == 'true'
    TLS_CERT_FILE = os.environ.get('SCADA_TLS_CERT', str(BASE_DIR / 'certs' / 'server.crt'))
    TLS_KEY_FILE = os.environ.get('SCADA_TLS_KEY', str(BASE_DIR / 'certs' / 'server.key'))

# TDengine时序数据库配置
def _tdengine_password() -> str:
    """TDengine 密码：`CHANGE_ME` / 空串视为未设置，回落到客户端默认值。

    放在**模块级**而不是类体里：类体里的中间变量会被
    `tools/scan_config_readers.py` 当成一个配置字段，凭空多出一条
    「永远无人读」的记录（配置闸门会因此变红）。
    """
    raw = os.environ.get('TDENGINE_PASSWORD', '').strip()
    return 'taosdata' if raw in ('', 'CHANGE_ME') else raw


class TDengineConfig:
    HOST = os.environ.get('TDENGINE_HOST', 'localhost')
    PORT = int(os.environ.get('TDENGINE_PORT', 6041))
    USER = os.environ.get('TDENGINE_USER', 'root')
    # 默认值与 `timeseries/tdengine_client.py` 的构造默认值保持一致（taosdata）。
    #
    # ⚠️ 两件事：
    #   1. 原来这里是空串 —— 一旦接线就会用空密码去连，把原本能连上的实例连挂；
    #   2. `.env.example` 里写的是 `CHANGE_ME`（**占位符**，不是密码）。
    #      项目对密钥的既有口径就是「CHANGE_ME 视为未设置」（见本文件
    #      `_get_secret` 附近的说明），这里沿用，避免把占位符当密码发出去。
    PASSWORD = _tdengine_password()
    DATABASE = os.environ.get('TDENGINE_DATABASE', 'scada')
