"""
流式数据导出
大数据量CSV/Excel流式生成，避免内存溢出。

使用方式:
    from core.streaming_export import StreamingCSVExporter, StreamingExcelExporter

    @app.route('/api/export/large')
    def export_large():
        def query_data():
            for batch in get_data_batches():
                yield batch
        return StreamingCSVExporter(query_data, headers=['id','name']).response()
"""

import csv
import io
import logging
from typing import Iterator, List, Any, Callable
from flask import Response, stream_with_context

logger = logging.getLogger(__name__)


class StreamingCSVExporter:
    """流式CSV导出器"""

    def __init__(self, data_source: Callable[[], Iterator[List[Any]]],
                 headers: List[str] = None, encoding: str = 'utf-8-sig'):
        self.data_source = data_source
        self.headers = headers
        self.encoding = encoding

    def _generate(self):
        """生成CSV内容流"""
        output = io.StringIO()
        writer = csv.writer(output)

        # 写入表头
        if self.headers:
            writer.writerow(self.headers)
            yield output.getvalue().encode(self.encoding)
            output.seek(0)
            output.truncate(0)

        # 流式写入数据
        row_count = 0
        for batch in self.data_source():
            for row in batch:
                writer.writerow(row)
                row_count += 1
                # 每100行flush一次
                if row_count % 100 == 0:
                    yield output.getvalue().encode(self.encoding)
                    output.seek(0)
                    output.truncate(0)

        # 写入剩余数据
        remaining = output.getvalue()
        if remaining:
            yield remaining.encode(self.encoding)

        logger.info("CSV导出完成: %d行", row_count)

    def response(self, filename: str = 'export.csv') -> Response:
        """生成流式响应"""
        return Response(
            stream_with_context(self._generate()),
            mimetype='text/csv',
            headers={
                'Content-Disposition': f'attachment; filename={filename}',
                'Cache-Control': 'no-cache',
                'X-Content-Type-Options': 'nosniff',
            }
        )


class StreamingExcelExporter:
    """流式Excel导出器（使用openpyxl的write_only模式）"""

    def __init__(self, data_source: Callable[[], Iterator[List[Any]]],
                 headers: List[str] = None, sheet_name: str = 'Sheet1'):
        self.data_source = data_source
        self.headers = headers
        self.sheet_name = sheet_name

    def _generate(self):
        """生成Excel内容流"""
        try:
            from openpyxl import Workbook
            from openpyxl.writer.excel import save_virtual_workbook
        except ImportError:
            logger.error("openpyxl未安装，无法导出Excel")
            yield b''
            return

        wb = Workbook(write_only=True)
        ws = wb.create_sheet(title=self.sheet_name)

        # 写入表头
        if self.headers:
            ws.append(self.headers)

        # 写入数据
        row_count = 0
        for batch in self.data_source():
            for row in batch:
                ws.append(row)
                row_count += 1

        # 保存到内存
        virtual_workbook = save_virtual_workbook(wb)
        yield virtual_workbook

        logger.info("Excel导出完成: %d行", row_count)

    def response(self, filename: str = 'export.xlsx') -> Response:
        """生成流式响应"""
        return Response(
            stream_with_context(self._generate()),
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={
                'Content-Disposition': f'attachment; filename={filename}',
                'Cache-Control': 'no-cache',
            }
        )


class BatchedQueryIterator:
    """分批查询迭代器"""

    def __init__(self, query_func: Callable, batch_size: int = 1000):
        self.query_func = query_func
        self.batch_size = batch_size

    def __iter__(self) -> Iterator[List[Any]]:
        offset = 0
        while True:
            batch = self.query_func(limit=self.batch_size, offset=offset)
            if not batch:
                break
            yield batch
            offset += self.batch_size
            if len(batch) < self.batch_size:
                break
