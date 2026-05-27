"""
负载测试模块
用于测试系统在多设备高并发场景下的性能
"""

import time
import logging
import threading
import statistics
from typing import Any, Dict, List
from datetime import datetime
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


@dataclass
class LoadTestResult:
    """负载测试结果"""
    test_name: str
    start_time: datetime
    end_time: datetime
    total_requests: int
    successful_requests: int
    failed_requests: int
    response_times: List[float] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        """测试持续时间（秒）"""
        return (self.end_time - self.start_time).total_seconds()

    @property
    def success_rate(self) -> float:
        """成功率"""
        if self.total_requests == 0:
            return 0.0
        return self.successful_requests / self.total_requests

    @property
    def requests_per_second(self) -> float:
        """每秒请求数"""
        if self.duration_seconds == 0:
            return 0.0
        return self.total_requests / self.duration_seconds

    @property
    def avg_response_time(self) -> float:
        """平均响应时间（毫秒）"""
        if not self.response_times:
            return 0.0
        return statistics.mean(self.response_times) * 1000

    @property
    def p95_response_time(self) -> float:
        """95%响应时间（毫秒）"""
        if not self.response_times:
            return 0.0
        sorted_times = sorted(self.response_times)
        index = int(len(sorted_times) * 0.95)
        return sorted_times[index] * 1000

    @property
    def p99_response_time(self) -> float:
        """99%响应时间（毫秒）"""
        if not self.response_times:
            return 0.0
        sorted_times = sorted(self.response_times)
        index = int(len(sorted_times) * 0.99)
        return sorted_times[index] * 1000

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'test_name': self.test_name,
            'start_time': self.start_time.isoformat(),
            'end_time': self.end_time.isoformat(),
            'duration_seconds': round(self.duration_seconds, 2),
            'total_requests': self.total_requests,
            'successful_requests': self.successful_requests,
            'failed_requests': self.failed_requests,
            'success_rate': round(self.success_rate * 100, 2),
            'requests_per_second': round(self.requests_per_second, 2),
            'avg_response_time_ms': round(self.avg_response_time, 2),
            'p95_response_time_ms': round(self.p95_response_time, 2),
            'p99_response_time_ms': round(self.p99_response_time, 2),
            'errors': self.errors[:10]  # 只返回前10个错误
        }


class LoadTester:
    """
    负载测试器

    功能：
    - 模拟多设备并发请求
    - 测试API端点性能
    - 生成测试报告
    """

    def __init__(self, base_url: str = 'http://localhost:5000'):
        """
        初始化负载测试器

        Args:
            base_url: API基础URL
        """
        self.base_url = base_url
        self.results: List[LoadTestResult] = []
        self._lock = threading.Lock()

        logger.info(f"负载测试器初始化: {base_url}")

    def run_api_load_test(self, endpoint: str, method: str = 'GET',
                          concurrent_users: int = 10, requests_per_user: int = 10,
                          headers: Dict[str, str] = None) -> LoadTestResult:
        """
        运行API负载测试

        Args:
            endpoint: API端点
            method: HTTP方法
            concurrent_users: 并发用户数
            requests_per_user: 每用户请求数
            headers: 请求头

        Returns:
            测试结果
        """
        import requests

        test_name = f"{method} {endpoint} ({concurrent_users}用户)"
        start_time = datetime.now()
        response_times = []
        errors = []
        successful = 0
        failed = 0
        total = concurrent_users * requests_per_user

        def make_request(user_id: int) -> tuple:
            """发送单个请求"""
            url = f"{self.base_url}{endpoint}"
            try:
                start = time.time()
                if method.upper() == 'GET':
                    resp = requests.get(url, headers=headers, timeout=10)
                elif method.upper() == 'POST':
                    resp = requests.post(url, headers=headers, timeout=10)
                else:
                    resp = requests.request(method, url, headers=headers, timeout=10)
                elapsed = time.time() - start
                return (resp.status_code == 200, elapsed, None)
            except Exception as e:
                return (False, 0, str(e))

        # 并发执行请求
        with ThreadPoolExecutor(max_workers=concurrent_users) as executor:
            futures = []
            for user_id in range(concurrent_users):
                for _ in range(requests_per_user):
                    futures.append(executor.submit(make_request, user_id))

            for future in as_completed(futures):
                success, elapsed, error = future.result()
                if success:
                    successful += 1
                    response_times.append(elapsed)
                else:
                    failed += 1
                    if error:
                        errors.append(error)

        end_time = datetime.now()

        result = LoadTestResult(
            test_name=test_name,
            start_time=start_time,
            end_time=end_time,
            total_requests=total,
            successful_requests=successful,
            failed_requests=failed,
            response_times=response_times,
            errors=errors
        )

        self.results.append(result)
        return result

    def run_device_load_test(self, device_count: int = 10,
                             requests_per_device: int = 10,
                             auth_token: str = None) -> LoadTestResult:
        """
        运行设备负载测试

        Args:
            device_count: 设备数量
            requests_per_device: 每设备请求数
            auth_token: 认证令牌

        Returns:
            测试结果
        """
        import requests

        test_name = f"设备负载测试 ({device_count}设备)"
        start_time = datetime.now()
        response_times = []
        errors = []
        successful = 0
        failed = 0
        total = device_count * requests_per_device

        headers = {}
        if auth_token:
            headers['Authorization'] = f'Bearer {auth_token}'

        def query_device(device_id: str) -> tuple:
            """查询设备数据"""
            url = f"{self.base_url}/api/data/latest/{device_id}"
            try:
                start = time.time()
                resp = requests.get(url, headers=headers, timeout=10)
                elapsed = time.time() - start
                return (resp.status_code == 200, elapsed, None)
            except Exception as e:
                return (False, 0, str(e))

        # 生成设备ID列表
        device_ids = [f"device_{i:03d}" for i in range(device_count)]

        # 并发查询设备
        with ThreadPoolExecutor(max_workers=device_count) as executor:
            futures = []
            for device_id in device_ids:
                for _ in range(requests_per_device):
                    futures.append(executor.submit(query_device, device_id))

            for future in as_completed(futures):
                success, elapsed, error = future.result()
                if success:
                    successful += 1
                    response_times.append(elapsed)
                else:
                    failed += 1
                    if error:
                        errors.append(error)

        end_time = datetime.now()

        result = LoadTestResult(
            test_name=test_name,
            start_time=start_time,
            end_time=end_time,
            total_requests=total,
            successful_requests=successful,
            failed_requests=failed,
            response_times=response_times,
            errors=errors
        )

        self.results.append(result)
        return result

    def run_full_load_test(self, auth_token: str = None) -> List[LoadTestResult]:
        """
        运行完整负载测试

        Args:
            auth_token: 认证令牌

        Returns:
            测试结果列表
        """
        headers = {}
        if auth_token:
            headers['Authorization'] = f'Bearer {auth_token}'

        results = []

        # 测试1: 设备列表API
        logger.info("测试设备列表API...")
        result = self.run_api_load_test(
            endpoint='/api/devices',
            concurrent_users=10,
            requests_per_user=5,
            headers=headers
        )
        results.append(result)
        logger.info(f"  成功率: {result.success_rate:.1%}, RPS: {result.requests_per_second:.1f}")

        # 测试2: 实时数据API
        logger.info("测试实时数据API...")
        result = self.run_api_load_test(
            endpoint='/api/data/realtime',
            concurrent_users=20,
            requests_per_user=5,
            headers=headers
        )
        results.append(result)
        logger.info(f"  成功率: {result.success_rate:.1%}, RPS: {result.requests_per_second:.1f}")

        # 测试3: 报警API
        logger.info("测试报警API...")
        result = self.run_api_load_test(
            endpoint='/api/alarms',
            concurrent_users=10,
            requests_per_user=5,
            headers=headers
        )
        results.append(result)
        logger.info(f"  成功率: {result.success_rate:.1%}, RPS: {result.requests_per_second:.1f}")

        # 测试4: 设备状态API
        logger.info("测试设备状态API...")
        result = self.run_api_load_test(
            endpoint='/api/system/status',
            concurrent_users=15,
            requests_per_user=5,
            headers=headers
        )
        results.append(result)
        logger.info(f"  成功率: {result.success_rate:.1%}, RPS: {result.requests_per_second:.1f}")

        # 测试5: 多设备并发查询
        logger.info("测试多设备并发查询...")
        result = self.run_device_load_test(
            device_count=10,
            requests_per_device=5,
            auth_token=auth_token
        )
        results.append(result)
        logger.info(f"  成功率: {result.success_rate:.1%}, RPS: {result.requests_per_second:.1f}")

        return results

    def generate_report(self) -> str:
        """
        生成测试报告

        Returns:
            测试报告文本
        """
        if not self.results:
            return "没有测试结果"

        report = []
        report.append("=" * 60)
        report.append("负载测试报告")
        report.append("=" * 60)
        report.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append("")

        for i, result in enumerate(self.results, 1):
            report.append(f"测试 {i}: {result.test_name}")
            report.append("-" * 40)
            report.append(f"  总请求数: {result.total_requests}")
            report.append(f"  成功请求数: {result.successful_requests}")
            report.append(f"  失败请求数: {result.failed_requests}")
            report.append(f"  成功率: {result.success_rate:.1%}")
            report.append(f"  每秒请求数: {result.requests_per_second:.1f}")
            report.append(f"  平均响应时间: {result.avg_response_time:.1f}ms")
            report.append(f"  95%响应时间: {result.p95_response_time:.1f}ms")
            report.append(f"  99%响应时间: {result.p99_response_time:.1f}ms")
            report.append(f"  测试时长: {result.duration_seconds:.1f}秒")

            if result.errors:
                report.append(f"  错误数: {len(result.errors)}")
                for error in result.errors[:3]:
                    report.append(f"    - {error}")
            report.append("")

        # 汇总统计
        report.append("=" * 60)
        report.append("汇总统计")
        report.append("=" * 60)

        total_requests = sum(r.total_requests for r in self.results)
        total_successful = sum(r.successful_requests for r in self.results)
        total_failed = sum(r.failed_requests for r in self.results)

        report.append(f"总请求数: {total_requests}")
        report.append(f"总成功数: {total_successful}")
        report.append(f"总失败数: {total_failed}")
        report.append(f"总体成功率: {total_successful / total_requests:.1%}" if total_requests > 0 else "总体成功率: N/A")

        all_response_times = []
        for r in self.results:
            all_response_times.extend(r.response_times)

        if all_response_times:
            report.append(f"总体平均响应时间: {statistics.mean(all_response_times) * 1000:.1f}ms")
            report.append(f"总体95%响应时间: {sorted(all_response_times)[int(len(all_response_times) * 0.95)] * 1000:.1f}ms")

        return "\n".join(report)

    def save_report(self, filepath: str):
        """
        保存测试报告到文件

        Args:
            filepath: 文件路径
        """
        report = self.generate_report()
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(report)
        logger.info(f"测试报告已保存到: {filepath}")


# 命令行运行
if __name__ == '__main__':
    import argparse
    import sys

    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    parser = argparse.ArgumentParser(description='SCADA系统负载测试工具')
    parser.add_argument('--url', type=str, default='http://localhost:5000',
                        help='API基础URL')
    parser.add_argument('--token', type=str, default=None,
                        help='认证令牌')
    parser.add_argument('--output', type=str, default='load_test_report.txt',
                        help='报告输出文件')
    parser.add_argument('--users', type=int, default=10,
                        help='并发用户数')
    parser.add_argument('--requests', type=int, default=10,
                        help='每用户请求数')

    args = parser.parse_args()

    # 创建测试器
    tester = LoadTester(base_url=args.url)

    # 运行测试
    logger.info("开始负载测试...")
    results = tester.run_full_load_test(auth_token=args.token)

    # 生成报告
    report = tester.generate_report()
    print(report)

    # 保存报告
    tester.save_report(args.output)

    logger.info("负载测试完成")
