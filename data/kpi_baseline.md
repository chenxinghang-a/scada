# 报警绩效 KPI 基线（ISA-18.2）

生成时间：2026-10-25T10:40:32

## `data\scada.db`

- 时间跨度：2026-04-29 17:29:02.111239 → 2026-05-08 17:38:45.094872（9.0 天）
- 报警总数：**47751**
- 报警率：**220.90 条/小时**（5301.69 条/天）❌ 超出 ISA 目标（≤6/小时）
- 确认率：0.2%（81/47751）
- 恢复率：**无法判定**（该库没有 cleared 列（round 260 之前建的库），"仍在报警"无法判定）
- critical 占比：15.4%
- 抖动（相邻间隔<60s 的对数）：26096（占 54.6%）

### 级别分布

| 级别 | 条数 |
|---|---|
| warning | 40378 |
| critical | 7373 |

### Bad actor（前 10）

| 设备 | 寄存器 | 报警ID | 条数 | 占比 |
|---|---|---|---|---|
| siemens_1500_01 | boiler_pressure | boiler_pressure_high | 362 | 0.8% |
| siemens_1500_01 | boiler_pressure | pressure_high | 231 | 0.5% |
| schneider_m340_01 | ph_value | ph_low | 171 | 0.4% |
| power_meter_01 | current | current_high | 130 | 0.3% |
| pressure_sensor_01 | pressure | pressure_high | 103 | 0.2% |
| pressure_sensor_01 | pressure | pressure_low | 92 | 0.2% |
| temp_sensor_01 | temperature | temp_low | 76 | 0.2% |
| temp_sensor_01 | temperature | temp_high | 74 | 0.1% |
| siemens_plc_01 | pressure | pressure_high | 61 | 0.1% |
| temp_sensor_01 | temperature_ch5 | env_temp_high | 46 | 0.1% |

### ⚠️ 重复定义（同一寄存器挂了多条规则）

- `edge_decision/auto`：20931 条 → edge_1778170434, edge_1778170435, edge_1778170436, edge_1778170437, edge_1778170439 …（共 20931 个）
- `power_meter_01/voltage`：2 条 → voltage_low, voltage_high
- `pressure_sensor_01/pressure`：2 条 → pressure_low, pressure_high
- `siemens_1500_01/boiler_pressure`：2 条 → boiler_pressure_high, pressure_high
- `siemens_plc_01/pressure`：2 条 → pressure_high, pressure_low
- `temp_sensor_01/temperature`：2 条 → temp_low, temp_high

### ⚠️ 最严重的 10 分钟窗口（ISA 泛滥阈值 >10）

- 2026-05-08 14:30：3330 条
- 2026-05-08 14:20：3045 条
- 2026-05-08 14:00：1958 条
- 2026-05-08 14:10：1929 条
- 2026-05-08 14:40：1866 条

## `data\scada_simulated.db`

- 时间跨度：2026-05-08 17:57:11.673444 → 2026-05-31 22:19:59.608076（23.2 天）
- 报警总数：**105028**
- 报警率：**188.77 条/小时**（4530.49 条/天）❌ 超出 ISA 目标（≤6/小时）
- 确认率：98.6%（103509/105028）
- 恢复率：**无法判定**（该库没有 cleared 列（round 260 之前建的库），"仍在报警"无法判定）
- critical 占比：96.8%
- 抖动（相邻间隔<60s 的对数）：101945（占 97.1%）

### 级别分布

| 级别 | 条数 |
|---|---|
| critical | 101680 |
| warning | 3348 |

### Bad actor（前 10）

| 设备 | 寄存器 | 报警ID | 条数 | 占比 |
|---|---|---|---|---|
| siemens_1500_01 | boiler_temperature | temp_high | 43505 | 41.4% |
| siemens_1500_01 | boiler_temperature | boiler_temp_high | 43501 | 41.4% |
| siemens_1500_01 | boiler_pressure | boiler_pressure_high | 7309 | 7.0% |
| siemens_1500_01 | boiler_pressure | pressure_high | 7309 | 7.0% |
| siemens_1500_01 | boiler_temperature | temp_low | 1066 | 1.0% |
| schneider_m340_01 | ph_value | ph_low | 952 | 0.9% |
| abb_m4m_01 | voltage_a | voltage_low | 656 | 0.6% |
| siemens_1500_01 | boiler_pressure | pressure_low | 375 | 0.4% |
| hollysys_lk_01 | distill_temperature | env_temp_high | 299 | 0.3% |
| abb_m4m_01 | voltage_a | voltage_high | 56 | 0.1% |

### ⚠️ 重复定义（同一寄存器挂了多条规则）

- `siemens_1500_01/boiler_pressure`：3 条 → pressure_high, boiler_pressure_high, pressure_low
- `siemens_1500_01/boiler_temperature`：3 条 → temp_low, temp_high, boiler_temp_high
- `abb_m4m_01/voltage_a`：2 条 → voltage_high, voltage_low

### ⚠️ 最严重的 10 分钟窗口（ISA 泛滥阈值 >10）

- 2026-05-29 05:10：1204 条
- 2026-05-29 04:00：1197 条
- 2026-05-29 02:00：1196 条
- 2026-05-29 01:30：1194 条
- 2026-05-29 18:50：1194 条
