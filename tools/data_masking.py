"""
数据脱敏工具
用于对敏感数据进行脱敏处理

使用方法:
    python tools/data_masking.py mask <input_file> <output_file>  # 脱敏数据
    python tools/data_masking.py rules                            # 显示脱敏规则
"""

import os
import sys
import re
import json
import yaml
import sqlite3
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class DataMasker:
    """数据脱敏器"""

    def __init__(self):
        self.rules = self._load_default_rules()

    # 敏感列名模式
    SENSITIVE_COLUMN_PATTERNS = [
        'password', 'passwd', 'pwd', 'secret', 'token', 'key', 'credential',
        'api_key', 'api_token', 'access_token', 'refresh_token',
        'ssn', 'social_security', 'credit_card', 'card_number',
    ]

    def _load_default_rules(self) -> List[Dict[str, Any]]:
        """加载默认脱敏规则"""
        return [
            # IP地址脱敏
            {
                'name': 'ip_address',
                'pattern': r'\b(\d{1,3}\.){3}\d{1,3}\b',
                'replacement': lambda m: self._mask_ip(m.group()),
                'description': 'IP地址保留前两段',
            },
            # 手机号脱敏
            {
                'name': 'phone',
                'pattern': r'\b1[3-9]\d{9}\b',
                'replacement': lambda m: m.group()[:3] + '****' + m.group()[-4:],
                'description': '手机号保留前3后4',
            },
            # 邮箱脱敏
            {
                'name': 'email',
                'pattern': r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
                'replacement': lambda m: self._mask_email(m.group()),
                'description': '邮箱保留首字符和域名',
            },
            # 身份证脱敏
            {
                'name': 'id_card',
                'pattern': r'\b\d{17}[\dXx]\b',
                'replacement': lambda m: m.group()[:6] + '********' + m.group()[-4:],
                'description': '身份证保留前6后4',
            },
            # 信用卡号脱敏
            {
                'name': 'credit_card',
                'pattern': r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b',
                'replacement': lambda m: '****-****-****-' + m.group().replace('-', '').replace(' ', '')[-4:],
                'description': '信用卡号保留后4位',
            },
            # API密钥/Token脱敏
            {
                'name': 'api_key',
                'pattern': r'(?i)(bearer|apikey|api-key|token)\s+[A-Za-z0-9\-._~+/]+=*',
                'replacement': lambda m: m.group().split()[0] + ' ***MASKED***',
                'description': 'API密钥/Token完全脱敏',
            },
            # 密码字段脱敏
            {
                'name': 'password',
                'pattern': r'(?i)(password|passwd|pwd|secret)\s*[=:]\s*\S+',
                'replacement': lambda m: m.group().split('=')[0] + '=***MASKED***' if '=' in m.group() else m.group().split(':')[0] + ':***MASKED***',
                'description': '密码字段完全脱敏',
            },
        ]

    def is_sensitive_column(self, column_name: str) -> bool:
        """检测列名是否为敏感字段"""
        lower = column_name.lower()
        return any(p in lower for p in self.SENSITIVE_COLUMN_PATTERNS)

    def mask_value_by_column(self, column_name: str, value: Any) -> Any:
        """根据列名自动脱敏"""
        if not isinstance(value, str):
            return value
        if self.is_sensitive_column(column_name):
            return '***MASKED***'
        return self.mask_text(value)

    def _mask_ip(self, ip: str) -> str:
        """脱敏IP地址"""
        parts = ip.split('.')
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.xxx.xxx"
        return ip

    def _mask_email(self, email: str) -> str:
        """脱敏邮箱"""
        local, domain = email.split('@')
        if len(local) > 1:
            return f"{local[0]}***@{domain}"
        return f"***@{domain}"

    def mask_text(self, text: str) -> str:
        """对文本进行脱敏"""
        result = text

        for rule in self.rules:
            pattern = re.compile(rule['pattern'])
            result = pattern.sub(rule['replacement'], result)

        return result

    def mask_dict(self, data: Dict[str, Any], sensitive_keys: List[str] = None) -> Dict[str, Any]:
        """对字典数据进行脱敏"""
        if sensitive_keys is None:
            sensitive_keys = ['password', 'secret', 'token', 'key', 'credential']

        result = {}

        for key, value in data.items():
            # 检查是否是敏感字段
            if any(s in key.lower() for s in sensitive_keys):
                result[key] = '***MASKED***'
            elif isinstance(value, str):
                result[key] = self.mask_text(value)
            elif isinstance(value, dict):
                result[key] = self.mask_dict(value, sensitive_keys)
            elif isinstance(value, list):
                result[key] = [self.mask_dict(item, sensitive_keys) if isinstance(item, dict) else item for item in value]
            else:
                result[key] = value

        return result

    def mask_database(self, db_path: str, output_path: str, tables: List[str] = None):
        """对数据库进行脱敏"""
        conn = sqlite3.connect(db_path)

        # 获取所有表
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        all_tables = [row[0] for row in cursor.fetchall()]

        if tables:
            all_tables = [t for t in all_tables if t in tables]

        # 创建输出数据库
        out_conn = sqlite3.connect(output_path)

        for table in all_tables:
            # 获取表结构
            cursor = conn.execute(f"PRAGMA table_info({table})")
            columns = [row[1] for row in cursor.fetchall()]

            # 复制表结构
            cursor = conn.execute(f"SELECT sql FROM sqlite_master WHERE name='{table}'")
            create_sql = cursor.fetchone()[0]
            out_conn.execute(create_sql)

            # 复制并脱敏数据
            cursor = conn.execute(f"SELECT * FROM {table}")
            rows = cursor.fetchall()

            for row in rows:
                masked_row = []
                for i, value in enumerate(row):
                    if isinstance(value, str):
                        masked_row.append(self.mask_text(value))
                    else:
                        masked_row.append(value)

                placeholders = ','.join(['?' for _ in columns])
                out_conn.execute(f"INSERT INTO {table} VALUES ({placeholders})", masked_row)

            out_conn.commit()

        conn.close()
        out_conn.close()

    def mask_file(self, input_path: str, output_path: str):
        """对文件进行脱敏"""
        with open(input_path, 'r', encoding='utf-8') as f:
            content = f.read()

        masked_content = self.mask_text(content)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(masked_content)

    def add_rule(self, name: str, pattern: str, replacement, description: str = ''):
        """添加自定义规则"""
        self.rules.append({
            'name': name,
            'pattern': pattern,
            'replacement': replacement,
            'description': description,
        })

    def list_rules(self) -> List[Dict[str, Any]]:
        """列出所有规则"""
        return [{
            'name': rule['name'],
            'description': rule.get('description', ''),
            'pattern': rule['pattern'],
        } for rule in self.rules]


def main():
    parser = argparse.ArgumentParser(description='数据脱敏工具')
    subparsers = parser.add_subparsers(dest='command', help='命令')

    # mask命令
    mask_parser = subparsers.add_parser('mask', help='脱敏数据')
    mask_parser.add_argument('input', help='输入文件')
    mask_parser.add_argument('output', help='输出文件')
    mask_parser.add_argument('--type', choices=['text', 'json', 'database'], default='text', help='数据类型')

    # rules命令
    subparsers.add_parser('rules', help='显示脱敏规则')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    masker = DataMasker()

    if args.command == 'mask':
        if args.type == 'text':
            masker.mask_file(args.input, args.output)
            print(f"文本脱敏完成: {args.output}")
        elif args.type == 'json':
            with open(args.input, 'r', encoding='utf-8') as f:
                data = json.load(f)
            masked = masker.mask_dict(data)
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump(masked, f, indent=2, ensure_ascii=False)
            print(f"JSON脱敏完成: {args.output}")
        elif args.type == 'database':
            masker.mask_database(args.input, args.output)
            print(f"数据库脱敏完成: {args.output}")

    elif args.command == 'rules':
        rules = masker.list_rules()
        print("脱敏规则:")
        for rule in rules:
            print(f"  - {rule['name']}: {rule['description']}")


if __name__ == '__main__':
    main()
