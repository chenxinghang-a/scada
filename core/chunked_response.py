"""
分块响应助手
对大数据量API响应使用Transfer-Encoding: chunked，
避免一次性加载全部数据到内存。

使用方式:
    from core.chunked_response import chunked_json, chunked_csv

    @app.route('/api/export/large')
    def export_large():
        def generate():
            for batch in data_batches():
                yield json.dumps(batch) + '\\n'
        return chunked_json(generate())
"""

import json
import csv
import io
import logging
from typing import Iterator, Any
from flask import Response, stream_with_context

logger = logging.getLogger(__name__)


def chunked_json(iterator: Iterator[str], status: int = 200) -> Response:
    """
    分块JSON响应

    Args:
        iterator: 生成JSON字符串的迭代器
        status: HTTP状态码
    """
    return Response(
        stream_with_context(iterator),
        status=status,
        mimetype='application/json',
        headers={
            'Transfer-Encoding': 'chunked',
            'X-Content-Format': 'chunked-json',
        }
    )


def chunked_csv(iterator: Iterator[list], headers: list[str] = None, status: int = 200) -> Response:
    """
    分块CSV响应

    Args:
        iterator: 生成行数据的迭代器
        headers: CSV表头
        status: HTTP状态码
    """
    def generate():
        output = io.StringIO()
        writer = csv.writer(output)

        if headers:
            writer.writerow(headers)
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

        for row in iterator:
            writer.writerow(row)
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

    return Response(
        stream_with_context(generate()),
        status=status,
        mimetype='text/csv',
        headers={
            'Transfer-Encoding': 'chunked',
            'Content-Disposition': 'attachment; filename=export.csv',
        }
    )


def chunked_ndjson(iterator: Iterator[dict], status: int = 200) -> Response:
    """
    分块NDJSON响应（每行一个JSON对象）

    Args:
        iterator: 生成字典的迭代器
        status: HTTP状态码
    """
    def generate():
        for item in iterator:
            yield json.dumps(item, ensure_ascii=False, default=str) + '\n'

    return Response(
        stream_with_context(generate()),
        status=status,
        mimetype='application/x-ndjson',
        headers={
            'Transfer-Encoding': 'chunked',
            'X-Content-Format': 'ndjson',
        }
    )


def chunked_lines(iterator: Iterator[str], status: int = 200) -> Response:
    """
    分块文本行响应

    Args:
        iterator: 生成文本行的迭代器
        status: HTTP状态码
    """
    def generate():
        for line in iterator:
            yield line + '\n'

    return Response(
        stream_with_context(generate()),
        status=status,
        mimetype='text/plain',
        headers={'Transfer-Encoding': 'chunked'}
    )


class BatchedIterator:
    """
    批量数据迭代器
    将大量数据分批处理，每批大小可控。
    """

    def __init__(self, data_source: Any, batch_size: int = 1000):
        self._data_source = data_source
        self._batch_size = batch_size

    def __iter__(self):
        batch = []
        for item in self._data_source:
            batch.append(item)
            if len(batch) >= self._batch_size:
                yield batch
                batch = []
        if batch:
            yield batch
