"""
REST HTTP设备客户端模块
通过REST API采集设备数据，支持对接各类智能网关和工业物联网平台

典型使用场景：
- 对接涂鸦/阿里云/华为云等IoT平台的设备API
- 对接智能网关（如研华、MOXA）的HTTP接口
- 对接PLC的内置Web Server（如西门子S7-1200/1500）
- 对接第三方SCADA系统的数据接口
"""

import json
import time
import logging
import threading
import requests
from typing import Dict, List, Any, Optional, Callable
from datetime import datetime

logger = logging.getLogger(__name__)


class RESTDeviceClient:
    """
    REST HTTP设备客户端
    通过HTTP请求采集设备数据，支持轮询和Webhook两种模式
    
    配置示例：
    ```yaml
    devices:
      - id: gateway_01
        name: "智能网关1号"
        protocol: rest
        base_url: "http://192.168.1.200/api"
        endpoints:
          - name: "temperature"
            path: "/sensors/temp"
            method: GET
            json_path: "data.value"
            unit: "°C"
          - name: "humidity"
            path: "/sensors/humi"
            method: GET
            json_path: "data.value"
            unit: "%RH"
        poll_interval: 10
        auth_type: bearer
        auth_token: "your_token_here"
    ```
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化REST设备客户端
        
        Args:
            config: 设备配置字典
        """
        self.config = config
        self.device_id = config.get('id', 'rest_device')
        self.device_name = config.get('name', 'REST设备')
        self.base_url = config.get('base_url', 'http://localhost')
        
        # 端点配置
        self.endpoints = config.get('endpoints', [])
        
        # 轮询间隔
        self.poll_interval = config.get('poll_interval', 10)
        
        # 认证配置
        self.auth_type = config.get('auth_type', 'none')  # none/basic/bearer/api_key
        self.auth_token = config.get('auth_token', '')
        self.auth_username = config.get('auth_username', '')
        self.auth_password = config.get('auth_password', '')
        self.api_key_header = config.get('api_key_header', 'X-API-Key')
        self.api_key_value = config.get('api_key_value', '')
        
        # 自定义请求头
        self.custom_headers = config.get('headers', {})
        
        # 连接状态
        self.connected = False
        
        # 最新数据缓存
        self.latest_data: Dict[str, Dict] = {}
        
        # 数据回调
        self._data_callbacks: List[Callable] = []
        
        # 轮询线程
        self._thread: Optional[threading.Thread] = None
        self._running = False
        
        # HTTP会话（复用连接）
        self._session: Optional[requests.Session] = None
        
        # 统计
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'connected_since': None,
            'last_request_time': None,
            'last_error': None
        }
        
        logger.info(f"REST设备客户端初始化: {self.base_url}")
    
    def add_data_callback(self, callback: Callable):
        """添加数据回调函数"""
        self._data_callbacks.append(callback)
    
    def connect(self) -> bool:
        """
        建立连接（创建HTTP会话，测试连通性）
        
        Returns:
            bool: 连接是否成功
        """
        try:
            self._session = requests.Session()
            self._session.headers.update({'Content-Type': 'application/json'})
            self._session.headers.update(self.custom_headers)
            
            # 设置认证
            self._setup_auth()
            
            # 设置超时
            self._session.timeout = (5, 30)  # (connect_timeout, read_timeout)
            
            # 测试连通性（尝试第一个端点）
            if self.endpoints:
                first_ep = self.endpoints[0]
                url = self.base_url.rstrip('/') + first_ep.get('path', '/')
                resp = self._session.request(
                    method=first_ep.get('method', 'GET'),
                    url=url
                )
                resp.raise_for_status()
            
            self.connected = True
            self.stats['connected_since'] = datetime.now().isoformat()
            logger.info(f"REST设备已连接: {self.base_url}")
            
            # 启动轮询
            self._running = True
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._thread.start()
            
            return True
            
        except Exception as e:
            logger.error(f"REST设备连接失败: {e}")
            self.stats['last_error'] = str(e)
            self.connected = False
            return False
    
    def disconnect(self):
        """断开连接"""
        self._running = False
        if self._session:
            self._session.close()
            self._session = None
        self.connected = False
        logger.info(f"REST设备已断开: {self.device_id}")
    
    def get_latest_data(self) -> Dict[str, Dict]:
        """获取所有端点的最新数据"""
        return dict(self.latest_data)
    
    def read_endpoint(self, endpoint_config: Dict) -> Optional[Any]:
        """
        读取单个端点数据
        
        Args:
            endpoint_config: 端点配置
            
        Returns:
            端点返回的数据
        """
        try:
            url = self.base_url.rstrip('/') + endpoint_config.get('path', '/')
            method = endpoint_config.get('method', 'GET').upper()
            params = endpoint_config.get('params', {})
            body = endpoint_config.get('body')
            
            resp = self._session.request(
                method=method,
                url=url,
                params=params if method == 'GET' else None,
                json=body if method in ('POST', 'PUT', 'PATCH') else None
            )
            resp.raise_for_status()
            
            self.stats['total_requests'] += 1
            self.stats['successful_requests'] += 1
            self.stats['last_request_time'] = datetime.now().isoformat()
            
            # 解析JSON
            data = resp.json()
            
            # 按json_path提取值
            json_path = endpoint_config.get('json_path', '')
            value = self._extract_by_path(data, json_path) if json_path else data
            
            return value
            
        except Exception as e:
            self.stats['total_requests'] += 1
            self.stats['failed_requests'] += 1
            self.stats['last_error'] = str(e)
            logger.error(f"REST请求失败 [{endpoint_config.get('path')}]: {e}")
            return None
    
    def write_endpoint(self, endpoint_config: Dict, value: Any) -> bool:
        """
        写入端点数据（控制命令）
        
        Args:
            endpoint_config: 端点配置
            value: 要写入的值
            
        Returns:
            bool: 是否成功
        """
        try:
            url = self.base_url.rstrip('/') + endpoint_config.get('path', '/')
            body = endpoint_config.get('write_body', {'value': value})
            if callable(body):
                body = body(value)
            
            resp = self._session.request(
                method=endpoint_config.get('write_method', 'POST'),
                url=url,
                json=body
            )
            resp.raise_for_status()
            return True
            
        except Exception as e:
            logger.error(f"REST写入失败: {e}")
            return False
    
    def _setup_auth(self):
        """设置HTTP认证"""
        if self.auth_type == 'bearer':
            self._session.headers['Authorization'] = f'Bearer {self.auth_token}'
        elif self.auth_type == 'basic':
            self._session.auth = (self.auth_username, self.auth_password)
        elif self.auth_type == 'api_key':
            self._session.headers[self.api_key_header] = self.api_key_value
    
    def _poll_loop(self):
        """轮询采集循环"""
        while self._running:
            try:
                for ep in self.endpoints:
                    ep_name = ep.get('name', ep.get('path', 'unknown'))
                    value = self.read_endpoint(ep)
                    
                    if value is not None:
                        self.latest_data[ep_name] = {
                            'value': value,
                            'unit': ep.get('unit', ''),
                            'timestamp': datetime.now().isoformat(),
                            'quality': 'good',
                            'endpoint': ep.get('path', ''),
                            'device_id': self.device_id
                        }
                        
                        # 触发回调
                        for callback in self._data_callbacks:
                            try:
                                callback(self.device_id, ep_name, value, ep.get('unit', ''))
                            except Exception as e:
                                logger.error(f"数据回调异常: {e}")
                    else:
                        # 标记为通信故障
                        self.latest_data[ep_name] = {
                            'value': None,
                            'quality': 'bad',
                            'timestamp': datetime.now().isoformat(),
                            'error': self.stats.get('last_error', 'unknown')
                        }
                        
            except Exception as e:
                logger.error(f"轮询异常: {e}")
            
            time.sleep(self.poll_interval)
    
    @staticmethod
    def _extract_by_path(data: Any, path: str) -> Any:
        """
        从JSON数据中按路径提取值
        
        Args:
            data: JSON数据（已解析）
            path: 点分路径，如 "data.sensors[0].value"
            
        Returns:
            提取到的值
        """
        if not path:
            return data
        
        parts = path.split('.')
        current = data
        
        for part in parts:
            if current is None:
                return None
            
            # 处理数组索引: sensors[0]
            if '[' in part and part.endswith(']'):
                bracket_pos = part.index('[')
                key = part[:bracket_pos]
                idx_str = part[bracket_pos + 1:-1]
                try:
                    idx = int(idx_str)
                except ValueError:
                    return None
                
                # 先按key导航
                if key:
                    if not isinstance(current, dict):
                        return None
                    current = current.get(key)
                
                # 再按索引取值
                if isinstance(current, list) and 0 <= idx < len(current):
                    current = current[idx]
                else:
                    return None
            elif isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        
        return current
