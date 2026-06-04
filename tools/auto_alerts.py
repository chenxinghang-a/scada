"""
告警规则自动生成工具
基于设备配置和历史数据自动生成告警规则

使用方法:
    python tools/auto_alerts.py generate   # 生成告警规则
    python tools/auto_alerts.py validate   # 验证现有规则
    python tools/auto_alerts.py optimize   # 优化告警规则
"""

import os
import sys
import json
import yaml
import sqlite3
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class AlertRuleGenerator:
    """告警规则生成器"""

    def __init__(self):
        self.project_root = project_root
        self.data_dir = project_root / 'data'
        self.config_dir = project_root / '配置'

        # 默认告警阈值模板
        self.threshold_templates = {
            'temperature': {
                'warning_high': 70,
                'critical_high': 90,
                'warning_low': 5,
                'critical_low': 0,
                'unit': '°C',
            },
            'pressure': {
                'warning_high': 8,
                'critical_high': 10,
                'warning_low': 2,
                'critical_low': 0.5,
                'unit': 'bar',
            },
            'vibration': {
                'warning_high': 4.5,
                'critical_high': 7.1,
                'unit': 'mm/s',
            },
            'level': {
                'warning_high': 80,
                'critical_high': 95,
                'warning_low': 20,
                'critical_low': 5,
                'unit': '%',
            },
            'flow': {
                'warning_high': 100,
                'critical_high': 120,
                'warning_low': 10,
                'critical_low': 5,
                'unit': 'm³/h',
            },
            'current': {
                'warning_high': 80,
                'critical_high': 100,
                'unit': 'A',
            },
            'speed': {
                'warning_high': 3000,
                'critical_high': 3500,
                'warning_low': 100,
                'unit': 'RPM',
            },
        }

    def load_devices(self) -> List[Dict[str, Any]]:
        """加载设备配置"""
        devices_file = self.config_dir / 'devices.yaml'

        if not devices_file.exists():
            return []

        try:
            with open(devices_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            return config.get('devices', [])
        except Exception as e:
            print(f"加载设备配置失败: {e}")
            return []

    def generate_rules_for_device(self, device: Dict[str, Any]) -> List[Dict[str, Any]]:
        """为单个设备生成告警规则"""
        rules = []
        device_id = device.get('id', device.get('device_id', ''))
        device_name = device.get('name', device_id)
        registers = device.get('registers', [])

        for register in registers:
            reg_name = register.get('name', '')
            reg_type = register.get('data_type', 'int16')

            # 根据寄存器名称匹配阈值模板
            template = self._match_template(reg_name)

            if template:
                # 生成高值告警
                if 'warning_high' in template:
                    rules.append({
                        'id': f'{device_id}_{reg_name}_high_warning',
                        'name': f'{device_name} {reg_name} 高值警告',
                        'device_id': device_id,
                        'register_name': reg_name,
                        'condition': 'greater_than',
                        'threshold': template['warning_high'],
                        'level': 'warning',
                        'enabled': True,
                        'message': f'{device_name} {reg_name} 超过警告阈值 {template["warning_high"]}{template.get("unit", "")}',
                    })

                if 'critical_high' in template:
                    rules.append({
                        'id': f'{device_id}_{reg_name}_high_critical',
                        'name': f'{device_name} {reg_name} 高值严重',
                        'device_id': device_id,
                        'register_name': reg_name,
                        'condition': 'greater_than',
                        'threshold': template['critical_high'],
                        'level': 'critical',
                        'enabled': True,
                        'message': f'{device_name} {reg_name} 超过严重阈值 {template["critical_high"]}{template.get("unit", "")}',
                    })

                # 生成低值告警
                if 'warning_low' in template:
                    rules.append({
                        'id': f'{device_id}_{reg_name}_low_warning',
                        'name': f'{device_name} {reg_name} 低值警告',
                        'device_id': device_id,
                        'register_name': reg_name,
                        'condition': 'less_than',
                        'threshold': template['warning_low'],
                        'level': 'warning',
                        'enabled': True,
                        'message': f'{device_name} {reg_name} 低于警告阈值 {template["warning_low"]}{template.get("unit", "")}',
                    })

                if 'critical_low' in template:
                    rules.append({
                        'id': f'{device_id}_{reg_name}_low_critical',
                        'name': f'{device_name} {reg_name} 低值严重',
                        'device_id': device_id,
                        'register_name': reg_name,
                        'condition': 'less_than',
                        'threshold': template['critical_low'],
                        'level': 'critical',
                        'enabled': True,
                        'message': f'{device_name} {reg_name} 低于严重阈值 {template["critical_low"]}{template.get("unit", "")}',
                    })

        return rules

    def _match_template(self, register_name: str) -> Optional[Dict[str, Any]]:
        """根据寄存器名称匹配阈值模板"""
        name_lower = register_name.lower()

        for keyword, template in self.threshold_templates.items():
            if keyword in name_lower:
                return template

        return None

    def generate_all_rules(self) -> List[Dict[str, Any]]:
        """为所有设备生成告警规则"""
        devices = self.load_devices()
        all_rules = []

        for device in devices:
            rules = self.generate_rules_for_device(device)
            all_rules.extend(rules)

        return all_rules

    def save_rules(self, rules: List[Dict[str, Any]], output_file: str = None):
        """保存告警规则"""
        if output_file is None:
            output_file = str(self.config_dir / 'alarms_generated.yaml')

        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 转换为YAML格式
        config = {
            'rules': rules,
            'generated_at': datetime.now().isoformat(),
            'total_rules': len(rules),
        }

        with open(output_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

        print(f"已生成 {len(rules)} 条告警规则")
        print(f"保存到: {output_path}")

    def validate_rules(self, rules_file: str = None) -> Dict[str, Any]:
        """验证告警规则"""
        if rules_file is None:
            rules_file = str(self.config_dir / 'alarms.yaml')

        try:
            with open(rules_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            rules = config.get('rules', [])

            issues = []
            for i, rule in enumerate(rules):
                # 检查必填字段
                required_fields = ['id', 'name', 'device_id', 'register_name', 'condition', 'threshold', 'level']
                for field in required_fields:
                    if field not in rule:
                        issues.append({
                            'rule_index': i,
                            'rule_id': rule.get('id', 'unknown'),
                            'issue': f'缺少必填字段: {field}',
                            'severity': 'error',
                        })

                # 检查条件有效性
                valid_conditions = ['greater_than', 'less_than', 'equal', 'not_equal']
                if rule.get('condition') not in valid_conditions:
                    issues.append({
                        'rule_index': i,
                        'rule_id': rule.get('id', 'unknown'),
                        'issue': f'无效的条件: {rule.get("condition")}',
                        'severity': 'error',
                    })

                # 检查级别有效性
                valid_levels = ['critical', 'warning', 'info']
                if rule.get('level') not in valid_levels:
                    issues.append({
                        'rule_index': i,
                        'rule_id': rule.get('id', 'unknown'),
                        'issue': f'无效的级别: {rule.get("level")}',
                        'severity': 'warning',
                    })

            return {
                'total_rules': len(rules),
                'issues': issues,
                'valid': len([i for i in issues if i['severity'] == 'error']) == 0,
            }

        except Exception as e:
            return {
                'error': str(e),
                'valid': False,
            }

    def optimize_rules(self, rules_file: str = None) -> List[Dict[str, Any]]:
        """优化告警规则（基于历史数据分析）"""
        if rules_file is None:
            rules_file = str(self.config_dir / 'alarms.yaml')

        try:
            with open(rules_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            rules = config.get('rules', [])
            optimized = []

            for rule in rules:
                # 基于历史数据调整阈值
                device_id = rule.get('device_id')
                register_name = rule.get('register_name')

                if device_id and register_name:
                    # 获取历史统计
                    stats = self._get_register_stats(device_id, register_name)

                    if stats:
                        # 根据统计数据优化阈值
                        avg = stats.get('avg', 0)
                        stddev = stats.get('stddev', 0)

                        if rule.get('condition') == 'greater_than':
                            # 建议阈值 = 平均值 + 3*标准差
                            suggested = round(avg + 3 * stddev, 2)
                            if suggested > rule.get('threshold', 0):
                                rule['suggested_threshold'] = suggested
                                rule['optimization_note'] = f'基于历史数据建议阈值: {suggested}'

                        elif rule.get('condition') == 'less_than':
                            # 建议阈值 = 平均值 - 3*标准差
                            suggested = round(avg - 3 * stddev, 2)
                            if suggested < rule.get('threshold', 0):
                                rule['suggested_threshold'] = suggested
                                rule['optimization_note'] = f'基于历史数据建议阈值: {suggested}'

                optimized.append(rule)

            return optimized

        except Exception as e:
            print(f"优化失败: {e}")
            return []

    def _get_register_stats(self, device_id: str, register_name: str) -> Optional[Dict[str, float]]:
        """获取寄存器历史统计"""
        db_path = self.data_dir / 'scada.db'

        if not db_path.exists():
            return None

        try:
            conn = sqlite3.connect(str(db_path), timeout=5)

            # 获取最近7天的数据
            cutoff = (datetime.now() - timedelta(days=7)).isoformat()
            cursor = conn.execute('''
                SELECT AVG(value), MIN(value), MAX(value), COUNT(*)
                FROM history_data
                WHERE device_id = ? AND register_name = ? AND timestamp > ?
            ''', (device_id, register_name, cutoff))

            row = cursor.fetchone()
            conn.close()

            if row and row[0] is not None:
                avg, min_val, max_val, count = row
                # 估算标准差
                stddev = (max_val - min_val) / 4 if max_val and min_val else 0

                return {
                    'avg': avg,
                    'min': min_val,
                    'max': max_val,
                    'count': count,
                    'stddev': stddev,
                }

            return None

        except Exception:
            return None


def main():
    parser = argparse.ArgumentParser(description='告警规则自动生成工具')
    parser.add_argument('command', choices=['generate', 'validate', 'optimize'],
                       help='执行的命令')
    parser.add_argument('--output', help='输出文件路径')
    parser.add_argument('--input', help='输入文件路径')

    args = parser.parse_args()

    generator = AlertRuleGenerator()

    if args.command == 'generate':
        rules = generator.generate_all_rules()
        generator.save_rules(rules, args.output)

    elif args.command == 'validate':
        result = generator.validate_rules(args.input)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == 'optimize':
        rules = generator.optimize_rules(args.input)
        if rules:
            generator.save_rules(rules, args.output or str(generator.config_dir / 'alarms_optimized.yaml'))


if __name__ == '__main__':
    main()
