#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按「厂区布局 + 设备模板库」生成设备清单与拓扑骨架。

## 为什么需要这个脚本

手写 42 台设备还行，写到 128 台必然出错 —— 地址撞车、量纲写错、单位缺失、
模拟参数漏配，而且每加一台要同步改两个文件（设备清单 + 模拟参数档）。

所以拆成三层：

    配置/device_templates.yaml   设备族 → 点位结构（电流/温度/振动这一套）
    tools/gen_factory.py         厂区布局 → 哪台设备、在哪个工序（本文件）
    配置/devices_simulated.yaml  生成结果（现场可读可改，是运行时的唯一真源）

**生成是一次性的**，不是每次启动都跑。改完布局重新生成即可；
现场直接改生成的 YAML 也没问题（下次生成会覆盖，所以改布局要改这里）。

## 保留策略

已有设备（`KEEP_AS_IS` 里的 id）**原样保留** —— 它们的点位和模拟参数是逐台
调过的（量纲、耦合链、物理上限），不能被模板覆盖。生成器只给它们补
`site/workshop/line/stage` 这些层级字段。

## 差异化

同型号设备如果参数完全一样，图上就是一排完全相同的数字 —— 一眼假。
所以每台设备的 `base_*` 按 id 的稳定哈希做 ±6% 偏移（模拟设备老化/安装差异），
同一条产线上两台泵的电流不会一模一样。

用法：
    .venv/Scripts/python.exe tools/gen_factory.py            # 生成
    .venv/Scripts/python.exe tools/gen_factory.py --dry-run  # 只统计不写盘
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DEVICES_PATH = REPO / '配置' / 'devices_simulated.yaml'
PROFILES_PATH = REPO / '配置' / 'simulation_profiles.yaml'
TEMPLATES_PATH = REPO / '配置' / 'device_templates.yaml'
TOPOLOGY_PATH = REPO / '配置' / 'topology.yaml'

#: 模拟服务端的监听端口段。分段是为了排查时一眼看出是哪种协议。
PORT_BASE = {'modbus_tcp': 15020, 'modbus_rtu': 15020,
             'opcua': 15400, 'rest': 15500, 'mqtt': 18830}

#: 数据类型的寄存器宽度。地址自动分配靠它累加。
TYPE_WIDTH = {'uint16': 1, 'int16': 1, 'uint32': 2, 'int32': 2,
              'float32': 2, 'float64': 4, 'bool': 1}


# =============================================================================
# 厂区骨架：车间 → 产线 → 工序顺序
# =============================================================================
# `stages` 是这条产线的工序顺序，决定拓扑图从左到右的列。
# 设备通过 `stage` 挂到某一列；同一列多台设备纵向排列。
SITE = {
    'id': 'east_plant',
    'name': '华东制造基地',
    'code': 'EP-01',
}

WORKSHOPS = [
    {'id': 'injection', 'name': '注塑车间', 'icon': '🏭', 'order': 10,
     'lines': [
         {'id': 'injection_a', 'name': '注塑A线',
          'stages': ['上料', '干燥', '注塑', '取件', '粉碎']},
         {'id': 'injection_b', 'name': '注塑B线',
          'stages': ['上料', '干燥', '注塑', '取件']},
         {'id': 'mold_temp', 'name': '模温单元',
          'stages': ['供水', '回水']},
     ]},

    {'id': 'paint', 'name': '涂装车间', 'icon': '🎨', 'order': 20,
     'lines': [
         {'id': 'paint_a', 'name': '涂装A线',
          'stages': ['前处理', '喷涂', '流平', '烘干', '废气处理']},
         {'id': 'paint_b', 'name': '涂装B线',
          'stages': ['喷涂', '流平', '烘干']},
         {'id': 'paint_supply', 'name': '供漆单元',
          'stages': ['调漆', '供漆', '喷房送风']},
     ]},

    {'id': 'packaging', 'name': '包装车间', 'icon': '📦', 'order': 30,
     'lines': [
         {'id': 'pack_a', 'name': '包装A线',
          'stages': ['灌装', '旋盖', '贴标', '装箱', '码垛']},
         {'id': 'pack_b', 'name': '包装B线',
          'stages': ['输送', '称重', '金检', '装箱']},
     ]},

    {'id': 'assembly', 'name': '装配车间', 'icon': '🔧', 'order': 40,
     'lines': [
         {'id': 'assembly_a', 'name': '装配A线',
          'stages': ['上料', '装配', '拧紧', '气密检测', '视觉检测', '打标']},
         {'id': 'welding', 'name': '焊接线',
          'stages': ['定位', '焊接', '打磨', '检验']},
     ]},

    {'id': 'machining', 'name': '机加车间', 'icon': '⚙️', 'order': 50,
     'lines': [
         {'id': 'cnc_a', 'name': '机加A线',
          'stages': ['粗加工', '精加工', '磨削', '清洗']},
         {'id': 'cnc_b', 'name': '机加B线',
          'stages': ['加工', '清洗', '检测']},
     ]},

    {'id': 'chemical', 'name': '化工车间', 'icon': '⚗️', 'order': 60,
     'lines': [
         {'id': 'reaction_a', 'name': '反应A线',
          'stages': ['进料', '反应', '精馏', '储运']},
         {'id': 'reaction_b', 'name': '反应B线',
          'stages': ['进料', '反应', '换热', '尾气']},
         {'id': 'chemical_meter', 'name': '反应A线计量',
          'stages': ['测温', '测压', '流量']},
     ]},

    {'id': 'power', 'name': '动力站', 'icon': '⚡', 'order': 70,
     'lines': [
         {'id': 'compressed_air', 'name': '压缩空气',
          'stages': ['空压', '干燥', '储气', '用气']},
         {'id': 'chilled_water', 'name': '冷冻水',
          'stages': ['制冷', '循环', '散热']},
         {'id': 'steam', 'name': '蒸汽',
          'stages': ['软化', '给水', '锅炉']},
         {'id': 'aux', 'name': '辅助',
          'stages': ['除尘', '制氮']},
     ]},

    {'id': 'electrical', 'name': '配电房', 'icon': '🔌', 'order': 80,
     'lines': [
         {'id': 'hv', 'name': '高压配电', 'stages': ['进线', '变压']},
         {'id': 'lv', 'name': '低压配电',
          'stages': ['进线', '馈出', '计量']},
         {'id': 'backup', 'name': '后备与补偿',
          'stages': ['后备', '补偿', '控制']},
     ]},

    {'id': 'utility', 'name': '公用工程', 'icon': '🏢', 'order': 90,
     'lines': [
         {'id': 'hvac', 'name': '暖通',
          'stages': ['空调', '送风', '排风']},
         {'id': 'water', 'name': '给排水',
          'stages': ['储水', '输水', '换热', '排放']},
         {'id': 'vibration_monitor', 'name': '旋转设备监测',
          'stages': ['监测']},
     ]},

    {'id': 'logistics', 'name': '仓储物流', 'icon': '🚚', 'order': 100,
     'lines': [
         {'id': 'asrs', 'name': '立体库',
          'stages': ['入库', '堆垛', '出库']},
         {'id': 'agv', 'name': 'AGV',
          'stages': ['调度', '充电']},
         {'id': 'yard', 'name': '厂区物流',
          'stages': ['吊运', '称重', '装卸']},
     ]},

    {'id': 'qc', 'name': '质检中心', 'icon': '🔬', 'order': 110,
     'lines': [
         {'id': 'env_monitor', 'name': '环保监测', 'stages': ['烟气']},
         {'id': 'material_lab', 'name': '材料实验室',
          'stages': ['成分', '力学', '表面', '水分']},
     ]},

    {'id': 'safety', 'name': '安全系统', 'icon': '🔥', 'order': 120,
     'lines': [
         {'id': 'fire', 'name': '消防',
          'stages': ['探测', '报警', '灭火', '疏散']},
     ]},

    {'id': 'energy', 'name': '能源站', 'icon': '🌱', 'order': 130,
     'lines': [
         {'id': 'solar', 'name': '光伏', 'stages': ['发电', '并网']},
         {'id': 'storage', 'name': '储能与风电',
          'stages': ['储能', '风电']},
     ]},

    {'id': 'it', 'name': '信息中心', 'icon': '💻', 'order': 140,
     'lines': [
         {'id': 'integration', 'name': '数据集成',
          'stages': ['采集', '平台', '上层']},
     ]},
]


# =============================================================================
# 已有设备：原样保留，只补层级字段
# =============================================================================
# (device_id, workshop, line, stage)
KEEP_AS_IS = [
    # 注塑
    ('siemens_1500_01', 'injection', 'injection_a', '注塑'),
    ('mitsubishi_fx5u_01', 'injection', 'injection_b', '注塑'),
    # 涂装
    ('inovance_h5u_01', 'paint', 'paint_a', '喷涂'),
    # 包装
    ('delta_dvp_01', 'packaging', 'pack_b', '装箱'),
    ('conveyor_01', 'packaging', 'pack_b', '输送'),
    # 装配
    ('robot_fanuc_01', 'assembly', 'assembly_a', '装配'),
    ('signal_tower_01', 'assembly', 'assembly_a', '打标'),
    ('turck_iolink_01', 'assembly', 'assembly_a', '装配'),
    ('welder_01', 'assembly', 'welding', '焊接'),
    # 机加
    ('cnc_mazak_01', 'machining', 'cnc_a', '精加工'),
    # 化工
    ('hollysys_lk_01', 'chemical', 'reaction_a', '反应'),
    ('dcs_supcon_01', 'chemical', 'reaction_a', '反应'),
    ('temp_xmitter_01', 'chemical', 'chemical_meter', '测温'),
    ('pressure_xmitter_01', 'chemical', 'chemical_meter', '测压'),
    ('flowmeter_01', 'chemical', 'chemical_meter', '流量'),
    # 动力站
    ('compressor_01', 'power', 'compressed_air', '空压'),
    ('air_header_01', 'power', 'compressed_air', '储气'),
    ('chiller_01', 'power', 'chilled_water', '制冷'),
    ('pump_vfd_01', 'power', 'chilled_water', '循环'),
    ('cooling_tower_01', 'power', 'chilled_water', '散热'),
    ('boiler_feed_pump_01', 'power', 'steam', '给水'),
    ('dust_fan_01', 'power', 'aux', '除尘'),
    # 配电
    ('transformer_01', 'electrical', 'hv', '变压'),
    ('schneider_m340_01', 'electrical', 'lv', '馈出'),
    ('power_meter_sub_01', 'electrical', 'lv', '计量'),
    ('abb_m4m_01', 'electrical', 'lv', '计量'),
    ('ups_apc_01', 'electrical', 'backup', '后备'),
    ('soft_starter_01', 'electrical', 'backup', '控制'),
    ('relay_output_01', 'electrical', 'backup', '控制'),
    # 公用
    ('hvac_ahu_01', 'utility', 'hvac', '空调'),
    ('water_quality_01', 'utility', 'water', '排放'),
    ('vibration_sensor_01', 'utility', 'vibration_monitor', '监测'),
    # 物流
    ('warehouse_plc_01', 'logistics', 'asrs', '入库'),
    ('agv_system_01', 'logistics', 'agv', '调度'),
    ('crane_01', 'logistics', 'yard', '吊运'),
    # 质检
    ('cems_01', 'qc', 'env_monitor', '烟气'),
    # 安全
    ('fire_alarm_01', 'safety', 'fire', '报警'),
    # 能源
    ('pv_inverter_01', 'energy', 'solar', '发电'),
    ('bess_bms_01', 'energy', 'storage', '储能'),
    # 信息
    ('opcua_plc_01', 'it', 'integration', '采集'),
    ('siemens_web_01', 'it', 'integration', '采集'),
    ('mes_api_01', 'it', 'integration', '平台'),
]


# =============================================================================
# 新增设备
# =============================================================================
# 字段：id / name / template / vendor / model / workshop / line / stage
#      （可选）description —— 覆盖模板的说明
#      （可选）protocol —— 覆盖模板协议
NEW_DEVICES = [
    # ---------------------------------------------------------------- 注塑
    dict(id='dryer_01', name='塑料干燥机1# (信易SHD-200)', template='dryer',
         vendor='信易', model='SHD-200', workshop='injection',
         line='injection_a', stage='干燥'),
    dict(id='injection_m1', name='注塑机1# (海天MA2100)', template='injection_machine',
         vendor='海天', model='MA2100II', workshop='injection',
         line='injection_a', stage='注塑'),
    dict(id='robot_takeout_01', name='取件机械手1# (威猛W818)', template='robot',
         vendor='威猛', model='W818', workshop='injection',
         line='injection_a', stage='取件'),
    dict(id='granulator_01', name='机边粉碎机1# (信易SG-2340)', template='rotating_machine',
         vendor='信易', model='SG-2340', workshop='injection',
         line='injection_a', stage='粉碎',
         description='机边粉碎机 — 水口料即时回收'),
    dict(id='dryer_02', name='塑料干燥机2# (信易SHD-150)', template='dryer',
         vendor='信易', model='SHD-150', workshop='injection',
         line='injection_b', stage='干燥'),
    dict(id='injection_m2', name='注塑机2# (海天MA1600)', template='injection_machine',
         vendor='海天', model='MA1600III', workshop='injection',
         line='injection_b', stage='注塑'),
    dict(id='robot_takeout_02', name='取件机械手2# (威猛W808)', template='robot',
         vendor='威猛', model='W808', workshop='injection',
         line='injection_b', stage='取件'),
    dict(id='mold_temp_01', name='模温机1# (奥兰特OTM-90)', template='mold_temp_controller',
         vendor='奥兰特', model='OTM-90', workshop='injection',
         line='mold_temp', stage='供水'),
    dict(id='mold_temp_02', name='模温机2# (奥兰特OTM-120)', template='mold_temp_controller',
         vendor='奥兰特', model='OTM-120', workshop='injection',
         line='mold_temp', stage='回水'),

    # ---------------------------------------------------------------- 涂装
    dict(id='pretreatment_01', name='前处理线 (磷化+脱脂)', template='tank',
         vendor='中机', model='PT-4000', workshop='paint',
         line='paint_a', stage='前处理',
         description='前处理槽 — 脱脂/磷化/水洗'),
    dict(id='spray_robot_01', name='喷涂机器人1# (ABB IRB5500)', template='robot',
         vendor='ABB', model='IRB5500', workshop='paint',
         line='paint_a', stage='喷涂'),
    dict(id='curing_oven_01', name='烘干炉1# (三段式燃气)', template='thermal_machine',
         vendor='中机', model='CO-3Z', workshop='paint',
         line='paint_a', stage='烘干'),
    dict(id='voc_rto_01', name='蓄热焚烧炉RTO (30k风量)', template='thermal_machine',
         vendor='恩国', model='RTO-30K', workshop='paint',
         line='paint_a', stage='废气处理',
         description='RTO 蓄热焚烧 — 燃烧室/换热/排放'),
    dict(id='spray_robot_02', name='喷涂机器人2# (ABB IRB4600)', template='robot',
         vendor='ABB', model='IRB4600', workshop='paint',
         line='paint_b', stage='喷涂'),
    dict(id='curing_oven_02', name='烘干炉2# (两段式电加热)', template='thermal_machine',
         vendor='中机', model='CO-2E', workshop='paint',
         line='paint_b', stage='烘干'),
    dict(id='paint_supply_01', name='供漆系统 (双组分)', template='tank',
         vendor='岩田', model='2K-SUP', workshop='paint',
         line='paint_supply', stage='供漆'),
    dict(id='booth_ahu_01', name='喷房空调机组 (恒温恒湿)', template='heat_exchanger',
         vendor='盾安', model='AHU-PB', workshop='paint',
         line='paint_supply', stage='喷房送风'),

    # ---------------------------------------------------------------- 包装
    dict(id='filler_01', name='灌装机1# (克朗斯Modulfill)', template='packaging_machine',
         vendor='克朗斯', model='Modulfill HES', workshop='packaging',
         line='pack_a', stage='灌装'),
    dict(id='capper_01', name='旋盖机1# (克朗斯Modulcap)', template='packaging_machine',
         vendor='克朗斯', model='Modulcap 12', workshop='packaging',
         line='pack_a', stage='旋盖'),
    dict(id='labeler_01', name='贴标机1# (克朗斯Moduljet)', template='packaging_machine',
         vendor='克朗斯', model='Moduljet 16', workshop='packaging',
         line='pack_a', stage='贴标'),
    dict(id='case_packer_01', name='装箱机1# (克朗斯Variopac)', template='packaging_machine',
         vendor='克朗斯', model='Variopac Pro', workshop='packaging',
         line='pack_a', stage='装箱'),
    dict(id='palletizer_01', name='码垛机器人 (ABB IRB660)', template='robot',
         vendor='ABB', model='IRB660', workshop='packaging',
         line='pack_a', stage='码垛'),
    dict(id='checkweigher_01', name='在线称重机 (梅特勒C33)', template='meter',
         vendor='梅特勒-托利多', model='C33 PlusLine', workshop='packaging',
         line='pack_b', stage='称重',
         description='动态称重 — 重量/超差计数'),
    dict(id='metal_detector_01', name='金属检测机 (梅特勒Profile)', template='analyzer',
         vendor='梅特勒-托利多', model='Profile Advantage', workshop='packaging',
         line='pack_b', stage='金检',
         # 在线金检机是**流水线上的设备**（产品从它中间穿过），
         # 不是挂在旁边的旁路仪表 —— role 写错会让它从物料流里掉出去，
         # 图上就变成「输送 → 称重 →（断了）→ 装箱」。
         role='equipment',
         description='金属异物检测 — 灵敏度/剔除计数'),

    # 包装B线的「装箱」原来只有台达 PLC（控制器），没有装箱设备本体 ——
    # 图上最后一步是个 PLC，物料流到那里就断了。
    dict(id='case_packer_02', name='装箱机2# (永创YS-20)', template='packaging_machine',
         vendor='永创智能', model='YS-20', workshop='packaging',
         line='pack_b', stage='装箱'),

    # ---------------------------------------------------------------- 装配
    dict(id='assembly_line_01', name='装配线体1# (倍速链)', template='conveyor_line',
         vendor='伟创', model='ASL-2400', workshop='assembly',
         line='assembly_a', stage='上料'),
    dict(id='screw_driver_01', name='拧紧机1# (阿特拉斯Tensor)', template='robot',
         vendor='阿特拉斯·科普柯', model='Tensor ST', workshop='assembly',
         line='assembly_a', stage='拧紧'),
    dict(id='leak_tester_01', name='气密检测台 (ATEQ VT56)', template='analyzer',
         vendor='ATEQ', model='VT56', workshop='assembly',
         line='assembly_a', stage='气密检测'),
    dict(id='vision_inspect_01', name='视觉检测 (康耐视In-Sight)', template='analyzer',
         vendor='康耐视', model='In-Sight 3800', workshop='assembly',
         line='assembly_a', stage='视觉检测'),
    dict(id='laser_marker_01', name='激光打标机 (大族YLP-20)', template='machine_tool',
         vendor='大族激光', model='YLP-20', workshop='assembly',
         line='assembly_a', stage='打标'),
    dict(id='screw_driver_02', name='拧紧机2# (阿特拉斯Tensor)', template='robot',
         vendor='阿特拉斯·科普柯', model='Tensor ST', workshop='assembly',
         line='welding', stage='定位'),
    dict(id='torque_cal_01', name='扭矩校准仪 (诺霸TST)', template='analyzer',
         vendor='诺霸', model='TST-50', workshop='assembly',
         line='welding', stage='检验'),

    # ---------------------------------------------------------------- 机加
    dict(id='cnc_mazak_02', name='加工中心2# (马扎克VCN-530)', template='machine_tool',
         vendor='马扎克', model='VCN-530C', workshop='machining',
         line='cnc_a', stage='粗加工'),
    dict(id='lathe_01', name='数控车床 (沈阳i5T5)', template='machine_tool',
         vendor='沈阳机床', model='i5T5', workshop='machining',
         line='cnc_a', stage='粗加工'),
    dict(id='grinder_01', name='数控磨床 (Mägerle MFP-50)', template='machine_tool',
         vendor='Mägerle', model='MFP-50', workshop='machining',
         line='cnc_a', stage='磨削'),
    dict(id='cnc_mazak_03', name='加工中心3# (马扎克QT-200)', template='machine_tool',
         vendor='马扎克', model='QT-200', workshop='machining',
         line='cnc_b', stage='加工'),
    dict(id='wash_machine_01', name='清洗机 (德科DEC-800)', template='rotating_machine',
         vendor='德科', model='DEC-800', workshop='machining',
         line='cnc_b', stage='清洗',
         description='零件清洗机 — 泵/加热/过滤'),
    dict(id='cmm_01', name='三坐标测量机 (蔡司CONTURA)', template='analyzer',
         vendor='蔡司', model='CONTURA 7/10/6', workshop='machining',
         line='cnc_b', stage='检测'),

    # ---------------------------------------------------------------- 化工
    dict(id='reactor_01', name='反应釜1# (5000L 搪玻璃)', template='reactor',
         vendor='淄博太极', model='K-5000', workshop='chemical',
         line='reaction_a', stage='反应'),
    dict(id='distillation_01', name='精馏塔1# (φ1200)', template='column',
         vendor='天大北洋', model='T-1200', workshop='chemical',
         line='reaction_a', stage='精馏'),
    dict(id='storage_tank_01', name='成品储罐1# (200m³)', template='tank',
         vendor='南通中集', model='ST-200', workshop='chemical',
         line='reaction_a', stage='储运'),
    dict(id='reactor_02', name='反应釜2# (3000L 不锈钢)', template='reactor',
         vendor='淄博太极', model='K-3000', workshop='chemical',
         line='reaction_b', stage='反应'),
    dict(id='heat_exchanger_02', name='螺旋板换热器 (60m²)', template='heat_exchanger',
         vendor='四平换热器', model='SX-60', workshop='chemical',
         line='reaction_b', stage='换热'),
    dict(id='scrubber_01', name='尾气洗涤塔 (φ800)', template='column',
         vendor='恩国', model='WS-800', workshop='chemical',
         line='reaction_b', stage='尾气',
         description='碱洗塔 — 进出口 pH/压差/循环泵'),

    # ---------------------------------------------------------------- 动力站
    dict(id='compressor_02', name='空压机2# (阿特拉斯GA75)', template='rotating_machine',
         vendor='阿特拉斯', model='GA75 VSD+', workshop='power',
         line='compressed_air', stage='空压',
         description='螺杆空压机 — 电流/转速/排气压力/流量'),
    dict(id='compressor_03', name='空压机3# (寿力LS20)', template='rotating_machine',
         vendor='寿力', model='LS20-150', workshop='power',
         line='compressed_air', stage='空压',
         description='螺杆空压机 — 电流/转速/排气压力/流量'),
    dict(id='air_dryer_01', name='冷冻式干燥机 (汉粤HY-30)', template='dryer',
         vendor='汉粤', model='HY-30AC', workshop='power',
         line='compressed_air', stage='干燥',
         description='压缩空气冷干机 — 露点/出口温度/压差'),
    dict(id='chiller_02', name='冷水机组2# (开利30XW)', template='rotating_machine',
         vendor='开利', model='30XW-400', workshop='power',
         line='chilled_water', stage='制冷',
         description='螺杆冷水机组 — 电流/转速/压力/流量'),
    dict(id='cooling_tower_02', name='冷却塔2# (CT-250)', template='rotating_machine',
         vendor='良机', model='CT-250', workshop='power',
         line='chilled_water', stage='散热',
         description='横流冷却塔 — 风机电流/转速/出水温度'),
    dict(id='boiler_01', name='燃气蒸汽锅炉 (4t/h)', template='thermal_machine',
         vendor='三浦', model='LX-4000', workshop='power',
         line='steam', stage='锅炉',
         description='燃气蒸汽锅炉 — 三区温度/汽压/排烟/效率'),
    dict(id='water_softener_01', name='全自动软水器 (20t/h)', template='tank',
         vendor='润新', model='RS-20', workshop='power',
         line='steam', stage='软化',
         description='钠离子交换软水器 — 硬度/盐位/再生'),
    dict(id='nitrogen_generator_01', name='制氮机 (PSA 50Nm³/h)', template='analyzer',
         vendor='瑞气', model='PSA-50', workshop='power',
         line='aux', stage='制氮',
         description='PSA 制氮 — 纯度/流量/压力'),

    # ---------------------------------------------------------------- 配电
    dict(id='transformer_02', name='干式变压器2# (SCB13-2000)', template='electrical_panel',
         vendor='顺特电气', model='SCB13-2000', workshop='electrical',
         line='hv', stage='变压',
         description='干式变压器 — 电压/电流/功率/温控'),
    dict(id='hv_switchgear_01', name='高压开关柜 (KYN28-12)', template='electrical_panel',
         vendor='正泰', model='KYN28-12', workshop='electrical',
         line='hv', stage='进线'),
    dict(id='lv_switchgear_01', name='低压开关柜 (MNS 3.0)', template='electrical_panel',
         vendor='正泰', model='MNS 3.0', workshop='electrical',
         line='lv', stage='进线'),
    dict(id='energy_meter_main_01', name='总进线电表 (安科瑞APM830)', template='electrical_panel',
         vendor='安科瑞', model='APM830', workshop='electrical',
         line='lv', stage='计量'),
    dict(id='dc_panel_01', name='直流屏 (GZDW-100Ah)', template='electrical_panel',
         vendor='许继', model='GZDW-100', workshop='electrical',
         line='backup', stage='后备'),
    dict(id='capacitor_bank_01', name='电容补偿柜 (300kvar)', template='electrical_panel',
         vendor='正泰', model='GGJ-300', workshop='electrical',
         line='backup', stage='补偿'),

    # ---------------------------------------------------------------- 公用
    dict(id='ahu_02', name='空调机组2# (组合式AHU)', template='heat_exchanger',
         vendor='盾安', model='AHU-2000', workshop='utility',
         line='hvac', stage='空调'),
    dict(id='ahu_03', name='空调机组3# (组合式AHU)', template='heat_exchanger',
         vendor='盾安', model='AHU-1600', workshop='utility',
         line='hvac', stage='空调'),
    dict(id='exhaust_fan_01', name='排风机 (4-72 No.8C)', template='rotating_machine',
         vendor='上风高科', model='4-72 No.8C', workshop='utility',
         line='hvac', stage='排风'),
    dict(id='water_tank_01', name='生活水箱 (50m³)', template='tank',
         vendor='南通中集', model='WT-50', workshop='utility',
         line='water', stage='储水'),
    dict(id='sewage_pump_01', name='污水提升泵 (WQ50-15)', template='rotating_machine',
         vendor='凯泉', model='WQ50-15-4', workshop='utility',
         line='water', stage='输水'),
    dict(id='heat_exchanger_01', name='板式换热器 (BR0.5-100)', template='heat_exchanger',
         vendor='四平换热器', model='BR0.5-100', workshop='utility',
         line='water', stage='换热'),

    # ---------------------------------------------------------------- 物流
    dict(id='stacker_crane_01', name='巷道堆垛机 (MS/1.5t)', template='stacker_crane',
         vendor='精星', model='MS-1500', workshop='logistics',
         line='asrs', stage='堆垛'),
    dict(id='conveyor_in_01', name='入库输送线 (辊筒)', template='conveyor_line',
         vendor='精星', model='RC-800', workshop='logistics',
         line='asrs', stage='入库'),
    dict(id='conveyor_out_01', name='出库输送线 (辊筒)', template='conveyor_line',
         vendor='精星', model='RC-800', workshop='logistics',
         line='asrs', stage='出库'),
    dict(id='agv_charger_01', name='AGV充电桩 (60A)', template='agv_charger',
         vendor='海康机器人', model='CH-60', workshop='logistics',
         line='agv', stage='充电'),
    dict(id='weighbridge_01', name='地磅 (3×18m 100t)', template='meter',
         vendor='梅特勒-托利多', model='SCS-100', workshop='logistics',
         line='yard', stage='称重',
         description='汽车衡 — 重量/车号识别'),
    dict(id='dock_leveler_01', name='液压登车桥 (6t)', template='safety_device',
         vendor='合力', model='DCQ-6', workshop='logistics',
         line='yard', stage='装卸'),

    # ---------------------------------------------------------------- 质检
    dict(id='spectrometer_01', name='直读光谱仪 (斯派克MAXx)', template='analyzer',
         vendor='斯派克', model='SPECTROMAXx', workshop='qc',
         line='material_lab', stage='成分'),
    dict(id='hardness_tester_01', name='洛氏硬度计 (莱州HR-150A)', template='analyzer',
         vendor='莱州华银', model='HR-150A', workshop='qc',
         line='material_lab', stage='力学'),
    dict(id='roughness_tester_01', name='粗糙度仪 (三丰SJ-210)', template='analyzer',
         vendor='三丰', model='SJ-210', workshop='qc',
         line='material_lab', stage='表面'),
    dict(id='moisture_analyzer_01', name='水分测定仪 (梅特勒HX204)', template='analyzer',
         vendor='梅特勒-托利多', model='HX204', workshop='qc',
         line='material_lab', stage='水分'),
    dict(id='tensile_tester_01', name='电子拉力机 (英斯特朗3365)', template='analyzer',
         vendor='英斯特朗', model='3365', workshop='qc',
         line='material_lab', stage='力学'),

    # ---------------------------------------------------------------- 安全
    dict(id='gas_detector_01', name='可燃气体探测器 (GT-903)', template='safety_device',
         vendor='汉威', model='GT-903', workshop='safety',
         line='fire', stage='探测',
         description='可燃气体探测 — 浓度/报警/自检'),
    dict(id='sprinkler_01', name='湿式喷淋系统 (DN100)', template='safety_device',
         vendor='首安', model='ZSP-100', workshop='safety',
         line='fire', stage='灭火',
         description='喷淋系统 — 压力/流量/阀门'),
    dict(id='access_control_01', name='门禁控制器 (海康DS-K2604)', template='safety_device',
         vendor='海康威视', model='DS-K2604', workshop='safety',
         line='fire', stage='疏散'),
    dict(id='emergency_lighting_01', name='应急照明集中电源 (EPS-3kW)', template='safety_device',
         vendor='首安', model='EPS-3K', workshop='safety',
         line='fire', stage='疏散'),

    # ---------------------------------------------------------------- 能源
    dict(id='pv_inverter_02', name='光伏逆变器2# (华为SUN2000-60KTL)', template='electrical_panel',
         vendor='华为', model='SUN2000-60KTL', workshop='energy',
         line='solar', stage='发电'),
    dict(id='grid_meter_01', name='并网计量表 (威胜DTSD341)', template='electrical_panel',
         vendor='威胜', model='DTSD341', workshop='energy',
         line='solar', stage='并网'),
    dict(id='bess_bms_02', name='储能BMS 2# (300kWh)', template='electrical_panel',
         vendor='宁德时代', model='EnerC-300', workshop='energy',
         line='storage', stage='储能'),
    dict(id='wind_turbine_01', name='小型风力发电机 (10kW)', template='rotating_machine',
         vendor='中科风电', model='FD-10K', workshop='energy',
         line='storage', stage='风电'),

    # ---------------------------------------------------------------- 信息
    dict(id='mes_api_02', name='MES 接口服务 (集群节点2)', template='server_node',
         vendor='自研', model='MES-Node2', workshop='it',
         line='integration', stage='平台'),
    dict(id='scada_server_01', name='SCADA 应用服务器', template='server_node',
         vendor='自研', model='APP-01', workshop='it',
         line='integration', stage='平台'),
    dict(id='historian_01', name='实时历史库 (InfluxDB)', template='server_node',
         vendor='InfluxData', model='TSDB-01', workshop='it',
         line='integration', stage='平台'),
    dict(id='erp_gateway_01', name='ERP 数据网关', template='server_node',
         vendor='SAP', model='GW-01', workshop='it',
         line='integration', stage='上层'),
]


#: 拓扑图里的三层角色。
#:
#: 为什么要在配置里显式标：PLC 和它控制的注塑机如果画在同一列，
#: 图上就是「PLC → 机械手」这种物理上不存在的物料流。
#: 控制器属于**控制层**，工艺设备属于**设备层**，仪表属于**仪表层** ——
#: 三层在拓扑图里应该分行画。
_CONTROLLER_HINTS = ('plc', 'dcs', 'supcon', 'server', 'historian',
                     'gateway', 'mes', 'scada', 'web_')
_INSTRUMENT_HINTS = ('xmitter', 'transmitter', 'flowmeter', 'analyzer',
                     'cems', 'detector', 'tester', 'meter', 'sensor',
                     'spectrometer', 'weighbridge')


def _infer_role(device_id: str, name: str = '') -> str:
    """推断设备在拓扑里的层级角色。"""
    key = f'{device_id} {name}'.lower()
    if any(h in key for h in _CONTROLLER_HINTS):
        return 'controller'
    if any(h in key for h in _INSTRUMENT_HINTS):
        return 'instrument'
    return 'equipment'


#: 按单位推断可写量程。键是单位，值是 [下限, 上限]。
#:
#: 为什么要自动生成：`access: rw` 但没配 `write_limits` 的点位，
#: `WriteSafetyValidator` 会 **fail-closed 拒绝一切写入**（这是对的，
#: 没有量程就没法判断「1.9 是不是超限」）。但现场看到的现象是
#: 「点保存没反应，提示未配置写入量程」，而不知道该填什么。
#: 给一个按量纲的合理默认值，现场再按工艺微调。
_UNIT_LIMITS = {
    '°C': [0, 600],
    'MPa': [0, 10],
    'kPa': [0, 1000],
    'rpm': [0, 30000],
    'm³/h': [0, 5000],
    'L/min': [0, 500],
    'm/min': [0, 300],
    'mm/s': [0, 200],
    'Hz': [0, 60],
    'A': [0, 2000],
    'V': [0, 1000],
    'kW': [0, 5000],
    'kvar': [0, 5000],
    '%': [0, 100],
    's': [0, 3600],
    'N': [0, 100000],
    'kN': [0, 5000],
    'kg': [0, 50000],
    '件': [0, 1000000],
    'm': [0, 1000],
    'm³': [0, 10000],
}


def _infer_write_limit(point: dict) -> list:
    """给一个可写点位推断量程。"""
    name = str(point.get('name', '')).lower()
    # 开关量：继电器/命令位/状态位只有 0/1
    if any(k in name for k in ('relay', 'cmd_', 'enable', 'switch', 'state', 'mode')):
        return [0, 1]
    unit = str(point.get('unit') or '').strip()
    if unit in _UNIT_LIMITS:
        return list(_UNIT_LIMITS[unit])
    # 有 sim 段的按它的物理上下限推
    sim = point.get('sim') or {}
    lo, hi = sim.get('min'), sim.get('max')
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and hi > lo:
        return [lo, hi]
    return [0, 10000]


def _ensure_write_limits(dev: dict) -> None:
    """给设备上所有 `access: rw` 的点位补 `write_limits`（已有值不覆盖）。

    `write_limits` 是**设备级**的，键是**点位名**，值是 `[下限, 上限]`。
    写成寄存器内的 `{min, max}` 不生效 —— 见 `配置/devices_real.yaml` 头部说明。
    """
    points = []
    for section in ('registers', 'nodes', 'topics', 'endpoints'):
        points.extend(dev.get(section) or [])

    rw = [p for p in points
          if isinstance(p, dict) and p.get('access') == 'rw' and p.get('name')]
    if not rw:
        return

    limits = dev.get('write_limits')
    if not isinstance(limits, dict):
        limits = {}
    for p in rw:
        limits.setdefault(p['name'], _infer_write_limit(p))
    dev['write_limits'] = limits


def _stable_offset(device_id: str, span: float = 0.06) -> float:
    """按 id 生成稳定的 ±span 偏移系数。

    用 sha256 而不是 `hash()`：`hash()` 在 Python 3.3+ 有随机化种子，
    每次进程启动结果不同 —— 那会让「同一台设备」每次跑出来的基准值不一样，
    历史曲线会出现无法解释的台阶。sha256 稳定。

    不用 md5：它是加密哈希，项目的安全闸门（S324）会拦 ——
    虽然这里只用来做偏移量、和安全性无关，但**闸门不该为个例开口子**，
    换个算法成本更低。
    """
    digest = hashlib.sha256(device_id.encode('utf-8')).hexdigest()
    # 取前 8 位十六进制 → [0,1) → [-1,1)
    unit = int(digest[:8], 16) / 0xFFFFFFFF
    return 1.0 + (unit * 2.0 - 1.0) * span


def _scale_process_model(device_id: str, model: dict) -> dict:
    """给 base_* 加稳定偏移，让同型号设备不会读出一模一样的数字。"""
    out = {}
    for k, v in (model or {}).items():
        if k.startswith('base_') and isinstance(v, (int, float)):
            out[k] = round(float(v) * _stable_offset(device_id), 3)
        else:
            out[k] = v
    return out


def _build_points(template: dict, device_id: str) -> list[dict]:
    """把模板的点位定义展开成设备配置的 registers，地址按宽度自动累加。"""
    regs = []
    addr = 0
    for p in template.get('points') or []:
        dtype = p.get('type', 'float32')
        width = TYPE_WIDTH.get(dtype, 1)
        item = {
            'address': addr,
            'name': p['name'],
            'data_type': dtype,
            'description': p.get('desc', p['name']),
            'unit': p.get('unit', ''),
            'scale': p.get('scale', 1.0),
            'length': width,
            'access': p.get('access', 'r'),
        }
        if p.get('sim'):
            item['sim'] = dict(p['sim'])
        regs.append(item)
        addr += width
    # 留 8 个寄存器的空隙再放控制线圈，避免和测量点位地址重叠
    coil_base = addr + 8
    # 线圈也要有确定初值（0 = 未动作）。不给的话探针会报「缺值」，
    # 而「线圈没有值」和「设备没这个线圈」在界面上分不开。
    regs.append({'address': coil_base, 'name': 'cmd_start', 'data_type': 'uint16',
                 'description': '启动命令（线圈，1=启动）', 'unit': '', 'scale': 1,
                 'length': 1, 'access': 'rw', 'kind': 'coil',
                 'sim': {'source': 'constant', 'value': 0, 'decimals': 0}})
    regs.append({'address': coil_base + 1, 'name': 'cmd_stop', 'data_type': 'uint16',
                 'description': '停止命令（线圈，1=停止）', 'unit': '', 'scale': 1,
                 'length': 1, 'access': 'rw', 'kind': 'coil',
                 'sim': {'source': 'constant', 'value': 0, 'decimals': 0}})
    return regs


def main() -> int:
    ap = argparse.ArgumentParser(description='按厂区布局生成设备清单与拓扑')
    ap.add_argument('--dry-run', action='store_true', help='只统计，不写盘')
    ap.add_argument('--force', action='store_true',
                    help='用模板重新生成 NEW_DEVICES 里的设备（改过模板后用）')
    args = ap.parse_args()

    templates = yaml.safe_load(TEMPLATES_PATH.read_text(encoding='utf-8'))['templates']
    existing = yaml.safe_load(DEVICES_PATH.read_text(encoding='utf-8'))['devices']

    if args.force:
        # 改了模板（加了点位、换了量纲）之后要能重新套用。
        # 只重生成 NEW_DEVICES 里的设备，KEEP_AS_IS 的手工设备永远不动 ——
        # 那些是逐台调过耦合链的，覆盖掉就找不回来了。
        regen = {spec['id'] for spec in NEW_DEVICES}
        before = len(existing)
        existing = [d for d in existing if d['id'] not in regen]
        print(f'--force：重新生成 {before - len(existing)} 台模板设备')

    existing_by_id = {d['id']: d for d in existing}

    # ---- 1. 给已有设备补层级字段 ----
    keep_map = {did: (ws, ln, st) for did, ws, ln, st in KEEP_AS_IS}

    # 生成器要能重复跑（改了布局重新生成）。第二次跑时，上一轮生成的新增设备
    # 已经在文件里了，它们自带 workshop/line/stage —— 不该再要求登记进
    # KEEP_AS_IS（那会把「生成结果」当成「手工维护的清单」）。
    # 判据：文件里已经有完整归属字段的，视为已归属。
    def _has_placement(d):
        return bool(d.get('workshop') and d.get('line') and d.get('stage'))

    missing = [d['id'] for d in existing
               if d['id'] not in keep_map and not _has_placement(d)]
    if missing:
        print(f'❌ 有 {len(missing)} 台已有设备既没在 KEEP_AS_IS 登记，'
              f'也没有 workshop/line/stage：{missing[:6]}')
        print('   补上归属，否则它们挂不上拓扑')
        return 1

    ws_name = {w['id']: w['name'] for w in WORKSHOPS}

    # 模拟服务端地址：**所有**设备都要有，包括手工维护的 42 台。
    # 没有它，模拟模式下的采集层不知道连哪里，只能退回进程内假客户端。
    port_cursor = {k: v for k, v in PORT_BASE.items()}

    def _assign_sim_endpoint(dev: dict) -> dict:
        proto = dev.get('protocol', 'modbus_tcp')
        if proto in ('opcua', 'mqtt'):
            # OPC UA / MQTT 是**一个服务端承载所有设备**（现场也常这样：
            # 一台 OPC UA Server 暴露整个车间）。所以端口固定，不递增。
            dev['sim_endpoint'] = {'host': '127.0.0.1', 'port': PORT_BASE[proto]}
            return dev
        port = port_cursor.get(proto)
        if port is None:
            return dev
        dev['sim_endpoint'] = {'host': '127.0.0.1', 'port': port}
        port_cursor[proto] += 1
        return dev

    merged: list[dict] = []
    for d in existing:
        did = d['id']
        d = dict(d)
        if did in keep_map:
            ws, ln, st = keep_map[did]
        else:
            # 上一轮生成的新增设备：沿用文件里的归属
            ws, ln, st = d['workshop'], d['line'], d['stage']
        d['site'] = SITE['id']
        d['workshop'] = ws
        d['line'] = ln
        d['stage'] = st
        # zone 保留（旧代码还在读），但统一成车间中文名
        d['zone'] = ws_name.get(ws, d.get('zone', ''))
        # role 决定拓扑图分行（控制层/设备层/仪表层）
        d.setdefault('role', _infer_role(did, d.get('name', '')))
        # 端口按**文件里的顺序**递增分配 —— 这个顺序必须稳定，
        # 否则每次重新生成都会让 sim_endpoint 变一遍，
        # 而采集层缓存的是上一次的端口，连不上。
        _ensure_write_limits(d)
        if 'sim_endpoint' not in d:
            _assign_sim_endpoint(d)
        else:
            # 已有端口：游标要跳过它，避免后续设备撞车
            proto = d.get('protocol', 'modbus_tcp')
            cur = port_cursor.get(proto)
            used = (d.get('sim_endpoint') or {}).get('port')
            if cur is not None and isinstance(used, int) and used >= cur:
                port_cursor[proto] = used + 1
        merged.append(d)

    # ---- 2. 生成新设备 ----
    added = []
    for spec in NEW_DEVICES:
        if spec['id'] in existing_by_id:
            # 已经生成过（重复跑），跳过 —— 保留文件里的版本，
            # 因为现场可能已经手工调过它的量程
            continue
        tpl = templates.get(spec['template'])
        if tpl is None:
            print(f"❌ {spec['id']}: 模板 {spec['template']!r} 不存在")
            return 1
        if spec['id'] in existing_by_id:
            print(f"❌ {spec['id']}: id 与已有设备重复")
            return 1

        proto = spec.get('protocol') or tpl.get('protocol', 'modbus_tcp')

        dev = {
            'id': spec['id'],
            'name': spec['name'],
            'protocol': proto,
            'enabled': True,
            'site': SITE['id'],
            'workshop': spec['workshop'],
            'line': spec['line'],
            'stage': spec['stage'],
            'zone': ws_name[spec['workshop']],
            'role': spec.get('role') or tpl.get('role') or _infer_role(spec['id'], spec['name']),
            'vendor': spec.get('vendor', ''),
            'device_model': spec.get('model', ''),
            'description': spec.get('description') or tpl.get('description', ''),
            'collection_interval': 5,
            'host': '192.168.1.1',        # 占位，现场按实际填
            'port': 502,
        }
        _assign_sim_endpoint(dev)

        if proto in ('modbus_tcp', 'modbus_rtu'):
            dev['slave_id'] = 1
            dev['byte_order'] = 'ABCD'
            dev['timeout'] = 5
            dev['registers'] = _build_points(tpl, spec['id'])
        elif proto == 'rest':
            base = 15500 + len(added)
            dev['base_url'] = f'http://127.0.0.1:{base}'
            dev['timeout'] = 5
            dev['endpoints'] = [
                dict({'name': p['name'], 'path': f"/api/{p['name']}",
                      'json_path': 'data.value', 'method': 'GET',
                      'description': p.get('desc', p['name']),
                      'unit': p.get('unit', '')},
                     **({'sim': dict(p['sim'])} if p.get('sim') else {}))
                for p in (tpl.get('points') or [])
            ]

        _ensure_write_limits(dev)

        if tpl.get('process_model'):
            dev['simulation'] = {
                'process_model': _scale_process_model(spec['id'], tpl['process_model'])
            }

        merged.append(dev)
        added.append(dev)

    # ---- 3. 统计 ----
    def point_count(d):
        return sum(len(d.get(s) or []) for s in ('registers', 'nodes', 'topics', 'endpoints'))

    total_pts = sum(point_count(d) for d in merged)
    print(f'设备 {len(merged)} 台（保留 {len(existing)} + 新增 {len(added)}），'
          f'点位 {total_pts} 个')
    by_proto = {}
    for d in merged:
        by_proto[d.get('protocol', '?')] = by_proto.get(d.get('protocol', '?'), 0) + 1
    print('协议分布:', dict(sorted(by_proto.items())))

    if args.dry_run:
        print('（--dry-run，未写盘）')
        return 0

    # ---- 4. 写设备清单 ----
    header = DEVICES_PATH.read_text(encoding='utf-8').split('\ndevices:')[0]
    header = header.rstrip('\n') + '''

# =============================================================================
# 本文件由 tools/gen_factory.py 生成（厂区布局在脚本里）。
# 现场直接改这个文件也可以 —— 但下次重新生成会覆盖，所以结构性改动请改脚本。
#
# 层级字段（拓扑图靠它挂载）：
#   site      厂区 id
#   workshop  车间 id（中文名见 zone）
#   line      产线 id
#   stage     工序名（决定拓扑图从左到右的列）
#   sim_endpoint  模拟服务端监听地址。模拟模式下采集层连这里；
#                 host/port 是现场地址，切现场时只改那两个。
# =============================================================================
'''
    body = yaml.dump({'devices': merged}, allow_unicode=True, sort_keys=True,
                     default_flow_style=False, width=200)
    text = header + '\ndevices:\n' + body[len('devices:\n'):] if body.startswith('devices:\n') else header + '\n' + body
    # 写前自检：必须能解析回来且设备数一致
    parsed = yaml.safe_load(text)
    assert len(parsed['devices']) == len(merged), '生成的 YAML 解析后设备数不一致'
    DEVICES_PATH.write_text(text, encoding='utf-8', newline='\n')
    print(f'✅ 已写 {DEVICES_PATH.relative_to(REPO)}')

    # ---- 5. 写拓扑骨架 ----
    topo = {
        'site': SITE,
        'workshops': WORKSHOPS,
        'notes': [
            '本文件只描述骨架（车间/产线/工序顺序）。设备挂载靠 devices_*.yaml 的 '
            'workshop/line/stage 字段，加设备不用改这里。',
            'stages 的顺序决定拓扑图从左到右的列；同一列多台设备纵向排列。',
        ],
    }
    topo_text = ('# 厂区拓扑骨架 —— 车间/产线/工序顺序\n'
                 '# 设备清单在 devices_simulated.yaml / devices_real.yaml，靠 '
                 'workshop/line/stage 挂上来。\n'
                 + yaml.dump(topo, allow_unicode=True, sort_keys=False,
                             default_flow_style=False, width=200))
    TOPOLOGY_PATH.write_text(topo_text, encoding='utf-8', newline='\n')
    print(f'✅ 已写 {TOPOLOGY_PATH.relative_to(REPO)}')
    print(f'   车间 {len(WORKSHOPS)} 个 / 产线 '
          f'{sum(len(w["lines"]) for w in WORKSHOPS)} 条')
    return 0


if __name__ == '__main__':
    sys.exit(main())
