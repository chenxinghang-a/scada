"""
数据导入校验器
CSV/Excel导入预检，验证数据格式、类型、完整性。

使用方式:
    from core.import_validator import ImportValidator
    validator = ImportValidator(schema)
    result = validator.validate_csv(file_content)
"""

import csv
import io
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ColumnSchema:
    """列定义"""

    def __init__(
        self,
        name: str,
        required: bool = False,
        data_type: str = 'string',
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
        allowed_values: Optional[List[Any]] = None,
        max_length: Optional[int] = None,
        pattern: Optional[str] = None,
    ):
        self.name = name
        self.required = required
        self.data_type = data_type
        self.min_value = min_value
        self.max_value = max_value
        self.allowed_values = allowed_values
        self.max_length = max_length
        self.pattern = pattern


class ImportResult:
    """导入验证结果"""

    def __init__(self):
        self.valid = True
        self.errors: List[Dict[str, Any]] = []
        self.warnings: List[Dict[str, Any]] = []
        self.row_count = 0
        self.valid_rows = 0
        self.preview: List[Dict[str, Any]] = []

    def add_error(self, row: int, column: str, message: str):
        self.valid = False
        self.errors.append({
            'row': row,
            'column': column,
            'message': message,
        })

    def add_warning(self, row: int, column: str, message: str):
        self.warnings.append({
            'row': row,
            'column': column,
            'message': message,
        })

    def to_dict(self) -> Dict[str, Any]:
        return {
            'valid': self.valid,
            'row_count': self.row_count,
            'valid_rows': self.valid_rows,
            'error_count': len(self.errors),
            'warning_count': len(self.warnings),
            'errors': self.errors[:100],  # 限制返回数量
            'warnings': self.warnings[:100],
            'preview': self.preview,
        }


class ImportValidator:
    """数据导入校验器"""

    def __init__(self, columns: List[ColumnSchema], max_rows: int = 10000):
        self.columns = {col.name: col for col in columns}
        self.column_names = [col.name for col in columns]
        self.max_rows = max_rows

    def validate_csv(self, content: str, encoding: str = 'utf-8') -> ImportResult:
        """验证CSV内容"""
        result = ImportResult()

        try:
            # 尝试解码
            if isinstance(content, bytes):
                content = content.decode(encoding)
        except UnicodeDecodeError:
            result.add_error(0, '', f'文件编码错误，无法使用{encoding}解码')
            return result

        reader = csv.DictReader(io.StringIO(content))

        # 检查列头
        if reader.fieldnames:
            self._validate_headers(reader.fieldnames, result)

        # 验证每行数据
        for row_num, row in enumerate(reader, start=2):
            if row_num > self.max_rows + 1:
                result.add_warning(row_num, '', f'超过最大行数限制({self.max_rows})')
                break

            result.row_count += 1
            row_valid = True

            for col_name, col_schema in self.columns.items():
                value = row.get(col_name, '').strip()

                # 必填检查
                if col_schema.required and not value:
                    result.add_error(row_num, col_name, f'{col_name}为必填项')
                    row_valid = False
                    continue

                if not value:
                    continue

                # 类型检查
                type_error = self._validate_type(value, col_schema, row_num, col_name)
                if type_error:
                    result.add_error(row_num, col_name, type_error)
                    row_valid = False
                    continue

                # 范围检查
                if col_schema.min_value is not None or col_schema.max_value is not None:
                    range_error = self._validate_range(value, col_schema, row_num, col_name)
                    if range_error:
                        result.add_error(row_num, col_name, range_error)
                        row_valid = False

                # 枚举值检查
                if col_schema.allowed_values:
                    if value not in [str(v) for v in col_schema.allowed_values]:
                        result.add_error(row_num, col_name, f'{col_name}必须是以下值之一: {", ".join(map(str, col_schema.allowed_values))}')
                        row_valid = False

                # 长度检查
                if col_schema.max_length and len(value) > col_schema.max_length:
                    result.add_error(row_num, col_name, f'{col_name}长度不能超过{col_schema.max_length}')
                    row_valid = False

            if row_valid:
                result.valid_rows += 1

            # 预览前10行
            if result.row_count <= 10:
                result.preview.append(dict(row))

        return result

    def validate_dict_list(self, data: List[Dict[str, Any]]) -> ImportResult:
        """验证字典列表"""
        result = ImportResult()

        for row_num, row in enumerate(data, start=1):
            if row_num > self.max_rows:
                result.add_warning(row_num, '', f'超过最大行数限制({self.max_rows})')
                break

            result.row_count += 1
            row_valid = True

            for col_name, col_schema in self.columns.items():
                value = row.get(col_name)

                if col_schema.required and (value is None or value == ''):
                    result.add_error(row_num, col_name, f'{col_name}为必填项')
                    row_valid = False
                    continue

                if value is None or value == '':
                    continue

                # 类型检查
                type_error = self._validate_type(value, col_schema, row_num, col_name)
                if type_error:
                    result.add_error(row_num, col_name, type_error)
                    row_valid = False

            if row_valid:
                result.valid_rows += 1

            if result.row_count <= 10:
                result.preview.append(row)

        return result

    def _validate_headers(self, headers: List[str], result: ImportResult):
        """验证列头"""
        header_set = set(headers)

        for col_name, col_schema in self.columns.items():
            if col_schema.required and col_name not in header_set:
                result.add_error(0, col_name, f'缺少必需列: {col_name}')

        # 检查未知列
        for header in headers:
            if header not in self.columns:
                result.add_warning(0, header, f'未知列: {header}')

    def _validate_type(self, value: Any, schema: ColumnSchema, row: int, col: str) -> Optional[str]:
        """验证数据类型"""
        str_value = str(value)

        if schema.data_type == 'integer':
            try:
                int(str_value)
            except ValueError:
                return f'{col}必须为整数'

        elif schema.data_type == 'number':
            try:
                float(str_value)
            except ValueError:
                return f'{col}必须为数字'

        elif schema.data_type == 'boolean':
            if str_value.lower() not in ('true', 'false', '1', '0', 'yes', 'no'):
                return f'{col}必须为布尔值'

        return None

    def _validate_range(self, value: Any, schema: ColumnSchema, row: int, col: str) -> Optional[str]:
        """验证数值范围"""
        try:
            num_value = float(str(value))

            if schema.min_value is not None and num_value < schema.min_value:
                return f'{col}不能小于{schema.min_value}'

            if schema.max_value is not None and num_value > schema.max_value:
                return f'{col}不能大于{schema.max_value}'

        except ValueError:
            return f'{col}必须为数字'

        return None


# 预定义设备导入校验
DEVICE_IMPORT_SCHEMA = [
    ColumnSchema('name', required=True, data_type='string', max_length=100),
    ColumnSchema('protocol', required=True, data_type='string', allowed_values=['modbus_tcp', 'modbus_rtu', 'opcua', 'mqtt']),
    ColumnSchema('host', data_type='string', max_length=255),
    ColumnSchema('port', data_type='integer', min_value=1, max_value=65535),
    ColumnSchema('slave_id', data_type='integer', min_value=1, max_value=247),
]

# 预定义历史数据导入校验
HISTORY_IMPORT_SCHEMA = [
    ColumnSchema('device_id', required=True, data_type='string'),
    ColumnSchema('register_name', required=True, data_type='string'),
    ColumnSchema('value', required=True, data_type='number'),
    ColumnSchema('timestamp', required=True, data_type='string'),
]
