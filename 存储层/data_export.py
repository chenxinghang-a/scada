"""
数据导出模块
支持CSV、Excel、JSON格式导出
"""

import csv
import json
import logging
import re
from typing import Any
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

#: 会被 Excel / LibreOffice 当成**公式**的起始字符（CWE-1236，CSV/Excel 注入）。
#: `\t` / `\r` 也算：某些版本会先剥掉前导空白再解析公式。
_FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')

#: 纯负数字符串（`-5` / `-1.2` / `-1.2e3`）。这类不是公式，不该被加引号。
_NEGATIVE_NUMBER_RE = re.compile(r'^-\d+(\.\d+)?([eE][+-]?\d+)?$')


def _sanitize_cell(value: Any) -> Any:
    """中和「以公式前缀开头的**字符串**单元格」（前置一个单引号）。

    为什么需要（CWE-1236）
    ----------------------
    设备名 / 寄存器名 / 单位都是用户可填的。若某个值以 `=` 开头，导出成 CSV
    再用 Excel 打开时会被当作公式执行 —— `=cmd|'/C calc'!A0` 这类 payload 能
    直接拉起进程；`=HYPERLINK(...)` 还能把数据外带。导出文件通常会被转发给
    别人，等于把攻击面带出系统。

    为什么**只处理字符串**、且放过纯负数
    ----------------------------------
    SCADA 数据里负数遍地（零下温度、反向功率）。若对数值也加引号，
    `-5.0` 会变成文本，Excel 里再也算不了 —— 修安全问题不能顺手把数据搞坏。
    数值本身不是公式载体；字符串形态的 `"-5"` / `"-1.2e3"` 同理放过。
    这条规则与前端 `scada-app/src/utils/export.ts::escCSV` 保持一致。
    """
    if not isinstance(value, str):
        return value
    if value[:1] not in _FORMULA_PREFIXES:
        return value
    if _NEGATIVE_NUMBER_RE.match(value):
        return value
    return "'" + value


def _sanitize_rows(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对整批数据逐单元格中和（保持键与顺序不变）。"""
    return [{k: _sanitize_cell(v) for k, v in row.items()} for row in data]



class DataExport:
    """
    数据导出类
    支持多种格式的数据导出
    """

    def __init__(self, export_dir: str | Path | None = None):
        """
        初始化数据导出

        Args:
            export_dir: 导出目录。缺省使用 paths.EXPORT_DIR（项目根的 exports/）。
        """
        if export_dir is None:
            # 必须给绝对路径：api_data.export_device_data() 会把这里返回的路径
            # 直接交给 flask.send_file()，而 werkzeug 对相对路径是按
            # current_app.root_path 解析的，不是按 cwd。用相对 'exports' 时
            # 写文件在 cwd/exports、读文件却在 app_root/exports，
            # 必然 FileNotFoundError → 导出接口恒 500。
            from paths import EXPORT_DIR
            export_dir = EXPORT_DIR
        self.export_dir = Path(export_dir).resolve()
        self.export_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_target(self, filename: str) -> Path:
        """把调用方给的文件名落成 export_dir 内的绝对路径（防目录穿越）。

        为什么必须做：filename 可能来自请求体（`/api/data/export` 就是），
        而 `self.export_dir / '../../x.csv'` 会直接写到目录外；
        `delete_export` 更是能删掉目录外任意文件。

        两道关：
          1. 只取 basename（`../../a.csv` → `a.csv`），拒绝空名 / `.` / `..`；
          2. resolve 之后再复核父目录必须**正好**是 export_dir
             （防符号链接之类的绕过）。

        不合规时抛 ValueError —— 调用方捕获后转成「导出失败」，
        绝不退化成「写到目录外」。
        """
        import os

        name = os.path.basename(str(filename or '').replace('\\', '/')).strip()
        if not name or name in ('.', '..'):
            raise ValueError(f'非法导出文件名: {filename!r}')
        target = (self.export_dir / name).resolve()
        if target.parent != self.export_dir:
            raise ValueError(f'导出路径越界: {filename!r}')
        return target

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
            return None

        # 生成文件名
        if not filename:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'export_{timestamp}.csv'

        try:
            filepath = self._resolve_target(filename)
            # 获取字段名
            fieldnames = data[0].keys()

            with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                # 逐格中和公式前缀（CWE-1236），见 _sanitize_cell 的说明
                writer.writerows(_sanitize_rows(data))

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

        try:
            filepath = self._resolve_target(filename)
            # pandas.to_excel 走 openpyxl 时，以 `=` 开头的**字符串**会被写成公式
            # （data_type='f'），所以 Excel 这条路同样要中和（CWE-1236）。
            df = pd.DataFrame(_sanitize_rows(data))
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

        try:
            filepath = self._resolve_target(filename)
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
            # 没有寄存器信息时，用 None 查询全部数据
            register_data = database.get_history_data(
                device_id=device_id,
                register_name=None,
                start_time=start_time,
                end_time=end_time
            )
            data.extend(register_data)
        else:
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
                    'created_at': datetime.fromtimestamp(stat.st_ctime).isoformat(),
                    'modified_at': datetime.fromtimestamp(stat.st_mtime).isoformat()
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
        try:
            filepath = self._resolve_target(filename)
        except ValueError as exc:
            logger.warning(f"拒绝删除非法导出文件名: {filename!r}（{exc}）")
            return False

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
