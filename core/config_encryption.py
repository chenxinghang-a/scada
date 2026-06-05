"""
配置加密工具
对敏感配置值进行AES加密存储，运行时解密使用。

使用方式:
    from core.config_encryption import ConfigEncryptor
    encryptor = ConfigEncryptor()
    encrypted = encryptor.encrypt('my-secret-password')
    decrypted = encryptor.decrypt(encrypted)
"""

import os
import base64
import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)

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

    def encrypt(self, plaintext: str) -> str:
        """加密字符串"""
        if not self._fernet:
            logger.warning("加密不可用，返回明文")
            return plaintext
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """解密字符串"""
        if not self._fernet:
            logger.warning("解密不可用，返回原文")
            return ciphertext
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except Exception as e:
            logger.error("解密失败: %s", e)
            return ciphertext

    def is_encrypted(self, value: str) -> bool:
        """判断值是否已加密"""
        if not value or not self._fernet:
            return False
        try:
            # Fernet加密的值以gAAAAA开头
            return value.startswith('gAAAAA')
        except Exception:
            return False


def encrypt_config_value(key: str, value: str, encryptor: ConfigEncryptor = None) -> str:
    """加密配置值"""
    if encryptor is None:
        encryptor = ConfigEncryptor()
    return encryptor.encrypt(value)


def decrypt_config_value(key: str, value: str, encryptor: ConfigEncryptor = None) -> str:
    """解密配置值"""
    if encryptor is None:
        encryptor = ConfigEncryptor()
    return encryptor.decrypt(value)


# 全局实例
config_encryptor = ConfigEncryptor()
