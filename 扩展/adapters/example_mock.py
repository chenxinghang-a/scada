# -*- coding: utf-8 -*-
"""示例插件：不连任何外部系统，产生正弦温度 + 运行状态，用于离线开发。

复制这个文件改成自己的实现即可；改完调用
`POST /api/devices/adapters/reload` 热加载，或重启进程。
"""

import math
import time

PLUGIN_INFO = {
    'version': '1.0',
    'description': '示例：正弦温度 + 运行状态（离线开发用）',
    'author': 'SCADA 项目组',
}


class Adapter:
    """最小插件实现：read() 返回点位字典，write_point() 可选。"""

    def __init__(self, settings: dict):
        # settings 来自配置里的 adapter.settings，已展开 ${ENV}
        self.base = float(settings.get('base_temp', 50.0))
        self.amplitude = float(settings.get('amplitude', 5.0))
        self._started_at = time.time()
        self._setpoint = None

    def read(self) -> dict:
        elapsed = time.time() - self._started_at
        return {
            'temperature': {
                'value': self.base + self.amplitude * math.sin(elapsed / 10.0),
                'unit': '°C',
            },
            'run_state': 'RUNNING',
            'setpoint': self._setpoint if self._setpoint is not None else self.base,
        }

    def write_point(self, name: str, value) -> bool:
        if name != 'setpoint':
            return False
        self._setpoint = float(value)
        return True

    def stop(self):
        """可选：释放连接/文件句柄。"""
