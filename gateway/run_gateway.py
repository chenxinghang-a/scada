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

    # 启动所有网关
    logger.info(f"启动 {len(gateways)} 个网关...")

    for gateway in gateways:
        try:
            gateway.start()
        except Exception as e:
            logger.error(f"启动网关失败: {e}")

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
