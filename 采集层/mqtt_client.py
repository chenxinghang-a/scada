"""
MQTT数据采集模块
实现MQTT协议的数据采集功能
"""

import json
import ssl
import logging
import threading
from typing import Any, Callable
from datetime import datetime
from core.safe_numeric import to_finite_float, try_finite_float
from config import MQTTConfig

from .adapter_spec import (
    AdapterConfigError,
    AdapterDataError,
    PointSpec,
    map_message,
    parse_point_specs,
)

logger = logging.getLogger(__name__)


def parse_topic_entry(entry: Any) -> tuple[str, int]:
    """解析一条 ``topics`` 配置，返回 ``(topic, qos)``。

    支持两种写法 —— 两种都在本仓库的真实配置里出现过：

    * **dict**（推荐，能带 json_path / name / unit）::

          - name: vibration_x
            topic: factory/motor_group/vibration/x_axis
            json_path: value
            qos: 1

    * **裸字符串**（`配置/devices_real.yaml` 的原始格式）::

          - env/warehouse/temperature

    为什么必须兼容：原实现对每条都直接 ``entry.get('topic')``，
    裸字符串会 ``AttributeError``。在 ``start()`` 里这个异常被外层
    ``except`` 吞掉 → 整个 MQTT 设备**启动失败**，日志只有一句
    「MQTT设备管理器启动失败」，看不出是配置格式的问题；
    在 ``_register_configured_topics()`` 里则只留一行 warning 就跳过，
    结果「配了 2 个主题，一条数据也收不到」。

    两种都是**静默失败**，现场极难定位。统一在这里解析，两处调用点共用。
    """
    if isinstance(entry, dict):
        topic = str(entry.get('topic') or '').strip()
        raw_qos = entry.get('qos', MQTTConfig.QOS)
    elif isinstance(entry, str):
        topic = entry.strip()
        raw_qos = MQTTConfig.QOS
    else:
        logger.warning("无法识别的 topic 配置（已跳过）: %r", entry)
        return '', MQTTConfig.QOS

    # 用 try_int 而不是裸 int()：裸转换只捕 (TypeError, ValueError) 会漏
    # OverflowError（`int(float('inf'))`），而 MQTT 的 qos 来自配置，
    # 一个 `qos: inf` 就能把订阅流程打成 500。项目有静态闸门专门扫这个。
    from core.safe_numeric import try_int
    parsed = try_int(raw_qos)
    qos = MQTTConfig.QOS if parsed is None else parsed
    if qos not in (0, 1, 2):
        logger.warning("topic %s 的 qos=%r 非法，按 %d 处理", topic, raw_qos, MQTTConfig.QOS)
        qos = MQTTConfig.QOS

    return topic, qos

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    logger.warning("paho-mqtt未安装，MQTT功能不可用。请运行: pip install paho-mqtt")


class MQTTClient:
    """
    MQTT数据采集客户端
    支持订阅多个主题，解析JSON数据并转发到数据库

    构造函数统一接受config字典，与其他协议客户端接口一致。
    config格式:
        {
            'id': 'mqtt_device_01',
            'name': 'MQTT IoT传感器',
            'protocol': 'mqtt',
            'host': 'localhost',
            'port': 1883,
            'username': None,
            'password': None,
            'topics': [
                {'topic': 'factory/temperature', 'qos': 1, 'name': 'temperature', 'unit': '°C'}
            ]
        }

    数据回调签名: callback(device_id, register_name, value, unit)
        - 与OPCUA/REST客户端回调签名一致
    """

    def __init__(self, config: dict[str, Any] | None = None, **kwargs):
        """
        初始化MQTT客户端

        Args:
            config: 设备配置字典（推荐方式，与其他协议一致）
                    支持TLS配置键: tls_enabled, ca_cert, client_cert, client_key, tls_insecure
            **kwargs: 兼容旧的独立参数方式 (broker_host, broker_port, username, password)
        """
        if not MQTT_AVAILABLE:
            raise ImportError("paho-mqtt未安装，请运行: pip install paho-mqtt")

        # 统一从config提取参数
        if config:
            self.config = config
            self.device_id = config.get('id', 'mqtt_device')
            self.device_name = config.get('name', 'MQTT设备')
            # 默认值来自 MQTTConfig（此前是硬编码 'localhost'/1883，
            # 改 config.py 的 MQTT_BROKER/MQTT_PORT 完全无效）
            self.broker_host = config.get('host', MQTTConfig.BROKER_HOST)
            self.broker_port = config.get('port', MQTTConfig.BROKER_PORT)
            username = config.get('username')
            password = config.get('password')
            self.topics_config = config.get('topics', [])
            # TLS参数
            self.tls_enabled = config.get('tls_enabled', MQTTConfig.TLS_ENABLED)
            self.ca_cert = config.get('ca_cert') or MQTTConfig.CA_CERT or None
            self.client_cert = config.get('client_cert') or MQTTConfig.CLIENT_CERT or None
            self.client_key = config.get('client_key') or MQTTConfig.CLIENT_KEY or None
            self.tls_insecure = config.get('tls_insecure', MQTTConfig.TLS_INSECURE)
        else:
            # 兼容旧的独立参数方式
            self.config = kwargs
            self.device_id = kwargs.get('client_id', 'mqtt_device')
            self.device_name = kwargs.get('client_id', 'MQTT设备')
            self.broker_host = kwargs.get('broker_host', MQTTConfig.BROKER_HOST)
            self.broker_port = kwargs.get('broker_port', MQTTConfig.BROKER_PORT)
            username = kwargs.get('username')
            password = kwargs.get('password')
            self.topics_config = []
            # TLS参数
            self.tls_enabled = kwargs.get('tls_enabled', MQTTConfig.TLS_ENABLED)
            self.ca_cert = kwargs.get('ca_cert') or MQTTConfig.CA_CERT or None
            self.client_cert = kwargs.get('client_cert') or MQTTConfig.CLIENT_CERT or None
            self.client_key = kwargs.get('client_key') or MQTTConfig.CLIENT_KEY or None
            self.tls_insecure = kwargs.get('tls_insecure', MQTTConfig.TLS_INSECURE)

        self.client_id = f'{MQTTConfig.CLIENT_ID_PREFIX}{self.device_id}'

        # 创建MQTT客户端（兼容 paho-mqtt v1.x 和 v2.x）
        try:
            self.client = mqtt.Client(client_id=self.client_id, protocol=mqtt.MQTTv311,
                                       clean_session=False)
        except TypeError:
            # paho-mqtt v2.x: clean_session -> clean_start, 参数位置变更
            self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
                                       client_id=self.client_id, protocol=mqtt.MQTTv311,
                                       clean_session=False)

        # 设置认证
        if username:
            self.client.username_pw_set(username, password)

        # LWT（遗嘱消息）：设备离线时 Broker 自动发布
        lwt_topic = f'{MQTTConfig.TOPIC_PREFIX}{self.device_id}/status'
        lwt_payload = json.dumps({'device_id': self.device_id, 'status': 'offline',
                                   'timestamp': datetime.now().isoformat()})
        self.client.will_set(lwt_topic, lwt_payload, qos=MQTTConfig.QOS, retain=True)

        # 自动重连延迟（指数退避：1s → 2s → 4s → ... → 60s）
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)

        # 设置回调
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.on_subscribe = self._on_subscribe

        # 数据回调函数
        self._data_callbacks: list[Callable[..., Any]] = []

        # 订阅主题列表: {topic: qos}
        self._subscriptions: dict[str, int] = {}

        # 最新数据缓存（与模拟客户端行为一致）
        self.latest_data: dict[str, dict[str, Any]] = {}

        # 连接状态
        self.connected = False
        self._lock = threading.Lock()

        # 统计信息
        self.stats: dict[str, Any] = {
            'messages_received': 0,
            'messages_parsed': 0,
            'parse_errors': 0,
            'connected_since': None,
            'last_message_time': None
        }

        logger.info(f"MQTT客户端初始化: {self.broker_host}:{self.broker_port} [{self.device_id}]")

    def add_data_callback(self, callback: Callable[..., Any]):
        """添加数据回调函数（签名: callback(device_id, name, value, unit)）"""
        self._data_callbacks.append(callback)

    def connect(self) -> bool:
        """
        连接到MQTT Broker

        注意: paho-mqtt的connect()只发起TCP连接，MQTT握手在loop_start()的后台线程中完成。
        连接成功后on_connect回调才会设置self.connected=True。

        Returns:
            bool: connect()调用是否成功（不代表MQTT握手完成）
        """
        try:
            # TLS/SSL配置
            if self.tls_enabled:
                tls_context = ssl.create_default_context()

                if self.ca_cert:
                    tls_context.load_verify_locations(self.ca_cert)
                # round 353：**没填 ca_cert ≠ 可以不校验证书**。
                # `create_default_context()` 默认就用系统根证书校验；
                # 原来 else 分支直接关掉主机名校验并设 CERT_NONE，
                # 于是「标准 CA 签发的证书、只是没额外指定 ca_cert」这种
                # 最常见的情况变成完全不校验 —— 中间人可截获全部采集报文。
                # 要放宽只能用 `tls_insecure`（下面那条，会告警）。

                if self.client_cert and self.client_key:
                    tls_context.load_cert_chain(self.client_cert, self.client_key)

                if self.tls_insecure:
                    logger.warning(
                        "MQTT 采集端 TLS: tls_insecure=true，**已关闭证书与主机名校验**"
                        " —— 仅可用于测试环境")
                    tls_context.check_hostname = False
                    tls_context.verify_mode = ssl.CERT_NONE

                self.client.tls_set_context(tls_context)
                logger.info("MQTT TLS已启用")

            # 把 device_config 里的 topics 登记进订阅表。
            #
            # **这里必须登记，否则配了 topics 也永远收不到数据。**
            # paho 的 connect() 只发起 TCP 连接，真正的 MQTT 握手在 loop_start()
            # 的后台线程里完成，所以此刻 self.connected 仍是 False —— 直接调用
            # subscribe() 只会把主题记进 _subscriptions 而不发送 SUBSCRIBE。
            # 真正的订阅动作发生在 _on_connect 回调里（它按 _subscriptions 补发）。
            # 原实现两处都没做：DataCollector._setup_push_device 只调用
            # add_data_callback() + connect()，从没调用过 subscribe()，
            # 于是 MQTT 设备"连上了但零数据"，且没有任何报错。
            self._register_configured_topics()

            logger.info(f"正在连接MQTT Broker: {self.broker_host}:{self.broker_port}")
            self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            self.client.loop_start()
            return True
        except Exception as e:
            logger.error(f"MQTT连接失败: {e}")
            self.stats['last_error'] = str(e)
            return False

    def _register_configured_topics(self) -> int:
        """把 ``config['topics']`` 登记到本地订阅表，供 ``_on_connect`` 自动订阅。

        幂等：重复调用不会重复计数，已登记的主题会被更新为最新 QoS。

        Returns:
            int: 本次新登记（此前不在订阅表中）的主题数
        """
        added = 0
        for topic_config in self.topics_config or []:
            topic, qos = parse_topic_entry(topic_config)
            if not topic:
                continue
            with self._lock:
                if topic not in self._subscriptions:
                    added += 1
                self._subscriptions[topic] = qos

        if added:
            logger.info(
                f"已登记 {added} 个待订阅主题（连接建立后自动订阅）: "
                f"{list(self._subscriptions.keys())}"
            )
        elif self._subscriptions:
            logger.debug(
                f"主题已登记，无需重复: {list(self._subscriptions.keys())}"
            )
        return added

    def disconnect(self):
        """断开MQTT连接"""
        try:
            # 先发 DISCONNECT 再停网络线程，否则报文发不出去。
            self.client.disconnect()
            self.client.loop_stop()
            logger.info("MQTT连接已断开")
        except Exception as e:
            logger.error(f"MQTT断开失败: {e}")
        finally:
            # 无论底层 loop_stop/disconnect 是否报错，状态都必须收敛为"未连接"。
            # 原实现把 self.connected = False 放在 try 内：一旦 loop_stop 抛异常，
            # 状态就停留在 True，上层会继续往可能已经死掉的连接 publish。
            self.connected = False

    def subscribe(self, topic: str, qos: int = 1) -> bool:
        """订阅主题"""
        with self._lock:
            self._subscriptions[topic] = qos

        if self.connected:
            result, mid = self.client.subscribe(topic, qos)
            if result == mqtt.MQTT_ERR_SUCCESS:
                logger.info(f"已订阅主题: {topic} (QoS: {qos})")
                return True
            else:
                logger.error(f"订阅失败: {topic}, 错误码: {result}")
                return False
        else:
            logger.info(f"主题已记录，连接后自动订阅: {topic}")
            return True

    def unsubscribe(self, topic: str):
        """取消订阅"""
        if topic in self._subscriptions:
            del self._subscriptions[topic]

        if self.connected:
            self.client.unsubscribe(topic)
            logger.info(f"已取消订阅: {topic}")

    def publish(self, topic: str, payload: dict[str, Any], qos: int = 1) -> bool:
        """发布消息"""
        if not self.connected:
            logger.warning("MQTT未连接，无法发布")
            return False

        try:
            message = json.dumps(payload, ensure_ascii=False)
            result = self.client.publish(topic, message, qos=qos)
            return result.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception as e:
            logger.error(f"MQTT发布失败: {e}")
            return False

    def get_latest_data(self) -> dict[str, dict[str, Any]]:
        """获取最新数据缓存（round 370：持锁快照）"""
        with self._lock:
            return dict(self.latest_data)

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息（与其他协议客户端接口统一）"""
        with self._lock:
            subscriptions = list(self._subscriptions.keys())
        return {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'connected': self.connected,
            'broker': f'{self.broker_host}:{self.broker_port}',
            'client_id': self.client_id,
            'subscriptions': subscriptions,
            **self.stats
        }

    def get_status(self) -> dict[str, Any]:
        """获取客户端状态"""
        with self._lock:
            subscriptions = list(self._subscriptions.keys())
        return {
            'connected': self.connected,
            'broker': f'{self.broker_host}:{self.broker_port}',
            'client_id': self.client_id,
            'subscriptions': subscriptions,
            'stats': self.stats.copy()
        }

    # ---- paho-mqtt 回调 ----

    def _on_connect(self, client, userdata, flags, rc):
        """连接成功回调"""
        if rc == 0:
            self.connected = True
            self.stats['connected_since'] = datetime.now().isoformat()
            logger.info(f"MQTT连接成功: {self.broker_host}:{self.broker_port}")

            # 发布上线状态（retained）
            status_topic = f'{MQTTConfig.TOPIC_PREFIX}{self.device_id}/status'
            status_payload = json.dumps({'device_id': self.device_id, 'status': 'online',
                                          'timestamp': datetime.now().isoformat()})
            client.publish(status_topic, status_payload, qos=MQTTConfig.QOS, retain=True)

            # 自动订阅已记录的主题（QoS 1）
            # round 373：`subscribe()/unsubscribe()` 是持 `_lock` 改这张表的，
            # 而 `_on_connect` 跑在 MQTT 网络线程里 —— 直接迭代活字典会抛
            # RuntimeError，重连时表现为"订阅恢复一半"。
            lock = getattr(self, '_lock', None)      # 兼容 `__new__` 构造的测试替身
            if lock is not None:
                with lock:
                    subscription_items = list(self._subscriptions.items())
            else:
                subscription_items = list(self._subscriptions.items())
            for topic, qos in subscription_items:
                subscribed = client.subscribe(topic, qos)
                # paho 返回 (result, mid)；测试桩可能返回 None/MagicMock。
                if isinstance(subscribed, tuple) and len(subscribed) == 2:
                    result, _mid = subscribed
                else:
                    result = subscribed
                if result == mqtt.MQTT_ERR_SUCCESS:
                    logger.info(f"自动订阅: {topic} (QoS {qos})")
                else:
                    logger.error(f"自动订阅失败: {topic}, 错误码: {result}")
        else:
            logger.error(f"MQTT连接失败，返回码: {rc}")

    def _on_disconnect(self, client, userdata, rc):
        """断开连接回调"""
        self.connected = False
        if rc != 0:
            logger.warning(f"MQTT意外断开，返回码: {rc}")
        else:
            logger.info("MQTT已断开连接")

    def _on_message(self, client, userdata, msg):
        """消息接收回调"""
        try:
            self.stats['messages_received'] += 1
            self.stats['last_message_time'] = datetime.now().isoformat()

            topic = msg.topic
            payload = msg.payload.decode('utf-8')

            # ⚠️ round 342：**retained 消息不是"刚发生的事件"**。
            #
            # MQTT Broker 会把每个 topic 的**最后一条**带 retain 标志的消息
            # 存下来，新订阅者一连上就立刻收到 —— 那可能是几天前的值。
            # 原实现不看 `msg.retain`，还给这类消息打 `now()` 的时间戳，
            # 于是几天前的旧值以「刚更新 / GOOD 质量码」入库，
            # 并参与报警判定与联锁计算。
            # 这里把标记透传下去，由 `_collect_from_cache` 降级为 stale。
            retain = bool(getattr(msg, 'retain', False))
            if retain:
                self.stats.setdefault('retained_messages', 0)
                self.stats['retained_messages'] += 1

            try:
                data = json.loads(payload)
                if not isinstance(data, dict):
                    # 标量/数组 JSON 不对象报文，退回原始值解析（如 "12.5"）
                    self._process_raw_message(topic, payload, retain=retain)
                else:
                    self.stats['messages_parsed'] += 1
                    self._process_message(topic, data, retain=retain)
            except json.JSONDecodeError:
                self._process_raw_message(topic, payload, retain=retain)

        except Exception as e:
            logger.error(f"处理MQTT消息失败: {e}")
            self.stats['parse_errors'] += 1

    def _on_subscribe(self, client, userdata, mid, granted_qos):
        """订阅成功回调"""
        logger.debug(f"订阅确认: mid={mid}, QoS={granted_qos}")

    def _process_message(self, topic: str, data: dict[str, Any], retain: bool = False):
        """
        处理JSON格式消息

        支持的消息格式:
        1. {"device_id": "xxx", "register": "temperature", "value": 25.5, "unit": "°C"}
        2. {"temperature": 25.5, "pressure": 0.101}  (自动从topic提取device_id)
        3. {"device_id": "xxx", "data": {"temperature": 25.5, "pressure": 0.101}}
        """
        meta = self._topic_meta(topic)

        # round 376：**声明式映射优先**。主题配置里写了 `points:` 时，
        # 走与 HTTP 适配器完全相同的映射引擎（路径/类型/枚举/scale/records）——
        # 一个主题可以出多个点位，不再需要为新接口写解析代码。
        declared = self._declared_topic_map()
        if topic in declared:
            compiled = declared[topic]
            if compiled is None:
                # 编译期已记 ERROR（字段拼错等），这里不能回退到启发式解析 ——
                # 否则会把错误配置"猜"出一堆乱七八糟的点位名，比直接不采更难查。
                return
            records_path, points = compiled
            try:
                mapped = map_message(data, points, records_path)
            except AdapterDataError as exc:
                logger.warning("MQTT %s 声明式映射失败: %s", topic, exc)
                self.stats['parse_errors'] = self.stats.get('parse_errors', 0) + 1
                return
            device_id = self._topic_device_id(topic)
            for name, item in mapped.items():
                self._notify_callbacks(device_id, name, item['value'],
                                       item.get('unit', ''), retain=retain)
            return

        # round 370（BUG-025）：**先看主题配置的 json_path / name**。
        # `topics` 里常写成 `json_path: value` + `name: vibration_x`，
        # 而载荷是 `{"value": 0.12, "ts": ...}` —— 原实现完全不看这两个字段，
        # 把整个 JSON 的每个数值键都当成点位，点位名变成 "value"/"ts"，
        # 与设备配置里的寄存器名对不上，数据被采集器丢弃。
        json_path = str(meta.get('json_path') or '').strip()
        if json_path:
            cur: Any = data
            for part in json_path.split('.'):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    cur = None
                    break
            numeric = try_finite_float(cur)
            if numeric is not None:
                self._notify_callbacks(
                    self._topic_device_id(topic),
                    meta.get('name') or topic,
                    numeric,
                    meta.get('unit', '') or '',
                    retain=retain)
                return
            logger.debug("MQTT %s 的 json_path=%r 取不到有限数值，回退按整包解析",
                         topic, json_path)

        # 格式1: 完整格式
        if 'device_id' in data and 'register' in data:
            device_id = data['device_id']
            register_name = data['register']
            # round 370（BUG-025）：`{"value": null}` 时 `float(None)` 抛 TypeError，
            # 整条消息被外层吞掉 —— 一个空值让同一包里的其它点位全部丢失。
            value = try_finite_float(data.get('value'))
            if value is None:
                logger.debug("MQTT 消息 value 非有限数值，已跳过: %s", topic)
                return
            unit = data.get('unit', '')
            self._notify_callbacks(device_id, register_name, value, unit, retain=retain)

        # 格式3: 嵌套数据格式
        elif 'device_id' in data and 'data' in data:
            device_id = data['device_id']
            points_dict = data.get('data')
            if not isinstance(points_dict, dict):
                logger.warning("MQTT %s 的 data 字段不是对象，已跳过", topic)
                return
            for register_name, raw in points_dict.items():
                value = try_finite_float(raw)
                if value is not None:
                    self._notify_callbacks(device_id, register_name, value, '',
                                           retain=retain)

        # 格式2: 简单键值对
        else:
            # round 369：**设备 ID 必须取本客户端绑定的设备**。
            # 原来写 `topic.split('/')[-1]` —— 对 `factory/motor_01/vibration_x`
            # 这种多级主题，取出来的是**点位名**而不是设备 ID，采集器按
            # device_id 查不到设备，数据被静默丢弃或写到错误设备上。
            device_id = self._topic_device_id(topic)
            unit = self._topic_unit(topic)
            for register_name, value in data.items():
                # round 382：bool 是 int 子类，不能抹成 1.0/0.0 丢布尔语义；
                # 同时必须挡 NaN/Inf，否则会污染报警与统计。
                if isinstance(value, bool):
                    numeric = float(int(value))
                else:
                    numeric = try_finite_float(value)
                if numeric is not None:
                    self._notify_callbacks(device_id, register_name, numeric,
                                           unit, retain=retain)

    def _topic_meta(self, topic: str) -> dict:
        """按 `topics_config` 取该主题的点位元数据（name/unit/json_path）。

        `topics_config` 是**列表**（元素为 dict 或裸字符串，见
        `parse_topic_entry` 的说明），不是 topic → meta 的字典。
        """
        entries = getattr(self, 'topics_config', None) or []
        if isinstance(entries, dict):       # 兼容旧式写法
            meta = entries.get(topic)
            return meta if isinstance(meta, dict) else {}
        for entry in entries:
            if isinstance(entry, dict) and str(entry.get('topic') or '').strip() == topic:
                return entry
        return {}

    def _declared_topic_map(self) -> dict[str, tuple[str | None, tuple[PointSpec, ...]] | None]:
        """编译 `topics[].points` 声明式映射；配置非法时值为 None（显式失败）。

        缓存按 `(id(topics_config), len(topics_config))` 失效，测试里改完配置
        直接调 `_process_message` 也能生效。
        """
        entries = getattr(self, 'topics_config', None) or []
        cache_key = (id(entries), len(entries))
        if getattr(self, '_topic_map_key', None) == cache_key:
            return self._topic_map_cache

        compiled: dict[str, tuple[str | None, tuple[PointSpec, ...]] | None] = {}
        for entry in entries:
            if not isinstance(entry, dict) or 'points' not in entry:
                continue
            topic = str(entry.get('topic') or '').strip()
            if not topic:
                logger.error("MQTT topics 里有 points 但缺少 topic，已忽略: %r", entry)
                continue
            try:
                points = parse_point_specs(entry.get('points'),
                                           f'topics[{topic}].points')
                records = entry.get('records')
                if records is not None and (not isinstance(records, str)
                                            or not records.strip()):
                    raise AdapterConfigError(
                        f'topics[{topic}].records 必须是非空字符串')
                compiled[topic] = (records.strip() if records else None, points)
            except AdapterConfigError as exc:
                logger.error("MQTT 主题 %s 的声明式映射配置非法（该主题不采集）: %s",
                             topic, exc)
                compiled[topic] = None

        self._topic_map_key = cache_key
        self._topic_map_cache = compiled
        return compiled

    def _topic_device_id(self, topic: str) -> str:
        """设备 ID：主题配置显式声明 > 客户端绑定设备 > 主题末段。"""
        meta = self._topic_meta(topic)
        explicit = str(meta.get('device_id') or '').strip()
        if explicit:
            return explicit
        device_id = getattr(self, 'device_id', '') or ''
        if device_id:
            return device_id
        return topic.split('/')[-1] if '/' in topic else topic

    def _topic_unit(self, topic: str) -> str:
        return self._topic_meta(topic).get('unit', '') or ''

    def _process_raw_message(self, topic: str, payload: str, retain: bool = False):
        """处理非JSON格式消息"""
        try:
            meta = self._topic_meta(topic)
            device_id = self._topic_device_id(topic)
            unit = meta.get('unit', '') or ''
            if '=' in payload:
                parts = payload.split('=', 1)   # 值里允许出现 '='（token/base64）
                if len(parts) == 2:
                    # 配置里给了点位名就用配置的（主题末段常常是通道名，
                    # 不是点位名）；否则用报文里的键
                    register_name = meta.get('name') or parts[0].strip()
                    value = to_finite_float(parts[1].strip(), 'mqtt_value')
                    self._notify_callbacks(device_id, register_name, value,
                                           unit, retain=retain)
            else:
                value = to_finite_float(payload.strip(), 'mqtt_value')
                register_name = meta.get('name') or 'value'
                # round 369：**单值分支也必须带 retain**。
                # 漏传会让 Broker 缓存的陈旧 retained 值被当成新鲜实时值。
                self._notify_callbacks(device_id, register_name, value,
                                       unit, retain=retain)
        except (ValueError, IndexError):
            logger.debug(f"无法解析原始消息: {topic} = {payload}")

    def _notify_callbacks(self, device_id: str, register_name: str,
                         value: Any, unit: str, retain: bool = False):
        """
        通知所有回调函数
        签名统一: callback(device_id, register_name, value, unit)
        与OPCUA/REST客户端的DataCollector.on_data回调签名一致
        """
        # 更新数据缓存。`retained` 必须带上：消费方据此判断
        # 「这是 Broker 缓存的旧状态，不是刚采集的实时值」。
        # round 370（BUG-025）：paho 回调线程写、采集线程读，必须加锁。
        # 原来 `self._lock` 建了却从没用过 —— 并发下 `dict` 迭代/写入
        # 可能抛 "dictionary changed size during iteration"，
        # 表现是偶发丢数据，现场极难复现。
        payload = {
            'value': value,
            'unit': unit,
            'timestamp': datetime.now().isoformat(),
            'retained': bool(retain),
        }
        lock = getattr(self, '_lock', None)
        if lock is None:            # 兼容 `__new__` 构造的测试替身
            self.latest_data[register_name] = payload
        else:
            with lock:
                self.latest_data[register_name] = payload

        with self._lock:
            callbacks = list(self._data_callbacks)
        for callback in callbacks:
            try:
                callback(device_id, register_name, value, unit)
            except Exception as e:
                logger.error(f"数据回调执行失败: {e}")


class MQTTDeviceManager:
    """
    MQTT设备管理器
    管理多个MQTT设备的数据采集
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.client = None
        self._running = False

    def start(self, data_callback: Callable[..., Any]) -> bool:
        """启动MQTT数据采集"""
        try:
            self.client = MQTTClient(self.config)
            self.client.add_data_callback(data_callback)

            # 订阅主题
            topics = self.config.get('topics', [])
            for topic_config in topics:
                topic, qos = parse_topic_entry(topic_config)
                if topic:
                    self.client.subscribe(topic, qos)

            if self.client.connect():
                self._running = True
                logger.info("MQTT设备管理器启动成功")
                return True
            else:
                logger.error("MQTT设备管理器启动失败")
                return False

        except Exception as e:
            logger.error(f"MQTT设备管理器启动失败: {e}")
            return False

    def stop(self):
        """停止MQTT数据采集"""
        if self.client:
            self.client.disconnect()
            self._running = False
            logger.info("MQTT设备管理器已停止")

    def get_status(self) -> dict[str, Any]:
        """获取状态"""
        if self.client:
            return self.client.get_status()
        return {'connected': False, 'broker': 'N/A'}
