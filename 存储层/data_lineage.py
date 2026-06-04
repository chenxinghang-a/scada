"""
数据血缘追踪模块
追踪数据从采集到存储到展示的完整流转路径

功能：
- 数据源追踪
- 转换记录
- 影响分析
- 血缘报告
"""

import time
import logging
import threading
import json
from datetime import datetime
from typing import Dict, List, Any, Optional, Set
from collections import defaultdict

logger = logging.getLogger(__name__)


class LineageNode:
    """血缘节点"""

    def __init__(self, node_id: str, node_type: str, name: str, metadata: Dict[str, Any] = None):
        self.node_id = node_id
        self.node_type = node_type  # source, transform, storage, display
        self.name = name
        self.metadata = metadata or {}
        self.created_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            'node_id': self.node_id,
            'node_type': self.node_type,
            'name': self.name,
            'metadata': self.metadata,
            'created_at': self.created_at,
        }


class LineageEdge:
    """血缘边（数据流转关系）"""

    def __init__(self, source_id: str, target_id: str, transform_type: str = 'direct',
                 metadata: Dict[str, Any] = None):
        self.source_id = source_id
        self.target_id = target_id
        self.transform_type = transform_type  # direct, aggregate, filter, convert
        self.metadata = metadata or {}
        self.created_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            'source_id': self.source_id,
            'target_id': self.target_id,
            'transform_type': self.transform_type,
            'metadata': self.metadata,
            'created_at': self.created_at,
        }


class DataLineageTracker:
    """数据血缘追踪器"""

    def __init__(self):
        self.nodes: Dict[str, LineageNode] = {}
        self.edges: List[LineageEdge] = []
        self._lock = threading.Lock()

        # 初始化核心节点
        self._init_core_nodes()

    def _init_core_nodes(self):
        """初始化核心节点"""
        # 数据源节点
        self.add_node(LineageNode('modbus_source', 'source', 'Modbus设备'))
        self.add_node(LineageNode('opcua_source', 'source', 'OPC UA设备'))
        self.add_node(LineageNode('mqtt_source', 'source', 'MQTT设备'))

        # 存储节点
        self.add_node(LineageNode('realtime_table', 'storage', '实时数据表'))
        self.add_node(LineageNode('history_table', 'storage', '历史数据表'))
        self.add_node(LineageNode('alarm_table', 'storage', '报警记录表'))
        self.add_node(LineageNode('archive_table', 'storage', '归档数据表'))

        # 展示节点
        self.add_node(LineageNode('dashboard', 'display', '仪表盘'))
        self.add_node(LineageNode('history_view', 'display', '历史数据页'))
        self.add_node(LineageNode('alarm_view', 'display', '报警管理页'))

    def add_node(self, node: LineageNode):
        """添加节点"""
        with self._lock:
            self.nodes[node.node_id] = node

    def add_edge(self, edge: LineageEdge):
        """添加边"""
        with self._lock:
            # 验证节点存在
            if edge.source_id not in self.nodes:
                logger.warning(f"源节点不存在: {edge.source_id}")
                return
            if edge.target_id not in self.nodes:
                logger.warning(f"目标节点不存在: {edge.target_id}")
                return

            self.edges.append(edge)

    def track_data_flow(self, device_id: str, register_name: str,
                       source_type: str, target_table: str):
        """追踪数据流转"""
        # 创建设备特定节点
        source_node_id = f"{source_type}:{device_id}"
        self.add_node(LineageNode(source_node_id, 'source', f"{device_id}"))

        # 创建边
        self.add_edge(LineageEdge(
            source_id=source_node_id,
            target_id=target_table,
            transform_type='collect',
            metadata={
                'device_id': device_id,
                'register_name': register_name,
            }
        ))

    def get_upstream(self, node_id: str, depth: int = 3) -> List[str]:
        """获取上游节点"""
        visited = set()
        result = []

        def dfs(current_id: str, current_depth: int):
            if current_depth > depth or current_id in visited:
                return
            visited.add(current_id)
            result.append(current_id)

            with self._lock:
                for edge in self.edges:
                    if edge.target_id == current_id:
                        dfs(edge.source_id, current_depth + 1)

        dfs(node_id, 0)
        return result

    def get_downstream(self, node_id: str, depth: int = 3) -> List[str]:
        """获取下游节点"""
        visited = set()
        result = []

        def dfs(current_id: str, current_depth: int):
            if current_depth > depth or current_id in visited:
                return
            visited.add(current_id)
            result.append(current_id)

            with self._lock:
                for edge in self.edges:
                    if edge.source_id == current_id:
                        dfs(edge.target_id, current_depth + 1)

        dfs(node_id, 0)
        return result

    def get_impact_analysis(self, node_id: str) -> Dict[str, Any]:
        """影响分析：如果节点故障，会影响哪些下游节点"""
        downstream = self.get_downstream(node_id)

        impact = {
            'source': node_id,
            'affected_nodes': [],
            'affected_count': len(downstream) - 1,  # 排除自身
        }

        for nid in downstream:
            if nid != node_id:
                node = self.nodes.get(nid)
                if node:
                    impact['affected_nodes'].append({
                        'node_id': nid,
                        'type': node.node_type,
                        'name': node.name,
                    })

        return impact

    def get_lineage_graph(self) -> Dict[str, Any]:
        """获取血缘图"""
        with self._lock:
            return {
                'nodes': [n.to_dict() for n in self.nodes.values()],
                'edges': [e.to_dict() for e in self.edges],
                'node_count': len(self.nodes),
                'edge_count': len(self.edges),
            }

    def generate_report(self) -> Dict[str, Any]:
        """生成血缘报告"""
        with self._lock:
            # 统计节点类型
            type_counts = defaultdict(int)
            for node in self.nodes.values():
                type_counts[node.node_type] += 1

            # 统计边类型
            edge_type_counts = defaultdict(int)
            for edge in self.edges:
                edge_type_counts[edge.transform_type] += 1

            # 找出孤立节点
            connected_nodes = set()
            for edge in self.edges:
                connected_nodes.add(edge.source_id)
                connected_nodes.add(edge.target_id)

            isolated_nodes = [
                nid for nid in self.nodes.keys()
                if nid not in connected_nodes
            ]

            return {
                'timestamp': datetime.now().isoformat(),
                'summary': {
                    'total_nodes': len(self.nodes),
                    'total_edges': len(self.edges),
                    'node_types': dict(type_counts),
                    'edge_types': dict(edge_type_counts),
                    'isolated_nodes': len(isolated_nodes),
                },
                'isolated_nodes': isolated_nodes,
            }

    def export_json(self, filepath: str):
        """导出血缘图到JSON"""
        graph = self.get_lineage_graph()

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(graph, f, indent=2, ensure_ascii=False, default=str)

        logger.info(f"血缘图已导出: {filepath}")
