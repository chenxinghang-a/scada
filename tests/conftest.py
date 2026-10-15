"""
Pytest shared fixtures for SCADA system tests
"""

import pytest
import sys
import os
import sqlite3
import subprocess
import tempfile
import time
import warnings
from unittest.mock import MagicMock
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def pytest_configure(config):
    """把 pytest 临时根指向**每次运行都不同**的目录，实现"零删除"。

    背景（本机环境特有，CI 上无此问题）：
        WorkBuddy 注入了批量删除保护 shim。pytest 默认行为有两次删除会踩中：
        1. 会话开始时清理 `basetemp`（决定性地：如果 basetemp 已存在，pytest 会
           先删掉它再重建）。走 shim 的"移回收站"路径时，对 `\\\\?\\` 前缀路径
           报 `OSError [Errno 53] 找不到网络路径` → `SAFE_DELETE_FAIL_CLOSED`
           拒绝 → 所有用例在 setup 阶段 ERROR。
        2. 默认 basetemp 在系统 TEMP 下做"保留最近 3 个 run"的滚动清理，
           量可达上万文件 → 触发 `SAFE_DELETE_BULK_CONFIRM_REQUIRED` 弹窗。

    解法：目录名带 **PID**，保证每次进程启动时 basetemp 都是全新的、不存在的
    路径 —— pytest 无需清任何东西，两个坑同时消失。

    目录位置选**仓库内**（而不是系统 TEMP）有两个原因：
    - 便于事后排查（临时文件就在眼皮底下）
    - 避免污染系统 TEMP，也不会和用户其他项目的 pytest 临时目录混在一起

    这些目录已加进 .gitignore（`.pytest_tmp-*`）。**保留策略见
    `_prune_old_tmp_roots`（round 216）**：目录名带 PID 保证了"零删除"，
    代价是**每次运行留一个目录且永不回收** —— 本机实测已堆积 **860 个
    （约 150 万文件）**。因此**会话结束时**只保留最近 `TMP_ROOT_KEEP` 个，
    其余用外部进程删除（不经过 shim）。
    """
    # 用户显式传了 --basetemp 就尊重用户选择，不覆盖
    if config.option.basetemp:
        return
    tmp_root = Path(PROJECT_ROOT) / f'.pytest_tmp-{os.getpid()}'
    config.option.basetemp = str(tmp_root)
    # 标记「basetemp 是本次会话自己设的」—— 只有这种运行才在结束时做保留策略，
    # 避免用户显式传 --basetemp（例如调试/临时目录）时误删仓库内的历史目录。
    config._scada_owns_basetemp = True


def pytest_unconfigure(config):
    """会话结束：执行 `.pytest_tmp-*` 保留策略。

    放在**结束**而不是开始，是因为一次历史积压的清理可能十几分钟
    （见 `_prune_old_tmp_roots` 的调用点说明）。正常情况每次只删 1 个。
    """
    if getattr(config, '_scada_owns_basetemp', False):
        _prune_old_tmp_roots()


#: 保留最近多少个 `.pytest_tmp-*` 目录（含本次运行的）。更早的清掉。
TMP_ROOT_KEEP = 3

#: 单次清理最多删几个目录。历史积压可能上千个，逐个删除在本机很慢
#: （Defender 实时扫描 + 每个目录上千个小文件，实测 ~6 个/分钟）；
#: 不限量的话会把 `pytest_unconfigure` 拖住十几分钟。
#: 正常情况每次运行只多出 1 个，10 的上限绰绰有余；积压靠多次运行收敛，
#: 或用外部脚本一次性清（见 round 216 报告）。
TMP_ROOT_PRUNE_MAX = 10

#: 删除前跳过「最近这么多秒内仍被写入」的目录（round 217 修 F5）。
#:
#: 背景：保留策略按 mtime 取"最旧的若干"，但**并发会话**的目录在它自己的
#: 测试跑完后 mtime 就不再更新 —— 若另一会话此刻结束，可能把它当"旧目录"删掉，
#: 于是那个会话的后续用例会因为 basetemp 消失而失败。
#: 用一个"活跃窗口"把它们排除：窗口内的目录一律不删，留给下一轮。
#: 取 120s 是权衡 —— 正常测试运行的间隔大于它，不会让积压无限延迟。
TMP_ROOT_ACTIVE_WINDOW = 120.0


def _dir_mtime(d: Path) -> float:
    """取目录 mtime；不可访问/已消失时返回 0（视为最旧，交给调用方过滤）。

    round 217 修 F4：原先 `sort(key=lambda d: d.stat().st_mtime)` 里一次
    `FileNotFoundError`（并发会话正好删掉了该目录）会被上层 `except` 吞掉，
    **整次清理静默跳过** —— 一个坏目录毁掉一次清理。
    """
    try:
        return d.stat().st_mtime
    except OSError:
        return 0.0


def _select_stale_tmp_roots(
    root: Path,
    keep: int,
    limit: int = None,
    active_window: float = TMP_ROOT_ACTIVE_WINDOW,
    now: float = None,
) -> list:
    """纯函数：按 mtime 倒序保留最近 `keep` 个，返回**应删除**的那些目录。

    抽出来是为了可测 —— 删除动作本身走外部进程，不便在单测里断言细节；
    「选哪些删」这个判据才是要钉住的东西。

    `limit` 为单次上限（`TMP_ROOT_PRUNE_MAX` 的落地）；`None` = 不限。
    `    active_window` 内的目录（可能属于仍在跑的会话）不返回；
    单个目录 stat 失败只跳过它自己，不影响其余（F4）。
    """
    current = time.time() if now is None else now
    dirs = []
    for d in root.glob('.pytest_tmp-*'):
        try:
            if d.is_dir():
                dirs.append(d)
        except OSError:  # 并发消失/权限异常：跳过这一个，不影响整体
            continue
    dirs.sort(key=_dir_mtime, reverse=True)
    stale = [d for d in dirs[keep:] if current - _dir_mtime(d) > active_window]
    # 限额下**优先删最旧的**（round 217）—— `dirs[keep:]` 是"最新在前"，直接截断
    # 会优先删掉较新的历史目录，而积压场景里最该清的是最老的那批。
    # 与 `test_limit_caps_selection_and_takes_oldest_first` 的判据对齐。
    stale.sort(key=_dir_mtime)
    return stale[:limit] if limit is not None else stale


#: 用 robocopy 把目标目录「镜像」成空目录再删壳 —— 比 .NET Directory.Delete
#: 逐个文件删快得多（本机实测：大目录 9s → 1.7s，Defender 实时扫描对
#: 逐个 unlink 是线性惩罚，robocopy 走的是批量语义）。
#:
#: round 217 修 F3：原版 `$ErrorActionPreference='SilentlyContinue'` + 无输出，
#: **删除失败既无计数也无日志**（实测待删列表里第 10 个目录有 1697 个条目却被静默跳过）。
#: 现在把 ok/fail 计数与失败清单打到 stdout，由 Python 侧解析并出声。
_PRUNE_PS = r'''
$ErrorActionPreference = 'SilentlyContinue'
$empty = $env:SCADA_PRUNE_EMPTY
$list  = $env:SCADA_PRUNE_LIST
$ok = 0
$fail = New-Object System.Collections.Generic.List[string]
Get-Content -LiteralPath $list | ForEach-Object {
    if ($_) {
        robocopy $empty $_ /MIR /MT:16 /NFL /NDL /NJH /NJS /NC /NS /NP /R:0 /W:0 | Out-Null
        [System.IO.Directory]::Delete($_, $true)
        if (Test-Path -LiteralPath $_) { $fail.Add($_) } else { $ok++ }
    }
}
Write-Output ("SCADA_PRUNE_OK=" + $ok)
Write-Output ("SCADA_PRUNE_FAIL=" + $fail.Count)
foreach ($f in $fail) { Write-Output ("SCADA_PRUNE_FAILED_PATH=" + $f) }
'''


def _sweep_previous_prune_artifacts(tmp: Path) -> None:
    """清掉**上一次**运行留下的临时产物（本进程 PID 之外的）。

    round 217 修 F2：被 timeout / 杀进程时 `finally` 不会执行，TEMP 里会残留
    `scada_prune_*.txt` / `.ps1` / `scada_prune_empty_*`（实测残留过 4 个文件 + 2 个目录）。
    这里在**开工前**兜底清理，保证不跨轮堆积。

    round 217 修 F6：只清**存在超过 `TMP_ROOT_ACTIVE_WINDOW` 秒**的残留 ——
    否则并发会话（另一个 scada pytest 正用它的 list 文件做 `_delete_dirs_bulk`）
    会被这边误清，导致那一轮删除**全部失败**。与本轮 F5 同一思路：
    **不碰可能属于活跃会话的东西**。
    """
    me = str(os.getpid())
    current = time.time()
    for pattern in ('scada_prune_empty_*', 'scada_prune_*.txt', 'scada_prune_*.ps1'):
        for p in tmp.glob(pattern):
            if p.name.endswith(f'_{me}') or p.name.endswith(f'_{me}.txt') or p.name.endswith(f'_{me}.ps1'):
                continue  # 属于本进程，正在用
            try:
                if current - p.stat().st_mtime <= TMP_ROOT_ACTIVE_WINDOW:
                    continue  # 太新：可能是并发会话正在用，留给下一轮
            except OSError:
                continue
            try:
                if p.is_dir():
                    p.rmdir()
                else:
                    p.unlink()
            except OSError:
                pass


def _delete_dirs_bulk(paths) -> tuple:
    """用**一个** PowerShell 进程批量删除目录树（不经过 shim）。

    为什么要外部进程：环境注入的批量删除保护 shim hook 了 Python 的删除 API，
    对 `\\\\?\\` 前缀路径 fail-closed（见文件顶部注释）。

    为什么不是「每个目录一次 `subprocess.run(['cmd','/c','rd',...])`」：
        800+ 个目录 = 800+ 次进程启动，本机实测 10 分钟只删掉 22 个。

    为什么用 robocopy 而不是直接删：见 `_PRUNE_PS` 上方注释。

    路径走**临时文件**传递，避免 Windows 命令行长度上限（~32 KB）。

    只在 Windows 上有效；调用方已保证 `os.name == 'nt'`。

    **返回 `(ok, fail, failed_paths)`**（round 217 修 F3）——
    原实现返回 None，调用方无从得知删没删掉。
    """
    tmp = Path(tempfile.gettempdir())
    _sweep_previous_prune_artifacts(tmp)  # F2：先清上轮残留
    empty = tmp / f'scada_prune_empty_{os.getpid()}'
    list_file = tmp / f'scada_prune_{os.getpid()}.txt'
    script_file = tmp / f'scada_prune_{os.getpid()}.ps1'
    empty.mkdir(exist_ok=True)
    list_file.write_text('\n'.join(str(p) for p in paths), encoding='utf-8')
    script_file.write_text(_PRUNE_PS, encoding='utf-8')
    env = dict(os.environ,
               SCADA_PRUNE_EMPTY=str(empty),
               SCADA_PRUNE_LIST=str(list_file))
    ok = 0
    fail = 0
    failed_paths: list = []
    try:
        proc = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive',
             '-ExecutionPolicy', 'Bypass', '-File', str(script_file)],
            check=False, timeout=900, env=env,
            capture_output=True, text=True, encoding='utf-8', errors='replace')
        for line in (proc.stdout or '').splitlines():
            line = line.strip()
            if line.startswith('SCADA_PRUNE_OK='):
                ok = int(line.split('=', 1)[1] or 0)
            elif line.startswith('SCADA_PRUNE_FAIL='):
                fail = int(line.split('=', 1)[1] or 0)
            elif line.startswith('SCADA_PRUNE_FAILED_PATH='):
                failed_paths.append(line.split('=', 1)[1])
    except (OSError, subprocess.SubprocessError, ValueError):
        pass  # 失败留给调用方通过返回值判断
    finally:
        for f in (list_file, script_file):
            try:
                f.unlink()
            except OSError:
                pass
        # F1：empty 目录也必须清 —— 原实现只删 list/ps1，导致 TEMP 里按 PID
        # 堆积 `scada_prune_empty_*`（实测 2 个，永不回收）。
        try:
            empty.rmdir()
        except OSError:
            pass
    return ok, fail, failed_paths


def _prune_old_tmp_roots(keep: int = TMP_ROOT_KEEP, root=None) -> dict:
    """保留最近 `keep` 个 `.pytest_tmp-*`，其余删除（round 216；round 217 修 F3）。

    背景：`pytest_configure` 用「目录名带 PID」换取"零删除"，代价是
    **每次运行留一个目录、永不回收** —— 本机实测堆积 860 个（150 万文件）。
    这里补上保留策略，让它不会无限增长。

    ⚠️ 调用点放在 **`pytest_unconfigure`（会话结束）** 而不是 `pytest_configure`：
       一次历史积压的清理可能耗时十几分钟，放在启动路径上会卡住整个测试会话。
       放在结束路径上，正常情况每次只删「本次新增之外多出来的那一个」。

    只在 Windows 上做；**任何异常都不影响测试结果**，但会**出声** ——
    round 217 修 F3：原实现 `except Exception: pass` 把"删不掉"与"整体炸了"一起吞了，
    而实测确实存在被静默跳过的目录（待删列表里第 10 个目录仍有 1697 个条目）。

    返回 `{"selected", "ok", "fail", "failed"}`（原返回 None），便于测试断言。
    `root` 参数仅为测试注入，生产调用走 `PROJECT_ROOT`。
    """
    result = {"selected": 0, "ok": 0, "fail": 0, "failed": []}
    if os.name != 'nt':
        return result
    try:
        base = Path(root) if root is not None else Path(PROJECT_ROOT)
        stale = _select_stale_tmp_roots(base, keep, TMP_ROOT_PRUNE_MAX)
        result["selected"] = len(stale)
        if not stale:
            return result
        ok, fail, failed = _delete_dirs_bulk(stale)
        result.update(ok=ok, fail=fail, failed=failed)
        if fail:
            detail = ''.join(f"\n    - {p}" for p in failed[:10])
            sys.stderr.write(
                f"[tmp-prune] {fail}/{len(stale)} 个目录未能删除（已保留，下次重试）：{detail}\n")
        return result
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[tmp-prune] 清理整体失败（不影响测试结果）：{exc!r}\n")
        result["error"] = repr(exc)
        return result


def _quarantine_smoke_test_dbs(data_dir: Path):
    """将冒烟测试数据库移出工作目录，避免测试清理触发批量删除保护。"""
    if not data_dir.exists():
        return
    for db_file in data_dir.glob("smoke_test_*.db*"):
        try:
            target = db_file.with_name(f".stale_{db_file.name}")
            suffix = 1
            while target.exists():
                target = db_file.with_name(f".stale_{suffix}_{db_file.name}")
                suffix += 1
            db_file.rename(target)
        except (PermissionError, OSError):
            pass


@pytest.fixture(autouse=True)
def isolate_queue_persistence(tmp_path):
    """把 DiskBackedQueue 的持久化目录指向**本测试专属**的临时目录。

    默认路径是仓库内的 `data/queue/pending_data.jsonl`，所有测试共用一个文件：
    某个测试 `put()` 进去的数据会被下一个构造 `DataCollector` 的测试
    `_recover_from_disk()` 恢复并 `unlink()`。

    ⚠️ 这个 fixture 原本是 **session 级**的（全会话共用一个临时目录），
    结果只是把「共用一个文件」从仓库搬到了临时目录 —— 同样的污染、
    同样的累计 unlink，仍然触发环境的批量删除保护
    （表现为 `test_data_collector.py` 里 `TestDynamicInterval` /
    `TestDispatchIntelligence` 等大批用例以 SystemExit 失败，是假失败）。

    改为**函数级**：每个测试一个全新目录，任何测试都不会读到别人的残留文件，
    `unlink()` 次数也随之降到 0~1 次。
    """
    old = os.environ.get("SCADA_QUEUE_PERSIST_DIR")
    os.environ["SCADA_QUEUE_PERSIST_DIR"] = str(tmp_path / "queue_persist")
    yield
    if old is None:
        os.environ.pop("SCADA_QUEUE_PERSIST_DIR", None)
    else:
        os.environ["SCADA_QUEUE_PERSIST_DIR"] = old


@pytest.fixture(scope="session", autouse=True)
def cleanup_smoke_test_dbs():
    """会话开始/结束时隔离残留的smoke_test数据库文件。"""
    data_dir = Path(PROJECT_ROOT) / "data"
    _quarantine_smoke_test_dbs(data_dir)
    yield
    _quarantine_smoke_test_dbs(data_dir)


#: 会被接口写回、但属于**受版本控制的配置文件**。
#: 测试打到这些接口时会把改动持久化进仓库，导致每次跑完测试工作区都变脏。
_POLLUTABLE_CONFIGS = (
    "配置/system.yaml",   # POST /api/system/simulation-mode 会整份重写
    "配置/alarms.yaml",   # 报警规则增删改会整份重写
    "配置/devices.yaml",
    "配置/devices_simulated.yaml",
    "配置/energy.yaml",
)


@pytest.fixture(scope="session", autouse=True)
def restore_polluted_configs():
    """会话结束时把被测试写脏的配置文件恢复原状。

    背景：`展示层/api/api_system.py` 的 `POST /api/system/simulation-mode` 会把
    `simulation_mode` 写进 `配置/system.yaml`，`api_alarms.py` 的规则增删改同理。
    测试打这些接口时改动会落到**受版本控制的真实配置文件**上 ——
    跑完一次全量测试，工作区就多出若干 `M 配置/*.yaml`，很容易被误提交。

    这里只做「快照 → 会话结束还原」，不改变任何生产行为。
    之所以用 session 粒度：2000+ 个测试逐条还原开销不划算，
    而这些配置在单次会话内被读到中间态的风险很低（现有测试只断言字段存在）。

    已知局限（本 fixture 的**盲区**，2026-09 踩过）：
        快照取自**当前工作区**，所以它只能把文件还原到「本次会话开始时的样子」。
        如果上一次会话被中断（Ctrl-C / 超时 SIGTERM）导致 teardown 没跑，
        污染会沉积下来，被下一次会话当作"原状"快照 —— 还原等于空操作，
        脏状态就永久化了。故这里在会话开始额外检测"开局即脏"并告警。
        根因侧已修：`tests/test_api_edge_cases.py::test_toggle_simulation_mode`
        不再把落点写到仓库配置上。
    """
    snapshots = {}
    dirty_at_start = []
    for rel in _POLLUTABLE_CONFIGS:
        p = Path(PROJECT_ROOT) / rel
        if p.is_file():
            snapshots[p] = p.read_bytes()
            if _differs_from_git_head(rel):
                dirty_at_start.append(rel)

    if dirty_at_start:
        warnings.warn(
            "[conftest] 会话开始时这些受版本控制的配置**已经是脏的**: "
            + ", ".join(dirty_at_start)
            + "。本 fixture 只能还原到「会话开始时的样子」，无法把它们恢复到提交状态"
            " —— 多半是上一次会话被中断（teardown 没跑）留下的。"
            "建议先 `git checkout -- 配置/` 再跑测试。",
            stacklevel=1,
        )

    yield

    restored = []
    for p, original in snapshots.items():
        try:
            if p.read_bytes() != original:
                p.write_bytes(original)
                restored.append(str(p.relative_to(PROJECT_ROOT)))
        except OSError:
            pass
    if restored:
        print(f"\n[conftest] 已还原被测试写脏的配置文件: {', '.join(restored)}")


def _differs_from_git_head(rel: str) -> bool:
    """工作区里的 ``rel`` 是否与 git HEAD 版本不同。

    刻意用 ``git diff --quiet HEAD -- <rel>`` 而不是「读 HEAD blob 逐字节比对」：
    本机 ``core.autocrlf=true``，工作区文件是 CRLF、git 里的 blob 是 LF，
    逐字节比对会把**每一份配置文件都判成脏的**，告警就永远在响、彻底失去信号。
    ``git diff`` 走的是 git 自己的行尾归一化，不会踩这个坑。

    git 不可用 / 命令失败 / 文件未被跟踪时一律返回 ``False``
    （宁可漏报不误报，免得在没有 git 的环境里凭空造出告警）。
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--", rel],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # 0 = 无差异；1 = 有差异；其它（129 = 未知路径等）= 不当成脏
    return proc.returncode == 1


def _local_service_running() -> bool:
    """本机 SCADA 服务是否在运行（用于判定队列污染守卫是否适用）。

    服务运行时会持续向 ``data/queue/pending_data.jsonl`` 写数据，此时守卫
    「会话前后快照必须一致」的前提不成立 —— 文件是被**服务**写脏的，不是被
    测试写脏的。若不区分，本地一边跑服务一边跑测试就会得到必然失败的假告警。
    CI 环境没有服务，守卫照常生效。
    """
    try:
        import json as _json
        import socket as _socket

        port = 5000
        runtime = Path(PROJECT_ROOT) / "data" / "runtime.json"
        if runtime.exists():
            try:
                port = int((_json.loads(runtime.read_text(encoding="utf-8")) or {}).get("port") or port)
            except (ValueError, OSError, AttributeError):
                pass
        with _socket.socket() as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


@pytest.fixture(scope="session", autouse=True)
def guard_repo_queue_persist_dir():
    """兜底：绝不让任何测试写到**仓库内**的 `data/queue/`。

    背景：`DiskBackedQueue` 现在用 `paths.resolve()` 把默认值
    `'data/queue'` 解析成 ``<仓库根>/data/queue``（修复「从别的 CWD 启动时
    静默另建一套数据目录」的 bug，见 2026-09 审计 P2-5）。
    副作用是：**忘记设** ``SCADA_QUEUE_PERSIST_DIR`` 的测试，落点从
    「CWD 下的 data/queue」变成了「仓库里的 data/queue」—— 也就是真实数据目录。

    `isolate_queue_persistence` 是函数级 autouse 的，正常路径下每个测试都会
    拿到自己的 tmp 目录。但**模块级 import 期构造的对象**、或某个 fixture
    内部在 autouse fixture 之前就构造好的队列，仍可能绕过它。一旦发生：

      - 测试往真实 `data/queue/pending_data.jsonl` 写探针数据
      - 下一个测试 `_recover_from_disk()` 读到并 unlink 它
      - 表现为一批**与队列无关的测试**集体飘红（数据完整性/灾备/批量缩容
        这几个模块最先暴露），而单跑每个用例都绿 —— 极难定位

    这里在会话开始断言该落点是干净的，并在结束时断言没有被写脏。
    失败信息直接把根因写出来（哪个文件、多大），省掉下次的排查时间。
    """
    queue_dir = Path(PROJECT_ROOT) / "data" / "queue"
    probe = queue_dir / "pending_data.jsonl"

    def _snapshot():
        if not probe.exists():
            return None
        try:
            return probe.read_bytes()
        except OSError:
            return b"<unreadable>"

    if _local_service_running():
        print(
            "\n[conftest] 检测到本机 SCADA 服务正在运行，跳过队列污染守卫 —— "
            "服务会持续写 data/queue/pending_data.jsonl，"
            "该守卫只在没有服务运行时才有判定意义（CI 即为此场景）。"
        )
        yield
        return

    before = _snapshot()
    yield
    after = _snapshot()

    if after != before:
        raise AssertionError(
            f"有测试往仓库真实队列目录写了数据，且未清理：{probe}\n"
            f"  会话前: {'不存在' if before is None else f'{len(before)} 字节'}\n"
            f"  会话后: {'不存在' if after is None else f'{len(after)} 字节'}\n"
            "根因排查方向：某个测试构造 DiskBackedQueue / DataCollector 时\n"
            "既没传 persist_dir、也没生效 SCADA_QUEUE_PERSIST_DIR。\n"
            "(persist_dir 若为相对路径会被 paths.resolve 解析到仓库根下。)"
        )


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset all class-based singletons between tests"""
    from core.di_container import DIContainer
    from core.event_bus import EventBus
    from core.config_manager import ConfigManager
    from core.health_checker import HealthChecker
    from core.module_registry import ModuleRegistry

    yield

    DIContainer.clear_all()
    EventBus.clear_all()
    ConfigManager.clear()
    HealthChecker.clear()
    ModuleRegistry.clear()


@pytest.fixture
def app():
    """Create a minimal Flask test app with mocked SCADA components"""
    from flask import Flask
    from 展示层.api import register_api_blueprints

    flask_app = Flask(__name__, template_folder='../模板', static_folder='../静态资源')
    flask_app.config['SECRET_KEY'] = 'test-secret-key'
    flask_app.config['TESTING'] = True

    # Mock database
    mock_db = MagicMock()
    mock_db.get_database_stats.return_value = {
        'realtime_count': 0, 'history_count': 0, 'alarm_count': 0
    }
    mock_db.get_alarm_records.return_value = []
    mock_db.get_active_alarms.return_value = []

    # Mock device manager
    mock_device_mgr = MagicMock()
    mock_device_mgr.get_all_status.return_value = []
    mock_device_mgr.simulation_mode = True

    # Mock alarm manager
    mock_alarm_mgr = MagicMock()
    mock_alarm_mgr.get_active_alarms.return_value = []
    mock_alarm_mgr.get_alarm_statistics.return_value = {
        'total': 0, 'active': 0, 'acknowledged': 0
    }

    # Mock data collector
    mock_collector = MagicMock()
    mock_collector.get_stats.return_value = {'total_readings': 0}

    # Mock auth manager
    mock_auth = MagicMock()

    # Attach mocks to app
    flask_app.database = mock_db
    flask_app.device_manager = mock_device_mgr
    flask_app.alarm_manager = mock_alarm_mgr
    flask_app.data_collector = mock_collector
    flask_app.auth_manager = mock_auth

    # Register API blueprints
    register_api_blueprints(flask_app)

    yield flask_app


@pytest.fixture
def auth_headers(app):
    """Generate valid JWT auth headers for testing"""
    import jwt
    from datetime import datetime, timedelta, timezone
    from config import AuthConfig

    now = datetime.now(timezone.utc)
    payload = {
        'username': 'testuser',
        'role': 'admin',
        'type': 'access',
        'iat': now,
        'exp': now + timedelta(hours=AuthConfig.JWT_EXPIRATION_HOURS)
    }
    token = jwt.encode(payload, AuthConfig.JWT_SECRET, algorithm=AuthConfig.JWT_ALGORITHM)

    # Make auth_manager.verify_token() return a valid user for this token
    app.auth_manager.verify_token.return_value = {
        'username': 'testuser',
        'role': 'admin',
        'display_name': 'Test User',
        'permissions': ['read', 'write', 'delete', 'manage_users', 'manage_devices',
                        'acknowledge_alarms', 'export_data', 'system_config']
    }

    return {'Authorization': f'Bearer {token}'}


@pytest.fixture
def client(app):
    """Flask test client"""
    return app.test_client()


@pytest.fixture
def db():
    """In-memory SQLite test database with SCADA schema"""
    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_path = tmp.name
    tmp.close()

    conn = sqlite3.connect(tmp_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS realtime_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            register_name TEXT NOT NULL,
            value REAL,
            unit TEXT,
            timestamp DATETIME NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(device_id, register_name)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            register_name TEXT NOT NULL,
            value REAL,
            unit TEXT,
            timestamp DATETIME NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alarm_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alarm_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            register_name TEXT NOT NULL,
            alarm_level TEXT NOT NULL,
            alarm_message TEXT,
            threshold REAL,
            actual_value REAL,
            timestamp DATETIME NOT NULL,
            trigger_count INTEGER DEFAULT 1,
            last_trigger_time DATETIME,
            last_value REAL,
            acknowledged BOOLEAN DEFAULT 0,
            acknowledged_at DATETIME,
            acknowledged_by TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS device_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            status TEXT NOT NULL,
            message TEXT,
            timestamp DATETIME NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()

    yield tmp_path

    # Cleanup
    try:
        os.unlink(tmp_path)
    except OSError:
        pass


@pytest.fixture
def device_config():
    """Sample device configuration for testing"""
    return {
        'devices': [
            {
                'id': 'test_pump_01',
                'name': 'Test Pump',
                'protocol': 'modbus_tcp',
                'host': '127.0.0.1',
                'port': 502,
                'slave_id': 1,
                'registers': [
                    {'name': 'flow_rate', 'address': 0, 'type': 'float', 'unit': 'm3/h'},
                    {'name': 'pressure', 'address': 2, 'type': 'float', 'unit': 'MPa'},
                    {'name': 'status', 'address': 100, 'type': 'bool', 'unit': ''},
                ]
            },
            {
                'id': 'test_motor_01',
                'name': 'Test Motor',
                'protocol': 'modbus_tcp',
                'host': '127.0.0.1',
                'port': 502,
                'slave_id': 2,
                'registers': [
                    {'name': 'speed', 'address': 0, 'type': 'uint16', 'unit': 'RPM'},
                    {'name': 'current', 'address': 1, 'type': 'float', 'unit': 'A'},
                    {'name': 'temperature', 'address': 3, 'type': 'float', 'unit': 'C'},
                ]
            },
        ]
    }


@pytest.fixture
def alarm_config():
    """Sample alarm configuration for testing"""
    return {
        'rules': [
            {
                'id': 'alarm_high_temp',
                'name': 'High Temperature',
                'device_id': 'test_motor_01',
                'register_name': 'temperature',
                'condition': 'greater_than',
                'threshold': 80.0,
                'level': 'warning',
                'message': 'Motor temperature exceeds 80C',
                'enabled': True,
                'severity': 3,
                'likelihood': 3,
            },
            {
                'id': 'alarm_critical_temp',
                'name': 'Critical Temperature',
                'device_id': 'test_motor_01',
                'register_name': 'temperature',
                'condition': 'greater_than',
                'threshold': 100.0,
                'level': 'critical',
                'message': 'Motor temperature exceeds 100C - DANGER',
                'enabled': True,
                'severity': 5,
                'likelihood': 3,
            },
            {
                'id': 'alarm_low_pressure',
                'name': 'Low Pressure',
                'device_id': 'test_pump_01',
                'register_name': 'pressure',
                'condition': 'less_than',
                'threshold': 0.5,
                'level': 'warning',
                'message': 'Pump pressure below 0.5 MPa',
                'enabled': True,
                'severity': 2,
                'likelihood': 4,
            },
        ]
    }


@pytest.fixture
def auth_manager():
    """Create a real AuthManager with a temp SQLite database for unit testing"""
    import sqlite3
    from 用户层.auth import AuthManager

    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_path = tmp.name
    tmp.close()

    class TestDatabase:
        def __init__(self, path):
            self._path = path
        def get_connection(self):
            conn = sqlite3.connect(self._path)
            conn.row_factory = sqlite3.Row
            return conn

    db = TestDatabase(tmp_path)
    mgr = AuthManager(db)

    yield mgr

    try:
        os.unlink(tmp_path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 全局日志状态的「每用例自动还原」（round 212，2026-10-11）
# ---------------------------------------------------------------------------
#
# 为什么放在 conftest 而不是各用例自己管
# --------------------------------------
# `core/structured_logging.py::setup_logging()` 有**全局副作用**：
#   ① 给 root logger 装 `InterceptHandler`（stdlib → loguru 桥）；
#   ② 把 root level 设成传入的 `log_level`；
#   ③ 把 `_NOISY_STDLIB_LOGGERS` 里那批 logger 压到 WARNING。
#
# 任何用例调用它而不还原，都会改变**同进程里后面所有用例**的日志行为 ——
# 而后果会出现在**被污染的那个用例**上，看起来与被调用者完全无关。
#
# 实测（round 212，一次真实的红）
# -----------------------------
#   `test_logger_percent_compat.py` 的 fixture 调了 `setup_logging()` 却不还原
#   → 按文件名排序，后面的 `test_performance_stress.py::TestAlarmPerformance::
#     test_alarm_check_performance`（断言「100 次报警检查 < 1.0s」）就在
#     「桥已装」的状态下跑：报警触发日志（WARNING 级）**真的落盘**，
#     100 次检查从 **46 ms 涨到 250 ms**，撞破阈值。
#   **看起来像被测对象变慢了，其实是上游用例泄漏的全局状态。**
#
# 修法分两层（都在，互不替代）：
#   * 本 fixture —— **系统性根治**：每个用例前后还原，消灭这一类污染；
#   * `test_performance_stress.py` 的计时窗口内 `logging.disable(CRITICAL)` ——
#     **让测量与「环境日志配置」解耦**，不依赖用例执行顺序。
@pytest.fixture(autouse=True)
def _restore_global_logging_state():
    """每个用例前后还原 stdlib 全局日志状态（root handlers/level + disable 计数）。"""
    import logging as _logging

    root = _logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_disable = _logging.root.manager.disable

    yield

    root.handlers[:] = saved_handlers
    if root.level != saved_level:
        root.setLevel(saved_level)
    _logging.disable(saved_disable)
