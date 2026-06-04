"""
安全扫描自动化工具
自动执行安全检查并生成报告

使用方法:
    python tools/security_scan.py full         # 完整扫描
    python tools/security_scan.py quick        # 快速扫描
    python tools/security_scan.py report       # 生成报告
"""

import os
import sys
import json
import re
import sqlite3
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class SecurityScanner:
    """安全扫描器"""

    def __init__(self):
        self.project_root = project_root
        self.findings: List[Dict[str, Any]] = []

    def run_full_scan(self) -> Dict[str, Any]:
        """运行完整安全扫描"""
        self.findings = []

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
                if file.is_file() and '.git' not in str(file):
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
        credential_patterns = [
            (r'password\s*=\s*["\'][^"\']+["\']', '硬编码密码'),
            (r'secret\s*=\s*["\'][^"\']+["\']', '硬编码密钥'),
            (r'api_key\s*=\s*["\'][^"\']+["\']', '硬编码API密钥'),
            (r'token\s*=\s*["\'][^"\']+["\']', '硬编码token'),
        ]

        # 检查Python文件
        for py_file in self.project_root.rglob('*.py'):
            if '.git' in str(py_file) or 'test' in str(py_file).lower():
                continue

            try:
                content = py_file.read_text(encoding='utf-8', errors='ignore')
                for pattern, message in credential_patterns:
                    matches = re.finditer(pattern, content, re.IGNORECASE)
                    for match in matches:
                        # 排除明显的占位符
                        value = match.group()
                        if 'your_' in value or 'xxx' in value or 'example' in value:
                            continue

                        self.findings.append({
                            'severity': 'critical',
                            'type': 'hardcoded_credential',
                            'file': str(py_file.relative_to(self.project_root)),
                            'message': message,
                            'recommendation': '使用环境变量或配置文件存储凭证',
                        })
            except:
                pass

    def _check_sql_injection(self):
        """检查SQL注入风险"""
        sql_patterns = [
            (r'execute\(\s*f["\']', '使用f-string构造SQL'),
            (r'execute\(\s*["\'].*%s', '使用字符串格式化构造SQL'),
            (r'execute\(\s*["\'].*\+', '使用字符串拼接构造SQL'),
        ]

        for py_file in self.project_root.rglob('*.py'):
            if '.git' in str(py_file) or 'test' in str(py_file).lower():
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
            except:
                pass

    def _check_xss_risks(self):
        """检查XSS风险"""
        xss_patterns = [
            (r'innerHTML\s*=', '使用innerHTML'),
            (r'v-html', '使用v-html指令'),
            (r'dangerouslySetInnerHTML', '使用dangerouslySetInnerHTML'),
        ]

        for vue_file in self.project_root.rglob('*.vue'):
            if '.git' in str(vue_file):
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
            except:
                pass

    def _check_permissions(self):
        """检查权限配置"""
        # 检查是否有默认密码
        config_files = list(self.project_root.rglob('*.yaml')) + list(self.project_root.rglob('*.yml'))

        for config_file in config_files:
            if '.git' in str(config_file):
                continue

            try:
                content = config_file.read_text(encoding='utf-8', errors='ignore')

                # 检查默认密码
                if 'admin123' in content or 'password123' in content:
                    self.findings.append({
                        'severity': 'critical',
                        'type': 'default_password',
                        'file': str(config_file.relative_to(self.project_root)),
                        'message': '使用默认密码',
                        'recommendation': '更改所有默认密码',
                    })

                # 检查通配符权限
                if 'host: "' in content or "host: '" in content:
                    if '0.0.0.0' in content:
                        self.findings.append({
                            'severity': 'medium',
                            'type': 'open_access',
                            'file': str(config_file.relative_to(self.project_root)),
                            'message': '绑定到所有网络接口',
                            'recommendation': '限制到特定IP地址',
                        })
            except:
                pass

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
            except:
                pass

    def _check_config_security(self):
        """检查配置安全"""
        # 检查debug模式
        for py_file in self.project_root.rglob('*.py'):
            if '.git' in str(py_file) or 'test' in str(py_file).lower():
                continue

            try:
                content = py_file.read_text(encoding='utf-8', errors='ignore')

                if 'debug=True' in content or 'DEBUG = True' in content:
                    self.findings.append({
                        'severity': 'medium',
                        'type': 'debug_mode',
                        'file': str(py_file.relative_to(self.project_root)),
                        'message': '生产代码中启用debug模式',
                        'recommendation': '生产环境禁用debug模式',
                    })
            except:
                pass

    def _check_logging_security(self):
        """检查日志安全"""
        for py_file in self.project_root.rglob('*.py'):
            if '.git' in str(py_file) or 'test' in str(py_file).lower():
                continue

            try:
                content = py_file.read_text(encoding='utf-8', errors='ignore')

                # 检查是否记录敏感信息
                sensitive_log_patterns = [
                    (r'logger\.\w+\(.*password', '日志中记录密码'),
                    (r'logger\.\w+\(.*token', '日志中记录token'),
                    (r'logger\.\w+\(.*secret', '日志中记录密钥'),
                ]

                for pattern, message in sensitive_log_patterns:
                    if re.search(pattern, content, re.IGNORECASE):
                        self.findings.append({
                            'severity': 'medium',
                            'type': 'sensitive_logging',
                            'file': str(py_file.relative_to(self.project_root)),
                            'message': message,
                            'recommendation': '避免在日志中记录敏感信息',
                        })
            except:
                pass

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
        except:
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
            },
            'findings': self.findings,
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
