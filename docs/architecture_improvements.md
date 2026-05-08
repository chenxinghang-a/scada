# 架构改进总结

## 1. 模拟/真实设备完全分离

### 问题
- 原架构中模拟和真实设备混在一起，通过`simulation_mode`标志切换
- 真实模式下仍然显示"真实设备"标识，影响用户体验

### 解决方案
创建了`DeviceManagerFactory`工厂类，实现完全分离：

```python
# 采集层/device_manager_factory.py
class DeviceManagerFactory:
    @staticmethod
    def create(config_path='配置/system.yaml') -> IDeviceManager:
        # 根据配置自动选择管理器
        if simulation_mode:
            return SimulatedDeviceManager()  # 完全独立的模拟管理器
        else:
            return RealDeviceManager()       # 完全独立的真实管理器
```

### 特点
- **模拟模式**：使用`SimulatedDeviceManager`，生成仿真数据
- **真实模式**：使用`RealDeviceManager`，连接实际硬件
- **真实模式不显示标识**：前端在真实模式下隐藏"模拟模式"指示器
- **两个管理器完全独立**：互不干扰，代码清晰

## 2. 增强PLC支持

### 问题
- 原模板只有简单的PLC模板，不支持具体品牌型号
- 缺乏主流PLC品牌的设备模板

### 解决方案
添加了8个主流PLC品牌的设备模板：

| 品牌 | 型号 | 应用场景 |
|------|------|----------|
| 西门子 | S7-1500 | 高端PLC，锅炉/换热器控制 |
| 西门子 | S7-1200 | 紧凑型PLC，中小型设备 |
| 三菱 | FX5U | 高性能小型PLC，注塑机/包装机 |
| 施耐德 | M340 | 中型PLC，水处理/化工 |
| 欧姆龙 | NJ系列 | 机器自动化控制器 |
| 台达 | DVP | 经济型PLC，包装/输送 |
| 汇川 | H5U | 国产高性能PLC，涂装/喷涂 |
| 和利时 | LK | 国产大型PLC，化工/制药 |

### 每个模板包含
- 品牌信息（vendor）
- 设备型号（device_model）
- 典型寄存器配置（温度、压力、流量、计数等）
- 真实的工业参数范围

## 3. 改进警告弹窗设计

### 问题
- 原报警通知使用弹窗形式，会打断用户操作
- 弹窗堆积影响工作效率
- 严重报警和普通警告显示方式相同

### 解决方案
改为非侵入式Toast通知：

```javascript
// 底部右侧Toast通知
function showAlarmNotification(alarm) {
    // 位置：右下角，不遮挡主要内容
    // 动画：滑入滑出，平滑过渡
    // 自动消失：严重报警30秒，普通警告8秒
    // 最多显示3个，避免堆积
}
```

### 改进点
1. **位置优化**：右下角，不遮挡主要内容
2. **动画效果**：滑入滑出，平滑过渡
3. **分级显示**：
   - 严重报警：红色背景，30秒消失，播放提示音
   - 普通警告：黄色背景，8秒消失，无提示音
4. **数量限制**：最多3个，避免堆积
5. **可手动关闭**：点击X按钮立即关闭

## 4. 文件变更清单

### 新增文件
- `采集层/device_manager_factory.py` - 设备管理器工厂

### 修改文件
- `模板/base.html` - 真实模式下隐藏模拟标识
- `静态资源/js/main.js` - 改用Toast通知
- `静态资源/css/style.css` - 添加Toast动画样式
- `展示层/api/api_devices.py` - 添加PLC设备模板

## 5. 使用说明

### 切换模式
在`配置/system.yaml`中设置：
```yaml
system:
  simulation_mode: true   # 模拟模式
  simulation_mode: false  # 真实模式
```

### 添加PLC设备
1. 进入设备管理页面
2. 点击"添加设备"
3. 点击"使用模板"
4. 选择对应的PLC模板
5. 修改IP地址和端口
6. 保存

### 报警通知
- 报警通知会以Toast形式显示在右下角
- 严重报警会播放提示音
- 可以点击X手动关闭
- 最多显示3个通知