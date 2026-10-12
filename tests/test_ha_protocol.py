"""HAManager 协议级测试 —— HA 心跳 UDP 链路（真 socket + 真线程 + 真 HMAC）。

来源：对端（WorkBuddy 主应用）在外部审计整改期间做的独立交叉验证脚本，
已在其 3698168 提交上真跑 6/6。并入时做了两处加固：

1. **补「监听就绪」探针**（关键）。草稿里的负向用例（「错误密钥的心跳被丢弃」）
   只发一个坏包再断言计数不变 —— 若监听还没 bind 上，这条断言会因为
   **压根没收到**而变绿（空绿）。现在每条接收链用例都先发一个**合法**包并等到
   被接受，证明链路真的通了，再做负向断言。
2. `_verify_hmac` 打桩改用 `monkeypatch`（异常安全、自动还原），而不是手工 save/restore。

覆盖（此前 `core/ha_manager.py` 的协议链路为 0 覆盖）：
  * 接收链：合法签名接受 / 错误密钥丢弃 / 无 signature 字段丢弃
  * 接管链：对端从未出现且超时 → 接管为 PRIMARY；心跳新鲜 → 不接管（反向对照）
  * 判别力自检：把 `_verify_hmac` 打桩为恒真后，错误签名包**应被接受**

⚠️ **同机限制**：本协议端口模型是「发送目标端口 = 自己 bind 的端口」
（`peer_port` 同一字段两用），为**两台不同机器**设计；同机双实例会 bind 冲突
（`0.0.0.0` 使 127.0.0.2 隔离也无效）。故这里用「单实例 + 手工构造 UDP 包」的
半端到端；完整双机演练需两台机器或两个 netns。部署前提见 docs/DEPLOYMENT.md §3.1。
"""
import hashlib
import hmac
import json
import socket
import threading
import time

from core.ha_manager import HAManager, HARole

SECRET = 'ha-protocol-test-secret'
WRONG_SECRET = 'a-different-secret'
PROBE_SECRET = SECRET


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _free_udp_port() -> int:
    """取一个空闲 UDP 端口（bind(0) 读回端口后释放；测试内极小竞态可接受）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_until(pred, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode('utf-8'), payload.encode('utf-8'),
                    hashlib.sha256).hexdigest()


def _heartbeat_bytes(node_id: str, role: str, priority: int, secret: str,
                     *, with_signature: bool = True) -> bytes:
    ts = time.time()
    payload = f'{node_id}:{role}:{priority}:{ts}'
    hb = {
        'type': 'heartbeat', 'node_id': node_id, 'role': role,
        'priority': priority, 'timestamp': ts, 'state': 'active',
    }
    if with_signature:
        hb['signature'] = _sign(payload, secret)
    return json.dumps(hb).encode()


def _make_node(port: int, **kw) -> HAManager:
    params = dict(
        node_id='A', priority=100, peer_address='127.0.0.1',
        peer_port=port, shared_secret=SECRET,
        heartbeat_interval=0.3, heartbeat_timeout=1.5,
    )
    params.update(kw)   # ⚠️ 用 update 覆盖而不是 **kw 并列：否则同键会 TypeError
    return HAManager(**params)


def _start_listener_ready(ha: HAManager, port: int):
    """只起 `_listen_loop`（不调 start()：避免心跳发送线程「自收」污染断言），
    并用合法包探到「已被接受」才算就绪。返回 (线程, socket)。

    没有这一步，负向断言（丢弃类）会因为「监听还没起来」而假绿。
    """
    ha._running = True
    ha._started_at = time.time()
    t = threading.Thread(target=ha._listen_loop, daemon=True, name='ha-listen-test')
    t.start()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        sock.sendto(_heartbeat_bytes('PROBE', 'primary', 1, PROBE_SECRET),
                    ('127.0.0.1', port))
        if _wait_until(lambda: ha.stats['heartbeats_received'] > 0, timeout=0.3):
            return t, sock
    sock.close()
    _stop(ha, t)
    raise AssertionError('HA 监听端口未就绪 —— 无法进行有效的接收链断言')


def _stop(ha: HAManager, *threads: threading.Thread) -> None:
    ha._running = False
    for t in threads:
        t.join(timeout=3)


# ---------------------------------------------------------------------------
# 1. 接收链（真 UDP）
# ---------------------------------------------------------------------------

class TestReceiveChain:
    def test_valid_signed_heartbeat_is_accepted(self):
        """合法签名的心跳必须被接受：状态更新 + 对端记录 + 计数 +1。"""
        port = _free_udp_port()
        ha = _make_node(port)
        t, s = _start_listener_ready(ha, port)
        try:
            rx_before = ha.stats['heartbeats_received']
            s.sendto(_heartbeat_bytes('B', 'primary', 90, SECRET), ('127.0.0.1', port))
            assert _wait_until(lambda: ha.stats['heartbeats_received'] > rx_before), \
                '合法签名心跳未被接受'
            assert ha._last_peer_heartbeat > 0
            assert ha._peer_node is not None and ha._peer_node.node_id == 'B'
        finally:
            s.close()
            _stop(ha, t)

    def test_wrong_secret_heartbeat_is_dropped(self):
        """错误密钥签名的心跳必须被丢弃（反向对照：证明不是「收什么都记」）。

        就绪探针已保证监听在收 —— 所以这里的「计数不变」是真的被丢弃，
        不是「压根没收到」。
        """
        port = _free_udp_port()
        ha = _make_node(port)
        t, s = _start_listener_ready(ha, port)
        try:
            rx_before = ha.stats['heartbeats_received']
            for _ in range(3):      # 多发几次，排除单包丢失的偶发
                s.sendto(_heartbeat_bytes('EVIL', 'primary', 999, WRONG_SECRET),
                         ('127.0.0.1', port))
                time.sleep(0.1)
            time.sleep(0.3)
            assert ha.stats['heartbeats_received'] == rx_before, \
                '错误密钥的心跳被接受了 —— 验签链路失效'
            assert getattr(ha._peer_node, 'node_id', None) != 'EVIL'
        finally:
            s.close()
            _stop(ha, t)

    def test_heartbeat_without_signature_is_dropped(self):
        """缺 signature 字段的心跳必须被丢弃（第一道防线，独立于验签）"""
        port = _free_udp_port()
        ha = _make_node(port)
        t, s = _start_listener_ready(ha, port)
        try:
            rx_before = ha.stats['heartbeats_received']
            for _ in range(3):
                s.sendto(_heartbeat_bytes('N', 'standby', 1, SECRET, with_signature=False),
                         ('127.0.0.1', port))
                time.sleep(0.1)
            time.sleep(0.3)
            assert ha.stats['heartbeats_received'] == rx_before
        finally:
            s.close()
            _stop(ha, t)


# ---------------------------------------------------------------------------
# 2. 接管链（真跑 _heartbeat_loop；发送打桩避免自收）
# ---------------------------------------------------------------------------

class TestFailoverChain:
    def _run_loop(self, ha, seconds: float):
        ha._send_heartbeat = lambda: None      # 打桩：不发（否则自收会让 peer_ever_seen=True）
        ha._running = True
        t = threading.Thread(target=ha._heartbeat_loop, daemon=True, name='ha-hb-test')
        t.start()
        try:
            time.sleep(seconds)
        finally:
            _stop(ha, t)
        return t

    def test_peer_never_seen_triggers_failover(self):
        """配了对端却从未见到 + 超过超时窗口 → 必须接管为 PRIMARY。"""
        ha = _make_node(_free_udp_port(), heartbeat_interval=0.2, heartbeat_timeout=0.6)
        ha._started_at = time.time() - 100       # 已运行很久（peer_never_seen 条件）
        ha._set_role(HARole.STANDBY)

        self._run_loop(ha, 1.6)

        assert ha.role == HARole.PRIMARY, \
            '对端从未出现且超时，却仍未接管 —— 备机将永远待命（历史缺陷形状）'

    def test_fresh_heartbeat_does_not_failover(self):
        """刚收到过心跳（未超时）→ 必须保持 STANDBY（反向对照，防「无脑接管」）。"""
        ha = _make_node(_free_udp_port(), heartbeat_interval=0.2, heartbeat_timeout=10.0)
        ha._started_at = time.time() - 100
        ha._set_role(HARole.STANDBY)
        ha._last_peer_heartbeat = time.time()    # 心跳新鲜

        self._run_loop(ha, 1.6)

        assert ha.role == HARole.STANDBY, '心跳新鲜却发生了接管'


# ---------------------------------------------------------------------------
# 3. 判别力自检
# ---------------------------------------------------------------------------

class TestDiscriminative:
    def test_wrong_secret_drop_judgement_has_teeth(self, monkeypatch):
        """判据判别力：把 `_verify_hmac` 打桩为恒真后，错误签名包**应当被接受**。

        若不接受，说明「错误签名被丢弃」那条判据测的不是验签结果（判据无判别力）。
        与 test_wrong_secret_heartbeat_is_dropped 正反互证：两条都绿 = 判据有效。
        """
        port = _free_udp_port()
        ha = _make_node(port)
        monkeypatch.setattr(HAManager, '_verify_hmac',
                            lambda self, payload, signature: True)
        t, s = _start_listener_ready(ha, port)
        try:
            rx_before = ha.stats['heartbeats_received']
            s.sendto(_heartbeat_bytes('EVIL', 'primary', 999, WRONG_SECRET),
                     ('127.0.0.1', port))
            assert _wait_until(lambda: ha.stats['heartbeats_received'] > rx_before), \
                '验签恒真时错误签名包仍被拒 —— 说明丢弃类判据测的不是验签结果（无判别力）'
        finally:
            s.close()
            _stop(ha, t)
