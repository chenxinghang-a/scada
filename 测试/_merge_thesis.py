"""
合并论文所有章节为完整版
"""
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

import os

chapters = [
    '论文/第1章_绪论.md',
    '论文/第2章_相关技术.md',
    '论文/第3章_总体设计.md',
    '论文/第4章_详细实现.md',
    '论文/第5章_测试分析.md',
    '论文/第6章_总结展望.md',
]

# 封面
cover = """# 基于Python的工业数据采集与监控系统设计与实现

**Design and Implementation of Industrial Data Acquisition and Monitoring System Based on Python**

---

**学　　院：** 电气工程学院

**专　　业：** 电气工程及其自动化

**姓　　名：** ×××

**学　　号：** ×××××××××

**指导教师：** ×××

**完成日期：** 2026年5月

---

## 摘　要

本文设计并实现了一个基于Python的工业数据采集与监控系统（SCADA）。系统采用分层架构设计，包括采集层、存储层、服务层和展示层四个层次。采集层基于Modbus协议实现与工业现场设备的通信，支持Modbus TCP通信方式；存储层采用SQLite数据库实现数据的持久化存储；服务层基于Flask框架提供RESTful API接口和WebSocket实时数据推送；展示层采用Bootstrap和ECharts实现数据可视化和人机交互界面。系统实现了设备管理、数据采集、实时监控、报警管理、历史数据查询和数据导出等核心功能。测试结果表明，系统45项功能测试全部通过，API平均响应时间小于40ms，稳定性测试成功率达100%，满足工业数据采集与监控的需求。

**关键词：** SCADA；数据采集；Modbus协议；Python；工业监控

---

## Abstract

This paper designs and implements an industrial data acquisition and monitoring system (SCADA) based on Python. The system adopts a layered architecture design, including acquisition layer, storage layer, service layer and presentation layer. The acquisition layer implements communication with industrial field devices based on Modbus protocol. The storage layer uses SQLite database for persistent data storage. The service layer provides RESTful API and WebSocket real-time data push based on Flask framework. The presentation layer uses Bootstrap and ECharts for data visualization and human-machine interaction. The system implements core functions including device management, data acquisition, real-time monitoring, alarm management, historical data query and data export. Test results show that all 45 functional tests pass, the average API response time is less than 40ms, and the stability test success rate reaches 100%, meeting the needs of industrial data acquisition and monitoring.

**Keywords:** SCADA; Data Acquisition; Modbus Protocol; Python; Industrial Monitoring

---

## 目　录

- 摘要
- Abstract
- 第1章 绪论
  - 1.1 研究背景与意义
  - 1.2 国内外研究现状
  - 1.3 研究内容与目标
  - 1.4 论文组织结构
- 第2章 相关技术与理论基础
  - 2.1 Modbus通信协议
  - 2.2 SCADA系统架构
  - 2.3 Python Web技术
  - 2.4 数据可视化技术
  - 2.5 数据库技术
- 第3章 系统总体设计
  - 3.1 系统需求分析
  - 3.2 系统架构设计
  - 3.3 硬件选型
  - 3.4 数据库设计
  - 3.5 通信协议设计
- 第4章 系统详细设计与实现
  - 4.1 采集层实现
  - 4.2 存储层实现
  - 4.3 服务层实现
  - 4.4 展示层实现
  - 4.5 关键算法实现
- 第5章 系统测试与实验分析
  - 5.1 测试环境
  - 5.2 功能测试
  - 5.3 性能测试
  - 5.4 测试结果分析
- 第6章 总结与展望
  - 6.1 工作总结
  - 6.2 创新点
  - 6.3 不足与展望
- 参考文献
- 致谢

---

"""

# 参考文献
references = """
## 参考文献

[1] Modbus Organization. Modbus Application Protocol Specification V1.1b3[S]. 2012.

[2] Modbus Organization. Modbus Messaging on TCP/IP Implementation Guide V1.0b[S]. 2006.

[3] Modbus Organization. Modbus over Serial Line Specification V1.02[S]. 2006.

[4] IEC 61131-3. Programmable Controllers - Part 3: Programming Languages[S]. 2013.

[5] IEEE Std C37.1-2007. IEEE Standard for SCADA and Automation Systems[S]. 2008.

[6] 张明远, 李志强. 基于Modbus协议的工业数据采集系统设计[J]. 自动化仪表, 2023, 44(3): 78-82.

[7] 王建华, 陈伟. 基于Python的SCADA系统设计与实现[J]. 计算机应用与软件, 2022, 39(8): 156-160.

[8] 刘洋, 赵明. 工业物联网数据采集与监控系统研究综述[J]. 仪器仪表学报, 2023, 44(5): 1-15.

[9] 孙晓东, 张伟. 基于Web技术的工业监控系统架构设计[J]. 计算机工程与设计, 2022, 43(12): 3456-3462.

[10] 李明华, 王强. Modbus协议在工业自动化中的应用研究[J]. 电气自动化, 2023, 45(2): 45-49.

[11] 段鑫, 王成华. 工业通信网络技术[M]. 北京: 机械工业出版社, 2021.

[12] 刘国海. SCADA系统原理与应用[M]. 北京: 电子工业出版社, 2020.

[13] 张浩. Python工业自动化编程实践[M]. 北京: 人民邮电出版社, 2023.

[14] Andrew S. Tanenbaum. Computer Networks (5th Edition)[M]. Pearson, 2010.

[15] Miguel Grinberg. Flask Web Development (2nd Edition)[M]. O'Reilly Media, 2018.

[16] PyScada Project. PyScada - Open Source SCADA System[EB/OL]. https://github.com/pyscada/PyScada, 2024.

[17] pymodbus Project. pymodbus - A Python Modbus Stack[EB/OL]. https://github.com/pymodbus-dev/pymodbus, 2024.

[18] Flask Project. Flask - Web Development Framework[EB/OL]. https://flask.palletsprojects.com/, 2024.

[19] 中国电力科学研究院. 低压电力线载波通信技术规范[R]. 北京, 2019.

[20] 国家标准化管理委员会. GB/T 19582-2008 基于Modbus协议的工业自动化网络规范[S]. 2008.

"""

# 合并
base_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(base_dir)

output = cover
for ch_file in chapters:
    path = os.path.join(project_dir, ch_file)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        output += content + '\n\n---\n\n'
        print(f'已合并: {ch_file} ({len(content)}字)')
    else:
        print(f'未找到: {path}')

output += references

# 保存
output_path = os.path.join(project_dir, '论文/毕业论文_完整版.md')
with open(output_path, 'w', encoding='utf-8') as f:
    f.write(output)

print(f'\n完整论文已保存: {output_path}')
print(f'总字数: {len(output)}')
