"""
安全扫描自动化工具
自动执行安全检查并生成报告

使用方法:
    python tools/security_scan.py full         # 完整扫描
    python tools/security_scan.py quick        # 快速扫描
    python tools/security_scan.py report       # 生成报告

设计要点（round 162 修复）
--------------------------
本工具的失效模式不是"崩掉"，而是**报告"未发现问题"**。
原来每个检查阶段都是 `for 文件: try: 扫描 except Exception: pass` ——
单个文件读不了就被静默跳过，扫描器照常输出「安全分数 100 / 发现问题 0」。
调用方看到的是"安全"，实际是"根本没扫"。

现在所有跳过都记进 `self.scan_errors`，并随报告一起输出
（`summary.scan_error_count` + `report.scan_errors`），
`format_report()` 会在开头显著提示"本次扫描不完整，结论不覆盖这些目标"。
"""

import os
import sys
import ast
import json
import re
import logging
import sqlite3
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

logger = logging.getLogger(__name__)

#: 不参与扫描的目录（第三方依赖 / 构建产物 / 缓存）。
#: round 369（BUG-35 修复）：原来所有检查都用 `rglob('*.py')` 扫全仓，
#: 把 `.venv` 与 `dist/_internal` 里的第三方库也当成本项目代码 ——
#: 一次 quick 扫描 12 条发现里 8 条来自 site-packages（werkzeug/pip/dateutil），
#: 真正的项目问题被淹没，报告失去可操作性。
_EXCLUDED_DIR_PARTS = {
    '.git', '.venv', 'venv', 'env', 'dist', 'build', 'node_modules',
    '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
    'site-packages', '.pytest_tmp',
}

#: 形如错误码/常量的短值：`E2003`、`TOKEN_EXPIRED`、`ERR_01`。
#: 这些不是凭证，但会被 `token = '...'` 这样的正则误报。
_CODE_LIKE_RE = re.compile(r'^[A-Z0-9_\-\.]{1,15}$')

#: 凭证名里出现这些后缀，说明它描述的是**状态/代码**而非密钥值。
_CODE_NAME_SUFFIXES = (
    '_EXPIRED', '_INVALID', '_ERROR', '_CODE', '_TYPE', '_STATUS',
    '_NAME', '_PREFIX', '_HEADER', '_FIELD', '_KEY_NAME', '_TTL',
)


def _is_excluded(path: Path) -> bool:
    """路径是否位于第三方依赖/构建产物/缓存目录内。"""
    return any(part in _EXCLUDED_DIR_PARTS for part in path.parts)


def _looks_like_credential(name: str, value: str) -> bool:
    """判断 `name = 'value'` 是否**真的**像一条硬编码凭证。

    误报来源（round 369 实测）：
      * `TOKEN_EXPIRED = 'E2003'` —— 错误码常量；
      * `CSRF_SECRET = line.split('=', 1)[1].strip()` —— 正则跨到了表达式里的引号；
      * `SECRET_KEY = _get_secret('SECRET_KEY', ...)` 之类。
    判据：值太短、纯大写短码、含表达式符号、变量名是状态量 → 都不算凭证。
    """
    if len(value) < 12:
        return False
    if _CODE_LIKE_RE.match(value):
        return False
    if any(token in value for token in ('(', ')', '[', ']', '+', '%', '\\n', 'line.split')):
        return False
    upper_name = name.upper()
    if upper_name.endswith(_CODE_NAME_SUFFIXES):
        return False
    return True


def _leaked_secret_log_lines(content: str,
                             secret_names: set[str]) -> list[tuple[int, str]]:
    """找出**日志参数里真的传入了密码/令牌变量**的调用。

    只认 `logger.xxx(...)` 调用参数中的 Name/Attribute：
      * `logger.info(f"token={token}")`     → 命中（真的记录了令牌）
      * `logger.warning("token 不匹配")`    → 不命中（只是提示文案）
    语法解析失败时返回空列表（该文件不计入日志检查，调用方会记录扫描错误）。
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == 'logger'):
            continue
        values = list(node.args) + [kw.value for kw in node.keywords]
        found: set[str] = set()
        for value in values:
            for sub in ast.walk(value):
                if isinstance(sub, ast.Name):
                    found.add(sub.id.lower())
                elif isinstance(sub, ast.Attribute):
                    found.add(sub.attr.lower())
        hit = found & secret_names
        if hit:
            hits.append((node.lineno,
                         f'日志中记录敏感信息: {", ".join(sorted(hit))}'))
    return hits


def _strip_comments(text: str) -> str:
    """去掉 Python/YAML 行内注释，避免"注释里提到弱口令"被当成真的弱口令。"""
    out = []
    for line in text.splitlines():
        out.append(line.split('#', 1)[0])
    return '\n'.join(out)


class SecurityScanner:
    """安全扫描器"""

    def __init__(self):
        self.project_root = project_root
        self.findings: List[Dict[str, Any]] = []
        #: 扫描过程中「本应扫描但失败」的目标。
        #: **必须随报告一起输出** —— 否则扫描器自己读不了文件、却报告「未发现问题」，
        #: 这是本项目最危险的静默失效（安全扫描器说"安全"，实际它压根没扫）。
        self.scan_errors: List[Dict[str, str]] = []

    def _note_scan_error(self, stage: str, target: Any, exc: BaseException) -> None:
        """记录一次「本应扫描但失败」。

        刻意**不中断整体扫描**（单个文件读不了不该让整次扫描作废），
        但会进 ``self.scan_errors`` 并被 ``_generate_report`` / ``format_report`` 输出，
        使「未发现问题」这个结论带上它自己的覆盖率说明。
        """
        entry = {
            'stage': stage,
            'target': str(target),
            'error': f'{type(exc).__name__}: {exc}',
        }
        self.scan_errors.append(entry)
        logger.warning("安全扫描 [%s] 跳过 %s：%s", stage, entry['target'], entry['error'])

    def run_full_scan(self) -> Dict[str, Any]:
        """运行完整安全扫描"""
        self.findings = []
        self.scan_errors = []

        # 1. 检查敏感文件
        self._check_sensitive_files()

        # 2. 检查硬编码凭证
        self._check_hardcoded_credentials()

        # 3. 检查SQL注入风险
        self._check_sql_injection()

        # 4. 检查XSS风险
        self._check_xss_risks()

        # 5. 检查权限配置
        self._check_permissions()

        # 6. 检查依赖安全
        self._check_dependencies()

        # 7. 检查配置安全
        self._check_config_security()

        # 8. 检查日志安全
        self._check_logging_security()

        return self._generate_report()

    def run_quick_scan(self) -> Dict[str, Any]:
        """运行快速扫描"""
        self.findings = []
        self.scan_errors = []

        # 只检查最关键的问题
        self._check_hardcoded_credentials()
        self._check_sensitive_files()
        self._check_permissions()

        return self._generate_report()

    def _check_sensitive_files(self):
        """检查敏感文件"""
        sensitive_patterns = [
            '.env',
            '.env.local',
            '.env.production',
            '*.key',
            '*.pem',
            '*.p12',
            '*.pfx',
            'id_rsa',
            'id_dsa',
        ]

        for pattern in sensitive_patterns:
            for file in self.project_root.rglob(pattern):
                # round 369：跳过 .venv/dist 里的第三方 cacert.pem，
                # 那既不是本项目的文件，也不该由本项目的 .gitignore 负责。
                if file.is_file() and not _is_excluded(file):
                    # 检查是否在.gitignore中
                    if not self._is_gitignored(file):
                        self.findings.append({
                            'severity': 'high',
                            'type': 'sensitive_file',
                            'file': str(file.relative_to(self.project_root)),
                            'message': f'敏感文件未被.gitignore忽略: {file.name}',
                            'recommendation': '将此文件添加到.gitignore',
                        })

    def _check_hardcoded_credentials(self):
        """检查硬编码凭证"""
        # round 369：带命名捕获，先判断"这个值像不像凭证"再报。
        # 原来只匹配 `token\s*=\s*'...'`，把 `TOKEN_EXPIRED = 'E2003'`
        # 这类错误码常量全报成"硬编码token"，扫描器失去可信度。
        credential_patterns = [
            (r'(?P<name>\b(?:password|passwd|pwd))\s*=\s*["\'](?P<value>[^"\']+)["\']',
             '硬编码密码'),
            (r'(?P<name>\b(?:secret|api_key|apikey|access_key|secret_key))\s*=\s*["\'](?P<value>[^"\']+)["\']',
             '硬编码密钥'),
            (r'(?P<name>\b(?:token|auth_token))\s*=\s*["\'](?P<value>[^"\']+)["\']',
             '硬编码token'),
        ]

        # 检查Python文件
        for py_file in self.project_root.rglob('*.py'):
            if _is_excluded(py_file) or 'test' in str(py_file).lower():
                continue

            try:
                content = py_file.read_text(encoding='utf-8', errors='ignore')
                for pattern, message in credential_patterns:
                    matches = re.finditer(pattern, content, re.IGNORECASE)
                    for match in matches:
                        name = match.group('name')
                        value = match.group('value')
                        # 排除明显的占位符
                        if ('your_' in value or 'xxx' in value
                                or 'example' in value):
                            continue
                        # 错误码/常量/表达式不是凭证
                        if not _looks_like_credential(name, value):
                            continue

                        self.findings.append({
                            'severity': 'critical',
                            'type': 'hardcoded_credential',
                            'file': str(py_file.relative_to(self.project_root)),
                            'line': content[:match.start()].count('\n') + 1,
                            'message': message,
                            'recommendation': '使用环境变量或配置文件存储凭证',
                        })
            except Exception as e:
                self._note_scan_error('hardcoded_credentials', py_file, e)

    def _check_sql_injection(self):
        """检查SQL注入风险"""
        sql_patterns = [
            (r'execute\(\s*f["\']', '使用f-string构造SQL'),
            (r'execute\(\s*["\'].*%s', '使用字符串格式化构造SQL'),
            (r'execute\(\s*["\'].*\+', '使用字符串拼接构造SQL'),
        ]

        for py_file in self.project_root.rglob('*.py'):
            if _is_excluded(py_file) or 'test' in str(py_file).lower():
                continue

            try:
                content = py_file.read_text(encoding='utf-8', errors='ignore')
                for pattern, message in sql_patterns:
                    matches = re.finditer(pattern, content)
                    for match in matches:
                        self.findings.append({
                            'severity': 'high',
                            'type': 'sql_injection',
                            'file': str(py_file.relative_to(self.project_root)),
                            'line': content[:match.start()].count('\n') + 1,
                            'message': f'SQL注入风险: {message}',
                            'recommendation': '使用参数化查询',
                        })
            except Exception as e:
                self._note_scan_error('sql_injection', py_file, e)

    def _check_xss_risks(self):
        """检查XSS风险"""
        xss_patterns = [
            (r'innerHTML\s*=', '使用innerHTML'),
            (r'v-html', '使用v-html指令'),
            (r'dangerouslySetInnerHTML', '使用dangerouslySetInnerHTML'),
        ]

        for vue_file in self.project_root.rglob('*.vue'):
            if _is_excluded(vue_file):
                continue

            try:
                content = vue_file.read_text(encoding='utf-8', errors='ignore')
                for pattern, message in xss_patterns:
                    matches = re.finditer(pattern, content)
                    for match in matches:
                        self.findings.append({
                            'severity': 'medium',
                            'type': 'xss_risk',
                            'file': str(vue_file.relative_to(self.project_root)),
                            'line': content[:match.start()].count('\n') + 1,
                            'message': f'XSS风险: {message}',
                            'recommendation': '使用安全的DOM操作方式',
                        })
            except Exception as e:
                self._note_scan_error('xss_risks', vue_file, e)

    def _check_permissions(self):
        """检查权限配置"""
        # 检查是否有默认密码
        config_files = list(self.project_root.rglob('*.yaml')) + list(self.project_root.rglob('*.yml'))

        for config_file in config_files:
            if _is_excluded(config_file):
                continue

            try:
                content = config_file.read_text(encoding='utf-8', errors='ignore')
                # round 369：只看**真实配置值**，不看注释。
                # 原来 `'admin123' in content` 会把「说明：不要使用 admin123」
                # 这类注释也报成"使用默认密码"（docker-compose.yml 就是这么中的）。
                effective = _strip_comments(content)

                # 检查默认密码
                if 'admin123' in effective or 'password123' in effective:
                    self.findings.append({
                        'severity': 'critical',
                        'type': 'default_password',
                        'file': str(config_file.relative_to(self.project_root)),
                        'message': '使用默认密码',
                        'recommendation': '更改所有默认密码',
                    })

                # 检查通配符权限
                if 'host: "' in effective or "host: '" in effective:
                    if '0.0.0.0' in effective:
                        self.findings.append({
                            'severity': 'medium',
                            'type': 'open_access',
                            'file': str(config_file.relative_to(self.project_root)),
                            'message': '绑定到所有网络接口',
                            'recommendation': '限制到特定IP地址',
                        })
            except Exception as e:
                self._note_scan_error('permissions', config_file, e)

    def _check_dependencies(self):
        """检查依赖安全"""
        requirements_file = self.project_root / 'requirements.txt'

        if requirements_file.exists():
            try:
                content = requirements_file.read_text(encoding='utf-8')
                lines = content.strip().split('\n')

                for line in lines:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue

                    # 检查是否固定版本
                    if '==' not in line and '>=' not in line and '<=' not in line:
                        self.findings.append({
                            'severity': 'low',
                            'type': 'dependency_version',
                            'file': 'requirements.txt',
                            'message': f'依赖未固定版本: {line}',
                            'recommendation': '使用==固定版本号',
                        })
            except Exception as e:
                self._note_scan_error('dependencies', requirements_file, e)

    def _check_config_security(self):
        """检查配置安全"""
        # 检查debug模式
        # round 369：只认**代码**里的 `debug = True`。
        # 原来用子串匹配，扫描器自己源码里的 `if 'debug=True' in content`
        # 也会命中 —— 自己报自己，是典型误报。
        debug_re = re.compile(r'\bdebug\s*=\s*True\b', re.IGNORECASE)
        for py_file in self.project_root.rglob('*.py'):
            if _is_excluded(py_file) or 'test' in str(py_file).lower():
                continue

            try:
                content = py_file.read_text(encoding='utf-8', errors='ignore')
                for match in debug_re.finditer(content):
                    line_start = content.rfind('\n', 0, match.start()) + 1
                    line_end = content.find('\n', match.start())
                    line = content[line_start:line_end if line_end != -1 else None]
                    stripped = line.lstrip()
                    # 注释 / 含引号的字面量（含扫描器自身的检测语句）都不算启用
                    if stripped.startswith('#') or '"' in line or "'" in line:
                        continue
                    self.findings.append({
                        'severity': 'medium',
                        'type': 'debug_mode',
                        'file': str(py_file.relative_to(self.project_root)),
                        'line': content[:match.start()].count('\n') + 1,
                        'message': '生产代码中启用debug模式',
                        'recommendation': '生产环境禁用debug模式',
                    })
                    break
            except Exception as e:
                self._note_scan_error('config_security', py_file, e)

    def _check_logging_security(self):
        """检查日志安全。

        round 369：改成 **AST 判定**，只报「日志参数里真的传了密码/令牌变量」。
        原来的正则 `logger\\.\\w+\\(.*token` 会把
        「拒绝 /metrics 抓取：scrape token 不匹配」这类**提示文案**也报成
        「日志中记录 token」—— 实测 8 条全是这种，全是误报。
        """
        secret_names = {'password', 'passwd', 'pwd', 'token', 'secret',
                        'api_key', 'apikey', 'jwt_secret', 'access_token'}
        for py_file in self.project_root.rglob('*.py'):
            if _is_excluded(py_file) or 'test' in str(py_file).lower():
                continue

            try:
                content = py_file.read_text(encoding='utf-8', errors='ignore')
                leaked = _leaked_secret_log_lines(content, secret_names)
                for lineno, message in leaked:
                    self.findings.append({
                        'severity': 'medium',
                        'type': 'sensitive_logging',
                        'file': str(py_file.relative_to(self.project_root)),
                        'line': lineno,
                        'message': message,
                        'recommendation': '避免在日志中记录敏感信息',
                    })
            except Exception as e:
                self._note_scan_error('logging_security', py_file, e)

    def _is_gitignored(self, file_path: Path) -> bool:
        """检查文件是否被.gitignore忽略"""
        gitignore = self.project_root / '.gitignore'

        if not gitignore.exists():
            return False

        try:
            import fnmatch
            content = gitignore.read_text(encoding='utf-8')
            patterns = [line.strip() for line in content.split('\n') if line.strip() and not line.startswith('#')]

            relative = str(file_path.relative_to(self.project_root))

            for pattern in patterns:
                if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(file_path.name, pattern):
                    return True

            return False
        except Exception as e:
            # 忽略规则读不了 → 只能按「不忽略」处理（宁可多报、不可漏报），
            # 但**必须记录**：否则会以「敏感文件未被忽略」的形式产生误报，
            # 而调用方无从知道这条结论来自一次失败的读取。
            self._note_scan_error('gitignore_check', file_path, e)
            return False

    def _generate_report(self) -> Dict[str, Any]:
        """生成扫描报告"""
        # 按严重程度统计
        severity_counts = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
        for finding in self.findings:
            severity = finding.get('severity', 'low')
            severity_counts[severity] = severity_counts.get(severity, 0) + 1

        # 计算安全分数 (100分满分)
        score = 100
        score -= severity_counts['critical'] * 20
        score -= severity_counts['high'] * 10
        score -= severity_counts['medium'] * 5
        score -= severity_counts['low'] * 1
        score = max(0, score)

        return {
            'timestamp': datetime.now().isoformat(),
            'summary': {
                'total_findings': len(self.findings),
                'severity_counts': severity_counts,
                'security_score': score,
                # 扫描覆盖率：被跳过的目标数。非 0 表示本次扫描**不完整**，
                # 「未发现问题」的结论不覆盖这些目标。
                'scan_error_count': len(self.scan_errors),
            },
            'findings': self.findings,
            'scan_errors': self.scan_errors,
        }


def format_report(report: Dict[str, Any]) -> str:
    """格式化报告"""
    lines = []
    lines.append("=" * 60)
    lines.append("安全扫描报告")
    lines.append(f"时间: {report['timestamp']}")
    lines.append("=" * 60)

    summary = report['summary']
    lines.append(f"\n安全分数: {summary['security_score']}/100")
    lines.append(f"发现问题: {summary['total_findings']}")

    if summary['severity_counts']['critical'] > 0:
        lines.append(f"  严重: {summary['severity_counts']['critical']}")
    if summary['severity_counts']['high'] > 0:
        lines.append(f"  高危: {summary['severity_counts']['high']}")
    if summary['severity_counts']['medium'] > 0:
        lines.append(f"  中危: {summary['severity_counts']['medium']}")
    if summary['severity_counts']['low'] > 0:
        lines.append(f"  低危: {summary['severity_counts']['low']}")

    # 扫描不完整时必须在最显眼处说明 —— 否则「0 问题」会被误读成「安全」。
    scan_error_count = summary.get('scan_error_count', 0)
    if scan_error_count:
        lines.append("")
        lines.append("!" * 60)
        lines.append(f"警告：本次扫描**不完整** —— {scan_error_count} 个目标被跳过")
        lines.append("上面的「发现问题」数字**不覆盖**这些目标，不能据此判定安全。")
        lines.append("!" * 60)
        for err in report.get('scan_errors', []):
            lines.append(f"  - [{err['stage']}] {err['target']}")
            lines.append(f"    {err['error']}")

    if report['findings']:
        lines.append("\n详细发现:")
        for i, finding in enumerate(report['findings'], 1):
            lines.append(f"\n{i}. [{finding['severity'].upper()}] {finding['message']}")
            lines.append(f"   文件: {finding['file']}")
            if 'line' in finding:
                lines.append(f"   行号: {finding['line']}")
            lines.append(f"   建议: {finding['recommendation']}")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description='安全扫描自动化工具')
    parser.add_argument('command', choices=['full', 'quick', 'report'], help='扫描类型')
    parser.add_argument('--output', help='输出文件路径')

    args = parser.parse_args()

    scanner = SecurityScanner()

    if args.command == 'full':
        report = scanner.run_full_scan()
    elif args.command == 'quick':
        report = scanner.run_quick_scan()
    elif args.command == 'report':
        report = scanner.run_full_scan()

    # 输出报告
    print(format_report(report))

    # 保存JSON
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = project_root / 'logs' / f'security_scan_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n报告已保存: {output_path}")


if __name__ == '__main__':
    main()
