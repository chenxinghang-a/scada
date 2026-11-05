"""最小可用 MQTT 3.1.1 broker：零依赖，只为「真协议线」自测/联调服务。

## 为什么不装 amqtt

本机没有 mosquitto/emqx，`amqtt` 也没装。装一个新依赖进 `.venv` 会牵动
主应用的依赖面（amqtt 依赖 transitions/websockets 等一串），而这里需要的
协议子集很小：CONNECT / SUBSCRIBE / UNSUBSCRIBE / PUBLISH / PINGREQ /
DISCONNECT，QoS 0/1，通配符，retained。

自己写还有一个更重要的理由：**retained 语义必须可控**。项目刚修过
「retained 消息被当成实时值」的缺陷（`采集层/mqtt_client.py` 的
`msg.retain` 透传），要复现/回归这个场景，就需要能精确地「先发 retained、
再让新订阅者连上」—— 用第三方 broker 反而不好构造。

## 实现范围（明确不做的事）

* 不做持久化会话：`clean_session=0` 也按「会话不保留」处理，CONNACK 里
  `session_present=0`。paho 客户端会在 `_on_connect` 里重新订阅，不影响采集。
* 不做 TLS / 认证 / 共享订阅 / `$SYS`。
* 不做 QoS 2 的完整四次握手（收到 QoS2 PUBLISH 时按 QoS1 语义处理并回
  PUBREC/PUBCOMP，避免客户端挂住）。

## 线程模型

`ThreadingTCPServer`，一个连接一个线程；订阅表和 retained 表由一把 RLock 保护。
周期发布在独立线程里，读 `DeviceDataHub.snapshot()` 后广播。
"""

from __future__ import annotations

import itertools
import json
import logging
import socketserver
import struct
import threading
from typing import Any

from core.safe_numeric import try_finite_float
from 模拟服务端.rest_gateway import build_nested

logger = logging.getLogger(__name__)

# MQTT 控制报文类型（高 4 位）
_CONNECT = 1
_CONNACK = 2
_PUBLISH = 3
_PUBACK = 4
_PUBREC = 5
_PUBREL = 6
_PUBCOMP = 7
_SUBSCRIBE = 8
_SUBACK = 9
_UNSUBSCRIBE = 10
_UNSUBACK = 11
_PINGREQ = 12
_PINGRESP = 13
_DISCONNECT = 14

# CONNACK 返回码
_RC_ACCEPTED = 0
_RC_BAD_PROTOCOL = 1
_RC_BAD_CLIENT_ID = 2
_RC_BAD_CREDENTIALS = 4


def topic_matches(topic_filter: str, topic: str) -> bool:
    """MQTT 通配符匹配：`+` 匹配一层，`#` 匹配剩余所有层（含零层）。"""
    f_parts = topic_filter.split('/')
    t_parts = topic.split('/')
    for i, part in enumerate(f_parts):
        if part == '#':
            # 'a/#' 也要匹配 'a' 本身，所以必须先判 '#' 再判越界
            return True
        if i >= len(t_parts):
            return False
        if part != '+' and part != t_parts[i]:
            return False
    return len(f_parts) == len(t_parts)


def _encode_length(n: int) -> bytes:
    """MQTT 剩余长度变长编码（每字节 7 位，最高位为续位）。"""
    out = bytearray()
    while True:
        digit = n % 128
        n //= 128
        if n > 0:
            digit |= 0x80
        out.append(digit)
        if n == 0:
            return bytes(out)


def _utf8(value: str) -> bytes:
    raw = value.encode('utf-8')
    return struct.pack('>H', len(raw)) + raw


def _read_utf8(body: bytes, pos: int) -> tuple[str, int]:
    """读一个 MQTT UTF-8 字符串（2 字节长度前缀）。

    BUG-092：**必须有边界检查**。原来直接 `struct.unpack('>H', body[pos:pos+2])`
    并在切片上做 decode —— 截断/畸形报文会让 `body[pos:pos+2]` 短于 2 字节
    （struct.error），或 `pos+length` 越界读到半截 UTF-8，单个畸形 PUBLISH
    就能把整条客户端处理协程打崩。
    """
    if pos + 2 > len(body):
        raise ValueError('UTF-8 长度字段越界')
    length = struct.unpack('>H', body[pos:pos + 2])[0]
    pos += 2
    if pos + length > len(body):
        raise ValueError(f'UTF-8 字符串越界（需要 {length} 字节，剩余 {len(body) - pos}）')
    return body[pos:pos + length].decode('utf-8', 'replace'), pos + length


def _read_binary(body: bytes, pos: int) -> tuple[bytes, int]:
    """读一个二进制字段（2 字节长度前缀）。边界检查同 `_read_utf8`。"""
    if pos + 2 > len(body):
        raise ValueError('二进制长度字段越界')
    length = struct.unpack('>H', body[pos:pos + 2])[0]
    pos += 2
    if pos + length > len(body):
        raise ValueError(f'二进制字段越界（需要 {length} 字节，剩余 {len(body) - pos}）')
    return body[pos:pos + length], pos + length


class _ClientSession:
    """一个客户端连接：订阅表 + 发送锁。"""

    def __init__(self, sock: Any, addr: Any):
        self.sock = sock
        self.addr = addr
        self.client_id = ''
        self.subscriptions: dict[str, int] = {}
        #: (topic, payload, qos, retain)；异常断开时由 broker 代为发布
        self.will: tuple[str, bytes, int, bool] | None = None
        self.clean_session = True
        self.clean_disconnect = False
        self._send_lock = threading.Lock()

    def send(self, data: bytes) -> bool:
        with self._send_lock:
            try:
                self.sock.sendall(data)
                return True
            except OSError as exc:
                # 客户端已断开：连接清理由 handler 的 finally 负责，
                # 这里只记 debug —— 广播路径上每 1 秒都会撞到一次
                logger.debug("MQTT 发送失败 %s: %s", self.addr, exc)
                return False

    def close(self) -> None:
        # 关闭路径上的 OSError 只可能是「对端已经关了」，重复关闭无害，
        # 记日志反而会在批量断开时刷屏
        try:
            self.sock.shutdown(2)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class _BrokerTCPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        broker: MqttSimBroker = self.server.broker  # type: ignore[attr-defined]
        session = _ClientSession(self.request, self.client_address)
        try:
            self._serve_session(broker, session)
        except (ConnectionError, OSError) as exc:
            logger.debug("MQTT 连接中断 %s: %s", self.client_address, exc)
        except ValueError as exc:
            logger.warning("MQTT 报文非法 %s: %s", self.client_address, exc)
        finally:
            broker._drop_session(session)

    def _serve_session(self, broker: MqttSimBroker, session: _ClientSession) -> None:
        while True:
            packet = self._read_packet()
            if packet is None:
                return
            header, body = packet
            ptype = header >> 4
            flags = header & 0x0F

            if ptype == _CONNECT:
                if not self._on_connect(broker, session, body):
                    return
            elif ptype == _SUBSCRIBE:
                self._on_subscribe(broker, session, body)
            elif ptype == _UNSUBSCRIBE:
                self._on_unsubscribe(broker, session, body)
            elif ptype == _PUBLISH:
                self._on_publish(broker, session, flags, body)
            elif ptype == _PINGREQ:
                session.send(bytes([_PINGRESP << 4, 0]))
            elif ptype == _DISCONNECT:
                session.clean_disconnect = True
                return
            elif ptype == _PUBREL:
                # QoS2 握手的第三步：只回 PUBCOMP（本 broker 按 QoS1 语义投递）
                pid = struct.unpack('>H', body[:2])[0] if len(body) >= 2 else 0
                session.send(bytes([_PUBCOMP << 4, 2]) + struct.pack('>H', pid))
            else:
                logger.warning("MQTT 不支持的控制报文 type=%d，忽略", ptype)

    # ---- 报文收发 ----

    def _read_packet(self) -> tuple[int, bytes] | None:
        first = self._recv_exact(1)
        if first is None:
            return None
        header = first[0]

        remaining = 0
        multiplier = 1
        for _ in range(4):
            raw = self._recv_exact(1)
            if raw is None:
                return None
            digit = raw[0]
            remaining += (digit & 0x7F) * multiplier
            if not digit & 0x80:
                break
            multiplier *= 128
        else:
            raise ValueError('剩余长度超过 4 字节')

        body = self._recv_exact(remaining) if remaining else b''
        if body is None:
            return None
        return header, body

    def _recv_exact(self, size: int) -> bytes | None:
        buf = b''
        while len(buf) < size:
            chunk = self.request.recv(size - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    # ---- 各控制报文 ----

    def _on_connect(self, broker: MqttSimBroker, session: _ClientSession,
                    body: bytes) -> bool:
        # round 369：**先校验报文完整性**。原来的 `body[pos]` 是无保护下标，
        # 半包/恶意帧（例如只有协议名的残缺 CONNECT）直接抛 IndexError，
        # 由上层 `except Exception` 吞掉 —— broker 看起来"没反应"，
        # 而现场以为是网络问题。
        try:
            pos = 0
            proto, pos = _read_utf8(body, pos)
            if pos + 4 > len(body):
                raise ValueError('CONNECT 报文过短')
            level = body[pos]
            pos += 1
            flags = body[pos]
            pos += 1
            pos += 2  # keepalive：本 broker 不做空闲踢线

            client_id, pos = _read_utf8(body, pos)
            session.clean_session = bool(flags & 0x02)

            username = None
            password = None
            if flags & 0x04:
                will_topic, pos = _read_utf8(body, pos)
                will_payload, pos = _read_binary(body, pos)
                session.will = (will_topic, will_payload, (flags >> 3) & 0x03,
                                bool(flags & 0x20))
            if flags & 0x80:
                username, pos = _read_utf8(body, pos)
            if flags & 0x40:
                password, pos = _read_binary(body, pos)
        except (ValueError, IndexError, struct.error) as exc:
            logger.warning("MQTT CONNECT 报文非法（%s），断开: %s",
                           exc, self.client_address)
            return False

        if proto not in ('MQTT', 'MQIsdp') or level not in (3, 4):
            session.send(bytes([_CONNACK << 4, 2, 0, _RC_BAD_PROTOCOL]))
            logger.warning("MQTT 协议版本不支持: %s level=%d", proto, level)
            return False

        # round 369：**配置了凭据就校验**。
        # 原来解析出 username/password 后直接丢掉、无条件回 ACCEPTED ——
        # 采集端把 MQTT 密码配错也能连上，"凭据配置是否正确"在模拟环境里
        # 根本验证不了。默认 (None, None) = 不鉴权，保持既有部署行为。
        expected_user = getattr(broker, 'username', None)
        expected_pass = getattr(broker, 'password', None)
        if expected_user is not None or expected_pass is not None:
            ok_user = (expected_user is None) or (username == expected_user)
            ok_pass = (expected_pass is None) or (
                (password.decode('utf-8', 'replace') if isinstance(password, bytes)
                 else password) == expected_pass)
            if not (ok_user and ok_pass):
                session.send(bytes([_CONNACK << 4, 2, 0, _RC_BAD_CREDENTIALS]))
                logger.warning("MQTT 凭据校验失败: client=%s user=%r",
                               client_id, username)
                return False

        if not client_id:
            if not session.clean_session:
                # 规范要求：空 client_id + clean_session=0 必须拒绝
                session.send(bytes([_CONNACK << 4, 2, 0, _RC_BAD_CLIENT_ID]))
                return False
            client_id = f'anon-{id(session)}'

        session.client_id = client_id
        broker._register(session)
        session.send(bytes([_CONNACK << 4, 2, 0, _RC_ACCEPTED]))
        logger.info("MQTT 客户端接入: %s (%s) clean=%s",
                    client_id, self.client_address, session.clean_session)
        return True

    def _on_subscribe(self, broker: MqttSimBroker, session: _ClientSession,
                      body: bytes) -> None:
        # round 369：长度与越界保护（同 CONNECT）
        if len(body) < 2:
            logger.warning("MQTT SUBSCRIBE 报文过短，忽略: %s", self.client_address)
            return
        pid = struct.unpack('>H', body[:2])[0]
        pos = 2
        granted: list[int] = []
        new_filters: list[tuple[str, int]] = []
        try:
            while pos < len(body):
                topic_filter, pos = _read_utf8(body, pos)
                if pos >= len(body):
                    raise ValueError('SUBSCRIBE 缺少 QoS 字节')
                requested = body[pos] & 0x03
                pos += 1
                qos = min(requested, 1)  # 本 broker 最高 QoS 1
                if requested > 1:
                    logger.debug("订阅 %s 请求 QoS%d，按 QoS%d 授权", topic_filter, requested, qos)
                session.subscriptions[topic_filter] = qos
                granted.append(qos)
                new_filters.append((topic_filter, qos))
        except (ValueError, IndexError, struct.error) as exc:
            logger.warning("MQTT SUBSCRIBE 报文非法（%s），已丢弃: %s",
                           exc, self.client_address)
            return

        session.send(bytes([_SUBACK << 4, 2 + len(granted)])
                     + struct.pack('>H', pid) + bytes(granted))

        # 新订阅者要立刻收到 retained —— 这正是「旧值被当实时值」的现场来源，
        # 必须能真实复现，采集层才谈得上正确处理。
        for topic_filter, qos in new_filters:
            broker._deliver_retained(session, topic_filter, qos)

    def _on_unsubscribe(self, broker: MqttSimBroker, session: _ClientSession,
                        body: bytes) -> None:
        if len(body) < 2:
            logger.warning("MQTT UNSUBSCRIBE 报文过短，忽略: %s", self.client_address)
            return
        pid = struct.unpack('>H', body[:2])[0]
        pos = 2
        try:
            while pos < len(body):
                topic_filter, pos = _read_utf8(body, pos)
                session.subscriptions.pop(topic_filter, None)
        except (ValueError, IndexError, struct.error) as exc:
            logger.warning("MQTT UNSUBSCRIBE 报文非法（%s），已丢弃: %s",
                           exc, self.client_address)
            return
        session.send(bytes([_UNSUBACK << 4, 2]) + struct.pack('>H', pid))

    def _on_publish(self, broker: MqttSimBroker, session: _ClientSession,
                    flags: int, body: bytes) -> None:
        qos = (flags >> 1) & 0x03
        retain = bool(flags & 0x01)
        # BUG-092：畸形/截断报文只丢弃这一条，不许穿出 `_on_publish` ——
        # 外层 `handle()` 只捕 ConnectionError/OSError/ValueError，
        # IndexError/struct.error 会一路抛到 `finally` 把整个连接 drop 掉。
        try:
            topic, pos = _read_utf8(body, 0)
            pid = 0
            if qos > 0:
                if pos + 2 > len(body):
                    raise ValueError('PUBLISH 报文缺少 packet id')
                pid = struct.unpack('>H', body[pos:pos + 2])[0]
                pos += 2
        except (ValueError, IndexError, struct.error) as exc:
            logger.warning("MQTT PUBLISH 报文非法（%s），已丢弃: %s",
                           exc, self.client_address)
            return
        payload = body[pos:]

        if qos == 1:
            session.send(bytes([_PUBACK << 4, 2]) + struct.pack('>H', pid))
        elif qos == 2:
            session.send(bytes([_PUBREC << 4, 2]) + struct.pack('>H', pid))

        broker._publish(topic, payload, min(qos, 1), retain=retain, from_client=True)

    # ---- 断开 ----

    def _drop_session(self, broker: MqttSimBroker, session: _ClientSession) -> None:
        broker._drop_session(session)
        session.close()


class _BrokerTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, addr: tuple[str, int], broker: 'MqttSimBroker'):
        self.broker = broker
        super().__init__(addr, _BrokerTCPHandler)


class MqttSimBroker:
    """一个 broker 承载所有 `protocol: mqtt` 设备。"""

    def __init__(self, hub: Any, port: int = 18830, host: str = '127.0.0.1',
                 publish_interval: float = 1.0,
                 username: str | None = None, password: str | None = None):
        self.hub = hub
        self.port = port
        self.host = host
        self.publish_interval = publish_interval
        #: round 369：可选的接入凭据。None = 不鉴权（保持既有部署行为）；
        #: 配了值就强制校验 —— 这样才能验证采集端的 MQTT 凭据配置是否正确。
        self.username = username
        self.password = password

        self._server: _BrokerTCPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._publisher_thread: threading.Thread | None = None
        self._stop = threading.Event()

        #: {client_id: session}
        self._clients: dict[str, _ClientSession] = {}
        #: {topic: (payload, qos)} —— 只保留最后一条，符合 retained 语义
        self._retained: dict[str, tuple[bytes, int]] = {}
        self._lock = threading.RLock()
        self._pid = itertools.count(1)

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> bool:
        try:
            server = _BrokerTCPServer((self.host, self.port), self)
        except OSError as exc:
            logger.error("MQTT 模拟 broker 绑定 %s:%d 失败: %s", self.host, self.port, exc)
            return False

        self._server = server
        self._stop.clear()
        self._server_thread = threading.Thread(
            target=server.serve_forever, kwargs={'poll_interval': 0.2},
            name='mqtt-sim-broker', daemon=True)
        self._server_thread.start()

        self._publisher_thread = threading.Thread(
            target=self._publish_loop, name='mqtt-sim-publisher', daemon=True)
        self._publisher_thread.start()

        logger.info("MQTT 模拟 broker 已就绪: mqtt://%s:%d", self.host, self.port)
        return True

    def stop(self) -> None:
        self._stop.set()
        publisher = self._publisher_thread
        if publisher and publisher.is_alive():
            publisher.join(timeout=5.0)
        self._publisher_thread = None

        server = self._server
        if server is not None:
            server.shutdown()
            server.server_close()
            thread = self._server_thread
            if thread and thread.is_alive():
                thread.join(timeout=5.0)
        self._server = None
        self._server_thread = None

        with self._lock:
            sessions = list(self._clients.values())
            self._clients.clear()
        for session in sessions:
            session.close()
        logger.info("MQTT 模拟 broker 已停止")

    # ------------------------------------------------------------------ 对外发布

    def publish_retained(self, topic: str, payload: bytes) -> None:
        """发布一条 retained 消息（用于构造「旧值」场景）。"""
        self._publish(topic, payload, 0, retain=True, from_client=False)

    def publish(self, topic: str, payload: bytes, qos: int = 0,
                retain: bool = False) -> None:
        self._publish(topic, payload, qos, retain=retain, from_client=False)

    # ------------------------------------------------------------------ 内部

    def _register(self, session: _ClientSession) -> None:
        with self._lock:
            old = self._clients.get(session.client_id)
            self._clients[session.client_id] = session
        if old is not None and old is not session:
            # 同一 client_id 重连：旧连接必须断掉，否则两个 session 抢同一个 id
            logger.info("MQTT client_id 重复，踢掉旧连接: %s", session.client_id)
            old.close()

    def _drop_session(self, session: _ClientSession) -> None:
        with self._lock:
            if self._clients.get(session.client_id) is session:
                self._clients.pop(session.client_id, None)
            will = session.will
            session.will = None

        if will and not session.clean_disconnect:
            topic, payload, qos, retain = will
            logger.info("MQTT 客户端异常断开，发布遗嘱: %s", topic)
            self._publish(topic, payload, min(qos, 1), retain=retain, from_client=True)

    def _publish(self, topic: str, payload: bytes, qos: int, retain: bool,
                 from_client: bool) -> None:
        if retain:
            with self._lock:
                if payload:
                    self._retained[topic] = (payload, qos)
                else:
                    # 空 payload + retain 是规范里的「删除 retained」
                    self._retained.pop(topic, None)

        with self._lock:
            sessions = list(self._clients.values())

        for session in sessions:
            sub_qos = self._match_qos(session, topic)
            if sub_qos is None:
                continue
            # 实时投递不带 retain 标志（规范：retain=1 只用于订阅时的补发），
            # 否则采集层会把每条实时值都当「历史旧值」降级。
            session.send(self._publish_packet(topic, payload, min(qos, sub_qos),
                                              retain=False))

        if from_client:
            logger.debug("MQTT 收到客户端发布: %s (%d bytes)", topic, len(payload))

    @staticmethod
    def _match_qos(session: _ClientSession, topic: str) -> int | None:
        best: int | None = None
        for topic_filter, qos in session.subscriptions.items():
            if topic_matches(topic_filter, topic):
                best = qos if best is None else max(best, qos)
        return best

    def _deliver_retained(self, session: _ClientSession, topic_filter: str,
                          sub_qos: int) -> None:
        with self._lock:
            matches = [(topic, payload, qos) for topic, (payload, qos)
                       in self._retained.items() if topic_matches(topic_filter, topic)]
        for topic, payload, qos in matches:
            session.send(self._publish_packet(topic, payload, min(qos, sub_qos),
                                              retain=True))

    def _publish_packet(self, topic: str, payload: bytes, qos: int,
                        retain: bool) -> bytes:
        header = (_PUBLISH << 4) | (qos << 1) | (1 if retain else 0)
        variable = _utf8(topic)
        if qos > 0:
            variable += struct.pack('>H', next(self._pid) % 65535 or 1)
        body = variable + payload
        return bytes([header]) + _encode_length(len(body)) + body

    # ---- 周期发布 ----

    def _publish_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._publish_once()
            except Exception as exc:  # noqa: BLE001 - 单轮失败不能让发布线程退出
                logger.warning("MQTT 周期发布异常: %s: %s", type(exc).__name__, exc)
            self._stop.wait(self.publish_interval)

    def _publish_once(self) -> None:
        devices = getattr(self.hub, 'devices', {}) or {}
        for device_id, cfg in devices.items():
            if not isinstance(cfg, dict):
                continue
            if str(cfg.get('protocol', '')).lower() != 'mqtt':
                continue
            if cfg.get('enabled') is False:
                continue
            topics = cfg.get('topics') or []
            if not topics:
                continue
            try:
                snap = self.hub.snapshot(device_id) or {}
            except (KeyError, ValueError, RuntimeError) as exc:
                logger.warning("MQTT 设备 %s 读取快照失败: %s", device_id, exc)
                continue

            for entry in topics:
                if not isinstance(entry, dict):
                    continue
                topic = str(entry.get('topic') or '').strip()
                name = entry.get('name') or topic.rsplit('/', 1)[-1]
                if not topic:
                    continue
                value = try_finite_float(snap.get(name))
                if value is None:
                    continue
                payload = json.dumps(
                    build_nested(entry.get('json_path', 'value'), value),
                    ensure_ascii=False).encode('utf-8')
                # 周期值是**实时**数据，不能带 retain：否则后连上的采集端会把
                # 上一轮的值当历史快照，正是 round 342 修的那个坑。
                self.publish(topic, payload, qos=0, retain=False)
