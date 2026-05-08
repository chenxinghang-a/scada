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

import sys
import argparse
import logging
import signal
import yaml
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from gateway import ModbusGateway


def setup_logging(log_level: str = "INFO"):
    """配置日志"""
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('logs/gateway.log', encoding='utf-8')
        ]
    )


def load_config(config_path: str) -> dict:
    """加载配置文件"""
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def create_gateway(gateway_type: str, config: dict):
    """根据类型创建网关实例"""
    gateway_map = {
        'modbus': ModbusGateway,
        # TODO: 添加其他协议网关
        # 's7': S7Gateway,
        # 'opcua': OPCUAGateway,
        # 'mqtt': MQTTGateway,
    }
    
    gateway_class = gateway_map.get(gateway_type)
    if not gateway_class:
        raise ValueError(f"不支持的网关类型: {gateway_type}")
    
    return gateway_class(config)


def main():
    parser = argparse.ArgumentParser(description='工业SCADA协议网关服务')
    parser.add_argument('--type', type=str, default='modbus',
                        choices=['modbus', 's7', 'opcua', 'mqtt'],
                        help='网关类型')
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
        # 启动所有网关
        for gw_config in config.get('gateways', []):
            gw_type = gw_config.get('type')
            if gw_type:
                try:
                    gateway = create_gateway(gw_type, gw_config)
                    gateways.append(gateway)
                except Exception as e:
                    logger.error(f"创建{gw_type}网关失败: {e}")
    else:
        # 启动指定类型的网关
        gw_config = config.get('gateways', [{}])[0] if config.get('gateways') else config
        gw_config['type'] = args.type
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
