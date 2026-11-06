# 工厂扩展目录

这个目录是**给现场/集成商写代码的地方**，不属于主程序。升级主程序时，
只要保留本目录即可继续使用自己的适配器。

```
扩展/
  README.md
  adapters/            ← 插件适配器（.py）
    example_mock.py    ← 可直接运行的示例
```

## 1. 最快上手

1. 复制 `adapters/example_mock.py`，改名为你的插件（如 `acme_mes.py`）；
2. 实现 `Adapter.read()`，返回 `{点位: 值}`（或 `{点位: {value, unit}}`）；
3. 在 `配置/devices_real.yaml` 里加设备：

```yaml
- id: acme_line1
  name: ACME MES 产线
  protocol: custom
  enabled: true
  poll_interval: 5
  adapter:
    transport: plugin
    plugin: acme_mes          # 对应 扩展/adapters/acme_mes.py
    settings:
      url: "${ACME_MES_URL}"
      token: "${ACME_MES_TOKEN}"
```

4. 重启，或调用 `POST /api/devices/adapters/reload`；
5. 查看 `GET /api/devices/adapters` 确认插件已被发现。

## 2. 插件契约

```python
PLUGIN_INFO = {'version': '1.0', 'description': '...', 'author': '...'}

class Adapter:
    def __init__(self, settings: dict): ...
    def read(self) -> dict: ...          # 必需（也可以叫 poll）
    def write_point(self, name, value): ...  # 可选
    def stop(self): ...                  # 可选（也可以叫 close）
```

也可以用模块级工厂代替类：

```python
def create_adapter(settings: dict):
    return MyAdapter(settings)
```

`read()` 返回两种形态都支持：

- 直接返回 `{点位: 值}`（数值/字符串/布尔）；
- 返回原始载荷，并在 `adapter.points` 里写路径映射（与 HTTP 适配器同一套
  `path/type/enum/scale/records` 规则）。

## 3. 调试

- 插件导入失败、`read()` 抛异常都只会让**这一台设备**失败：日志有 ERROR，
  `GET /api/devices` 的 `last_error` 能看到原因，采集循环不会停。
- 改完插件代码：`POST /api/devices/adapters/reload`（engineer 及以上）。
- 插件目录可用环境变量 `SCADA_PLUGIN_DIR` 指向工厂自己的可写目录
  （打包部署时建议这么做，避免升级覆盖）。

## 4. 安全边界（务必阅读）

- 插件代码**以主进程权限运行**，没有沙箱、没有超时强杀；
  只部署经过评审的代码，不要从网络下载插件直接放进来。
- 只允许 `扩展/adapters/` 下的 `<名字>.py`，插件名必须是合法标识符，
  路径做了父目录校验，`../` 之类会被拒绝。
- 插件里的密钥建议只从 `settings` 的 `${ENV}` 读取，不要把明文写进 YAML。
