# git-hooks

本仓的版本化 git hook。用 `core.hooksPath` 挂载，不依赖 `.git/hooks`（那里默认是样例，不随仓库分发）。

## 安装（每个 clone 做一次）

```bash
git config core.hooksPath tools/git-hooks
chmod +x tools/git-hooks/pre-commit   # Windows 下 git 可忽略
```

检查是否生效：

```bash
git config --get core.hooksPath   # 应输出 tools/git-hooks
```

卸载：

```bash
git config --unset core.hooksPath
```

## pre-commit 挡什么

| 类别 | 规则 |
|---|---|
| 合并冲突 | 暂存内容里有 `<<<<<<< ` 或 `>>>>>>> ` |
| 敏感文件 | `.env` / `*.key` / `*.pem` / `id_rsa*` / `*credentials*` / `*secret*` / `*token*` |
| 大文件 | 单文件 > 5 MB（二进制资产走 LFS 或单独发布） |
| 临时产物 | `.pytest_tmp*` / `__pycache__` / `*.pyc` / `*.log` |

空白错误（`git diff --cached --check`）只提示不拦，避免历史文件积压的行尾空格挡住正常提交。

应急跳过：`git commit --no-verify`。

## 和 `tools/scada_preflight.py` 的分工

| | 何时跑 | 查什么 |
|---|---|---|
| `tools/scada_preflight.py` | **开工前**，手动 | 分支/同步/工作区是否脏/VERSION/`.pytest_tmp` 积压/stash/最近提交 |
| `tools/git-hooks/pre-commit` | **提交前**，自动 | 只挡真不该进库的东西（冲突标记/密钥/大文件/临时产物） |

preflight 里「工作区是否脏」这一项在提交时必然为脏，所以两者不合并，各管一段。
