"""
自适应压缩策略
根据内容类型和大小自动选择最佳压缩算法。

使用方式:
    from core.adaptive_compression import AdaptiveCompressor
    compressor = AdaptiveCompressor()
    compressed = compressor.compress(data, content_type='application/json')
"""

import gzip
import logging
from typing import Any, Dict, Optional, Tuple
from io import BytesIO

logger = logging.getLogger(__name__)

# 尝试导入brotli
try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False


class CompressionStrategy:
    """压缩策略"""
    NONE = 'none'
    GZIP = 'gzip'
    BROTLI = 'br'


class AdaptiveCompressor:
    """自适应压缩器"""

    # 最小压缩阈值（字节）
    MIN_COMPRESS_SIZE = 256

    # 内容类型压缩优先级
    COMPRESSIBLE_TYPES = {
        'application/json': True,
        'text/html': True,
        'text/css': True,
        'text/javascript': True,
        'application/javascript': True,
        'text/plain': True,
        'application/xml': True,
        'image/svg+xml': True,
    }

    # 不压缩的类型（已压缩）
    SKIP_TYPES = {
        'image/png',
        'image/jpeg',
        'image/gif',
        'image/webp',
        'video/mp4',
        'application/zip',
        'application/gzip',
        'application/x-brotli',
    }

    def __init__(
        self,
        min_size: int = None,
        gzip_level: int = 6,
        brotli_quality: int = 4,
    ):
        self.min_size = min_size or self.MIN_COMPRESS_SIZE
        self.gzip_level = gzip_level
        self.brotli_quality = brotli_quality

        # 统计
        self._stats = {
            'total': 0,
            'compressed': 0,
            'gzip': 0,
            'brotli': 0,
            'none': 0,
            'bytes_saved': 0,
        }

    def should_compress(self, content_type: str = None, content_length: int = 0) -> bool:
        """判断是否应该压缩"""
        # 检查大小
        if content_length < self.min_size:
            return False

        # 检查内容类型
        if content_type:
            base_type = content_type.split(';')[0].strip().lower()

            # 已压缩的类型
            if base_type in self.SKIP_TYPES:
                return False

            # 可压缩的类型
            if base_type in self.COMPRESSIBLE_TYPES:
                return True

            # 文本类型
            if base_type.startswith('text/'):
                return True

        # 默认压缩
        return True

    def choose_algorithm(self, accept_encoding: str = '') -> str:
        """根据客户端支持选择压缩算法"""
        accept_lower = accept_encoding.lower()

        # 优先brotli（压缩率更高）
        if HAS_BROTLI and 'br' in accept_lower:
            return CompressionStrategy.BROTLI

        # 其次gzip
        if 'gzip' in accept_lower:
            return CompressionStrategy.GZIP

        return CompressionStrategy.NONE

    def compress(
        self,
        data: bytes,
        content_type: str = None,
        algorithm: str = None,
    ) -> Tuple[bytes, str]:
        """
        压缩数据

        Args:
            data: 原始数据
            content_type: 内容类型
            algorithm: 指定压缩算法

        Returns:
            (压缩后数据, 使用的算法)
        """
        self._stats['total'] += 1

        # 检查是否需要压缩
        if not self.should_compress(content_type, len(data)):
            self._stats['none'] += 1
            return data, CompressionStrategy.NONE

        # 选择算法
        if algorithm is None:
            algorithm = CompressionStrategy.GZIP

        try:
            if algorithm == CompressionStrategy.BROTLI and HAS_BROTLI:
                compressed = brotli.compress(data, quality=self.brotli_quality)
                self._stats['brotli'] += 1
            elif algorithm == CompressionStrategy.GZIP:
                compressed = gzip.compress(data, compresslevel=self.gzip_level)
                self._stats['gzip'] += 1
            else:
                self._stats['none'] += 1
                return data, CompressionStrategy.NONE

            # 检查压缩效果
            if len(compressed) >= len(data):
                self._stats['none'] += 1
                return data, CompressionStrategy.NONE

            self._stats['compressed'] += 1
            self._stats['bytes_saved'] += len(data) - len(compressed)

            return compressed, algorithm

        except Exception as e:
            logger.warning(f"压缩失败: {e}")
            self._stats['none'] += 1
            return data, CompressionStrategy.NONE

    def get_stats(self) -> Dict[str, Any]:
        """获取压缩统计"""
        total = self._stats['total']
        compressed = self._stats['compressed']
        return {
            **self._stats,
            'compression_rate': round(compressed / max(total, 1) * 100, 1),
            'avg_savings': round(self._stats['bytes_saved'] / max(compressed, 1)),
            'brotli_available': HAS_BROTLI,
        }


# 全局实例
compressor = AdaptiveCompressor()
