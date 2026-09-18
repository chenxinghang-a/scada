# 证据包（Evidence Package）说明

> 对应审计第 9.5 节硬性要求：每个阶段出口必须生成一份完整、可追溯、含人工见证的证据包。

## 1. 用途

证据包用于在阶段评审 / 外部审计时，证明该版本（version）的软件：

- 做了什么（范围、需求追溯）
- 怎么验证的（测试、协议 E2E、soak、安全扫描、安装烟测、备份恢复、SAT）
- 在什么环境、用什么命令、由谁见证下完成的

每份证据文件必须可被独立审阅，字段缺失即视为不合规。

## 2. 命名规则

```
evidence/<version>/           # 每个发布/阶段版本一个目录，version 与 VERSION 文件一致，如 1.3.1028
evidence/_template/           # 结构化模板，勿直接回填；复制为 evidence/<version>/ 后填写
```

- `<version>` 必须与仓库根目录 `VERSION` 文件内容一致。
- 同一版本重复生成时覆盖同名文件，历史版本目录不得修改（审计留痕）。

## 3. 必备文件清单（15 份）

| 文件 | 内容 | 产出方式 |
|---|---|---|
| scope-matrix.md | 功能范围矩阵（引用 `文档/功能范围矩阵.md`） | 人工回填（模板已含引用） |
| requirements-traceability.csv | 需求 → 设计 → 代码 → 测试用例追溯 | 人工回填 |
| risk-register.md | 风险登记册 | 人工回填 |
| api-contract-report.json | API 契约测试结果 | 脚本/人工回填 |
| protocol-e2e-report.md | 协议端到端测试报告 | 脚本/人工回填 |
| pytest-report.xml | 后端 pytest JUnit 报告 | `collect_evidence.py --run-tests` 自动生成 |
| frontend-test-report.xml | 前端测试报告 | 存在时自动复制，否则 skipped |
| coverage.xml | 覆盖率报告 | 存在时自动复制 |
| soak-metrics.csv | 长时间运行（soak）指标 | 人工/脚本回填 |
| security-scan.json | 安全扫描结果 | 脚本（`tools/security_scan.py`）/人工回填 |
| sbom.spdx.json | 软件物料清单（SPDX 或简化依赖清单） | 自动生成（占位/简化结构） |
| build-manifest.json | 版本/commit/构建时间/环境/产物哈希 | 自动生成 |
| install-smoke-report.md | 安装烟测报告 | 人工回填 |
| backup-restore-report.md | 备份恢复演练报告 | 人工回填 |
| sat-record.md | 现场验收（SAT）记录（引用 `文档/安全边界声明.md`） | 人工回填（模板已含引用） |

## 4. 每份文件的必备字段

无论格式（md/json/xml/csv），每份证据文件必须包含以下元信息：

1. **版本**（version）
2. **commit SHA**（git rev-parse HEAD，非 git 环境记 `unknown`）
3. **运行环境**（OS、Python/Node 版本、硬件或容器标识）
4. **执行命令**（生成该证据的完整命令行）
5. **开始/结束时间**（UTC ISO8601）
6. **输入配置摘要**（关键配置项，不得含口令/密钥明文）
7. **结果**（pass / fail / partial / skipped）
8. **失败项**（无失败则明确写"无"）
9. **人工见证人**（姓名 + 日期；机器生成项也须由见证人复核签字）

## 5. 生成 / 回填流程

```bash
# 1) 机器可产出项自动收集（不跑测试）
.venv/Scripts/python.exe tools/collect_evidence.py --version 1.3.1028

# 1') 同时运行 pytest 生成 pytest-report.xml
.venv/Scripts/python.exe tools/collect_evidence.py --version 1.3.1028 --run-tests

# 2) 按脚本末尾打印的"回填清单"，人工补齐其余文件
#    模板位于 evidence/_template/，复制到 evidence/<version>/ 后回填

# 3) 每份文件由人工见证人复核并在"见证人"字段签字

# 4) 阶段评审时整包提交；证据目录随版本冻结，不再修改
```

缺 git、缺 pytest、缺前端产物时脚本**不会崩溃**，对应项在 `build-manifest.json` 的
`evidence_status` 与控制台输出中记为 `skipped` 并注明原因，绝不静默成功。
