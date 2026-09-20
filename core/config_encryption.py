"""
配置加密工具
对敏感配置值进行AES加密存储，运行时解密使用。

使用方式:
    from core.config_encryption import ConfigEncryptor, ConfigDecryptionError
    encryptor = ConfigEncryptor()
    encrypted = encryptor.encrypt('my-secret-password')
    try:
        decrypted = encryptor.decrypt(encrypted)
    except ConfigDecryptionError as e:
        # 密钥不对 / 密文损坏 / cryptography 未安装 —— 必须显式处理
        raise

失败语义（fail-closed）
-----------------------
`encrypt()` / `decrypt()` 失败一律抛 `ConfigDecryptionError`，**不再返回原文**。
历史实现里解密失败 `return ciphertext`，调用方拿到的是密文（gAAAAA...）却以为
是明文，属于最危险的"静默假成功"。若需要"没有密钥就不加密"的降级，请在调用方
显式判断 `ConfigEncryptor().available`，不要依赖本模块静默降级。
"""

# ============================================================================
# 接线状态：未接线（WIRED = False）
# ============================================================================
# 本模块在生产代码（run.py / 各业务层 / 其它 core 模块）中**没有任何 import 引用**。
# 模块本身可用，但当前没有调用方 —— 也就是说它宣称的这项能力**当前并未生效**。
#
# 为什么保留而不删除：删掉即丢能力，模块本身有测试价值；这里只把「没接线」显式化、
# 可追踪，避免「代码在库里」被误读成「功能在跑」。
#
# 自动化复核（防止本标注过期）：
#   tests/test_core_regressions.py::test_unwired_marker_matches_reality
#   —— 该用例用 AST 扫描全仓库 import。一旦有人把本模块接进生产代码，
#      而这里仍写着 WIRED = False，用例即失败，强制文档与事实同步。
#
# 接线建议（需改 run.py / 各层，core 内部无权自行接线）：
#     在 core/config_manager.py 读取敏感配置项时解密、写入时加密。
# ============================================================================
WIRED = False


import os
import base64
import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class ConfigDecryptionError(RuntimeError):
    """配置加/解密失败（fail-closed）。

    调用方必须处理本异常；**不允许**把"解密失败"降级成返回值 ——
    返回密文原文会让上层把它当明文使用，属于静默失败。
    """


# 尝试导入cryptography
try:
    from cryptography.fernet import Fernet
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


class ConfigEncryptor:
    """配置加密器"""

    def __init__(self, key: Optional[str] = None):
        """
        Args:
            key: 加密密钥（None则从环境变量读取）
        """
        if not HAS_CRYPTO:
            logger.warning("cryptography未安装，配置加密不可用")
            self._fernet = None
            return

        if key is None:
            key = os.environ.get('CONFIG_ENCRYPTION_KEY', '')

        if not key:
            # 生成随机密钥（仅用于开发环境）
            key = Fernet.generate_key().decode()
            logger.warning("使用随机加密密钥（重启后失效），请设置CONFIG_ENCRYPTION_KEY环境变量")

        # 确保密钥是有效的Fernet密钥
        if len(key) == 32:
            # 假设是32字节的hex密钥
            key = base64.urlsafe_b64encode(bytes.fromhex(key)).decode()
        elif len(key) != 44:
            # 从字符串派生密钥
            key = base64.urlsafe_b64encode(
                hashlib.sha256(key.encode()).digest()
            ).decode()

        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    @property
    def available(self) -> bool:
        """加密后端是否可用（cryptography 已安装且密钥有效）。

        调用方若需要"无密钥则不加密"的降级，必须显式判断本属性，
        不要依赖 encrypt/decrypt 静默降级。
        """
        return self._fernet is not None

    def encrypt(self, plaintext: str) -> str:
        """加密字符串

        Raises:
            ConfigDecryptionError: 加密不可用（cryptography 未安装 / 密钥无效）。

        SECURITY: 加密不可用时**不再**静默返回明文 —— 那会让调用方以为密文已落库，
        实际上密钥材料以明文进了配置文件/数据库。
        """
        if not self._fernet:
            raise ConfigDecryptionError(
                "加密不可用（cryptography 未安装或密钥无效），拒绝返回明文")
        try:
            return self._fernet.encrypt(plaintext.encode()).decode()
        except Exception as e:
            logger.error("加密失败: %s", e)
            raise ConfigDecryptionError(f"加密失败: {e}") from e

    def decrypt(self, ciphertext: str) -> str:
        """解密字符串（fail-closed）

        Raises:
            ConfigDecryptionError: 解密不可用或解密失败。

        SECURITY: 解密失败时**绝不返回密文原文**。旧实现在异常分支 `return ciphertext`，
        调用方拿到的是不可用值（看起来像明文密码，实际是 gAAAAA... 密文），
        会当成"解密成功"继续使用 —— 典型静默失败。现在一律抛错，失败可见。
        """
        if not self._fernet:
            raise ConfigDecryptionError(
                "解密不可用（cryptography 未安装或密钥无效），拒绝返回原文")
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except Exception as e:
            logger.error("解密失败: %s", e)
            raise ConfigDecryptionError(f"解密失败: {e}") from e

    def is_encrypted(self, value: str) -> bool:
        """判断值是否已加密

        注意：判定失败与"未加密"是两回事，失败会留 WARNING 以便排查。
        """
        if not value:
            return False
        if not self._fernet:
            logger.warning("加密不可用，无法判定值是否已加密（长度 %d），按未加密处理",
                           len(value))
            return False
        try:
            # Fernet加密的值以gAAAAA开头
            return value.startswith('gAAAAA')
        except Exception as e:
            logger.warning("判定值是否已加密时异常(按未加密处理): %s", e)
            return False


def encrypt_config_value(key: str, value: str, encryptor: ConfigEncryptor = None) -> str:
    """加密配置值"""
    if encryptor is None:
        encryptor = ConfigEncryptor()
    return encryptor.encrypt(value)


def decrypt_config_value(key: str, value: str, encryptor: ConfigEncryptor = None) -> str:
    """解密配置值

    Raises:
        ConfigDecryptionError: 解密失败（fail-closed，不返回原文）。
    """
    if encryptor is None:
        encryptor = ConfigEncryptor()
    return encryptor.decrypt(value)


# 全局实例
config_encryptor = ConfigEncryptor()
