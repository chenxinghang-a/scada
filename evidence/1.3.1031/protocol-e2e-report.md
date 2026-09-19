# 协议端到端（E2E）测试报告 — 证据模板

<!-- 本段必须包含：版本、commit、环境、命令、起止时间、配置摘要、结果、失败项、见证人 -->
<!-- 参考测试：tests/test_modbus_protocol_e2e.py 等；待人工/待脚本回填 -->

- 版本: 1.3.1031
- commit: unknown
- 运行环境: <OS / Python / Node>
- 执行命令: <如 pytest tests/test_modbus_protocol_e2e.py -v>
- 开始时间: <UTC ISO8601>
- 结束时间: <UTC ISO8601>
- 输入配置摘要: <协议类型 Modbus/MC/FINS/MQTT、设备或模拟器地址、点表规模>
- 结果: <pass / fail / partial>
- 失败项: <无 或 列表>
- 人工见证人: <姓名> / <日期>

## 测试项

| 协议 | 场景 | 预期 | 实际 | 结果 | 备注 |
|---|---|---|---|---|---|
| Modbus TCP | <采集→存储→展示链路> | | | <pass/fail> | |
