/**
 * 仪表盘页面JavaScript
 */

// 图表实例
let temperatureChart = null;
let pressureChart = null;

// 数据缓存
const temperatureData = [];
const pressureData = [];
const MAX_DATA_POINTS = 50;

/**
 * 初始化仪表盘
 */
document.addEventListener('DOMContentLoaded', function() {
    // 初始化图表
    initCharts();
    
    // 加载实时数据
    loadRealtimeData();
    
    // 定时更新
    setInterval(loadRealtimeData, 3000);
    
    // 加载最新报警
    loadLatestAlarms();
});

/**
 * 初始化ECharts图表
 */
function initCharts() {
    // 温度图表
    const tempDom = document.getElementById('temperature-chart');
    if (tempDom) {
        temperatureChart = echarts.init(tempDom);
        const tempOption = {
            title: { text: '温度趋势', left: 'center' },
            tooltip: { trigger: 'axis' },
            xAxis: { type: 'category', data: [] },
            yAxis: { type: 'value', name: '°C' },
            series: [{
                name: '温度',
                type: 'line',
                data: [],
                smooth: true,
                areaStyle: { opacity: 0.3 }
            }]
        };
        temperatureChart.setOption(tempOption);
    }
    
    // 压力图表
    const pressDom = document.getElementById('pressure-chart');
    if (pressDom) {
        pressureChart = echarts.init(pressDom);
        const pressOption = {
            title: { text: '压力趋势', left: 'center' },
            tooltip: { trigger: 'axis' },
            xAxis: { type: 'category', data: [] },
            yAxis: { type: 'value', name: 'MPa' },
            series: [{
                name: '压力',
                type: 'line',
                data: [],
                smooth: true,
                areaStyle: { opacity: 0.3 }
            }]
        };
        pressureChart.setOption(pressOption);
    }
}

/**
 * 加载实时数据
 */
async function loadRealtimeData() {
    try {
        const response = await fetch('/api/data/realtime?limit=20');
        const data = await response.json();
        
        if (data.data && data.data.length > 0) {
            updateRealtimeTable(data.data);
            updateCharts(data.data);
            updateGaugeValues(data.data);
            
            // 更新数据状态
            const dataStatus = document.getElementById('data-status');
            if (dataStatus) {
                dataStatus.textContent = '实时更新中';
                dataStatus.className = 'badge bg-success';
            }
            
            // 更新最后更新时间
            const lastUpdate = document.getElementById('last-update');
            if (lastUpdate) {
                lastUpdate.textContent = new Date().toLocaleTimeString('zh-CN');
            }
        } else {
            // 没有数据时显示提示
            const dataStatus = document.getElementById('data-status');
            if (dataStatus) {
                dataStatus.textContent = '等待数据...';
                dataStatus.className = 'badge bg-warning';
            }
        }
        
        // 更新系统统计
        const statusResponse = await fetch('/api/system/status');
        const statusData = await statusResponse.json();
        updateDashboardStats(statusData);
        
        // 更新设备热力图
        if (statusData.devices) {
            let deviceList = statusData.devices;
            if (typeof deviceList === 'object' && !Array.isArray(deviceList)) {
                deviceList = Object.values(deviceList);
            }
            updateDeviceHeatmap(deviceList);
        }
        
        // 更新系统健康度
        updateHealthMetrics(statusData);
        
    } catch (error) {
        console.error('加载实时数据失败:', error);
        const dataStatus = document.getElementById('data-status');
        if (dataStatus) {
            dataStatus.textContent = '连接失败';
            dataStatus.className = 'badge bg-danger';
        }
    }
}

/**
 * 更新仪表盘图表值
 */
function updateGaugeValues(data) {
    let temp = null, pressure = null, voltage = null;
    let tempUnit = '°C', pressureUnit = 'MPa', voltageUnit = 'V';
    
    data.forEach(item => {
        // 动态匹配温度相关寄存器
        if (item.register_name.includes('temperature') || item.register_name.includes('temp')) {
            if (temp === null) {
                temp = item.value;
                tempUnit = item.unit || '°C';
            }
        }
        // 动态匹配压力相关寄存器
        if (item.register_name.includes('pressure')) {
            if (pressure === null) {
                pressure = item.value;
                pressureUnit = item.unit || 'MPa';
            }
        }
        // 动态匹配电压相关寄存器
        if (item.register_name.includes('voltage') || item.register_name.includes('volt')) {
            if (voltage === null) {
                voltage = item.value;
                voltageUnit = item.unit || 'V';
            }
        }
    });
    
    // 更新仪表盘
    if (typeof updateGauges === 'function') {
        updateGauges({ 
            temperature: temp, 
            pressure: pressure, 
            voltage: voltage,
            tempUnit: tempUnit,
            pressureUnit: pressureUnit,
            voltageUnit: voltageUnit
        });
    }
}

/**
 * 更新实时数据表格
 */
function updateRealtimeTable(data) {
    const tbody = document.getElementById('realtime-data');
    if (!tbody) return;
    
    // 按设备和参数分组，取最新值
    const latestData = {};
    data.forEach(item => {
        const key = `${item.device_id}_${item.register_name}`;
        if (!latestData[key] || new Date(item.timestamp) > new Date(latestData[key].timestamp)) {
            latestData[key] = item;
        }
    });
    
    // 生成表格行
    const rows = Object.values(latestData).map(item => `
        <tr>
            <td>${getDeviceName(item.device_id)}</td>
            <td>${getRegisterName(item.register_name)}</td>
            <td><strong>${formatNumber(item.value)}</strong></td>
            <td>${item.unit || ''}</td>
            <td>${formatDateTime(item.timestamp)}</td>
            <td><span class="badge bg-success">正常</span></td>
        </tr>
    `).join('');
    
    tbody.innerHTML = rows;
}

/**
 * 更新图表数据
 */
function updateCharts(data) {
    const now = new Date().toLocaleTimeString();
    
    // 更新温度数据
    data.forEach(item => {
        if (item.register_name.includes('temperature') || item.register_name.includes('temp')) {
            temperatureData.push({ time: now, value: item.value });
            if (temperatureData.length > MAX_DATA_POINTS) {
                temperatureData.shift();
            }
        }
        
        if (item.register_name.includes('pressure')) {
            pressureData.push({ time: now, value: item.value });
            if (pressureData.length > MAX_DATA_POINTS) {
                pressureData.shift();
            }
        }
    });
    
    // 更新温度图表
    if (temperatureChart) {
        temperatureChart.setOption({
            xAxis: { data: temperatureData.map(d => d.time) },
            series: [{ data: temperatureData.map(d => d.value) }]
        });
    }
    
    // 更新压力图表
    if (pressureChart) {
        pressureChart.setOption({
            xAxis: { data: pressureData.map(d => d.time) },
            series: [{ data: pressureData.map(d => d.value) }]
        });
    }
}

/**
 * 更新仪表盘统计信息
 */
function updateDashboardStats(stats) {
    // 设备统计
    if (stats.devices) {
        // 处理设备列表（可能是数组或对象）
        let deviceList = stats.devices;
        if (typeof deviceList === 'object' && !Array.isArray(deviceList)) {
            deviceList = Object.values(deviceList);
        }
        
        const total = deviceList.length;
        const online = deviceList.filter(d => d.connected).length;
        
        document.getElementById('device-count').textContent = total;
        document.getElementById('device-online').textContent = online;
        document.getElementById('device-offline').textContent = total - online;
        
        // 更新设备状态列表
        updateDeviceStatusList(deviceList);
    }
    
    // 数据采集统计
    if (stats.collector) {
        document.getElementById('data-count').textContent = stats.collector.total_collections || 0;
    }
    
    // 数据库统计
    if (stats.database) {
        document.getElementById('data-today').textContent = (stats.database.total_records || stats.database.realtime_records || 0).toLocaleString();
    }
    
    // 报警统计
    if (stats.alarms) {
        document.getElementById('alarm-count-card').textContent = stats.alarms.total_active_alarms || 0;
        document.getElementById('alarm-critical').textContent = stats.alarms.by_level?.critical || 0;
        document.getElementById('alarm-warning').textContent = stats.alarms.by_level?.warning || 0;
    }
    
    // 运行时间
    if (stats.uptime_seconds !== undefined) {
        document.getElementById('uptime').textContent = formatUptime(stats.uptime_seconds);
    }
    
    // 运行模式
    if (stats.simulation_mode !== undefined) {
        const modeEl = document.getElementById('run-mode');
        if (modeEl) {
            modeEl.textContent = stats.simulation_mode ? '模拟模式' : '真实设备';
            modeEl.className = `badge ${stats.simulation_mode ? 'bg-info' : 'bg-success'}`;
        }
    }
    
    // 更新导航栏报警数
    const navAlarmEl = document.getElementById('active-alarms');
    if (navAlarmEl && stats.alarms) {
        navAlarmEl.textContent = stats.alarms.total_active_alarms || 0;
    }
}

/**
 * 格式化运行时间
 */
function formatUptime(seconds) {
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    
    if (days > 0) {
        return `${days}天${hours}时${minutes}分`;
    } else if (hours > 0) {
        return `${hours}时${minutes}分${secs}秒`;
    } else if (minutes > 0) {
        return `${minutes}分${secs}秒`;
    } else {
        return `${secs}秒`;
    }
}

/**
 * 更新设备状态列表
 */
function updateDeviceStatusList(devices) {
    const container = document.getElementById('device-status-list');
    if (!container) return;
    
    container.innerHTML = devices.map(d => `
        <div class="d-flex justify-content-between align-items-center mb-2 p-2 border rounded">
            <div>
                <strong>${d.name || d.device_id}</strong>
                <br>
                <small class="text-muted">${d.description || ''}</small>
            </div>
            <span class="badge bg-${d.connected ? 'success' : 'danger'}">
                ${d.connected ? '在线' : '离线'}
            </span>
        </div>
    `).join('');
}

/**
 * 加载最新报警
 */
async function loadLatestAlarms() {
    try {
        const response = await fetch('/api/alarms?limit=5');
        const data = await response.json();
        const container = document.getElementById('latest-alarms');
        if (!container) return;
        
        if (data.alarms && data.alarms.length > 0) {
            container.innerHTML = data.alarms.map(a => `
                <div class="alert alert-${a.alarm_level === 'critical' ? 'danger' : 'warning'} py-2 mb-2">
                    <small>
                        <strong>${a.alarm_message}</strong><br>
                        ${a.device_id} | 值: ${a.actual_value?.toFixed(2) || '-'}
                    </small>
                </div>
            `).join('');
        } else {
            container.innerHTML = '<p class="text-muted text-center">暂无报警</p>';
        }
    } catch (error) {
        console.error('加载报警失败:', error);
    }
}

/**
 * 获取设备显示名称
 */
function getDeviceName(deviceId) {
    const names = {
        'temp_sensor_01': '温度传感器1号',
        'pressure_sensor_01': '压力传感器1号',
        'power_meter_01': '电力仪表1号'
    };
    return names[deviceId] || deviceId;
}

/**
 * 获取参数显示名称
 */
function getRegisterName(name) {
    const names = {
        'temperature': '温度',
        'humidity': '湿度',
        'pressure': '压力',
        'voltage': '电压',
        'current': '电流',
        'power': '功率',
        'energy': '电量',
        'status': '状态'
    };
    return names[name] || name;
}

/**
 * 格式化数字
 */
function formatNumber(value) {
    if (value === null || value === undefined) return '-';
    if (typeof value === 'number') {
        return value.toFixed(2);
    }
    return value;
}

/**
 * 格式化日期时间
 */
function formatDateTime(timestamp) {
    if (!timestamp) return '-';
    const date = new Date(timestamp);
    return date.toLocaleString('zh-CN', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });
}

/**
 * 更新温度图表时间范围
 */
function updateTemperatureChart() {
    // 重新加载数据
    loadRealtimeData();
}

/**
 * 更新压力图表时间范围
 */
function updatePressureChart() {
    // 重新加载数据
    loadRealtimeData();
}
