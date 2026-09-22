"""把 pytest 失败详情发成 GitHub Actions annotation。

为什么需要它
------------
CI 挂在远端时，本机看不到任何细节：

- `/actions/jobs/{id}/logs`（下载 job log）**需要仓库管理员权限**，
  只读访问返回 `403 Must have admin rights to Repository`；
- `$GITHUB_STEP_SUMMARY` 虽然能在网页上看到，但**不会**出现在
  check-runs API 的 `output.summary` 字段里，脚本读不到；
- artifacts（`pytest.log`）下载同样需要认证。

唯一**公开可读**的通道是 annotation：
`GET /repos/{owner}/{repo}/check-runs/{id}/annotations`。

为什么要"提取关键行"而不是直接截末尾
------------------------------------
第一版取「末尾 60 行」，结果拿到的是 **coverage 表格的中段** ——
GitHub 对单条 annotation 的长度有限制，末尾的 `N passed, M failed` 汇总行
反而被截掉了。而 coverage 表格有 168 行，纯属噪声。

所以这里**先按模式筛出有信息量的行**，再兜底附上最后几行，
保证 `FAILED` / `ERROR` / 汇总行 / Traceback 一定落在限额内。
"""

import pathlib
import re
import sys

# 相对 __file__ 定位仓库根，而不是靠 CWD。
# 这个脚本是 CI 失败时**唯一公开可读**的诊断通道（job log 要管理员权限），
# 一旦它自己因为"在别的目录下执行"而读不到 pytest.log，就会退化成一句
# "未找到 pytest.log（测试步骤可能没跑到）" —— 把"通道坏了"伪装成"测试没跑"。
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
LOG = _REPO_ROOT / 'pytest.log'

#: 单条 annotation 的字符上限（GitHub 侧还有更严格的总量限制，这里留足余量）
MAX_CHARS = 8000

#: 值得保留的行
_PATTERNS = (
    re.compile(r'^FAILED\b'),
    re.compile(r'^ERROR\b'),
    re.compile(r'^E\s'),                       # pytest 的断言失败行
    re.compile(r'^_{5,}.*_{5,}$'),             # 失败用例的分隔标题
    re.compile(r'\b\d+ (passed|failed|error|skipped)'),  # 汇总行
    re.compile(r'Required test coverage'),
    re.compile(r'^Traceback \(most recent call last\)'),
    re.compile(r'^(UnicodeEncodeError|UnicodeDecodeError|RuntimeError|AssertionError|ImportError|ModuleNotFoundError)'),
    re.compile(r'^short test summary info'),
)


def extract(text: str) -> list[str]:
    """筛出有信息量的行；一条都没筛到时退回最后 20 行。"""
    lines = text.splitlines()
    picked = [ln for ln in lines if any(p.search(ln) for p in _PATTERNS)]

    # 去重但保持顺序（pytest 有时会把同一行重复输出）
    seen: set[str] = set()
    unique = [ln for ln in picked if not (ln in seen or seen.add(ln))]

    # 汇总行往往在最后，取尾部；同时补上原始末尾 5 行做兜底
    result = unique[-60:]
    for ln in lines[-5:]:
        if ln not in result:
            result.append(ln)
    return result


def main() -> int:
    if not LOG.exists():
        print('::error title=pytest 失败::未找到 pytest.log（测试步骤可能没跑到）')
        return 0

    text = LOG.read_text(encoding='utf-8', errors='replace')
    body = '\n'.join(extract(text)) or '(pytest.log 里没有筛出关键行)'

    # GitHub annotation 的转义要求：% 必须写成 %25，换行写成 %0A
    escaped = body.replace('%', '%25').replace('\r', '').replace('\n', '%0A')

    print(f'::error title=pytest 失败关键行::{escaped[:MAX_CHARS]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
