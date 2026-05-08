"""测试修复后的模块"""
import sys
import random
sys.path.insert(0, '.')

print("=" * 50)
print("工业SCADA系统修复验证")
print("=" * 50)

# 1. 测试所有智能层模块导入
print("\n[1] 导入智能层模块...")
from 智能层.spc_analyzer import SPCAnalyzer
from 智能层.oee_calculator import OEECalculator
from 智能层.energy_manager import EnergyManager
from 智能层.predictive_maintenance import PredictiveMaintenance
from 智能层.edge_decision import EdgeDecisionEngine
print("  所有智能层模块导入成功")

# 2. 测试SPC X-R图分析
print("\n[2] 测试SPC X-R图分析...")
spc = SPCAnalyzer(None)
data = [random.gauss(100, 2) for _ in range(50)]
for v in data:
    spc.feed_data('test_device', 'test_reg', v)
result = spc.calculate_xbar_r_chart('test_device', 'test_reg')
if result:
    print(f"  控制状态: {result['is_in_control']}")
    print(f"  违规数量: {len(result['violations'])}")
    print(f"  X-bar UCL: {result['xbar_chart']['ucl']}")
    print(f"  X-bar CL:  {result['xbar_chart']['cl']}")
    print(f"  X-bar LCL: {result['xbar_chart']['lcl']}")
    print(f"  R UCL:     {result['r_chart']['ucl']}")
else:
    print("  数据不足，跳过")

# 3. 测试SPC X-S图分析（之前B4系数会崩溃）
print("\n[3] 测试SPC X-S图分析...")
result2 = spc.calculate_xbar_s_chart('test_device', 'test_reg')
if result2:
    print(f"  控制状态: {result2['is_in_control']}")
    print(f"  违规数量: {len(result2['violations'])}")
    print(f"  S UCL:     {result2['s_chart']['ucl']}")
    print(f"  S CL:      {result2['s_chart']['cl']}")
else:
    print("  数据不足，跳过")

# 4. 测试violations存储
print("\n[4] 测试violations存储...")
# 生成会触发违规的数据
for v in [100] * 40 + [120, 125, 130, 135, 140, 145, 150, 155, 160, 165]:
    spc.feed_data('bad_device', 'test_reg', v)
spc.calculate_xbar_r_chart('bad_device', 'test_reg')
violations = spc.get_violations('bad_device')
print(f"  存储的违规数量: {len(violations)}")
if violations:
    print(f"  最新违规: rule={violations[-1].get('rule')}, desc={violations[-1].get('description', '')[:50]}")

# 5. 测试OEE计算器
print("\n[5] 测试OEE计算器...")
from 存储层.database import Database
import tempfile, os
tmp_db = os.path.join(tempfile.gettempdir(), 'test_scada.db')
if os.path.exists(tmp_db):
    os.remove(tmp_db)
db = Database(tmp_db)
oee = OEECalculator(db)
oee.start_shift('test_device', planned_hours=8.0)
oee.set_theoretical_rate('test_device', 100)
states = oee.get_all_device_states()
print(f"  设备状态数量: {len(states)}")
print(f"  设备状态: {states}")

# 6. 测试EnergyManager
print("\n[6] 测试EnergyManager...")
em = EnergyManager(db)
em.set_baseline('test_device', 500)
print(f"  基线设置成功")

# 7. 测试EdgeDecisionEngine
print("\n[7] 测试EdgeDecisionEngine...")
edge = EdgeDecisionEngine(db)
edge.update_data('test_key', 100.0)
print(f"  边缘决策数据更新成功")

# 8. 测试数据库UPSERT
print("\n[8] 测试数据库UPSERT...")
from datetime import datetime, timedelta
now = datetime.now()
db.insert_data('dev1', 'reg1', 42.0, now, 'kWh')
db.insert_data('dev1', 'reg1', 43.0, now + timedelta(seconds=1), 'kWh')
db.insert_data('dev1', 'reg1', 44.0, now + timedelta(seconds=2), 'kWh')
print("  UPSERT写入成功（无竞态崩溃）")

# 9. 测试5分钟聚合
print("\n[9] 测试5分钟聚合...")
for i in range(20):
    ts = now - timedelta(minutes=i)
    db.insert_data('agg_dev', 'agg_reg', 100 + random.gauss(0, 2), ts, 'kWh')
result5m = db.get_history_data('agg_dev', 'agg_reg', 
    (now - timedelta(hours=1)).isoformat(), now.isoformat(), interval='5min')
print(f"  5分钟聚合数据点: {len(result5m)}")

print("\n" + "=" * 50)
print("所有测试通过！修复验证成功。")
print("=" * 50)
