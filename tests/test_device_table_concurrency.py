# -*- coding: utf-8 -*-
"""设备表 `devices` 的并发纪律：增删必须持锁，迭代必须用快照（round 225）。

缺陷
----
`DeviceManager.reload_config()` 的注释里已经记录过这个坑：

    > 不能写成 `self.devices.clear()` 然后逐条填 —— 重连循环正在
    > `for device_id, config in self.devices.items()`，中间态会让它抛
    > `RuntimeError: dictionary changed size during iteration`

但**同一个坑还开着两扇门**：

1. `add_device()` / `remove_device()` 修改 `self.devices` / `self.clients`
   **完全不加锁**，而 `_save_config()` 在锁内做 `list(self.devices.values())`。
   于是「API 线程增删设备」与「重连线程迭代 / 保存配置」并发时：
   - 重连循环抛 RuntimeError → 被 `except Exception` 吞掉 →
     **这一轮所有设备的自动重连整批跳过**（现场表现：某段时间设备掉线不恢复）；
   - `_save_config()` 抛 RuntimeError → 配置**没写盘**，重启后设备消失。
2. `RealDeviceManager` / `SimulatedDeviceManager` 原来**一把锁都没有**，
   `add_device`/`remove_device` 与重连循环、`_save_config()` 完全无保护。

修法
----
* 增删持 `self._lock`（锁范围只包住字典操作，不包 connect/disconnect，
  `_lock` 是不可重入的 `threading.Lock`，嵌套会死锁）；
* 所有对 `self.devices` 的迭代改成快照 `list(...)`。

本文件两条腿：
* 行为：持锁期间 `add_device`/`remove_device` 必须**阻塞**（证明真的取了锁）；
* 静态：生产代码里不允许出现对 `self.devices` 的活视图迭代。
"""

from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 行为：增删必须持锁
# ---------------------------------------------------------------------------

def _manager(tmp_path):
    from 采集层.device_manager import DeviceManager

    return DeviceManager(
        config_path=str(tmp_path / 'devices.yaml'),
        simulation_mode=True,
        use_enhanced_simulation=False,
    )


def _manager_by_kind(kind: str, tmp_path):
    """三个实现各自建一个实例 —— 它们都有同一张 `devices` 表和同一个坑。"""
    cfg = str(tmp_path / f'{kind}.yaml')
    if kind == 'unified':
        from 采集层.device_manager import DeviceManager
        return DeviceManager(config_path=cfg, simulation_mode=True, use_enhanced_simulation=False)
    if kind == 'real':
        from 采集层.real_device_manager import RealDeviceManager
        return RealDeviceManager(config_path=cfg)
    if kind == 'simulated':
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        return SimulatedDeviceManager(config_path=cfg)
    raise AssertionError(kind)


MANAGER_KINDS = ['unified', 'real', 'simulated']


def _device(device_id: str, enabled: bool = False) -> dict:
    # enabled=False：不触发连接，让「唯一的阻塞点」只剩锁本身
    return {
        'id': device_id, 'name': device_id, 'protocol': 'modbus_tcp',
        'enabled': enabled, 'host': '127.0.0.1', 'port': 502, 'registers': [],
    }


class _LockSpyDict(dict):
    """记录「每次增/删时，`self._lock` 是否被当前线程持有」。

    为什么不用「持锁 → 另起线程调用 → 看字典变没变」：
    `remove_device()` 的第一个阻塞点是 `disconnect_device()`（它自己也取锁），
    所以即使字典写入在锁外，线程也会先卡在那儿 —— 那种判据对 remove 是瞎的。

    直接记录 `lock.locked()` 则是精确的：变异掉锁之后，记录里会出现 False。
    """

    def __init__(self, *args, lock=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock_ref = lock
        self.set_states: list[bool] = []
        self.pop_states: list[bool] = []

    def __setitem__(self, key, value):
        self.set_states.append(bool(self._lock_ref and self._lock_ref.locked()))
        return super().__setitem__(key, value)

    def pop(self, key, *args):
        self.pop_states.append(bool(self._lock_ref and self._lock_ref.locked()))
        return super().pop(key, *args)


@pytest.mark.parametrize('kind', MANAGER_KINDS)
def test_add_device_mutates_under_lock(tmp_path, kind):
    """新增设备时必须**持锁**写 `self.devices`。"""
    dm = _manager_by_kind(kind, tmp_path)
    spy = _LockSpyDict(lock=dm._lock)
    dm.devices = spy

    assert dm.add_device(_device('dev_spy_add')) is True

    assert spy.set_states, 'add_device 根本没往 self.devices 写'
    assert all(spy.set_states), (
        'add_device 在锁外写了 self.devices —— 与 _save_config 的'
        ' list(self.devices.values()) / 重连循环迭代并发时抛 RuntimeError'
        '（配置静默不写盘 / 重连整批跳过）'
    )


@pytest.mark.parametrize('kind', MANAGER_KINDS)
def test_remove_device_mutates_under_lock(tmp_path, kind):
    """删除设备时必须**持锁**删 `self.devices`。"""
    dm = _manager_by_kind(kind, tmp_path)
    spy = _LockSpyDict(lock=dm._lock)
    dm.devices = spy
    spy['dev_spy_rm'] = _device('dev_spy_rm')
    spy.pop_states.clear()

    assert dm.remove_device('dev_spy_rm') is True

    assert spy.pop_states, 'remove_device 根本没从 self.devices 删'
    assert all(spy.pop_states), (
        'remove_device 在锁外删了 self.devices —— 重连循环正在迭代时删会抛'
        ' RuntimeError（dictionary changed size during iteration）'
    )


@pytest.mark.parametrize('kind', MANAGER_KINDS)
def test_save_config_takes_lock(tmp_path, kind):
    """`_save_config()` 必须在锁内取快照 —— 否则并发增删时抛 RuntimeError，
    被它自己的 `except Exception` 吞掉 → **配置静默不写盘**（重启后设备消失）。"""
    dm = _manager_by_kind(kind, tmp_path)
    cfg_file = Path(dm.config_path)

    dm._lock.acquire()
    try:
        t = threading.Thread(target=dm._save_config, daemon=True)
        t.start()
        t.join(0.5)
        assert t.is_alive(), '_save_config 没等锁 —— 它在锁外迭代 self.devices'
        assert not cfg_file.exists(), '持锁期间配置就被写盘了（说明没走临界区）'
    finally:
        dm._lock.release()

    t.join(10)
    assert cfg_file.exists(), '放锁后配置仍未写盘'


@pytest.mark.parametrize('kind', MANAGER_KINDS)
def test_save_config_returns_bool(tmp_path, kind):
    """`_save_config()` 必须返回 bool —— 否则「保存成功」被判成失败。

    `展示层/api/api_devices.py::update_device` 的判据是
    `if not device_manager._save_config(): return 500`。
    三个实现里有两个原本返回 `None` → 真实设备模式 / 纯模拟模式下
    **每次改设备都回 500「未能写入配置文件，重启后会丢失」**，而文件其实写好了。
    这是「静默假失败」，和之前修的「静默假成功」是同一枚硬币的两面。
    """
    dm = _manager_by_kind(kind, tmp_path)
    assert dm._save_config() is True, f'{kind}: 保存成功却没返回 True'

    blocker = tmp_path / 'blocker'
    blocker.write_text('not a dir', encoding='utf-8')
    dm.config_path = str(blocker / 'sub' / 'devices.yaml')   # 父路径是文件 → 必然失败
    assert dm._save_config() is False, f'{kind}: 保存失败却没返回 False'


def test_mutation_is_not_blocked_by_unrelated_lock(tmp_path):
    """反向用例：没人持锁时增删必须立刻完成（防止「修成永远卡住」）。"""
    dm = _manager(tmp_path)
    t = threading.Thread(target=dm.add_device, args=(_device('dev_free'),), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), '没人持锁却卡住了'
    assert 'dev_free' in dm.devices


def test_add_device_still_works(tmp_path):
    """防止「加锁」加成「什么都做不了」。"""
    dm = _manager(tmp_path)
    assert dm.add_device(_device('dev_ok')) is True
    assert 'dev_ok' in dm.devices
    assert dm.remove_device('dev_ok') is True
    assert 'dev_ok' not in dm.devices


def test_save_config_not_deadlocked_by_add(tmp_path):
    """`_save_config` 自己也会取锁 —— 增删不能持锁调用它（不可重入锁会死锁）。

    这条用例的价值：如果实现写成
        with self._lock:
            self.devices[id] = cfg
            self._save_config()      # 内部再取一次锁 → 永久卡住
    那么这里会在 join 超时后失败。
    """
    dm = _manager(tmp_path)
    t = threading.Thread(target=dm.add_device, args=(_device('dev_dl'),), daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), 'add_device 卡住了 —— _save_config 与增删持锁范围重叠（死锁）'
    assert 'dev_dl' in dm.devices


# ---------------------------------------------------------------------------
# 静态：不允许对 self.devices 做活视图迭代
# ---------------------------------------------------------------------------

#: 允许迭代活视图的文件（目前没有）。加之前先想清楚为什么不快照。
ALLOWED_LIVE_ITERATION: dict[tuple[str, int], str] = {}

PROD_DIRS = ('采集层', '存储层', '展示层', '报警层', '智能层', '用户层', 'core')


def _iter_prod_files():
    for d in PROD_DIRS:
        root = REPO / d
        if not root.exists():
            continue
        for p in root.rglob('*.py'):
            if '__pycache__' in p.parts:
                continue
            yield p


def _is_self_devices(node: ast.AST) -> bool:
    """判断表达式是否为 `self.devices`（含 `self.devices.items()` 之类）。"""
    while isinstance(node, ast.Call):
        node = node.func
    while isinstance(node, ast.Attribute):
        if node.attr == 'devices' and isinstance(node.value, ast.Name) and node.value.id == 'self':
            return True
        node = node.value
    return False


def test_no_live_iteration_of_self_devices():
    """`for x in self.devices...` 必须是 `for x in list(self.devices...)...`。"""
    hits: list[str] = []
    for path in _iter_prod_files():
        rel = path.relative_to(REPO).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # `for` 语句与各种推导式都要查：推导式同样是活视图迭代
            if isinstance(node, ast.For):
                candidates = [node.iter]
            elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
                candidates = [g.iter for g in node.generators]
            elif isinstance(node, ast.DictComp):
                candidates = [g.iter for g in node.generators]
            else:
                continue

            for it in candidates:
                if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id in (
                    'list', 'tuple', 'set', 'sorted',
                ):
                    continue
                if isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute) and it.func.attr == 'copy':
                    continue
                if not _is_self_devices(it):
                    continue
                lineno = node.lineno
                if (rel, lineno) in ALLOWED_LIVE_ITERATION:
                    continue
                hits.append(f'{rel}:{lineno}  for ... in {ast.unparse(it)}')

    assert not hits, (
        '这些地方直接迭代 `self.devices` 的活视图。'
        '`add_device`/`remove_device` 在别的线程增删时会抛\n'
        '  RuntimeError: dictionary changed size during iteration\n'
        '被 except 吞掉 → 重连整批跳过 / 配置静默不写盘。\n'
        '改成 `list(self.devices...)`（快照）或 `.copy()`。\n\n'
        + '\n'.join('  ' + h for h in hits)
    )


def test_static_guard_is_not_blind():
    """元守卫：确认扫描器真的能认出来（否则上面那条永远绿）。"""
    src = 'for k, v in self.devices.items():\n    pass\n'
    tree = ast.parse(src)
    for_node = next(n for n in ast.walk(tree) if isinstance(n, ast.For))
    assert _is_self_devices(for_node.iter), '扫描器认不出 self.devices.items()'
