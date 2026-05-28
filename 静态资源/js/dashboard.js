/**
 * 仪表盘页面JavaScript — 重写版
 * 动态图表、真实数据、设备选择
 */

// 图表实例
let trendChart = null;
const gaugeCharts = {};

// 数据缓存：按变量分组
const dataBuffers = {};  // { variable_key: [{time, value}] }
const MAX_DATA_POINTS = 120;

// 设备名缓存（从API动态获取）
let deviceNameCache = {};

// 当前选中的设备（趋势图只显示该设备的变量）
let selectedDeviceId = null;

/**
 * 初始化
 */
document.addEventListener('DOMContentLoaded', function() {
    initTrendChart();
    loadDeviceNames();
    loadRealtimeData();
    setInterval(loadRealtimeData, 3000);

    // 窗口 resize
    window.addEventListener('resize', () => {
        if (trendChart) trendChart.resize();
        Object.values(gaugeCharts).forEach(g => g.resize());
    });
});

/**
 * 从API加载设备名映射，填充设备选择器
 */
async function loadDeviceNames() {
    try {
        const resp = await apiRequest('/devices');
        if (resp && resp.devices) {
            const deviceIds = [];
            resp.devices.forEach(d => {
                const id = d.device_id || d.id;
                deviceNameCache[id] = d.name || id;
                deviceIds.push(id);
            });

            // 默认选中第一个设备
            if (!selectedDeviceId && deviceIds.length > 0) {
                selectedDeviceId = deviceIds[0];
            }

            // 填充设备选择下拉框
            populateDeviceSelector(deviceIds);
        }
    } catch (e) {
        console.warn('加载设备名失败，使用默认');
    }
}

/**
 * 填充设备选择下拉框
 */
function populateDeviceSelector(deviceIds) {
    const select = document.getElementById('trend-device-select');
    if (!select) return;

    select.innerHTML = deviceIds.map(id =>
        `<option value="${id}" ${id === selectedDeviceId ? 'selected' : ''}>${deviceNameCache[id] || id}</option>`
    ).join('');

    select.addEventListener('change', function() {
        selectedDeviceId = this.value;
        // 清空缓存，重新开始收集该设备的数据
        Object.keys(dataBuffers).forEach(key => {
            if (!key.startsWith(selectedDeviceId + ':')) {
                delete dataBuffers[key];
            }
        });
        if (trendChart) {
            trendChart.setOption({ series: [], legend: { data: [] } });
        }
    });
}

/**
 * 获取设备显示名
 */
function getDeviceName(deviceId) {
    return deviceNameCache[deviceId] || deviceId;
}

/**
 * 获取寄存器中文名
 */
function getRegisterLabel(name) {
    const map = {
        'temperature': '温度', 'boiler_temperature': '锅炉温度',
        'heat_exchanger_temperature': '换热器温度', 'flue_gas_temperature': '排烟温度',
        'extrusion_temperature': '挤出温度', 'mold_temperature': '模具温度',
        'pressure': '压力', 'boiler_pressure': '锅炉压力',
        'injection_pressure': '注射压力',
        'flow': '流量', 'steam_flow': '蒸汽流量', 'cooling_water_flow': '冷却水流量',
        'level': '液位', 'feed_water_level': '给水液位', 'hopper_level': '料斗液位',
        'voltage': '电压', 'current': '电流', 'power': '功率',
        'vibration': '振动', 'ph': 'pH值',
        'oxygen_content': '含氧量', 'boiler_status': '锅炉状态',
        'humidity': '湿度', 'energy': '电量',
    };
    // 先精确匹配
    if (map[name]) return map[name];
    // 模糊匹配
    const lower = name.toLowerCase();
    for (const [key, val] of Object.entries(map)) {
        if (lower.includes(key)) return val;
    }
    return name;
}

/**
 * 获取变量单位
 */
function getRegisterUnit(name) {
    const lower = name.toLowerCase();
    if (lower.includes('temperature') || lower.includes('temp')) return '°C';
    if (lower.includes('pressure')) return 'MPa';
    if (lower.includes('flow')) return 't/h';
    if (lower.includes('level')) return 'mm';
    if (lower.includes('voltage')) return 'V';
    if (lower.includes('current')) return 'A';
    if (lower.includes('power')) return 'kW';
    if (lower.includes('energy')) return 'kWh';
    if (lower.includes('vibration')) return 'mm/s';
    if (lower.includes('ph')) return '';
    if (lower.includes('oxygen')) return '%';
    if (lower.includes('humidity')) return '%';
    return '';
}

/**
 * 初始化趋势图
 */
function initTrendChart() {
    const dom = document.getElementById('trend-chart');
    if (!dom) return;

    trendChart = echarts.init(dom);
    trendChart.setOption({
        tooltip: { trigger: 'axis' },
        legend: { top: 4, type: 'scroll' },
        grid: { left: 60, right: 20, top: 40, bottom: 30 },
        xAxis: { type: 'category', data: [], boundaryGap: false },
        yAxis: { type: 'value' },
        series: [],
    });
}

/**
 * 主数据加载
 */
async function loadRealtimeData() {
    try {
        const data = await apiRequest('/data/realtime?limit=5000');

        if (data.data && data.data.length > 0) {
            updateRealtimeTable(data.data);
            updateTrendChart(data.data);
            updateGaugeSection(data.data);

            const statusEl = document.getElementById('data-status');
            if (statusEl) {
                statusEl.textContent = '实时更新中';
                statusEl.className = 'badge bg-success';
            }
            const lastUpdate = document.getElementById('last-update');
            if (lastUpdate) lastUpdate.textContent = new Date().toLocaleTimeString('zh-CN');
        }

        // 系统统计
        const statusData = await apiRequest('/system/status');
        updateDashboardStats(statusData);

        // 设备热力图
        if (statusData.devices) {
            let deviceList = statusData.devices;
            if (typeof deviceList === 'object' && !Array.isArray(deviceList)) {
                deviceList = Object.values(deviceList);
            }
            updateDeviceHeatmap(deviceList);
        }

        updateHealthMetrics(statusData);

    } catch (error) {
        console.error('加载数据失败:', error);
        const statusEl = document.getElementById('data-status');
        if (statusEl) {
            statusEl.textContent = '连接失败';
            statusEl.className = 'badge bg-danger';
        }
    }
}

/**
 * 更新趋势图 — 只显示选中设备的变量，最多 MAX_DATA_POINTS 个点
 */
function updateTrendChart(data) {
    if (!trendChart || !selectedDeviceId) return;

    const now = new Date();
    const timeLabel = now.toTimeString().slice(0, 8);  // HH:MM:SS

    // 只缓存选中设备的数据
    data.forEach(item => {
        if (item.device_id !== selectedDeviceId) return;
        if (item.value === null || item.value === undefined) return;
        const key = item.register_name;
        if (!dataBuffers[key]) dataBuffers[key] = [];
        dataBuffers[key].push({ time: timeLabel, value: parseFloat(item.value) });
        if (dataBuffers[key].length > MAX_DATA_POINTS) dataBuffers[key].shift();
    });

    const keys = Object.keys(dataBuffers);
    if (keys.length === 0) return;

    // 统一时间轴：取所有变量的时间并集
    const timeSet = new Set();
    keys.forEach(k => dataBuffers[k].forEach(d => timeSet.add(d.time)));
    const times = Array.from(timeSet).sort().slice(-MAX_DATA_POINTS);

    const series = keys.map(key => {
        const label = getRegisterLabel(key);
        const unit = getRegisterUnit(key);
        const map = {};
        dataBuffers[key].forEach(d => { map[d.time] = d.value; });

        return {
            name: unit ? `${label} (${unit})` : label,
            type: 'line',
            smooth: true,
            symbol: 'none',
            lineStyle: { width: 2 },
            data: times.map(t => map[t] ?? null),
        };
    });

    trendChart.setOption({
        legend: { data: series.map(s => s.name) },
        xAxis: { data: times },
        series: series,
    }, true);  // true = notMerge，清除旧数据
}

/**
 * 更新实时数据表格
 */
function updateRealtimeTable(data) {
    const tbody = document.getElementById('realtime-data');
    if (!tbody) return;

    // 按设备+寄存器去重取最新
    const latest = {};
    data.forEach(item => {
        const key = `${item.device_id}_${item.register_name}`;
        if (!latest[key] || new Date(item.timestamp) > new Date(latest[key].timestamp)) {
            latest[key] = item;
        }
    });

    tbody.innerHTML = Object.values(latest).map(item => {
        const value = typeof item.value === 'number' ? item.value.toFixed(2) : item.value;
        const unit = item.unit || getRegisterUnit(item.register_name);
        const time = new Date(item.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });

        // 状态判断
        let statusBadge = '<span class="badge bg-success">正常</span>';
        if (item.value !== null && item.value !== undefined) {
            // 根据变量类型判断异常
            const v = parseFloat(item.value);
            const name = item.register_name.toLowerCase();
            if (name.includes('temperature') && v > 150) statusBadge = '<span class="badge bg-danger">超温</span>';
            else if (name.includes('temperature') && v > 120) statusBadge = '<span class="badge bg-warning">偏高</span>';
            else if (name.includes('pressure') && v > 1.5) statusBadge = '<span class="badge bg-danger">超压</span>';
            else if (name.includes('pressure') && v > 1.2) statusBadge = '<span class="badge bg-warning">偏高</span>';
        }

        return `<tr>
            <td>${getDeviceName(item.device_id)}</td>
            <td>${getRegisterLabel(item.register_name)}</td>
            <td><strong class="data-value">${value}</strong></td>
            <td>${unit}</td>
            <td>${time}</td>
            <td>${statusBadge}</td>
        </tr>`;
    }).join('');
}

/**
 * 更新仪表盘区域 — 动态生成，基于实际数据
 */
function updateGaugeSection(data) {
    const container = document.getElementById('gauge-section');
    if (!container) return;

    // 选取前3个数值型变量做仪表盘
    const numericItems = data.filter(item => typeof item.value === 'number').slice(0, 3);
    if (numericItems.length === 0) return;

    // 确保容器有3个gauge div
    if (container.children.length !== numericItems.length) {
        container.innerHTML = numericItems.map((item, i) => `
            <div class="col-md-4">
                <div class="gauge-card">
                    <h6 class="mb-2"><i class="bi bi-gauge text-info"></i> ${getRegisterLabel(item.register_name)}</h6>
                    <div class="gauge-container" id="gauge-${i}"></div>
                    <div class="gauge-value" id="gauge-val-${i}">--</div>
                    <div class="gauge-label">${getDeviceName(item.device_id)} · ${getRegisterUnit(item.register_name)}</div>
                </div>
            </div>
        `).join('');
    }

    numericItems.forEach((item, i) => {
        const dom = document.getElementById(`gauge-${i}`);
        if (!dom) return;

        if (!gaugeCharts[i]) {
            gaugeCharts[i] = echarts.init(dom);
        }

        const val = item.value;
        const unit = getRegisterUnit(item.register_name);
        // 动态范围：值的 ±50%，最小10
        const absVal = Math.abs(val) || 1;
        const max = Math.ceil(absVal * 1.5 / 10) * 10 || 100;

        gaugeCharts[i].setOption({
            series: [{
                type: 'gauge',
                startAngle: 200,
                endAngle: -20,
                min: 0,
                max: max,
                progress: { show: true, width: 14 },
                pointer: { show: false },
                axisLine: { lineStyle: { width: 14, color: [[0.6, '#00e676'], [0.8, '#ffd600'], [1, '#ff5252']] } },
                axisTick: { show: false },
                splitLine: { show: false },
                axisLabel: { show: false },
                detail: { show: false },
                data: [{ value: val }],
            }]
        });

        const valEl = document.getElementById(`gauge-val-${i}`);
        if (valEl) valEl.textContent = val.toFixed(1) + ' ' + unit;
    });
}

/**
 * 更新设备热力图
 */
function updateDeviceHeatmap(devices) {
    const container = document.getElementById('device-heatmap');
    if (!devices || devices.length === 0) {
        container.innerHTML = '<div class="col-12 text-center text-muted py-3">暂无设备</div>';
        return;
    }

    container.innerHTML = devices.map(device => {
        const isOnline = device.connected;
        const hasWarning = device.status === 'warning' || device.status === 'fault';
        let bgColor = isOnline ? (hasWarning ? '#ff9100' : '#00e676') : '#ff5252';
        const name = (device.name || device.device_id || '').substring(0, 8);

        return `<div class="col-6">
            <div class="heatmap-cell" style="background: ${bgColor};" title="${device.name || device.device_id}">
                <div>
                    <div>${name}</div>
                    <div style="font-size:0.65rem;opacity:0.8">${isOnline ? (hasWarning ? '告警' : '在线') : '离线'}</div>
                </div>
            </div>
        </div>`;
    }).join('');
}

/**
 * 更新系统健康度（真实数据）
 */
function updateHealthMetrics(stats) {
    if (!stats) return;

    // 数据库
    if (stats.database) {
        const dbSize = stats.database.database_size_mb || 0;
        const el = document.getElementById('db-size');
        if (el) el.textContent = dbSize.toFixed(1) + ' MB';
        const bar = document.getElementById('db-bar');
        if (bar) bar.style.width = Math.min(dbSize, 100) + '%';

        const totalEl = document.getElementById('total-records');
        if (totalEl) totalEl.textContent = (stats.database.total_records || stats.database.realtime_records || 0).toLocaleString();
    }

    // 采集
    if (stats.collector) {
        const rate = Math.floor((stats.collector.total_collections || 0) / Math.max((stats.uptime_seconds || 1) / 60, 1));
        const rateEl = document.getElementById('collection-rate');
        if (rateEl) rateEl.textContent = rate;
    }
}

/**
 * 更新仪表盘统计
 */
function updateDashboardStats(stats) {
    if (!stats) return;

    // 设备
    if (stats.devices) {
        let deviceList = stats.devices;
        if (typeof deviceList === 'object' && !Array.isArray(deviceList)) {
            deviceList = Object.values(deviceList);
        }
        const total = deviceList.length;
        const online = deviceList.filter(d => d.connected).length;

        setText('device-count', total);
        setText('device-online', online);
        setText('device-offline', total - online);
    }

    // 数据
    if (stats.collector) setText('data-count', stats.collector.total_collections || 0);
    if (stats.database) setText('data-today', (stats.database.total_records || stats.database.realtime_records || 0).toLocaleString());

    // 报警
    if (stats.alarms) {
        setText('alarm-count-card', stats.alarms.total_active_alarms || 0);
        setText('alarm-critical', stats.alarms.by_level?.critical || 0);
        setText('alarm-warning', stats.alarms.by_level?.warning || 0);

        const navEl = document.getElementById('active-alarms');
        if (navEl) navEl.textContent = stats.alarms.total_active_alarms || 0;
    }

    // 运行时间
    if (stats.uptime_seconds !== undefined) setText('uptime', formatUptime(stats.uptime_seconds));

    // 运行模式
    if (stats.simulation_mode !== undefined) {
        const el = document.getElementById('run-mode');
        if (el) {
            el.textContent = stats.simulation_mode ? '模拟模式' : '真实设备';
            el.className = `badge ${stats.simulation_mode ? 'bg-info' : 'bg-success'}`;
        }
    }
}

/**
 * 格式化运行时间
 */
function formatUptime(seconds) {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (d > 0) return `${d}天${h}时${m}分`;
    if (h > 0) return `${h}时${m}分`;
    return `${m}分${Math.floor(seconds % 60)}秒`;
}

/**
 * 格式化数字
 */
function formatNumber(v) {
    if (v === null || v === undefined) return '-';
    return typeof v === 'number' ? v.toFixed(2) : v;
}

/**
 * setText helper
 */
function setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
}
