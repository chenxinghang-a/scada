#!/usr/bin/env python3
"""
网关服务启动脚本

独立进程运行协议网关，实现与主系统的完全解耦。

使用方式：
    # 启动Modbus网关
    python gateway/run_gateway.py --type modbus --config gateway/config.yaml

    # 启动所有网关
    python gateway/run_gateway.py --all --config gateway/config.yaml
"""

from typing import Any
import sys
import argparse
import logging
import signal
import yaml
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from gateway import (
    ModbusGateway, S7Gateway, OPCUAGateway, IEC104Gateway, DNP3Gateway,
)
import paths

#: 网关类型 → 实现类。**唯一真源**：argparse 的 choices 与 --all 的分发都用它，
#: 避免出现「choices 里列了 mqtt、但 map 里没有 → 一选就报『不支持的网关类型』」
#: 这种幽灵选项。新增网关时只改这一处。
#: 模块级 logger —— `start_gateways()` 是模块级函数，不能用 `main()` 里的局部 logger。
#: （`main()` 里仍会建一个同名局部变量，作用域互不影响。）
logger = logging.getLogger("GatewayMain")

GATEWAY_TYPES: dict[str, Any] = {
    'modbus': ModbusGateway,
    's7': S7Gateway,
    'opcua': OPCUAGateway,
    'iec104': IEC104Gateway,
    'dnp3': DNP3Gateway,
}


def setup_logging(log_level: str = "INFO"):
    """配置日志"""
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(paths.resolve('logs/gateway.log')), encoding='utf-8')
        ]
    )


def load_config(config_path: str) -> dict[str, Any]:
    """加载配置文件"""
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def create_gateway(gateway_type: str, config: dict[str, Any]):
    """根据类型创建网关实例"""
    gateway_class = GATEWAY_TYPES.get(gateway_type)
    if not gateway_class:
        raise ValueError(f"不支持的网关类型: {gateway_type}")

    return gateway_class(config)


def select_gateway_configs(config: dict[str, Any], gateway_type: str,
                           run_all: bool) -> list[dict[str, Any]]:
    """从配置文件里挑出要启动的网关条目。

    * ``run_all=True`` → 配置里**所有**带 type 的条目；
    * 否则 → 按 ``gateway_type`` **筛选**出全部匹配条目（可能多条）。

    ⚠️ 历史实现是「取 ``gateways[0]``，再把它的 type 改成 ``--type``」，
    有两个后果（2026-10-13 实测复刻）：
      1. **拿错连接参数**：`--protocol s7` 会带着 ``modbus_gateway_01`` 的
         host/port 去建 S7 网关 —— 连到错误设备，且不报错；
      2. **原地改写配置**：`gw_config['type'] = args.type` 直接改了
         ``config['gateways'][0]``，后续再遍历该列表会二次踩到。
    改为「只读筛选 + 返回副本」，两个问题一起消失。
    """
    entries = config.get('gateways')
    if entries:
        if run_all:
            return [e for e in entries if isinstance(e, dict) and e.get('type')]
        return [e for e in entries
                if isinstance(e, dict) and e.get('type') == gateway_type]

    # 老格式：配置文件本身就是单个网关条目（可能没有 type 字段，由 --type 指定）。
    # 复制一份再补 type，绝不动入参。
    single = dict(config)
    single.setdefault('type', gateway_type)
    if run_all or single.get('type') == gateway_type:
        return [single]
    return []


def _declared_gateway_types(config: dict[str, Any]) -> str:
    """列出配置里声明过的 type，供「找不到条目」时给出可行动的提示。"""
    entries = config.get('gateways') or []
    types = sorted({str(e.get('type')) for e in entries
                    if isinstance(e, dict) and e.get('type')})
    return ', '.join(types) if types else '（无）'


def start_gateways(gateways: list) -> tuple[list, list]:
    """逐个启动网关，返回 (启动成功的 id 列表, 失败的 id 列表)。

    round 293：原先 `main()` 里写的是
        try: gateway.start()
        except Exception as e: logger.error(...)
    而 `BaseGateway.start()` 既没有返回值、失败时也只是打日志 ——
    于是"全部启动失败"和"全部启动成功"在外部表现完全一致：
    进程活着、退出码 0、最后一行日志写的是「网关服务已启动」。
    """
    started: list[str] = []
    failed: list[str] = []
    for gateway in gateways:
        gw_id = getattr(gateway, 'gateway_id', repr(gateway))
        try:
            ok = gateway.start()
        except Exception as e:      # noqa: BLE001 —— 单个网关崩溃不能带崩其余
            logger.error("启动网关 %s 异常: %s", gw_id, e)
            failed.append(gw_id)
            continue
        if ok:
            started.append(gw_id)
        else:
            failed.append(gw_id)
    return started, failed


def main():
    parser = argparse.ArgumentParser(description='工业SCADA协议网关服务')
    # `--protocol` 是 `--type` 的兼容别名：README 一直写的是 `--protocol modbus`，
    # 而代码只认 `--type` —— 照文档执行会直接报「unrecognized arguments」。
    parser.add_argument('--type', '--protocol', dest='type', type=str, default='modbus',
                        choices=sorted(GATEWAY_TYPES),
                        help='网关类型（--protocol 为兼容别名）')
    parser.add_argument('--config', type=str, default='gateway/config.yaml',
                        help='配置文件路径')
    parser.add_argument('--all', action='store_true',
                        help='启动所有配置的网关')
    parser.add_argument('--log-level', type=str, default='INFO',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                        help='日志级别')

    args = parser.parse_args()

    # 配置日志
    setup_logging(args.log_level)
    logger = logging.getLogger("GatewayMain")

    # 加载配置
    try:
        config = load_config(args.config)
    except Exception as e:
        logger.error(f"加载配置失败: {e}")
        sys.exit(1)

    # 创建网关实例
    gateways = []

    if args.all:
        # 启动所有网关（单条创建失败不阻断其余）
        for gw_config in select_gateway_configs(config, args.type, True):
            gw_type = gw_config.get('type')
            try:
                gateway = create_gateway(gw_type, gw_config)
                gateways.append(gateway)
            except Exception as e:
                logger.error(f"创建{gw_type}网关失败: {e}")
    else:
        # 启动指定类型的网关 —— 按 type 筛选，而不是「取第 0 条再改它的 type」
        selected = select_gateway_configs(config, args.type, False)
        if not selected:
            logger.error(
                "配置 %s 里没有 type=%s 的网关条目（配置里可用: %s）",
                args.config, args.type, _declared_gateway_types(config),
            )
            sys.exit(1)
        for gw_config in selected:
            try:
                gateway = create_gateway(args.type, gw_config)
                gateways.append(gateway)
            except Exception as e:
                logger.error(f"创建{args.type}网关失败: {e}")
                sys.exit(1)

    if not gateways:
        logger.error("没有可用的网关配置")
        sys.exit(1)

    # 启动所有网关，并按**真实结果**统计（round 293）
    started, failed = start_gateways(gateways)
    logger.info("网关启动结果：成功 %d / 共 %d", len(started), len(gateways))
    if failed:
        # 一个都没起来 → 退出码 1：让 systemd / 服务管理器 / 容器编排
        # 看见"这不是在运行、而是启动失败"，而不是对着一个空转进程干等。
        logger.error("以下网关启动失败: %s", failed)
        if not started:
            logger.error("没有任何网关启动成功，退出（退出码 1）")
            sys.exit(1)

    # 信号处理
    def signal_handler(signum, frame):
        logger.info("接收到停止信号，正在关闭...")
        for gateway in gateways:
            gateway.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 保持运行
    logger.info("网关服务已启动，按 Ctrl+C 停止")

    try:
        while True:
            import time
            time.sleep(1)

            # 打印统计信息
            for gateway in gateways:
                stats = gateway.get_stats()
                logger.debug(f"网关 {gateway.gateway_id}: {stats}")

    except KeyboardInterrupt:
        logger.info("正在停止...")
    finally:
        for gateway in gateways:
            gateway.stop()
        logger.info("所有网关已停止")


if __name__ == '__main__':
    main()
