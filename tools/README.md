# 工具集

## Modbus TCP 模拟器

独立进程，模拟真实 Modbus TCP 设备。采集层走真实 Modbus 协议连接。

### 启动

```bash
# 默认监听 5020 端口
python tools/modbus_simulator.py

# 自定义端口
python tools/modbus_simulator.py --port 502

# 只模拟西门子 PLC
python tools/modbus_simulator.py --devices siemens

# 对某台设备注入故障
python tools/modbus_simulator.py --fault siemens
```

### 配合采集层使用

1. 启动模拟器: `python tools/modbus_simulator.py`
2. 启动系统（真实模式）: `python run.py --real`
   - 设备配置中 host 改为 `127.0.0.1`，port 改为 `5020`

### Docker 环境

模拟器和 SCADA 应用在同一个 Docker 网络中，host 用容器名 `modbus-sim`。
