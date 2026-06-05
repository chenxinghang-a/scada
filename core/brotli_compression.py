"""
Brotli压缩支持
比gzip更高的压缩率，适合静态资源和API响应。

使用方式:
    from core.brotli_compression import BrotliCompressor
    compressor = BrotliCompressor()
    compressed = compressor.compress(data)
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 尝试导入brotli
try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False
    logger.info("brotli库未安装，Brotli压缩不可用")


class BrotliCompressor:
    """Brotli压缩器"""

    def __init__(self, quality: int = 4, lgwin: int = 22):
        """
        Args:
            quality: 压缩质量 (0-11, 默认4, 平衡速度和压缩率)
            lgwin: 窗口大小 (10-24, 默认22)
        """
        self.quality = min(max(quality, 0), 11)
        self.lgwin = min(max(lgwin, 10), 24)
        self._stats = {
            'compressed': 0,
            'original_bytes': 0,
            'compressed_bytes': 0,
        }

    def compress(self, data: bytes) -> bytes:
        """压缩数据"""
        if not HAS_BROTLI:
            return data

        try:
            compressed = brotli.compress(
                data,
                quality=self.quality,
                lgwin=self.lgwin,
            )

            self._stats['compressed'] += 1
            self._stats['original_bytes'] += len(data)
            self._stats['compressed_bytes'] += len(compressed)

            return compressed
        except Exception as e:
            logger.warning(f"Brotli压缩失败: {e}")
            return data

    def decompress(self, data: bytes) -> bytes:
        """解压数据"""
        if not HAS_BROTLI:
            return data

        try:
            return brotli.decompress(data)
        except Exception as e:
            logger.warning(f"Brotli解压失败: {e}")
            return data

    def get_stats(self) -> dict:
        """获取压缩统计"""
        total = self._stats['original_bytes']
        compressed = self._stats['compressed_bytes']
        ratio = (1 - compressed / total) * 100 if total > 0 else 0

        return {
            'available': HAS_BROTLI,
            'quality': self.quality,
            'compressed_count': self._stats['compressed'],
            'original_bytes': self._stats['original_bytes'],
            'compressed_bytes': self._stats['compressed_bytes'],
            'compression_ratio': round(ratio, 2),
        }


class BrotliMiddleware:
    """Brotli压缩WSGI中间件"""

    def __init__(self, app, minimum_size: int = 1024, quality: int = 4):
        self.app = app
        self.minimum_size = minimum_size
        self.compressor = BrotliCompressor(quality=quality)

    def __call__(self, environ, start_response):
        if not HAS_BROTLI:
            return self.app(environ, start_response)

        # 检查客户端是否支持Brotli
        accept_encoding = environ.get('HTTP_ACCEPT_ENCODING', '')
        if 'br' not in accept_encoding:
            return self.app(environ, start_response)

        # 包装start_response以拦截响应
        response_data = []
        status_code = [None]
        response_headers = [None]

        def custom_start_response(status, headers, exc_info=None):
            status_code[0] = status
            response_headers[0] = headers
            return start_response(status, headers, exc_info)

        # 获取响应内容
        app_iter = self.app(environ, custom_start_response)

        # 收集响应数据
        content = b''.join(app_iter)
        if hasattr(app_iter, 'close'):
            app_iter.close()

        # 检查是否需要压缩
        content_type = ''
        for header_name, header_value in (response_headers[0] or []):
            if header_name.lower() == 'content-type':
                content_type = header_value.lower()
                break

        # 只压缩文本类型的响应
        compressible_types = ['text/', 'application/json', 'application/javascript', 'application/xml']
        should_compress = (
            len(content) >= self.minimum_size
            and any(ct in content_type for ct in compressible_types)
        )

        if should_compress:
            compressed = self.compressor.compress(content)
            if len(compressed) < len(content):
                # 添加Content-Encoding头
                new_headers = []
                for name, value in (response_headers[0] or []):
                    if name.lower() != 'content-length':
                        new_headers.append((name, value))
                new_headers.append(('Content-Encoding', 'br'))
                new_headers.append(('Content-Length', str(len(compressed))))

                start_response(status_code[0], new_headers)
                return [compressed]

        return [content]


def get_brotli_stats() -> dict:
    """获取Brotli压缩统计"""
    return {
        'available': HAS_BROTLI,
        'library': 'brotli' if HAS_BROTLI else None,
    }
