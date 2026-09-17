"""把 pytest 失败详情发成 GitHub Actions annotation。

为什么需要它
------------
CI 挂在远端时，本机看不到任何细节：

- `/actions/jobs/{id}/logs`（下载 job log）**需要仓库管理员权限**，
  只读访问返回 `403 Must have admin rights to Repository`；
- `$GITHUB_STEP_SUMMARY` 虽然能在网页上看到，但**不会**出现在
  check-runs API 的 `output.summary` 字段里，脚本读不到；
- artifacts（`pytest.log`）下载同样需要认证。

唯一**公开可读**的通道是 annotation：通过
`GET /repos/{owner}/{repo}/check-runs/{id}/annotations` 匿名就能拿到。

所以这里把 `pytest.log` 的末尾若干行转义后以 `::error::` 形式输出，
让「CI 为什么红」这件事在远端也能查到，而不是只能看到一句
"Process completed with exit code 1"。
"""

import pathlib
import sys

LOG = pathlib.Path('pytest.log')

#: 只取末尾这些行 —— annotation 有总量上限，且真正的报错总在最后
MAX_LINES = 60
#: 再按字符数兜一层，避免单条 annotation 过长被丢弃
MAX_CHARS = 60000


def main() -> int:
    if not LOG.exists():
        print('::error title=pytest 失败::未找到 pytest.log（测试步骤可能没跑到）')
        return 0

    text = LOG.read_text(encoding='utf-8', errors='replace')
    tail = '\n'.join(text.splitlines()[-MAX_LINES:])

    # GitHub annotation 的转义要求：% 必须写成 %25，换行写成 %0A
    escaped = tail.replace('%', '%25').replace('\r', '').replace('\n', '%0A')

    print(f'::error title=pytest 失败详情（末 {MAX_LINES} 行）::{escaped[:MAX_CHARS]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
