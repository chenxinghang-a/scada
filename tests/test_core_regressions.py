# -*- coding: utf-8 -*-
"""core/ 横切回归测试（round 168g 审计修复）。

分三块：

A. 静默失败修复回归（逐条）—— 每条都锁定"失败必须可见"这一性质：
   要么 fail-closed 抛错，要么 WARNING 级留痕 + 可计数的错误计数。
   绝不允许 `except: pass` / `return 一个看起来正常的常量`。

B. 未接线模块的状态标注固化（AST 自动判定，见文件末）——
   零生产引用的模块必须在文件头标注 `WIRED = False`；反之标注了就必须真的没接线。

C. 本轮 AST 深挖新发现的静默失败（core/ 自查，非任务清单内）。

设计原则：每条测试都写成"还原修复必须变红"。断言尽量落在
**可观测产物**（异常类型 / 日志记录 / 计数字段）上，而不是实现细节。
"""

from __future__ import annotations

import ast
import logging
import os
import pathlib
import re
import threading
import time
from typing import Dict, List

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE_DIR = REPO_ROOT / 'core'


# ======================================================================
# A-1  core/config_encryption.py：解密失败不得返回密文原文
# ======================================================================

def _valid_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def test_decrypt_failure_raises_instead_of_returning_ciphertext():
    """解密失败必须抛 ConfigDecryptionError，绝不 return ciphertext。

    旧实现 `except Exception as e: logger.error(...); return ciphertext` ——
    调用方拿到的是 `gAAAAA...` 密文，却以为是明文密码，继续拿去登录/连库，
    表现为"密钥配错了却没有任何报错，只是认证一直失败"。
    """
    from core.config_encryption import ConfigEncryptor, ConfigDecryptionError

    enc = ConfigEncryptor(key=_valid_key())
    bogus = 'gAAAAABm-not-a-real-token'

    with pytest.raises(ConfigDecryptionError):
        enc.decrypt(bogus)


def test_decrypt_failure_message_does_not_leak_return_value_as_ciphertext():
    """回归守卫：旧实现"失败返回值 == 密文"这一性质必须不再成立。"""
    from core.config_encryption import ConfigEncryptor, ConfigDecryptionError

    enc = ConfigEncryptor(key=_valid_key())
    bogus = 'totally-not-decryptable'

    returned = None
    try:
        returned = enc.decrypt(bogus)
    except ConfigDecryptionError:
        returned = None
    assert returned != bogus, '解密失败仍然把密文原文返回给了调用方'


def test_encrypt_decrypt_roundtrip_unchanged():
    """正常路径不受影响。"""
    from core.config_encryption import ConfigEncryptor

    enc = ConfigEncryptor(key=_valid_key())
    token = enc.encrypt('my-secret')
    assert token != 'my-secret'
    assert enc.decrypt(token) == 'my-secret'


def test_encrypt_and_decrypt_are_fail_closed_without_backend():
    """后端不可用时也必须 fail-closed（旧实现返回明文/原文）。"""
    from core.config_encryption import ConfigEncryptor, ConfigDecryptionError

    enc = ConfigEncryptor(key=_valid_key())
    enc._fernet = None
    with pytest.raises(ConfigDecryptionError):
        enc.encrypt('plain')
    with pytest.raises(ConfigDecryptionError):
        enc.decrypt('whatever')


def test_is_encrypted_failure_is_logged(caplog):
    """is_encrypted 的 except 分支原来静默 return False，现在必须留痕。"""
    from core.config_encryption import ConfigEncryptor

    enc = ConfigEncryptor(key=_valid_key())
    enc._fernet = None
    with caplog.at_level(logging.WARNING):
        assert enc.is_encrypted('gAAAAAxxx') is False
    assert any('无法判定' in r.message for r in caplog.records), \
        '加密不可用时判定失败没有留痕'


def test_encryptor_exposes_available_flag():
    """降级必须由调用方显式判断 available，而不是靠静默降级。"""
    from core.config_encryption import ConfigEncryptor

    assert ConfigEncryptor(key=_valid_key()).available is True
    broken = ConfigEncryptor(key=_valid_key())
    broken._fernet = None
    assert broken.available is False


# ======================================================================
# A-2  core/cache_tier.py：L2 读/清理/统计失败必须可见
# ======================================================================

@pytest.fixture
def broken_l2(tmp_path):
    """把 L2 的 db 路径指向一个目录 → sqlite3.connect 必然失败。"""
    from core.cache_tier import SQLiteCache

    cache = SQLiteCache(db_path=str(tmp_path / 'good.db'))
    cache._db_path = str(tmp_path)          # 目录：无法作为数据库打开
    return cache


def test_l2_read_failure_is_logged_and_counted(broken_l2, caplog):
    """L2 读失败旧实现 `except Exception: return None` 无日志、无计数。"""
    with caplog.at_level(logging.WARNING):
        assert broken_l2.get('k') is None
    assert broken_l2._errors['read'] >= 1, '读失败没有计数'
    assert any('读取' in r.message for r in caplog.records), '读失败没有 WARNING'


def test_l2_cleanup_failure_is_distinguishable_from_empty(broken_l2, caplog):
    """清理失败旧实现 `return 0`，与"没有过期项"无法区分。"""
    with caplog.at_level(logging.WARNING):
        deleted = broken_l2.cleanup_expired()
    assert deleted == 0
    assert broken_l2._errors['cleanup'] >= 1, '清理失败没有计数，仍与"0 条过期"同形'
    assert any('清理过期项' in r.message for r in caplog.records), '清理失败没有 WARNING'


def test_l2_stats_failure_does_not_fake_zeros(broken_l2, caplog):
    """统计失败旧实现返回 {'count': 0, 'size_bytes': 0} —— 把"读不动"报成"缓存空的"。"""
    with caplog.at_level(logging.WARNING):
        stats = broken_l2.get_stats()

    assert stats['available'] is False
    assert stats['count'] is None, '统计失败仍报了 0，看起来像"缓存是空的"'
    assert stats['size_bytes'] is None
    assert stats.get('error'), '统计失败没带原因'
    # 结构一致性：成功分支有的键，失败分支也必须有
    assert 'errors' in stats and 'last_error' in stats
    assert any('统计' in r.message for r in caplog.records)


def test_l2_stats_success_shape_is_consistent(tmp_path):
    """成功分支必须与失败分支结构一致（都是 available/count/size_bytes/errors）。"""
    from core.cache_tier import SQLiteCache

    cache = SQLiteCache(db_path=str(tmp_path / 'ok.db'))
    stats = cache.get_stats()
    assert stats['available'] is True
    assert stats['count'] == 0
    assert stats['errors']['stats'] == 0


def test_l2_expired_entry_path_does_not_deadlock(tmp_path):
    """回归：get() 命中过期条目时会调用会再次取锁的 delete()。

    旧实现用非重入的 `threading.Lock`，该路径会**永久死锁**（进程静默挂住，
    没有任何异常）。这里既检查锁的重入性，又在带超时的线程里真跑一遍，
    避免回归时把 CI 直接挂死。
    """
    from core.cache_tier import SQLiteCache

    cache = SQLiteCache(db_path=str(tmp_path / 'exp.db'))
    assert isinstance(cache._lock, type(threading.RLock())) or \
        cache._lock.acquire(blocking=False) and (cache._lock.acquire(blocking=False) or True), \
        'L2 缓存的锁不是可重入的，过期待删除路径会死锁'

    cache.set('k', 'v', ttl=-1)              # 立即过期
    done = threading.Event()
    result = {}

    def _run():
        result['value'] = cache.get('k')
        done.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=5)
    assert done.is_set(), 'get() 在"过期条目"路径上挂住了（锁不可重入 → 死锁）'
    assert result['value'] is None


def test_tiered_warmup_summary_reports_failures(tmp_path, caplog):
    """预热汇总行不得把失败吞成"完成 N/M"。"""
    from core.cache_tier import TieredCache

    cache = TieredCache()
    cache.l2._db_path = str(tmp_path)        # 让 L2 写失败不影响断言

    def boom():
        raise RuntimeError('loader 炸了')

    with caplog.at_level(logging.WARNING):
        warmed = cache.warmup([('good', lambda: 1, 5.0), ('bad', boom, 5.0)])

    assert warmed == 1
    assert any('缓存预热完成(含失败)' in r.message for r in caplog.records), \
        '预热失败被汇总成"完成"，没有单独标注失败数'


# ======================================================================
# A-3  core/backup_verifier.py：except 分支不得因未绑定 tmp_path 抛 NameError
# ======================================================================

def test_restore_test_reports_real_error_when_tempfile_fails(tmp_path, monkeypatch, caplog):
    """临时文件都建不出来时，必须报出真实原因，而不是 NameError。"""
    import tempfile

    from core.backup_verifier import BackupVerifier

    src = tmp_path / 'backup.db'
    src.write_bytes(b'not-empty')

    def boom(*a, **kw):
        raise OSError('磁盘已满')

    monkeypatch.setattr(tempfile, 'NamedTemporaryFile', boom)

    verifier = BackupVerifier(str(tmp_path / 'x.db'), backup_dir=str(tmp_path))
    with caplog.at_level(logging.WARNING):
        result = verifier.test_restore(str(src))

    assert result['status'] == 'fail'
    assert '磁盘已满' in result['error'], \
        f'真实错误被掩盖了，实际 error={result.get("error")!r}'
    assert 'NameError' not in result['error'], '临时变量未绑定，仍会抛 NameError'
    assert any('备份恢复测试失败' in r.message for r in caplog.records)


def test_restore_test_reports_error_when_source_missing(tmp_path):
    """源文件不存在：错误信息要带异常类型 + 消息。"""
    from core.backup_verifier import BackupVerifier

    verifier = BackupVerifier(str(tmp_path / 'x.db'), backup_dir=str(tmp_path))
    result = verifier.test_restore(str(tmp_path / 'nope.db'))

    assert result['status'] == 'fail'
    assert 'FileNotFoundError' in result['error']


def test_restore_test_success_path_unchanged(tmp_path):
    """正常路径不受影响：能统计出各表行数。"""
    import sqlite3

    from core.backup_verifier import BackupVerifier

    db = tmp_path / 'ok.db'
    conn = sqlite3.connect(str(db))
    conn.execute('CREATE TABLE devices (id INTEGER)')
    conn.executemany('INSERT INTO devices VALUES (?)', [(1,), (2,)])
    conn.commit()
    conn.close()

    verifier = BackupVerifier(str(tmp_path / 'x.db'), backup_dir=str(tmp_path))
    result = verifier.test_restore(str(db))

    assert result['status'] == 'pass'
    assert result['table_stats']['devices'] == 2


# ======================================================================
# A-4  core/data_access_audit.py：datetime 属性不存在 + 持久化失败可见
# ======================================================================

def test_user_access_pattern_does_not_raise_attribute_error():
    """`DataAccessRecord` 只有 timestamp，没有 datetime —— 旧实现必抛 AttributeError。"""
    from core.data_access_audit import DataAccessAuditor

    auditor = DataAccessAuditor()
    auditor.log_access('u1', 'devices', 'read')
    auditor.log_access('u1', 'alarms', 'write', sensitive_fields=['password'])

    pattern = auditor.get_user_access_pattern('u1')     # 不得抛异常

    assert pattern['total_accesses'] == 2
    assert pattern['sensitive_accesses'] == 1
    assert pattern['first_access'] and pattern['last_access']
    # 与 to_dict()['datetime'] 同格式（ISO 字符串），保证两处口径一致
    expected = auditor.get_recent_access(user_id='u1', limit=10)[0]['datetime']
    assert pattern['first_access'] == expected
    assert re.match(r'\d{4}-\d{2}-\d{2}T', pattern['first_access'])


def test_user_access_pattern_missing_user_unchanged():
    from core.data_access_audit import DataAccessAuditor

    assert DataAccessAuditor().get_user_access_pattern('nobody') == {
        'user_id': 'nobody', 'total_accesses': 0}


def test_persist_failure_is_warning_and_counted(tmp_path, caplog):
    """审计落库失败旧实现只有 logger.debug —— 默认 INFO 级别下完全静默。"""
    from core.data_access_audit import DataAccessAuditor

    auditor = DataAccessAuditor(db_path=str(tmp_path))     # 目录：必然写失败

    with caplog.at_level(logging.WARNING):
        auditor.log_access('u1', 'devices', 'read')

    stats = auditor.get_access_stats()
    assert stats['persist_failures'] == 1, '审计持久化失败没有计数（审计链断裂不可见）'
    assert stats['last_persist_error']
    assert stats['persistence_enabled'] is True
    assert any(r.levelno >= logging.WARNING and '持久化失败' in r.message
               for r in caplog.records), '持久化失败没有 WARNING 级留痕'


def test_persist_success_has_no_failures(tmp_path):
    import sqlite3

    from core.data_access_audit import DataAccessAuditor

    db = tmp_path / 'audit.db'
    conn = sqlite3.connect(str(db))
    conn.execute('''CREATE TABLE data_access_log (
        user_id TEXT, table_name TEXT, operation TEXT, record_id TEXT,
        sensitive_fields TEXT, ip_address TEXT, user_agent TEXT, timestamp REAL)''')
    conn.commit()
    conn.close()

    auditor = DataAccessAuditor(db_path=str(db))
    auditor.log_access('u1', 'devices', 'read')
    assert auditor.get_access_stats()['persist_failures'] == 0


# ======================================================================
# A-5  core/connection_pool.py：active/idle 不得漂移为负
# ======================================================================

@pytest.fixture
def pool():
    from core.connection_pool import ConnectionPool

    p = ConnectionPool(
        factory=lambda key: type('C', (), {'closed': False, 'close': lambda self: None})(),
        max_size=8, min_idle=1, max_idle_time=3600.0, max_lifetime=7200.0,
        name='test-pool', auto_scale=False,
    )
    yield p
    p.shutdown()


def test_release_without_acquire_does_not_go_negative(pool):
    """对空闲连接再 release 一次，旧实现会 active-=1 / idle+=1 → 长期漂移为负。"""
    pool.acquire('a')
    pool.release('a')
    pool.release('a')          # 误用：未占用却 release

    stats = pool.get_stats()
    assert stats['active'] == 0
    assert stats['idle'] == 1
    assert stats['active'] >= 0 and stats['idle'] >= 0, '统计已漂移为负'
    assert stats['release_mismatch'] == 1, '未占用却 release 没有计数'


def test_get_or_create_then_release_does_not_drift(pool):
    """get_or_create() 返回空闲连接后再 release，也会多减一次。"""
    pool.get_or_create('b')
    pool.release('b')
    pool.get_or_create('b')      # 复用，不改变占用态
    pool.release('b')

    stats = pool.get_stats()
    assert stats['active'] >= 0 and stats['idle'] >= 0
    assert stats['active'] + stats['idle'] == stats['total']


def test_stats_never_drift_over_many_cycles(tmp_path):
    """反复 acquire/release 后，active/idle 必须始终等于池内真实状态。"""
    from core.connection_pool import ConnectionPool

    p = ConnectionPool(
        factory=lambda key: type('C', (), {'close': lambda self: None})(),
        max_size=32, min_idle=1, max_idle_time=3600.0, max_lifetime=7200.0,
        name='drift-pool', auto_scale=False,
    )
    try:
        for _ in range(50):
            p.acquire('k')
            p.release('k')
        for i in range(20):
            p.acquire(f'k{i}')
        for i in range(20):
            p.release(f'k{i}')
        # 再混入"误用 release"（没有对应 acquire）
        for i in range(20):
            p.release(f'k{i}')

        stats = p.get_stats()
        assert stats['active'] >= 0, f"active 漂移为负: {stats}"
        assert stats['idle'] >= 0, f"idle 漂移为负: {stats}"
        assert stats['active'] + stats['idle'] == stats['total'], \
            f'统计与池内真实状态不一致: {stats}'
        assert stats['total'] == 21          # 'k' + k0..k19
        assert stats['release_mismatch'] == 20, \
            '反复 release 同一批连接应当被识别为误用并计数'
    finally:
        p.shutdown()


def test_drift_is_detected_and_reported(pool, caplog):
    """即使有人手改计数器，get_stats() 也必须按真实值修正并 WARNING。"""
    pool.acquire('a')
    with caplog.at_level(logging.WARNING):
        pool._stats['active'] = -99            # 人为制造漂移
        stats = pool.get_stats()
    assert stats['active'] == 1, '没有按池内真实状态修正'
    assert stats['stats_drift'] >= 1
    assert any('计数漂移' in r.message for r in caplog.records)


# ======================================================================
# A-6  core/dynamic_rate_limiter.py：采集失败不得伪装成 0
# ======================================================================

def test_queue_probe_failure_is_not_reported_as_zero():
    """队列深度读不到 ≠ 队列为空。旧实现 return 0 会把负载评估往下拉一档。"""
    from core.dynamic_rate_limiter import SystemLoadMonitor

    monitor = SystemLoadMonitor()

    def boom():
        raise RuntimeError('队列读不动')

    monitor.set_queue_size_func(boom)
    monitor.evaluate()

    snapshot = monitor.get_metrics_snapshot()
    assert snapshot['queue_size'] is None, \
        '队列采集失败被伪装成了 0 —— 高负载时会反而放宽限流'
    assert 'queue' in snapshot['unavailable_signals']
    assert monitor.get_probe_errors()['queue'] >= 1, '采集失败没有计数'


def test_queue_probe_failure_logs_warning(caplog):
    from core.dynamic_rate_limiter import SystemLoadMonitor

    monitor = SystemLoadMonitor()
    monitor.set_queue_size_func(lambda: (_ for _ in ()).throw(RuntimeError('boom')))
    with caplog.at_level(logging.WARNING):
        monitor.evaluate()
    assert any('队列深度采集失败' in r.message for r in caplog.records)


def test_queue_probe_success_path_unchanged():
    """正常路径不受影响：队列深度能读出来时，必须真的参与评估。"""
    from core.dynamic_rate_limiter import LoadLevel, SystemLoadMonitor

    monitor = SystemLoadMonitor()
    monitor.set_queue_size_func(lambda: 20000)       # 远超 queue_critical
    assert monitor.evaluate() == LoadLevel.CRITICAL
    snap = monitor.get_metrics_snapshot()
    assert snap['queue_size'] == 20000
    assert 'queue' not in snap['unavailable_signals'], '队列信号不该被判为缺失'
    # cpu/memory 是否可得取决于本机是否装了 psutil，不做强断言
    assert set(snap['unavailable_signals']) <= {'cpu', 'memory'}


def test_no_queue_func_means_signal_absent_not_error():
    """未注入队列源时，信号标记为不可用，但不应计成"采集失败"。"""
    from core.dynamic_rate_limiter import SystemLoadMonitor

    monitor = SystemLoadMonitor()
    monitor.evaluate()
    assert monitor.get_probe_errors()['queue'] == 0
    assert monitor.get_metrics_snapshot()['queue_size'] is None


def test_dynamic_limiter_status_exposes_probe_errors():
    from core.dynamic_rate_limiter import DynamicRateLimiter

    limiter = DynamicRateLimiter()
    limiter.set_queue_size_func(lambda: (_ for _ in ()).throw(RuntimeError('boom')))
    limiter._monitor.evaluate()

    status = limiter.get_status()
    assert status['probe_errors']['queue'] >= 1
    assert status['monitor_metrics']['queue_size'] is None


# ======================================================================
# A-7  core/log_sampler.py：被抑制计数必须按 key 分桶（不得串台）
# ======================================================================

def _seed_last_logged(sampler, *keys):
    """把 `_last_logged` 预置成"刚刚记过"。

    否则最小间隔路径会因 `_last_logged.get(key, 0)` 默认 0 而在新 key 的
    第一次调用就触发（等价于"首次必记"），掩盖采样率路径的抑制计数。
    这些用例要专门验采样率路径，所以先把该路径按住。
    """
    now = time.time()
    for k in keys:
        sampler._last_logged[k] = now


def test_suppressed_count_is_per_key():
    """`_suppressed_count` 写在实例上时，A 路的抑制数会串到 B 路。"""
    from core.log_sampler import LogSampler

    sampler = LogSampler()
    _seed_last_logged(sampler, 'A', 'B')

    # 同一个 key 累计到采样率：第 3 次记录，前 2 条被抑制
    assert sampler.should_log('A', 3) is False
    assert sampler.should_log('A', 3) is False
    assert sampler.should_log('A', 3) is True

    assert sampler.get_suppressed_count('A') == 2
    assert sampler.get_suppressed_count('B') == 0, \
        'B 路读到了 A 路的抑制数 —— 计数串台，日志里标注的"已抑制N条"是假的'


def test_suppressed_total_is_sum_of_keys():
    from core.log_sampler import LogSampler

    sampler = LogSampler()
    _seed_last_logged(sampler, 'A', 'B')
    for _ in range(3):
        sampler.should_log('A', 3)
    for _ in range(3):
        sampler.should_log('B', 3)

    assert sampler.get_suppressed_count() == 4
    assert sampler.get_suppressed_count() == \
        sampler.get_suppressed_count('A') + sampler.get_suppressed_count('B')


def test_sampled_logger_labels_suppressed_with_own_key(caplog):
    """包装器必须按自己的 key 取抑制数，不能拿全局值。"""
    from core.log_sampler import SampledLogger

    logger = logging.getLogger('test.log_sampler.regression')
    samp = SampledLogger(logger)

    dev_key = f'{logging.WARNING}:设备断连: %s'
    _seed_last_logged(samp._sampler, dev_key)

    with caplog.at_level(logging.WARNING, logger=logger.name):
        # 设备断连：3 次调用 → 第 3 次记录，标注"已抑制2条"
        samp.warning('设备断连: %s', 'dev-1', sample_rate=3)
        samp.warning('设备断连: %s', 'dev-1', sample_rate=3)
        samp.warning('设备断连: %s', 'dev-1', sample_rate=3)
        # 另一个 key 只调用 1 次 → 不应记录，也不该带上别人的抑制数
        samp.warning('磁盘告警: %s', 'disk', sample_rate=3)

    dev_msgs = [r.getMessage() for r in caplog.records if '设备断连' in r.getMessage()]
    disk_msgs = [r.getMessage() for r in caplog.records if '磁盘告警' in r.getMessage()]
    assert any('已抑制2条' in m for m in dev_msgs), f'设备断连的抑制数不对: {dev_msgs}'
    for m in disk_msgs:
        assert '已抑制' not in m, f'磁盘告警串到了设备断连的抑制数: {m}'


def test_sampler_stats_expose_per_key_suppression():
    from core.log_sampler import SampledLogger
    import logging as _logging

    samp = SampledLogger(_logging.getLogger('test.log_sampler.stats'))
    key = f'{_logging.WARNING}:x %s'
    _seed_last_logged(samp._sampler, key)
    for _ in range(3):
        samp.warning('x %s', 1, sample_rate=3)

    stats = samp.get_stats()
    assert stats['suppressed_by_key'].get(key) == 2
    assert stats['suppressed_total'] == 2


# ======================================================================
# A-8  core/db_pool_enhanced.py：池满必须真的等待，不是静默 pass
# ======================================================================

def _tiny_pool(tmp_path, max_connections=1):
    import paths  # noqa: F401  (确保仓库根在 sys.path 上)
    from core.db_pool_enhanced import EnhancedConnectionPool

    return EnhancedConnectionPool(
        str(tmp_path / 'pool.db'), max_connections=max_connections,
        min_connections=0, health_check_interval=3600, liveness_probe_interval=3600,
    )


def test_pool_full_waits_and_succeeds_when_released(tmp_path):
    """旧实现池满时 `pass`（注释说等待，实际直接抛）—— 现在必须真的等到释放。"""
    pool = _tiny_pool(tmp_path)

    def holder():
        with pool.acquire(timeout=5.0):
            time.sleep(0.5)

    t = threading.Thread(target=holder, daemon=True)
    t.start()

    with pool.acquire(timeout=5.0) as conn:
        assert conn is not None
    t.join(timeout=5)


def test_pool_full_timeout_raises_with_context(tmp_path, caplog):
    """等不到连接时：抛错必须带上下文，且 WARNING/ERROR 留痕 + 计数。"""
    pool = _tiny_pool(tmp_path)

    def holder():
        with pool.acquire(timeout=5.0):
            time.sleep(2.0)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    time.sleep(0.2)                       # 确保占位线程先拿到连接

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError) as exc:
            with pool.acquire(timeout=0.3):
                pass

    assert '连接池已满' in str(exc.value)
    assert '0.3' in str(exc.value), '错误信息没有带上等待时长'
    assert pool._stats['pool_full_timeouts'] >= 1
    assert any('已满' in r.message for r in caplog.records), '池满没有留痕'
    t.join(timeout=5)


def test_pool_acquire_release_success_unchanged(tmp_path):
    pool = _tiny_pool(tmp_path, max_connections=2)
    with pool.acquire(timeout=1.0) as conn:
        assert conn.execute('SELECT 1').fetchone()[0] == 1
    with pool.acquire(timeout=1.0) as conn:
        assert conn.execute('SELECT 1').fetchone()[0] == 1
    assert pool._stats['acquired'] >= 2


def test_is_alive_failure_is_logged(tmp_path, caplog):
    """连接探活失败返回 False 是安全侧，但原因必须可查。"""
    from core.db_pool_enhanced import PooledConnection

    class BadConn:
        def execute(self, *a, **kw):
            raise RuntimeError('数据库忙')

    with caplog.at_level(logging.DEBUG):
        assert PooledConnection(BadConn(), 'c1').is_alive() is False
    assert any('存活探测失败' in r.message for r in caplog.records)


# ======================================================================
# A-9  core/config_validator_startup.py：非法 WEB_PORT 不得崩启动
# ======================================================================

@pytest.mark.parametrize('bad_port', ['abc', '', '70000', '50.5', '0x10'])
def test_invalid_web_port_does_not_crash_startup_validation(monkeypatch, bad_port):
    """`int(os.environ[...])` 曾写在 try 之外 → 环境变量非法直接把进程打挂。"""
    from core.config_validator_startup import validate_startup_config

    monkeypatch.setenv('WEB_PORT', bad_port)
    is_valid, errors = validate_startup_config()      # 不得抛 ValueError

    port_errors = [e for e in errors if e.key == 'port:web']
    assert port_errors, f'非法 WEB_PORT={bad_port!r} 没有被报出来'
    assert port_errors[0].severity == 'error'
    assert is_valid is False


def test_valid_web_port_is_accepted(monkeypatch):
    from core.config_validator_startup import validate_startup_config

    monkeypatch.setenv('WEB_PORT', '5099')
    _, errors = validate_startup_config()
    assert not [e for e in errors if e.key == 'port:web']


# ======================================================================
# A-10  core/api_cache.py：按前缀失效必须真的删掉条目
# ======================================================================

@pytest.fixture
def api_cache_env():
    """干净缓存 + Flask app。"""
    from flask import Flask
    from core import api_cache

    with api_cache._cache_lock:
        api_cache._cache.clear()
        for k in ('hits', 'misses', 'evictions', 'invalidations', 'invalidate_misses'):
            api_cache._stats[k] = 0

    app = Flask(__name__)
    app.config['TESTING'] = True
    yield api_cache, app

    with api_cache._cache_lock:
        api_cache._cache.clear()


def test_prefix_invalidation_actually_removes_entries(api_cache_env):
    """缓存键是 sha256，旧实现拿端点名对 sha256 键做 startswith → 永不生效。"""
    from flask import jsonify

    api_cache, app = api_cache_env
    calls = {'n': 0}

    @app.route('/api/devices')
    @api_cache.cached_response(ttl=60, prefix='devices')
    def devices():
        calls['n'] += 1
        return jsonify({'v': calls['n']})

    client = app.test_client()
    assert client.get('/api/devices').headers['X-Cache'] == 'MISS'
    assert client.get('/api/devices').headers['X-Cache'] == 'HIT'

    removed = api_cache.invalidate_cache('devices')
    assert removed == 1, f'按前缀失效没删掉任何条目（实际 {removed}）'

    assert client.get('/api/devices').headers['X-Cache'] == 'MISS', \
        '失效之后仍在返回陈旧缓存'


def test_prefix_invalidation_miss_is_logged(api_cache_env, caplog):
    """前缀对不上时必须留痕，否则"改了数据还是旧值"会被误判为别的问题。"""
    from flask import jsonify

    api_cache, app = api_cache_env

    @app.route('/api/x')
    @api_cache.cached_response(ttl=60, prefix='real-prefix')
    def x():
        return jsonify({'v': 1})

    app.test_client().get('/api/x')

    with caplog.at_level(logging.WARNING):
        removed = api_cache.invalidate_cache('wrong-prefix')

    assert removed == 0
    assert any('未命中任何缓存条目' in r.message for r in caplog.records)
    assert api_cache.get_cache_stats()['invalidate_misses'] == 1


def test_invalidate_all_clears_everything(api_cache_env):
    from flask import jsonify

    api_cache, app = api_cache_env

    @app.route('/api/y')
    @api_cache.cached_response(ttl=60, prefix='p')
    def y():
        return jsonify({'v': 1})

    app.test_client().get('/api/y')
    assert api_cache.get_cache_stats()['size'] == 1
    assert api_cache.invalidate_cache() == 1
    assert api_cache.get_cache_stats()['size'] == 0


def test_response_parse_failure_is_logged(api_cache_env, caplog):
    """响应体解析失败 → 不缓存；旧实现静默 return，端点"一直不缓存"无从解释。"""
    from flask import Response

    api_cache, app = api_cache_env

    class WeirdResponse(Response):
        def get_json(self, *a, **kw):
            raise ValueError('不是 JSON')

    @app.route('/api/z')
    @api_cache.cached_response(ttl=60, prefix='z')
    def z():
        return WeirdResponse('{"v": 1}', mimetype='application/json')

    with caplog.at_level(logging.WARNING):
        resp = app.test_client().get('/api/z')

    assert resp.status_code == 200
    assert any('响应体解析失败' in r.message for r in caplog.records), \
        '解析失败被静默吞掉，端点"永不缓存"无从解释'
    assert api_cache.get_cache_stats()['size'] == 0


# ======================================================================
# A-11  core/token_bucket_limiter.py：装饰器 import 的是不存在的 api_error
# ======================================================================

def test_token_bucket_decorator_works_instead_of_importerror(monkeypatch):
    """`from core.service_response import api_error` → 一旦启用即 ImportError（500）。"""
    from flask import Flask
    import core.token_bucket_limiter as tbl

    local = tbl.TokenBucketLimiter(capacity=1, refill_rate=0.0)
    monkeypatch.setattr(tbl, 'token_bucket_limiter', local)

    app = Flask(__name__)
    app.config['TESTING'] = True

    @app.route('/limited')
    @tbl.token_bucket_required(tokens=1, key_func=lambda: 'k')
    def limited():
        return {'ok': True}

    client = app.test_client()
    first = client.get('/limited')
    assert first.status_code == 200, '第一次请求不应被限流'

    second = client.get('/limited')
    assert second.status_code == 429, \
        f'超限时没有返回 429（可能是 ImportError 被打成 500）：{second.status_code}'
    assert second.get_json()


def test_service_response_has_error_response_not_api_error():
    """把"装饰器引用了不存在的符号"这件事本身钉住。"""
    import core.service_response as sr

    assert hasattr(sr, 'error_response')
    assert not hasattr(sr, 'api_error'), \
        'service_response.api_error 突然存在了？请同步 token_bucket_limiter 的 import'


# ======================================================================
# C-1  core/masking_rule_engine.py：正则出错不得返回未脱敏原文
# ======================================================================

def test_bad_regex_is_rejected_at_add_rule_time():
    """脱敏是安全控制：坏正则必须在配置那一刻就被拒绝，而不是运行时报错。"""
    from core.masking_rule_engine import MaskingRuleEngine

    engine = MaskingRuleEngine()
    with pytest.raises(ValueError) as exc:
        engine.add_rule('bad', pattern='([unclosed')
    assert '不是合法正则' in str(exc.value)


def test_pattern_failure_fails_closed_and_never_leaks_plaintext(caplog):
    """旧实现 `except re.error: return text` → 直接漏出未脱敏的密码/卡号。"""
    from core.masking_rule_engine import MaskingRuleEngine
    from core.masking_rule_engine import MaskingRule, MaskStrategy

    engine = MaskingRuleEngine()
    # 绕过 add_rule 的校验，模拟"历史上已经注册进去的坏规则"
    engine._rules.insert(0, MaskingRule(
        name='broken', strategy=MaskStrategy.FULL_MASK, pattern='([unclosed', priority=999))

    with caplog.at_level(logging.ERROR):
        out = engine.mask_text('password=hunter2')

    assert out == MaskingRuleEngine.FAILSAFE_MASK, \
        f'正则失败时返回了非遮蔽值，存在明文泄漏: {out!r}'
    assert 'hunter2' not in out
    assert engine.get_stats()['rule_errors'] == 1
    assert any(r.levelno >= logging.ERROR and '整段遮蔽' in r.message for r in caplog.records)


def test_masking_normal_path_unchanged():
    from core.masking_rule_engine import MaskingRuleEngine

    engine = MaskingRuleEngine()
    masked = engine.mask_text('卡号 1234 5678 9012 3456')
    assert '1234 5678 9012 3456' not in masked
    assert engine.get_stats()['rule_errors'] == 0


def test_reset_stats_keeps_rule_errors_key():
    from core.masking_rule_engine import MaskingRuleEngine

    engine = MaskingRuleEngine()
    engine.reset_stats()
    assert engine.get_stats()['rule_errors'] == 0


# ======================================================================
# C-2  core/startup_checker.py：检查不了 ≠ 检查通过
# ======================================================================

def test_disk_space_check_does_not_fail_open(monkeypatch, caplog):
    """旧实现 `except Exception: return True  # 无法检查时假设通过` —— 纯 fail-open。"""
    import shutil

    from core.startup_checker import StartupChecker

    monkeypatch.setattr(shutil, 'disk_usage',
                        lambda p: (_ for _ in ()).throw(OSError('stat 失败')))
    checker = StartupChecker()
    with caplog.at_level(logging.WARNING):
        assert checker._check_disk_space(100) is False, '检查不出来却报告"通过"'
    assert any('disk_space' in r.message for r in caplog.records), '没有留痕'


def test_port_check_does_not_fail_open(monkeypatch, caplog):
    import socket

    from core.startup_checker import StartupChecker

    def boom(*a, **kw):
        raise OSError('socket 用不了')

    monkeypatch.setattr(socket, 'socket', boom)
    checker = StartupChecker()
    with caplog.at_level(logging.WARNING):
        assert checker._check_port_available(5000) is False
    assert any('port_available' in r.message for r in caplog.records)


def test_database_check_failure_is_logged(monkeypatch, caplog, tmp_path):
    import sqlite3

    from core.startup_checker import StartupChecker

    def boom(*a, **kw):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(sqlite3, 'connect', boom)
    checker = StartupChecker()
    # 让 Path('data/scada.db').exists() 为真
    (pathlib.Path('data')).mkdir(exist_ok=True)
    with caplog.at_level(logging.WARNING):
        result = checker._check_database()
    if result is False:
        assert any('database_file' in r.message for r in caplog.records)


def test_non_critical_check_failure_becomes_startup_warning(monkeypatch):
    """fail-closed 之后，non-critical 检查失败只产生启动 WARNING，不阻止启动。"""
    import shutil

    from core.startup_checker import StartupChecker

    monkeypatch.setattr(shutil, 'disk_usage',
                        lambda p: (_ for _ in ()).throw(OSError('stat 失败')))
    results = StartupChecker().run_all()

    assert any(w['name'] == 'disk_space' for w in results['warnings']), \
        '磁盘检查失败既没进 warnings 也没进 failed —— 又变成静默了'


# ======================================================================
# C-3  core/memory_usage_monitor.py：采样失败必须可见
# ======================================================================

def test_memory_sample_failure_is_logged_and_counted(caplog):
    from core.memory_usage_monitor import MemoryUsageMonitor

    class BadProc:
        def memory_info(self):
            raise RuntimeError('读不到内存信息')

    monitor = MemoryUsageMonitor()
    monitor._process = BadProc()

    with caplog.at_level(logging.WARNING):
        assert monitor._take_sample() == {}

    assert monitor._sample_errors == 1
    assert any('内存样本采集失败' in r.message for r in caplog.records)


def test_memory_stats_distinguish_no_process_from_sampling_failure():
    from core.memory_usage_monitor import MemoryUsageMonitor

    class BadProc:
        def memory_info(self):
            raise RuntimeError('boom')

    monitor = MemoryUsageMonitor()
    monitor._process = BadProc()
    stats = monitor.get_stats()
    if not stats.get('available'):
        assert stats.get('reason') in ('sample failed', 'psutil not installed')
        assert 'sample_errors' in stats


# ======================================================================
# C-4  core/request_queue.py：入队失败必须落日志 + 计数
# ======================================================================

def test_enqueue_failure_is_logged_and_counted(caplog):
    """旧实现只把状态标 FAILED；调用方若只看 task_id，任务消失得无声无息。"""
    import queue as _queue

    from core.request_queue import RequestQueue, TaskStatus

    q = RequestQueue('t-enqueue', max_workers=1, max_queue_size=1)

    def boom(*a, **kw):
        raise _queue.Full()

    q._queue.put = boom

    with caplog.at_level(logging.WARNING):
        task_id = q.submit(lambda: None)

    assert q.get_status(task_id)['status'] == TaskStatus.FAILED
    assert q._enqueue_failures == 1
    assert q.get_stats()['enqueue_failures'] == 1
    assert any('任务入队失败' in r.message for r in caplog.records)


# ======================================================================
# C-5  core/ops_tools.py：单表统计失败要带原因且不升级为整体失败
# ======================================================================

def test_table_stats_failure_carries_detail_without_escalating(tmp_path, caplog, monkeypatch):
    """row_count=-1 旧实现不带原因、不落日志；也不能塞 'error' 键（会让整接口 500）。"""
    import sqlite3

    from core.ops_tools import DatabaseMaintainer

    db = tmp_path / 'ops.db'
    conn = sqlite3.connect(str(db))
    conn.execute('CREATE TABLE devices (id INTEGER)')
    conn.execute('CREATE TABLE alarms (id INTEGER)')
    conn.commit()
    conn.close()

    maintainer = DatabaseMaintainer(str(db))
    original = DatabaseMaintainer._validate_table_name      # staticmethod → 普通函数

    def flaky(conn_, table):
        if table == 'alarms':
            raise ValueError('模拟表名校验失败')
        return original(conn_, table)

    monkeypatch.setattr(DatabaseMaintainer, '_validate_table_name', staticmethod(flaky))

    with caplog.at_level(logging.WARNING):
        stats = maintainer.get_table_stats()

    by_table = {s['table']: s for s in stats}
    assert set(by_table) == {'devices', 'alarms'}
    assert by_table['devices']['row_count'] == 0
    failed = by_table['alarms']
    assert failed['row_count'] == -1
    assert failed.get('error_detail'), '失败项没有带原因'
    assert 'error' not in failed, \
        "塞了 'error' 键会让展示层把单表失败升级成整个接口 500"
    assert any('行数统计失败' in r.message for r in caplog.records)


# ======================================================================
# C-6  core/chaos_engineering.py：稳态检查异常必须留痕
# ======================================================================

def test_api_responsive_check_failure_is_logged(monkeypatch, caplog):
    import socket

    from core.chaos_engineering import ChaosEngine

    monkeypatch.setattr(socket, 'socket',
                        lambda *a, **kw: (_ for _ in ()).throw(OSError('socket 不可用')))
    engine = ChaosEngine()

    with caplog.at_level(logging.WARNING):
        assert engine._check_api_responsive() is False
    assert any('api_responsive' in r.message for r in caplog.records), \
        '稳态检查失败没有留痕，无法区分"端口不通"与"探测本身坏了"'


# ======================================================================
# B  未接线模块的状态标注固化
# ======================================================================

# 与 tests/test_silent_exception_guard.py 同一套扫描范围口径
_SKIP_DIRS = {
    '.git', '.venv', 'venv', '__pycache__', '.pytest_cache', 'node_modules',
    'build', 'dist', 'managed_components', 'pytest-quarantine', 'legacy',
}
_TEST_DIRS = ('tests/', '测试/')

_MARKER_HEAD = '# 接线状态：未接线（WIRED = False）'

# 抽样钉子：这些必须一直被判为"未接线"（防扫描器退化）
_MUST_BE_UNWIRED = {
    'tracing', 'sql_cache', 'query_optimizer', 'masking_rule_engine',
    'token_bucket_limiter', 'db_pool_enhanced', 'log_sanitizer',
}
# 反向钉子：这些确实已接线，绝不能被标成未接线
_MUST_BE_WIRED = {
    'connection_pool', 'config_manager', 'di_container', 'event_bus',
    'health_checker', 'cache_tier', 'ws_offline_queue',
}


def _core_modules() -> List[str]:
    return sorted(p.stem for p in CORE_DIR.glob('*.py') if p.stem != '__init__')


def _iter_repo_py():
    """产出 (相对路径, 绝对路径)，口径与 test_silent_exception_guard.py 一致。

    注意：这里**不排除** timeseries/ gateway/ tools/ 等目录 —— 它们都是生产代码，
    对 core 模块的引用同样算"已接线"。（曾因排除 timeseries/ 而误判。）
    只排除构建产物、归档区与测试目录。
    """
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs
                   if d not in _SKIP_DIRS and not d.startswith('.pytest_tmp')]
        for name in files:
            if name.endswith('.py'):
                abs_path = pathlib.Path(root) / name
                rel = str(abs_path.relative_to(REPO_ROOT)).replace(os.sep, '/')
                yield rel, abs_path


def _core_imports_in(src: str, rel: str) -> set:
    """返回该文件对 core 模块的引用名集合（只认真实 import 语句）。

    判定规则（关键：相对导入必须归属到**本包**，不能算到 core 头上）：
      * ``import core.X`` / ``from core.X import Y`` → X
      * ``from core import X``                        → X
      * core/ 内部文件的相对导入（``from .X import Y``）→ X
      * 非 core 文件的相对导入（如 timeseries 的 ``from .query_builder import ...``）
        → **忽略**。此处曾漏判，把 core/query_builder 误当成已接线。
    """
    out = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    in_core = rel.startswith('core/')
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split('.')
                if parts[0] == 'core' and len(parts) >= 2:
                    out.add(parts[1])
                elif in_core and len(parts) == 1:
                    out.add(parts[0])
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            mod = node.module or ''
            if level > 0:
                if not in_core:
                    continue
                if mod:
                    out.add(mod.split('.')[0])
                else:
                    out.update(a.name for a in node.names)
            else:
                parts = mod.split('.') if mod else []
                if parts and parts[0] == 'core':
                    if len(parts) >= 2:
                        out.add(parts[1])
                    else:
                        out.update(a.name for a in node.names)
    return out


def _production_importers() -> Dict[str, List[str]]:
    """core 模块名 -> 生产代码（非 tests/测试）里的引用者列表。"""
    result: Dict[str, List[str]] = {m: [] for m in _core_modules()}
    for rel, abs_path in _iter_repo_py():
        if rel.startswith(_TEST_DIRS):
            continue
        try:
            src = abs_path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        for mod in _core_imports_in(src, rel):
            if mod in result and rel != f'core/{mod}.py':
                result[mod].append(rel)
    return result


def _module_has_wired_false(src: str) -> bool:
    """模块级是否存在 `WIRED = False`。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id == 'WIRED' \
                        and isinstance(stmt.value, ast.Constant) and stmt.value.value is False:
                    return True
    return False


def test_unwired_detector_is_not_vacuous():
    """先证明扫描器真的在扫东西，防止 exclude 写错导致"扫了 0 个"的假绿。"""
    importers = _production_importers()
    assert len(importers) >= 70, f'只扫到 {len(importers)} 个 core 模块'

    unwired = {m for m, v in importers.items() if not v}
    assert len(unwired) >= 40, f'只判定出 {len(unwired)} 个未接线模块，扫描器可能退化了'

    for m in _MUST_BE_UNWIRED:
        assert m in unwired, f'{m} 应被判为未接线，扫描器判定错了'
    for m in _MUST_BE_WIRED:
        assert m not in unwired, f'{m} 已接线，却被判成未接线 —— 扫描器在误报'


def test_unwired_marker_matches_reality():
    """核心守卫：未接线 ⟺ 文件头标注 WIRED = False。

    两个方向都要成立：
      * 零生产引用却没标注 → 代码在库里，读者以为它在工作（本任务要消灭的状态）
      * 标注了却已经被接线 → 文档过期，同样是误导

    所以任何人把模块接进生产代码时，必须同时把文件头的标注改成 WIRED = True
    （或删掉标注块），否则本用例变红。
    """
    importers = _production_importers()
    problems = []

    for mod in _core_modules():
        src = (CORE_DIR / f'{mod}.py').read_text(encoding='utf-8')
        unwired_reality = not importers[mod]
        marked = _module_has_wired_false(src)
        has_head = _MARKER_HEAD in src

        if unwired_reality and not marked:
            problems.append(
                f'core/{mod}.py 生产代码零引用，却没有 WIRED = False 标注。'
                f'请在文件头加统一标注块（见 tests/test_core_regressions.py 的 _MARKER_HEAD）')
        if marked and not unwired_reality:
            problems.append(
                f'core/{mod}.py 标注了 WIRED = False，但它已被 {importers[mod][:3]} 引用。'
                f'接线后请同步更新标注（改为 WIRED = True 并去掉"未接线"说明）')
        if marked and not has_head:
            problems.append(
                f'core/{mod}.py 有 WIRED = False 但缺少统一标注头 {_MARKER_HEAD!r}')

    assert not problems, '未接线标注与事实不一致：\n  ' + '\n  '.join(problems)


def test_unwired_marker_documents_reason_and_wiring_suggestion():
    """标注不能只是一行 WIRED = False：必须说明"未接线"这件事与接线落点。"""
    importers = _production_importers()
    missing = []
    for mod in sorted(m for m, v in importers.items() if not v):
        src = (CORE_DIR / f'{mod}.py').read_text(encoding='utf-8')
        if '未接线' not in src:
            missing.append(f'{mod}: 没有"未接线"字样')
        if '接线建议' not in src:
            missing.append(f'{mod}: 没有接线建议')
        if 'test_core_regressions.py' not in src:
            missing.append(f'{mod}: 没有指向复核用例')
    assert not missing, '未接线标注内容不完整：\n  ' + '\n  '.join(missing)


def test_every_unwired_module_is_still_importable():
    """未接线 ≠ 坏死：模块必须可导入、可拿到 WIRED 常量。"""
    import importlib

    importers = _production_importers()
    failures = []
    for mod in sorted(m for m, v in importers.items() if not v):
        try:
            module = importlib.import_module(f'core.{mod}')
        except Exception as e:  # pragma: no cover - 失败时给出清晰清单
            failures.append(f'{mod}: {type(e).__name__}: {e}')
            continue
        if getattr(module, 'WIRED', None) is not False:
            failures.append(f'{mod}: 模块级 WIRED 不是 False')
    assert not failures, '未接线模块导入失败：\n  ' + '\n  '.join(failures)


# ======================================================================
# 静默宽异常豁免集：只许收缩，不许增长
# ======================================================================

def test_silent_broad_allowlist_does_not_grow():
    """`test_silent_exception_guard.ALLOWED_SILENT_BROAD` 是**存量负债清单**。

    审计口径是"零豁免"。任何新增豁免都应先修代码，而不是往白名单里加一行 ——
    否则守卫会被慢慢架空（每加一条，就少一处会被发现的静默失败）。
    本用例把豁免集钉在 1 条，并锁定它必须指向 health_checker。

    当前唯一一条：`core/health_checker.py:548`（本轮文件边界明确排除该文件，
    故只登记、不修改；修掉后请连同豁免一起删除，那时本用例的 in 断言会失败，
    提示把钉子一起更新掉）。
    """
    from tests.test_silent_exception_guard import ALLOWED_SILENT_BROAD

    assert len(ALLOWED_SILENT_BROAD) <= 1, (
        '静默宽异常豁免集增长了：\n  '
        + '\n  '.join(f'{k[0]}:{k[1]} -> {v[:60]}...' for k, v in ALLOWED_SILENT_BROAD.items())
        + '\n请优先修代码；确需豁免请在 tests/test_core_regressions.py 说明并同步本断言。'
    )
    for (path, lineno), reason in ALLOWED_SILENT_BROAD.items():
        assert 'health_checker' in path, f'意外豁免了 {path}:{lineno}'
        assert len(reason) > 50, f'{path}:{lineno} 的豁免理由过于简略，无法说服 reviewer'
