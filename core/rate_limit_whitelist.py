"""
API限流白名单
内部服务和可信IP免限流，支持动态更新。

使用方式:
    from core.rate_limit_whitelist import RateLimitWhitelist
    whitelist = RateLimitWhitelist()
    if whitelist.is_whitelisted(ip, endpoint):
        # 跳过限流
"""

import logging
import threading
import time
from typing import Dict, List, Optional, Set
from ipaddress import ip_address, ip_network

logger = logging.getLogger(__name__)


class RateLimitWhitelist:
    """API限流白名单"""

    def __init__(self):
        self._whitelisted_ips: Set[str] = set()
        self._whitelisted_networks: List[str] = []
        self._whitelisted_endpoints: Set[str] = set()
        self._whitelisted_users: Set[str] = set()
        self._internal_services: Set[str] = set()
        self._lock = threading.Lock()

        # 默认白名单
        self._init_defaults()

    def _init_defaults(self):
        """初始化默认白名单"""
        # 本地回环
        self._whitelisted_ips.update(['127.0.0.1', '::1', 'localhost'])

        # 内部网络
        self._whitelisted_networks.extend([
            '10.0.0.0/8',
            '172.16.0.0/12',
            '192.168.0.0/16',
        ])

        # 健康检查端点
        self._whitelisted_endpoints.update([
            '/api/health/status',
            '/api/health/ping',
        ])

        # 内部服务用户
        self._internal_services.update([
            'health_checker',
            'monitoring',
            'backup_service',
        ])

    def is_whitelisted(
        self,
        ip: str = None,
        endpoint: str = None,
        user_id: str = None,
    ) -> bool:
        """
        检查是否在白名单中

        Args:
            ip: 客户端IP
            endpoint: API端点
            user_id: 用户ID

        Returns:
            是否免限流
        """
        with self._lock:
            # 检查IP
            if ip and self._is_ip_whitelisted(ip):
                return True

            # 检查端点
            if endpoint and endpoint in self._whitelisted_endpoints:
                return True

            # 检查用户
            if user_id:
                if user_id in self._whitelisted_users:
                    return True
                if user_id in self._internal_services:
                    return True

            return False

    def _is_ip_whitelisted(self, ip: str) -> bool:
        """检查IP是否在白名单中"""
        # 精确匹配
        if ip in self._whitelisted_ips:
            return True

        # 网段匹配
        try:
            addr = ip_address(ip)
            for network in self._whitelisted_networks:
                if addr in ip_network(network, strict=False):
                    return True
        except ValueError:
            pass

        return False

    def add_ip(self, ip: str):
        """添加IP到白名单"""
        with self._lock:
            self._whitelisted_ips.add(ip)
            logger.info(f"添加IP白名单: {ip}")

    def remove_ip(self, ip: str):
        """从白名单移除IP"""
        with self._lock:
            self._whitelisted_ips.discard(ip)
            logger.info(f"移除IP白名单: {ip}")

    def add_network(self, network: str):
        """添加网段到白名单"""
        try:
            ip_network(network, strict=False)
            with self._lock:
                self._whitelisted_networks.append(network)
                logger.info(f"添加网段白名单: {network}")
        except ValueError as e:
            logger.error(f"无效的网段: {network}, {e}")

    def add_endpoint(self, endpoint: str):
        """添加端点到白名单"""
        with self._lock:
            self._whitelisted_endpoints.add(endpoint)
            logger.info(f"添加端点白名单: {endpoint}")

    def remove_endpoint(self, endpoint: str):
        """从白名单移除端点"""
        with self._lock:
            self._whitelisted_endpoints.discard(endpoint)

    def add_user(self, user_id: str):
        """添加用户到白名单"""
        with self._lock:
            self._whitelisted_users.add(user_id)
            logger.info(f"添加用户白名单: {user_id}")

    def remove_user(self, user_id: str):
        """从白名单移除用户"""
        with self._lock:
            self._whitelisted_users.discard(user_id)

    def add_internal_service(self, service_name: str):
        """添加内部服务"""
        with self._lock:
            self._internal_services.add(service_name)
            logger.info(f"添加内部服务: {service_name}")

    def get_whitelist(self) -> Dict[str, any]:
        """获取白名单配置"""
        with self._lock:
            return {
                'ips': list(self._whitelisted_ips),
                'networks': self._whitelisted_networks,
                'endpoints': list(self._whitelisted_endpoints),
                'users': list(self._whitelisted_users),
                'internal_services': list(self._internal_services),
            }

    def update_whitelist(self, config: Dict[str, any]):
        """批量更新白名单"""
        with self._lock:
            if 'ips' in config:
                self._whitelisted_ips = set(config['ips'])
            if 'networks' in config:
                self._whitelisted_networks = config['networks']
            if 'endpoints' in config:
                self._whitelisted_endpoints = set(config['endpoints'])
            if 'users' in config:
                self._whitelisted_users = set(config['users'])
            if 'internal_services' in config:
                self._internal_services = set(config['internal_services'])

        logger.info("白名单配置已更新")

    def is_empty(self) -> bool:
        """检查白名单是否为空"""
        with self._lock:
            return (
                len(self._whitelisted_ips) == 0
                and len(self._whitelisted_networks) == 0
                and len(self._whitelisted_endpoints) == 0
                and len(self._whitelisted_users) == 0
            )


# 全局实例
rate_limit_whitelist = RateLimitWhitelist()
