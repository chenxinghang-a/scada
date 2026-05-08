"""
数据导出模块
支持CSV、Excel、JSON格式导出
"""

import csv
import json
import logging
from typing import Any
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class DataExport:
    """
    数据导出类
    支持多种格式的数据导出
    """

    def __init__(self, export_dir: str = 'exports'):
        """
        初始化数据导出

        Args:
            export_dir: 导出目录
        """
        self.export_dir = Path(export_dir)
        self.export_dir.mkdir(parents=True, exist_ok=True)

    def export_csv(self, data: list[dict[str, Any]], filename: str | None = None) -> str:
        """
        导出CSV格式

        Args:
            data: 数据列表
            filename: 文件名（可选）

        Returns:
            str: 导出文件路径
        """
        if not data:
            logger.warning("没有数据可导出")
            return ""

        # 生成文件名
        if not filename:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'export_{timestamp}.csv'

        filepath = self.export_dir / filename

        try:
            # 获取字段名
            fieldnames = data[0].keys()

            with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(data)

            logger.info(f"CSV导出成功: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"CSV导出失败: {e}")
            return ""

    def export_excel(self, data: list[dict[str, Any]], filename: str | None = None,
                     sheet_name: str = 'Sheet1') -> str:
        """
        导出Excel格式

        Args:
            data: 数据列表
            filename: 文件名（可选）
            sheet_name: 工作表名称

        Returns:
            str: 导出文件路径
        """
        try:
            import pandas as pd
        except ImportError:
            logger.error("pandas未安装，无法导出Excel")
            return None

        if not data:
            logger.warning("没有数据可导出")
            return None

        # 生成文件名
        if not filename:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'export_{timestamp}.xlsx'

        filepath = self.export_dir / filename

        try:
            df = pd.DataFrame(data)
            df.to_excel(filepath, sheet_name=sheet_name, index=False)

            logger.info(f"Excel导出成功: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"Excel导出失败: {e}")
            return None

    def export_json(self, data: list[dict[str, Any]], filename: str | None = None,
                    pretty: bool = True) -> str:
        """
        导出JSON格式

        Args:
            data: 数据列表
            filename: 文件名（可选）
            pretty: 是否格式化

        Returns:
            str: 导出文件路径
        """
        if not data:
            logger.warning("没有数据可导出")
            return None

        # 生成文件名
        if not filename:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'export_{timestamp}.json'

        filepath = self.export_dir / filename

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                if pretty:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                else:
                    json.dump(data, f, ensure_ascii=False)

            logger.info(f"JSON导出成功: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"JSON导出失败: {e}")
            return None

    def export_device_data(self, database, device_id: str,
                           start_time: datetime, end_time: datetime,
                           format: str = 'csv') -> str | None:
        """
        导出设备数据

        Args:
            database: 数据库实例
            device_id: 设备ID
            start_time: 开始时间
            end_time: 结束时间
            format: 导出格式（csv, excel, json）

        Returns:
            str: 导出文件路径
        """
        # 获取所有寄存器的历史数据
        data = []
        # 先获取设备的所有寄存器名称
        registers = database.get_device_registers(device_id)
        if not registers:
            # 如果没有寄存器信息，尝试获取所有数据
            registers = ['*']

        for register_name in registers:
            register_data = database.get_history_data(
                device_id=device_id,
                register_name=register_name,
                start_time=start_time,
                end_time=end_time
            )
            data.extend(register_data)

        if not data:
            logger.warning(f"设备 {device_id} 在指定时间范围内没有数据")
            return None

        # 生成文件名
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'{device_id}_{timestamp}'

        # 根据格式导出
        if format == 'csv':
            return self.export_csv(data, f'{filename}.csv')
        elif format == 'excel':
            return self.export_excel(data, f'{filename}.xlsx')
        elif format == 'json':
            return self.export_json(data, f'{filename}.json')
        else:
            logger.error(f"不支持的导出格式: {format}")
            return None

    def export_alarm_records(self, database, device_id: str | None = None,
                             start_time: datetime | None = None, end_time: datetime | None = None,
                             format: str = 'csv') -> str | None:
        """
        导出报警记录

        Args:
            database: 数据库实例
            device_id: 设备ID（可选）
            start_time: 开始时间（可选）
            end_time: 结束时间（可选）
            format: 导出格式

        Returns:
            str: 导出文件路径
        """
        # 获取报警记录
        data = database.get_alarm_records(
            device_id=device_id,
            start_time=start_time,
            end_time=end_time,
            limit=10000
        )

        if not data:
            logger.warning("没有报警记录可导出")
            return None

        # 生成文件名
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'alarms_{timestamp}'

        # 根据格式导出
        if format == 'csv':
            return self.export_csv(data, f'{filename}.csv')
        elif format == 'excel':
            return self.export_excel(data, f'{filename}.xlsx')
        elif format == 'json':
            return self.export_json(data, f'{filename}.json')
        else:
            logger.error(f"不支持的导出格式: {format}")
            return None

    def list_exports(self) -> list[dict[str, Any]]:
        """
        列出所有导出文件

        Returns:
            list[dict[str, Any]]: 导出文件列表
        """
        exports = []

        for filepath in self.export_dir.iterdir():
            if filepath.is_file():
                stat = filepath.stat()
                exports.append({
                    'filename': filepath.name,
                    'size_bytes': stat.st_size,
                    'size_mb': round(stat.st_size / (1024 * 1024), 2),
                    'created_at': datetime.fromtimestamp(stat.st_ctime),
                    'modified_at': datetime.fromtimestamp(stat.st_mtime)
                })

        # 按修改时间排序
        exports.sort(key=lambda x: x['modified_at'], reverse=True)

        return exports

    def delete_export(self, filename: str) -> bool:
        """
        删除导出文件

        Args:
            filename: 文件名

        Returns:
            bool: 删除是否成功
        """
        filepath = self.export_dir / filename

        try:
            if filepath.exists():
                filepath.unlink()
                logger.info(f"删除导出文件: {filename}")
                return True
            else:
                logger.warning(f"文件不存在: {filename}")
                return False

        except Exception as e:
            logger.error(f"删除文件失败: {e}")
            return False
