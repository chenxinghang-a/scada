"""
用LoomLLM调Gemini写毕设论文
"""
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

import requests
import json
import os

# Gemini配置
API_KEY = "AIzaSyC0MMf6fQkQsG5kMj9mDeGSKn0i2imhfik"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta/chat/completions"
PROXY = "http://127.0.0.1:7890"

def ask_gemini(prompt, max_tokens=8000):
    """调用Gemini API (OpenAI兼容格式)"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}"
    }
    data = {
        "model": "gemini-2.5-flash-lite",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7
    }
    
    try:
        r = requests.post(
            BASE_URL,
            headers=headers,
            json=data,
            proxies={"http": PROXY, "https": PROXY},
            timeout=120
        )
        result = r.json()
        if "choices" in result:
            return result["choices"][0]["message"]["content"]
        else:
            return f"Error: {json.dumps(result, ensure_ascii=False)}"
    except Exception as e:
        return f"Error: {e}"

# 系统信息
SYSTEM_INFO = """
系统名称：基于Python的工业数据采集与监控系统
技术栈：Python 3.12 + Flask + SQLite + pymodbus + ECharts + Bootstrap 5
架构：四层架构（采集层/存储层/服务层/展示层）

目录结构：
industrial_scada/
├── 采集层/          # Modbus通信、设备管理、数据采集
│   ├── data_collector.py      # 数据采集器
│   ├── device_manager.py      # 设备管理器
│   ├── modbus_client.py       # Modbus客户端
│   └── simulated_client.py    # 模拟客户端
├── 存储层/          # SQLite数据库
│   ├── database.py            # 数据库操作
│   └── data_export.py         # 数据导出
├── 展示层/          # Flask Web
│   ├── api.py                 # RESTful API
│   ├── routes.py              # 页面路由
│   └── websocket.py           # WebSocket
├── 报警层/          # 报警管理
│   └── alarm_manager.py       # 报警引擎
├── 配置/            # YAML配置
│   ├── devices.yaml           # 设备配置
│   └── alarms.yaml            # 报警规则
├── 模板/            # HTML模板
├── 静态资源/        # JS/CSS
├── 测试/            # 测试脚本
└── 文档/            # 项目文档

设备配置：
- 温度传感器: temp_sensor_01, 192.168.1.100:502, 寄存器: temperature/humidity/status
- 压力传感器: pressure_sensor_01, 192.168.1.101:502, 寄存器: pressure/temperature
- 电力仪表: power_meter_01, 192.168.1.102:502, 寄存器: voltage/current/power/energy

测试结果（45/45全部通过）：
- API平均响应: 7-36ms
- P95响应时间: 25-50ms
- 稳定性成功率: 100%
- 报警触发: 20条（11严重+9警告）
- 数据导出: 正常
"""

# 写论文各章节
chapters = [
    {
        "file": "论文/第1章_绪论.md",
        "prompt": f"""你是电气工程及其自动化专业的毕业论文作者。请撰写论文第一章「绪论」，约4000字。

{SYSTEM_INFO}

要求：
1. 研究背景与意义（1500字）：工业自动化发展趋势、SCADA系统重要性、传统SCADA局限性、Python开发优势
2. 国内外研究现状（1500字）：国外SCADA发展、国内SCADA发展、开源SCADA系统（PyScada等）
3. 研究内容与目标（500字）：功能需求、性能需求、研究目标
4. 论文组织结构（500字）：各章节概述

格式要求：
- 学术论文风格，语言严谨
- 使用中文标点
- 段落之间空行
- 标题用 # ## ### 格式
- 不要有虚构的数据，但可以有合理的论述
"""
    },
    {
        "file": "论文/第2章_相关技术.md",
        "prompt": f"""你是电气工程及其自动化专业的毕业论文作者。请撰写论文第二章「相关技术与理论基础」，约5000字。

{SYSTEM_INFO}

要求：
1. Modbus通信协议（2000字）：协议概述、通信模型（主从）、数据模型（4种）、功能码详解、RTU帧格式与CRC校验、TCP帧格式
2. SCADA系统架构（1000字）：定义与功能、层次结构、关键技术
3. Python Web技术（1000字）：Flask框架、RESTful API、WebSocket
4. 数据可视化技术（500字）：ECharts、Bootstrap
5. 数据库技术（500字）：SQLite特点、时序数据存储

格式要求：
- 学术论文风格
- 包含表格（如功能码表、数据类型表）
- 包含代码示例（Python Modbus通信）
- 标题用 # ## ### 格式
"""
    },
    {
        "file": "论文/第3章_总体设计.md",
        "prompt": f"""你是电气工程及其自动化专业的毕业论文作者。请撰写论文第三章「系统总体设计」，约5000字。

{SYSTEM_INFO}

要求：
1. 系统需求分析（1000字）：功能需求、性能需求、安全需求
2. 系统架构设计（1500字）：四层架构详解、各层职责
3. 硬件选型（1000字）：传感器（PT100/扩散硅/APM810）、PLC（S7-1200）、通信设备
4. 数据库设计（1000字）：实时数据表、历史数据表、报警记录表（含字段说明表格）
5. 通信协议设计（500字）：Modbus参数配置、数据帧格式

格式要求：
- 学术论文风格
- 包含架构图描述（用文字描述，后续会画图）
- 包含数据库表结构表格
- 标题用 # ## ### 格式
"""
    },
    {
        "file": "论文/第4章_详细实现.md",
        "prompt": f"""你是电气工程及其自动化专业的毕业论文作者。请撰写论文第四章「系统详细设计与实现」，约8000字。

{SYSTEM_INFO}

要求：
1. 采集层实现（2000字）：Modbus客户端（pymodbus）、设备管理器、数据采集器、模拟数据生成器。包含关键代码片段。
2. 存储层实现（1500字）：数据库初始化、数据存储接口、数据查询接口、数据导出。包含SQL语句示例。
3. 服务层实现（2000字）：Flask应用、RESTful API设计（列出所有端点）、WebSocket推送、报警引擎。包含API表格。
4. 展示层实现（1500字）：仪表盘、设备管理、报警管理、历史数据页面。包含页面功能描述。
5. 关键算法实现（1000字）：数据类型转换、缩放系数、报警阈值判断、延迟确认机制。包含算法伪代码。

格式要求：
- 学术论文风格
- 包含大量代码片段（Python）
- 包含API端点表格
- 标题用 # ## ### 格式
"""
    },
    {
        "file": "论文/第5章_测试分析.md",
        "prompt": f"""你是电气工程及其自动化专业的毕业论文作者。请撰写论文第五章「系统测试与实验分析」，约5000字。

{SYSTEM_INFO}

测试数据：
- API端点测试：17个端点全部通过
- 设备操作测试：5项全部通过
- 数据查询测试：4项全部通过
- 报警操作测试：4项全部通过
- 导出功能测试：2项全部通过
- Web页面测试：5个页面全部通过
- 边界情况测试：7项全部通过
- 并发请求测试：20并发全部通过
- 总计：45/45，通过率100%

响应时间数据：
- 设备列表: 平均9.76ms, P50=12.56ms, P95=25.66ms
- 实时数据: 平均36.36ms, P50=30.86ms, P95=50.65ms
- 最新数据: 平均7.19ms, P50=3.27ms, P95=24.20ms
- 报警列表: 平均11.94ms, P50=14.78ms, P95=27.05ms
- 系统状态: 平均17.22ms, P50=10.11ms, P95=32.06ms

稳定性测试：30秒，29次请求，成功率100%
报警测试：20条报警，11严重+9警告

要求：
1. 测试环境（500字）：硬件环境、软件环境、测试工具
2. 功能测试（1500字）：设备管理、数据采集、报警管理、数据导出
3. 性能测试（1500字）：采集精度、响应时间（含表格）、稳定性
4. 测试结果分析（1500字）：数据汇总表格、性能指标分析、问题与改进

格式要求：
- 学术论文风格
- 包含测试数据表格
- 包含性能对比分析
- 标题用 # ## ### 格式
"""
    },
    {
        "file": "论文/第6章_总结展望.md",
        "prompt": f"""你是电气工程及其自动化专业的毕业论文作者。请撰写论文第六章「总结与展望」，约2000字。

{SYSTEM_INFO}

要求：
1. 工作总结（800字）：系统设计与实现的主要工作、取得的成果
2. 创新点（400字）：基于Python的轻量级SCADA方案、分层架构、模拟/真实双模式
3. 不足与展望（800字）：当前局限性、未来改进方向（OPC UA/MQTT、用户权限、预测性维护、云平台）

格式要求：
- 学术论文风格
- 标题用 # ## ### 格式
"""
    }
]

# 执行
os.makedirs("论文", exist_ok=True)

for i, ch in enumerate(chapters):
    print(f"\n{'='*60}")
    print(f"正在写 {ch['file']} ...")
    print(f"{'='*60}")
    
    content = ask_gemini(ch["prompt"], max_tokens=8000)
    
    with open(ch["file"], "w", encoding="utf-8") as f:
        f.write(content)
    
    print(f"已保存: {ch['file']}")
    print(f"字数: {len(content)}")

print("\n全部章节写完！")
