# -*- coding: utf-8 -*-
"""打包产物**运行时**冒烟：起 exe → 等端口 → 判健康 → 整树杀掉。

为什么必须有这一步
------------------
``build-backend`` job 原先只做 ``test -f dist/scada-backend/scada-backend.exe``，
也就是**只证明文件存在**。但 PyInstaller 的「构建成功」≠「产物能启动」：

  - 动态导入的模块（``importlib`` / ``__import__`` / 插件注册表 /
    ``pkg_resources`` 入口点）缺 ``hiddenimports`` 时，**打包阶段不会报错**；
  - 只有在**进程启动**那一刻才会 ``ModuleNotFoundError``；
  - 于是构建成功、exe 存在、CI 全绿 —— 而产物一运行就崩。

这是典型的 **fail-open 闸门**：闸门装了，但它测的性质和「能不能用」无关。

本脚本把闸门改成 fail-closed，判四件事：

  1. 进程**活着**（没在启动阶段就退出 —— 缺 hiddenimport 的典型症状）；
  2. 端口**起来了**；
  3. ``/api/health/status`` 返回 200 且 ``success == true``；
  4. 健康判定**没有确定的负面信号**（模块未就绪 / 任一检查项 unhealthy），
     且后台巡检线程**已跑过至少一轮**（证明它在这份打包产物里也能起线程）。

第 4 条是这里最容易被做浅的地方。``/api/health/status`` 是**探活**接口，
它在磁盘写满、内存打爆、落库停摆时**照样返回 200** —— 只看状态码等于没测。
真正的结论在响应体里（``global_status`` / ``modules`` / ``checks``）。

**刻意没有「跳过」分支**：exe 不存在、进程提前退出、端口一直不开、
健康端点非 200、判定出负面信号 —— 一律 exit 1。闸门不允许静默放行。

用法::

    python .github/scripts/smoke_packaged_backend.py \
        --exe dist/scada-backend/scada-backend.exe

退出码::

    0  通过
    1  闸门失败（任何原因）
    2  命令行用法错误

注意：不带参数启动时后端进**模拟模式**（``devices_simulated.yaml``），
不会去连真实设备 —— 这是冒烟该有的姿势。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

#: 默认候选端口。模拟模式用 ``WebConfig.PORT``（5000），真实/模拟器模式用 5001。
DEFAULT_PORTS = "5000,5001"

#: 默认健康端点。它是**唯一无需认证**的健康接口
#: （``/status/detail`` 与 ``/checks`` 都挂了 ``@jwt_required``）。
DEFAULT_HEALTH_PATH = "/api/health/status"

#: 端口起来后等待健康检查解析的默认秒数。
#:
#: 必须**显著大于** ``run.py`` 里 ``start_periodic_checks(interval=30)`` 的间隔：
#: 巡检线程是「先 sleep 再跑第一轮」（``core/health_checker.py``），
#: 所以闸门至少要等一个完整间隔才能看到检查项从 ``unknown`` 变成结论。
#: 这是两处独立常量，靠 ``tests/test_packaged_backend_smoke.py`` 里的守卫用例绑住。
DEFAULT_CHECKS_TIMEOUT = 150.0

#: 「模块未就绪」的状态集合，口径与 ``展示层/api/api_health.py`` 一致。
BAD_MODULE_STATUSES = ("error", "disabled", "unavailable")

#: 唯一被当作硬失败的检查项状态。
UNHEALTHY = "unhealthy"

#: 响应体读取上限，防止异常产物吐出巨量内容把 CI 日志刷爆。
_MAX_BODY_BYTES = 1 << 20


# --------------------------------------------------------------------------
# 纯函数（可单测，见 tests/test_packaged_backend_smoke.py）
# --------------------------------------------------------------------------

def candidate_ports(raw: str) -> list[int]:
    """把 ``"5000,5001"`` 解析成 ``[5000, 5001]``（去重、保序）。"""
    out: list[int] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            port = int(part)
        except ValueError as exc:
            raise ValueError(f"非法端口: {part!r}") from exc
        if not (1 <= port <= 65535):
            raise ValueError(f"端口越界: {port}")
        if port not in out:
            out.append(port)
    if not out:
        raise ValueError("候选端口列表为空")
    return out


def runtime_json_candidates(exe_path: Path) -> list[Path]:
    """列出 ``runtime.json`` 的可能位置。

    冻结 onedir 布局下 ``paths._get_project_root()`` 返回 exe 所在目录，
    且因为 ``_internal/`` 存在，``_BASE = exe_dir/_internal`` ——
    所以 ``DATA_DIR = exe_dir/_internal/data``。
    但 onefile / 配置目录平铺的布局会退化成 ``exe_dir/data``，
    两种都列出来，谁先存在用谁（不要猜，探测）。
    """
    exe_dir = exe_path.parent
    return [exe_dir / "_internal" / "data" / "runtime.json",
            exe_dir / "data" / "runtime.json"]


def read_runtime_port(runtime_json: Path) -> int | None:
    """从 ``runtime.json`` 里读出实际监听端口；读不到 / 格式不对返回 ``None``。

    刻意不抛异常：这是「**辅助**发现端口」的手段，不是判定依据 ——
    真正的判定依据是健康端点。辅助手段失败不该直接判死。
    """
    try:
        data = json.loads(runtime_json.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    port = data.get("port")
    # 注意 bool 是 int 的子类，必须显式排除，否则 True 会被当成端口 1
    if isinstance(port, bool) or not isinstance(port, int):
        return None
    return port if 1 <= port <= 65535 else None


def tail(text: str, limit: int = 4000) -> str:
    """取文本末尾 ``limit`` 个字符（失败时打日志用，别把 CI 日志刷爆）。"""
    if not text:
        return "(空)"
    return text if len(text) <= limit else "…(前略)\n" + text[-limit:]


def find_open_port(ports: list[int], host: str = "127.0.0.1") -> int | None:
    """返回第一个能连上的端口；都不通返回 ``None``。"""
    for port in ports:
        try:
            with socket.socket() as sock:
                sock.settimeout(0.5)
                if sock.connect_ex((host, port)) == 0:
                    return port
        except OSError:
            continue
    return None


def http_status(url: str, timeout: float) -> tuple[int | None, str]:
    """GET 一次，返回 ``(status_code, 响应体)``。连不上 / 超时返回 ``(None, 原因)``。

    响应体**必须整份读回**：健康结论全在 body 里，读前 N 字节会把 JSON 截断，
    于是 ``json.loads`` 必然失败 —— 闸门就变成了「响应是不是合法 JSON」的随机测试。
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read(_MAX_BODY_BYTES)
            return int(resp.status), raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(_MAX_BODY_BYTES)
            body = raw.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 读错误体失败不影响主判定
            body = f"HTTPError: {exc.reason}"
        return int(exc.code), body
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def assess_health(payload: Any) -> dict:
    """把 ``/api/health/status`` 的响应体归纳成判定结果（纯函数）。

    响应结构（见 ``展示层/api/api_health.py`` 与 ``core/health_checker.py``）::

        {"success": true,
         "data": {"global_status": "healthy|degraded|unhealthy",
                  "modules": {"data_collector": {"status": "initialized"}, ...},
                  "checks": {"global_status": ...,
                             "checks": {"database": {"status": ...,
                                                     "last_check": iso8601 | None},
                                        ...},
                             "total_checks": 6}}}

    返回::

        {"ok": bool,            # False = 闸门必须失败
         "reason": str,         # ok=False 的原因
         "global_status": str | None,
         "modules": {名: 状态},
         "checks": {名: 状态},
         "resolved": bool}      # 是否有检查项跑过（证明后台巡检线程在转）

    判定口径 —— **只对确定的负面信号判负**：

      - ``success != true`` / 缺 ``data``           → 判负（接口本身坏了）
      - ``global_status == 'unhealthy'``            → 判负
      - ``checks.global_status == 'unhealthy'``     → 判负（巡检线程自己的结论）
      - 任一模块 ``error|disabled|unavailable``      → 判负
      - 任一检查项 ``unhealthy``                     → 判负

    ``unknown``（巡检还没跑第一轮）与 ``degraded``（数据新鲜度临界）**不判负**：
    前者是启动瞬态，后者会随 runner 快慢抖动 —— 会抖的闸门早晚被人关掉。

    ``unhealthy_modules`` 这个字段**不采信**，一律从 ``modules`` 重算：
    一个派生字段写错了，采信它的人就会跟着错。
    """
    result: dict = {"ok": True, "reason": "", "global_status": None,
                    "modules": {}, "checks": {}, "resolved": False}

    def _fail(reason: str) -> dict:
        return {**result, "ok": False, "reason": reason}

    if not isinstance(payload, dict):
        return _fail("响应不是 JSON 对象")

    if payload.get("success") is not True:
        return _fail(f"success != true（{payload.get('success')!r}）—— 接口返回了业务错误")

    data = payload.get("data")
    if not isinstance(data, dict):
        return _fail("data 字段缺失或不是对象")

    result["global_status"] = data.get("global_status")

    modules = data.get("modules")
    modules = modules if isinstance(modules, dict) else {}
    result["modules"] = {
        name: (info.get("status") if isinstance(info, dict) else None)
        for name, info in modules.items()
    }

    checks_wrapper = data.get("checks")
    checks_wrapper = checks_wrapper if isinstance(checks_wrapper, dict) else {}
    inner = checks_wrapper.get("checks")
    inner = inner if isinstance(inner, dict) else {}
    result["checks"] = {
        name: (info.get("status") if isinstance(info, dict) else None)
        for name, info in inner.items()
    }
    result["resolved"] = any(
        isinstance(info, dict) and info.get("last_check") for info in inner.values()
    )

    if result["global_status"] == UNHEALTHY:
        return _fail("global_status == 'unhealthy'")

    if checks_wrapper.get("global_status") == UNHEALTHY:
        return _fail("checks.global_status == 'unhealthy'（巡检线程自己的结论）")

    bad_modules = sorted(n for n, s in result["modules"].items() if s in BAD_MODULE_STATUSES)
    if bad_modules:
        return _fail(f"模块未就绪 {bad_modules} —— 打包缺 hiddenimport 的典型表现")

    bad_checks = sorted(n for n, s in result["checks"].items() if s == UNHEALTHY)
    if bad_checks:
        return _fail(f"健康检查项为 unhealthy: {bad_checks}")

    return result


# --------------------------------------------------------------------------
# 进程管理
# --------------------------------------------------------------------------

def kill_tree(pid: int) -> None:
    """杀掉整棵进程树（含根进程）。

    必须整树：PyInstaller onedir 的 exe 会拉起子进程（采集线程 / 后台任务），
    只杀父进程会留下孤儿占着端口，让**下一次** CI 跑出莫名其妙的"端口被占"。

    POSIX 分支额外显式 ``os.kill`` 根进程：``pkill -P`` 只杀子进程，
    光靠它根进程会一直活着，``proc.wait()`` 白等到超时才由兜底 ``kill()`` 收掉。
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, check=False)
        else:
            subprocess.run(["pkill", "-P", str(pid)], capture_output=True, check=False)
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _snapshot_tree(root: Path) -> tuple[set[str], set[str]]:
    """记录一棵树下的 ``(文件相对路径, 目录相对路径)``。"""
    files: set[str] = set()
    dirs: set[str] = set()
    for path in root.rglob("*"):
        try:
            rel = str(path.relative_to(root))
            if path.is_dir():
                dirs.add(rel)
            elif path.is_file():
                files.add(rel)
        except OSError:
            continue
    return files, dirs


def cleanup_created(exe_dir: Path, before: tuple[set[str], set[str]]) -> list[str]:
    """删掉冒烟运行期间在产物目录里**新建**的文件与空目录，返回删不掉的路径。

    为什么必须做：冒烟会**真启动产物**，而产物首次运行会在自己旁边建库 /
    写 ``runtime.json`` / 建 ``logs``。``scada-backend.spec`` 的 ``datas``
    **不含** ``data/``，也就是说这些目录是纯运行期产物 —— 若不清理，
    紧接着的 ``upload-artifact`` 会把它们一起打包出去，产物就被运行期状态污染了
    （本机手动跑还会在 `dist-scada-<版本>/` 里留下垃圾）。

    用「运行前后清单求差」而不是「按目录名删固定几个」，而且**只删本次新建的**：
    哪天 spec 真的开始随包携带 ``data/`` 预置内容、或携带一个空目录，
    按名字删会把产物自己的东西一起删掉。
    """
    before_files, before_dirs = before
    after_files, after_dirs = _snapshot_tree(exe_dir)

    failed: list[str] = []
    for rel in sorted(after_files - before_files,
                      key=lambda p: p.count(os.sep), reverse=True):
        try:
            (exe_dir / rel).unlink()
        except (OSError, SystemExit):
            # SystemExit 是本机 WorkBuddy 注入的批量删除保护（safe-delete guard）
            # 在「本轮删除额度用完」时的拒绝方式 —— 它抛 SystemExit(1) 而不是
            # OSError，所以只 catch OSError 会让异常穿出 smoke()，把一次
            # 干净的成功判成崩溃。
            #
            # 这里捕获它**不是绕过安全控制**：文件确实没有被删掉，只是记进
            # `failed` 由调用方打 WARN。CI 上没有这个 guard，正常路径不受影响。
            failed.append(rel)

    # 清掉本次新建的空目录（深度优先，免得父目录被非空子目录挡住）
    for rel in sorted(after_dirs - before_dirs,
                      key=lambda p: p.count(os.sep), reverse=True):
        try:
            (exe_dir / rel).rmdir()
        except (OSError, SystemExit):
            pass  # 非空（里面有删不掉的文件）或守卫拒绝就留着
    return failed



def _print_health(assess: dict | None) -> None:
    """把健康判定结果打成人看得懂的一行行。"""
    if not assess:
        return
    print(f"global_status : {assess['global_status']}")
    if assess["modules"]:
        print("modules       : " + ", ".join(
            f"{n}={s}" for n, s in sorted(assess["modules"].items())))
    if assess["checks"]:
        print("checks        : " + ", ".join(
            f"{n}={s}" for n, s in sorted(assess["checks"].items())))


def _fail(reason: str, log_text: str, log_tail: int) -> int:
    print(f"\n[FAIL] {reason}", file=sys.stderr)
    print("--- 后端日志尾部 ---", file=sys.stderr)
    print(tail(log_text, log_tail), file=sys.stderr)
    return 1


def smoke(exe: Path, ports: list[int], health_path: str,
          timeout: float, checks_timeout: float, log_tail: int) -> int:
    """起 exe、等端口、判健康、杀掉。返回进程退出码。"""
    if not exe.is_file():
        # fail-closed：产物不存在是**闸门失败**，不是"跳过"
        print(f"[FAIL] 打包产物不存在: {exe}", file=sys.stderr)
        print("       build-backend 步骤可能没产出 exe，或路径写错了。", file=sys.stderr)
        return 1

    exe_dir = exe.parent
    print(f"exe      : {exe}")
    print(f"size     : {exe.stat().st_size:,} bytes")
    print(f"cwd      : {exe_dir}")
    print(f"ports    : {ports}")
    print(f"timeout  : 启动 {timeout:.0f}s / 健康 {checks_timeout:.0f}s")

    log_path = Path(tempfile.mkdtemp(prefix="smoke-backend-")) / "backend.log"
    print(f"log      : {log_path}")

    # 产物清单快照：运行结束后据此删掉本次新建的文件，保证「冒烟不改动产物」。
    before = _snapshot_tree(exe_dir)

    with open(log_path, "wb") as log_file:
        proc = subprocess.Popen(
            [str(exe)],
            cwd=str(exe_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

    def _read_log() -> str:
        try:
            return log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    ok = False
    try:
        # ---- 阶段一：等端口 -------------------------------------------------
        port: int | None = None
        startup_deadline = time.time() + timeout
        runtime_hint: Path | None = None
        while time.time() < startup_deadline:
            rc = proc.poll()
            if rc is not None:
                return _fail(
                    f"后端进程在监听端口前就退出了（rc={rc}）—— "
                    "典型原因：PyInstaller 缺 hiddenimports（动态导入的模块没被打进去）",
                    _read_log(), log_tail)

            # 先看 runtime.json（应用自己报的端口，最准），再退回到端口探测
            if runtime_hint is None:
                for cand in runtime_json_candidates(exe):
                    if cand.is_file():
                        runtime_hint = cand
                        break
            if runtime_hint is not None:
                reported = read_runtime_port(runtime_hint)
                if reported is not None and reported not in ports:
                    ports = [reported] + ports

            port = find_open_port(ports)
            if port is not None:
                break
            time.sleep(1.0)

        if port is None:
            return _fail(
                f"{timeout:.0f}s 内候选端口 {ports} 一个都没起来 —— "
                "进程可能卡在启动阶段（缺依赖 / 配置读不到 / 端口被占）",
                _read_log(), log_tail)

        print(f"listening: 127.0.0.1:{port}")

        # ---- 阶段二：判健康 -------------------------------------------------
        # 端口开了不代表应用就绪（Flask 可能在路由注册完成前就 bind），
        # 所以要在剩余时间内重试，而不是探一次就下结论。
        url = f"http://127.0.0.1:{port}{health_path}"
        print(f"GET      : {url}")

        checks_deadline = time.time() + checks_timeout
        last_note = "(未尝试)"
        last_assess: dict | None = None

        while True:
            rc = proc.poll()
            if rc is not None:
                return _fail(f"后端进程在健康检查期间退出（rc={rc}）",
                             _read_log(), log_tail)

            status, body = http_status(url, timeout=10.0)
            if status != 200:
                last_note = f"HTTP {status}: {body[:300]}"
            else:
                try:
                    payload = json.loads(body)
                except ValueError as exc:
                    last_note = f"响应不是合法 JSON: {exc}; body={body[:300]}"
                else:
                    assess = assess_health(payload)
                    last_assess = assess
                    if not assess["ok"]:
                        # 确定的负面信号 —— 再等也不会自己好，直接判死
                        _print_health(assess)
                        return _fail(f"健康判定失败: {assess['reason']}",
                                     _read_log(), log_tail)
                    if assess["resolved"]:
                        _print_health(assess)
                        print("\n[PASS] 打包产物能启动、模块全部就绪、"
                              "后台巡检线程已跑过至少一轮")
                        ok = True
                        return 0
                    last_note = ("HTTP 200 且无负面信号，但后台巡检线程尚未跑第一轮"
                                 "（检查项全为 unknown）")

            if time.time() >= checks_deadline:
                break
            time.sleep(1.0)

        _print_health(last_assess)
        return _fail(
            f"健康检查未在 {checks_timeout:.0f}s 内通过: {last_note}",
            _read_log(), log_tail)
    finally:
        kill_tree(proc.pid)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

        # 「冒烟不得改动产物」：把本次运行在产物目录里新建的东西删掉，
        # 否则紧接着的 upload-artifact 会把运行期状态（模拟库 / runtime.json /
        # logs）一起打包出去。
        left = cleanup_created(exe_dir, before)
        if left:
            print(f"[WARN] 产物目录里有 {len(left)} 个运行期文件未能清理"
                  f"（可能仍被占用）: {left[:5]}", file=sys.stderr)

        if ok:
            # 成功时把临时日志收掉。本机已经被 `.pytest_tmp-*` 淹过一次
            # （约 250 个目录 / 7380 个文件），不要再往里加一类会累积的垃圾。
            # 失败时**保留**，因为排查看完整日志比看截尾有用。
            shutil.rmtree(log_path.parent, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="打包后端运行时冒烟（起进程 → 判健康 → 杀掉）")
    parser.add_argument("--exe", required=True,
                        help="scada-backend.exe 的路径（onedir 布局）")
    parser.add_argument("--ports", default=DEFAULT_PORTS,
                        help=f"候选端口，逗号分隔（默认 {DEFAULT_PORTS}）")
    parser.add_argument("--health-path", default=DEFAULT_HEALTH_PATH,
                        help=f"健康端点路径（默认 {DEFAULT_HEALTH_PATH}）")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="等待端口起来的秒数（默认 180）")
    parser.add_argument("--checks-timeout", type=float, default=DEFAULT_CHECKS_TIMEOUT,
                        help=f"端口起来后等待健康检查通过的秒数"
                             f"（默认 {DEFAULT_CHECKS_TIMEOUT:.0f}）")
    parser.add_argument("--log-tail", type=int, default=4000,
                        help="失败时打印日志尾部字符数（默认 4000）")
    args = parser.parse_args(argv)

    try:
        ports = candidate_ports(args.ports)
    except ValueError as exc:
        parser.error(str(exc))
        return 2  # pragma: no cover - parser.error 会先 SystemExit

    if args.timeout <= 0:
        parser.error("--timeout 必须为正数")
        return 2  # pragma: no cover
    if args.checks_timeout <= 0:
        parser.error("--checks-timeout 必须为正数")
        return 2  # pragma: no cover

    return smoke(Path(args.exe).resolve(), ports, args.health_path,
                 args.timeout, args.checks_timeout, args.log_tail)


if __name__ == "__main__":
    raise SystemExit(main())
