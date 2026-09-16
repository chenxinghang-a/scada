# legacy/ — 归档区

**这里的代码不参与构建、打包与静态守卫扫描。不要 `import` 这里面的东西。**

## root-scripts/（round 161 归档）

原先散落在**仓库根目录**、且被提交进 git 的 10 个一次性调试脚本，合计 1042 行。
放在根目录的问题是：它们看起来像项目的正式入口，新人/AI 会误以为是要维护的代码。

| 脚本 | 行数 | 为什么归档 |
|---|---|---|
| `_api_push.py` | 293 | 一次性 API 调试 |
| `_audit_deps.py` | 69 | 一次性依赖审计 |
| `_check_bugs.py` | 104 | 一次性 bug 排查 |
| `_git_push.py` | 43 | 一次性推送脚本 |
| `_test_all.py` | 73 | 一次性跑测试 |
| `test_fixes.py` | 105 | 一次性验证修复 |
| `test_simple.py` | 103 | 一次性验证 |
| `test_start.py` | 227 | 一次性启动调试 |
| `quick_test.py` | 8 | 打 `localhost:5000` 的一次性脚本 |
| `check_status.py` | 17 | 打 `localhost:5000` 的一次性脚本 |

**归档前已逐个 grep 全仓库确认零引用**（`test_start` 的 16 处命中经逐条检查
全部是 `test_start_device_not_found` 这类测试函数名的**子串误匹配**，
不是对 `test_start.py` 的引用）。

**没有删除**，只是移出根目录 —— 内容仍在 git 历史与当前 HEAD 里，随时可取回。
按主人「先归档、一个版本周期后无回归再删」的口径处置。

## 注意

- 这些脚本**不保证能跑**（多数打的是已经不存在的本地端口）。
- 若要取回使用，先确认它依赖的接口还在。
- 静态守卫 `tests/test_silent_exception_guard.py` 已把 `legacy/` 加入 `EXCLUDED_DIRS`，
  理由是归档区按定义是废弃代码，不该拿生产标准要求它。
