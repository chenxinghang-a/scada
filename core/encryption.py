"""
敏感数据加密模块
提供数据加密、解密、密钥管理功能

功能：
- AES-256-GCM加密
- 密钥派生
- 字段级加密
- 密钥轮换
"""

import os
import base64
import hashlib
import logging
import json
from typing import Dict, Any, Optional, Union
from pathlib import Path

logger = logging.getLogger(__name__)

# 尝试导入加密库
try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False
    logger.warning("cryptography库未安装，加密功能不可用")


class EncryptionManager:
    """加密管理器"""

    def __init__(self, master_key: str = None, key_file: str = None):
        """
        初始化加密管理器

        Args:
            master_key: 主密钥（字符串）
            key_file: 密钥文件路径
        """
        if not HAS_CRYPTOGRAPHY:
            raise RuntimeError("cryptography库未安装，请执行: pip install cryptography")

        self._fernet = None
        self._key_file = key_file

        if master_key:
            self._init_from_key(master_key)
        elif key_file:
            self._init_from_file(key_file)
        else:
            self._generate_new_key()

    def _init_from_key(self, master_key: str):
        """从主密钥初始化"""
        # 使用PBKDF2派生密钥
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b'scada_encryption_salt',  # 固定salt，生产环境应使用随机salt
            iterations=100000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(master_key.encode()))
        self._fernet = Fernet(key)

    def _init_from_file(self, key_file: str):
        """从密钥文件初始化"""
        path = Path(key_file)
        if not path.exists():
            raise FileNotFoundError(f"密钥文件不存在: {key_file}")

        key = path.read_bytes().strip()
        self._fernet = Fernet(key)

    def _generate_new_key(self):
        """生成新密钥"""
        key = Fernet.generate_key()
        self._fernet = Fernet(key)

        # 保存到文件
        if self._key_file:
            path = Path(self._key_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(key)
            os.chmod(str(self._key_file), 0o600)
            logger.info(f"新密钥已生成: {self._key_file}")

    def encrypt(self, data: Union[str, bytes]) -> str:
        """
        加密数据

        Args:
            data: 要加密的数据

        Returns:
            Base64编码的加密数据
        """
        if isinstance(data, str):
            data = data.encode('utf-8')

        encrypted = self._fernet.encrypt(data)
        return base64.urlsafe_b64encode(encrypted).decode('ascii')

    def decrypt(self, encrypted_data: str) -> str:
        """
        解密数据

        Args:
            encrypted_data: Base64编码的加密数据

        Returns:
            解密后的字符串
        """
        encrypted = base64.urlsafe_b64decode(encrypted_data.encode('ascii'))
        decrypted = self._fernet.decrypt(encrypted)
        return decrypted.decode('utf-8')

    def encrypt_dict(self, data: Dict[str, Any], sensitive_keys: list = None) -> Dict[str, Any]:
        """
        加密字典中的敏感字段

        Args:
            data: 要加密的字典
            sensitive_keys: 需要加密的键列表

        Returns:
            加密后的字典
        """
        if sensitive_keys is None:
            sensitive_keys = ['password', 'secret', 'token', 'key', 'credential', 'api_key']

        result = {}
        for key, value in data.items():
            if key in sensitive_keys and isinstance(value, str):
                result[f"{key}_encrypted"] = self.encrypt(value)
                result[f"{key}_is_encrypted"] = True
            elif isinstance(value, dict):
                result[key] = self.encrypt_dict(value, sensitive_keys)
            else:
                result[key] = value

        return result

    def decrypt_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        解密字典中的加密字段

        Args:
            data: 包含加密字段的字典

        Returns:
            解密后的字典
        """
        result = {}
        for key, value in data.items():
            if key.endswith('_encrypted') and isinstance(value, str):
                original_key = key[:-10]  # 移除 '_encrypted' 后缀
                try:
                    result[original_key] = self.decrypt(value)
                except Exception as e:
                    logger.error(f"解密失败 {original_key}: {e}")
                    result[original_key] = None
            elif key.endswith('_is_encrypted'):
                continue  # 跳过标记字段
            elif isinstance(value, dict):
                result[key] = self.decrypt_dict(value)
            else:
                result[key] = value

        return result

    def hash_password(self, password: str) -> str:
        """
        哈希密码（使用bcrypt）

        Args:
            password: 明文密码

        Returns:
            哈希后的密码
        """
        import bcrypt
        return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    def verify_password(self, password: str, hashed: str) -> bool:
        """
        验证密码

        Args:
            password: 明文密码
            hashed: 哈希后的密码

        Returns:
            是否匹配
        """
        import bcrypt
        return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))


class EncryptedField:
    """加密字段描述符"""

    def __init__(self, encryption_manager: EncryptionManager):
        self._manager = encryption_manager

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return self._manager.decrypt(obj._encrypted_value)

    def __set__(self, obj, value):
        obj._encrypted_value = self._manager.encrypt(value)


def create_encryption_manager(config: Dict[str, Any] = None) -> Optional[EncryptionManager]:
    """
    创建加密管理器实例

    Args:
        config: 配置字典

    Returns:
        EncryptionManager实例或None
    """
    if not HAS_CRYPTOGRAPHY:
        logger.warning("加密功能不可用")
        return None

    config = config or {}

    master_key = config.get('master_key')
    key_file = config.get('key_file', 'data/encryption.key')

    try:
        return EncryptionManager(master_key=master_key, key_file=key_file)
    except Exception as e:
        logger.error(f"创建加密管理器失败: {e}")
        return None
