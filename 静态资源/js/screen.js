/**
 * 数据大屏 JavaScript
 * 从 Flask API 拉取数据，ECharts 渲染，WebSocket 实时更新
 */

// ========== XSS 安全转义 ==========
function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ========== 全局状态 ==========
let trendChart, oeeChart, energyChart, spcChart, healthChart, deviceStatusChart;
const dataBuffers = {};
const MAX_CHART_POINTS = 200;
let selectedDeviceId = null;
let deviceNameCache = {};
let alarmList = [];
let loadGeneration = 0;
let lastDeviceValues = {};

// ========== API 请求 ==========
async function apiFetch(url) {
    const token = localStorage.getItem('auth_token');
    const headers = token ? { 'Authorization': `Bearer ${token}` } : {};
    const resp = await fetch('/api' + url, { headers });
    if (resp.status === 401) {
        localStorage.removeItem('auth_token');
        window.location.href = '/login';
        return null;
    }
    if (!resp.ok) return null;
    return resp.json();
}

// ========== 初始化 ==========
document.addEventListener('DOMContentLoaded', () => {
    initClock();
    initCharts();
    loadDeviceList();
    loadData();
    setInterval(loadData, 3000);
});

// ========== 时钟 ==========
function initClock() {
    function tick() {
        const now = new Date();
        document.getElementById('header-time').textContent = now.toTimeString().slice(0, 8);
        document.getElementById('header-date').textContent = now.toLocaleDateString('zh-CN');
    }
    tick();
    setInterval(tick, 1000);
}

// ========== 图表初始化 ==========
function initCharts() {
    const darkTheme = {
        backgroundColor: 'transparent',
        textStyle: { color: '#8899aa' },
    };

    // 设备状态饼图
    deviceStatusChart = echarts.init(document.getElementById('device-status-chart'));
    deviceStatusChart.setOption({
        ...darkTheme,
        tooltip: { trigger: 'item' },
        series: [{
            type: 'pie',
            radius: ['40%', '70%'],
            center: ['50%', '50%'],
            itemStyle: { borderRadius: 4, borderColor: '#0a0e27', borderWidth: 2 },
            label: { show: true, color: '#8899aa', fontSize: 11 },
            data: [
                { value: 0, name: '在线', itemStyle: { color: '#2fb344' } },
                { value: 0, name: '离线', itemStyle: { color: '#d63939' } },
            ]
        }]
    });

    // OEE 仪表盘
    oeeChart = echarts.init(document.getElementById('oee-chart'));
    oeeChart.setOption({
        ...darkTheme,
        series: [{
            type: 'gauge',
            startAngle: 200, endAngle: -20,
            min: 0, max: 100,
            progress: { show: true, width: 16, itemStyle: { color: '#5dc2fe' } },
            pointer: { show: false },
            axisLine: { lineStyle: { width: 16, color: [[0.6, '#d63939'], [0.85, '#f59f00'], [1, '#2fb344']] } },
            axisTick: { show: false },
            splitLine: { show: false },
            axisLabel: { show: false },
            detail: {
                valueAnimation: true,
                formatter: '{value}%',
                fontSize: 28,
                fontFamily: 'Consolas',
                color: '#5dc2fe',
                offsetCenter: [0, '20%'],
            },
            title: { show: false },
            data: [{ value: 0 }]
        }]
    });

    // 能源趋势
    energyChart = echarts.init(document.getElementById('energy-chart'));
    energyChart.setOption({
        ...darkTheme,
        tooltip: { trigger: 'axis' },
        grid: { left: 45, right: 10, top: 10, bottom: 25 },
        xAxis: { type: 'category', data: [], axisLine: { lineStyle: { color: '#1a3040' } }, axisLabel: { fontSize: 10 } },
        yAxis: { type: 'value', axisLine: { show: false }, splitLine: { lineStyle: { color: '#1a3040' } }, axisLabel: { fontSize: 10 } },
        series: [{
            type: 'bar',
            data: [],
            itemStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: '#5dc2fe' }, { offset: 1, color: 'rgba(93,194,254,0.1)' }] } },
            barWidth: '60%',
        }]
    });

    // 趋势图
    trendChart = echarts.init(document.getElementById('trend-chart'));
    trendChart.setOption({
        ...darkTheme,
        tooltip: { trigger: 'axis' },
        legend: { top: 4, type: 'scroll', textStyle: { color: '#6b8aa8', fontSize: 11 } },
        grid: { left: 55, right: 15, top: 35, bottom: 25 },
        xAxis: { type: 'category', data: [], boundaryGap: false, axisLine: { lineStyle: { color: '#1a3040' } }, axisLabel: { fontSize: 10 } },
        yAxis: { type: 'value', axisLine: { show: false }, splitLine: { lineStyle: { color: '#1a3040' } }, axisLabel: { fontSize: 10 } },
        series: [],
    });

    // SPC 控制图
    spcChart = echarts.init(document.getElementById('spc-chart'));
    spcChart.setOption({
        ...darkTheme,
        tooltip: { trigger: 'axis' },
        grid: { left: 45, right: 10, top: 10, bottom: 25 },
        xAxis: { type: 'category', data: [], axisLine: { lineStyle: { color: '#1a3040' } }, axisLabel: { fontSize: 10 } },
        yAxis: { type: 'value', axisLine: { show: false }, splitLine: { lineStyle: { color: '#1a3040' } }, axisLabel: { fontSize: 10 } },
        series: [{
            type: 'line',
            data: [],
            smooth: true,
            symbol: 'none',
            lineStyle: { color: '#5dc2fe', width: 2 },
            areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: 'rgba(93,194,254,0.3)' }, { offset: 1, color: 'rgba(93,194,254,0)' }] } },
            markLine: {
                silent: true,
                lineStyle: { type: 'dashed' },
                data: [
                    { yAxis: 80, lineStyle: { color: '#d63939' }, label: { formatter: 'UCL', color: '#d63939', fontSize: 10 } },
                    { yAxis: 50, lineStyle: { color: '#6b8aa8' }, label: { formatter: 'CL', color: '#6b8aa8', fontSize: 10 } },
                    { yAxis: 20, lineStyle: { color: '#d63939' }, label: { formatter: 'LCL', color: '#d63939', fontSize: 10 } },
                ]
            }
        }]
    });

    // 健康度
    healthChart = echarts.init(document.getElementById('health-chart'));
    healthChart.setOption({
        ...darkTheme,
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
        grid: { left: 80, right: 15, top: 10, bottom: 5 },
        xAxis: { type: 'value', max: 100, axisLine: { show: false }, splitLine: { lineStyle: { color: '#1a3040' } }, axisLabel: { fontSize: 10 } },
        yAxis: { type: 'category', data: [], axisLine: { lineStyle: { color: '#1a3040' } }, axisLabel: { fontSize: 11, color: '#8899aa' } },
        series: [{
            type: 'bar',
            data: [],
            barWidth: '60%',
            itemStyle: {
                borderRadius: [0, 4, 4, 0],
                color: function(params) {
                    const v = params.value;
                    if (v >= 80) return '#2fb344';
                    if (v >= 60) return '#f59f00';
                    return '#d63939';
                }
            },
            label: { show: true, position: 'right', formatter: '{c}%', color: '#8899aa', fontSize: 11 },
        }]
    });

    // resize
    window.addEventListener('resize', () => {
        [trendChart, oeeChart, energyChart, spcChart, healthChart, deviceStatusChart].forEach(c => c && c.resize());
    });
}

// ========== 加载设备列表 ==========
async function loadDeviceList() {
    try {
        const resp = await apiFetch('/devices');
        if (resp.devices) {
            const select = document.getElementById('trend-device-select');
            const devices = Array.isArray(resp.devices) ? resp.devices : Object.values(resp.devices);
            devices.forEach(d => {
                const id = d.device_id || d.id;
                deviceNameCache[id] = d.name || id;
            });
            const ids = Object.keys(deviceNameCache);
            if (ids.length > 0) {
                selectedDeviceId = ids[0];
                select.innerHTML = ids.map(id =>
                    `<option value="${escapeHtml(id)}" ${id === selectedDeviceId ? 'selected' : ''}>${escapeHtml(deviceNameCache[id])}</option>`
                ).join('');
                select.addEventListener('change', function() {
                    selectedDeviceId = this.value;
                    Object.keys(dataBuffers).forEach(k => delete dataBuffers[k]);
                    Object.keys(lastDeviceValues).forEach(k => delete lastDeviceValues[k]);
                    if (window.socket) {
                        window.socket.emit('subscribe', {device_id: selectedDeviceId});
                    }
                    trendChart.clear();
                });
            }
        }
    } catch (e) { console.warn('loadDeviceList:', e); }
}

// ========== 数据加载（防抖 + rAF 批处理） ==========
let loadDataInProgress = false;
let pendingChartData = null;
let chartRafScheduled = false;

function scheduleChartUpdate(data) {
    pendingChartData = data;
    if (!chartRafScheduled) {
        chartRafScheduled = true;
        requestAnimationFrame(() => {
            if (pendingChartData) {
                updateTrendChart(pendingChartData);
                updateEnergyChart(pendingChartData);
                updateSPCChart(pendingChartData);
                pendingChartData = null;
            }
            chartRafScheduled = false;
        });
    }
}

async function loadData() {
    if (loadDataInProgress) return;
    loadDataInProgress = true;
    const gen = ++loadGeneration;
    try {
        // 系统状态
        const status = await apiFetch('/system/status');
        if (gen !== loadGeneration) return;
        updateKPI(status);
        updateDeviceStatus(status);
        updateHealth(status);

        // 实时数据
        const data = await apiFetch('/data/realtime?limit=5000');
        if (gen !== loadGeneration) return;
        if (data && data.data && data.data.length > 0) {
            // Populate lastDeviceValues cache from API data
            data.data.forEach(item => {
                if (item.device_id && item.register_name && item.value != null) {
                    const key = `${item.device_id}:${item.register_name}`;
                    lastDeviceValues[key] = typeof item.value === 'number'
                        ? item.value.toFixed(1) : String(item.value);
                }
            });
            scheduleChartUpdate(data.data);
        }

        // 报警
        const alarms = await apiFetch('/alarms?limit=50');
        if (gen !== loadGeneration) return;
        if (alarms && alarms.alarms) {
            alarmList = alarms.alarms.slice(0, 50);
            updateAlarmList(alarmList);
        }

    } catch (e) { console.error('loadData:', e); }
    finally {
        loadDataInProgress = false;
    }
}

// ========== KPI 更新 ==========
function updateKPI(stats) {
    if (stats.devices) {
        let devices = stats.devices;
        if (typeof devices === 'object' && !Array.isArray(devices)) devices = Object.values(devices);
        setText('kpi-total', devices.length);
        setText('kpi-online', devices.filter(d => d.connected).length);
        setText('kpi-offline', devices.filter(d => !d.connected).length);
    }
    if (stats.alarms) {
        setText('kpi-alarm', stats.alarms.total_active_alarms || 0);
        setText('alarm-count-badge', stats.alarms.total_active_alarms || 0);
    }
    if (stats.collector) setText('kpi-collections', (stats.collector.total_collections || 0).toLocaleString());
    if (stats.uptime_seconds !== undefined) setText('kpi-uptime', formatUptime(stats.uptime_seconds));

    // 采集频率
    if (stats.collector && stats.uptime_seconds) {
        const rate = Math.floor((stats.collector.total_collections || 0) / Math.max(stats.uptime_seconds / 60, 1));
        setText('kpi-rate', rate);
    }

    // 数据质量
    if (stats.collector) {
        const total = (stats.collector.successful_collections || 0) + (stats.collector.failed_collections || 0);
        const quality = total > 0 ? Math.round((stats.collector.successful_collections || 0) / total * 100) : 100;
        setText('kpi-quality', quality + '%');
    }
}

// ========== 设备状态饼图 ==========
function updateDeviceStatus(stats) {
    if (!stats.devices || !deviceStatusChart) return;
    let devices = stats.devices;
    if (typeof devices === 'object' && !Array.isArray(devices)) devices = Object.values(devices);
    const online = devices.filter(d => d.connected).length;
    const offline = devices.length - online;
    deviceStatusChart.setOption({ series: [{ data: [
        { value: online, name: '在线' },
        { value: offline, name: '离线' },
    ] }] });
}

// ========== 健康度 ==========
function updateHealth(stats) {
    if (!stats.devices || !healthChart) return;
    let devices = stats.devices;
    if (typeof devices === 'object' && !Array.isArray(devices)) devices = Object.values(devices);
    const names = devices.map(d => (d.name || d.device_id || '').substring(0, 8));
    const scores = devices.map(d => d.connected ? 100 : 0);
    healthChart.setOption({
        yAxis: { data: names },
        series: [{ data: scores }],
    });
}

// ========== 趋势图 ==========
function updateTrendChart(data) {
    if (!trendChart || !selectedDeviceId) return;
    const now = new Date().toTimeString().slice(0, 8) + '.' + String(new Date().getMilliseconds()).padStart(3, '0');

    data.forEach(item => {
        if (item.device_id !== selectedDeviceId) return;
        if (item.value === null || item.value === undefined) return;
        const key = item.register_name;
        if (!dataBuffers[key]) dataBuffers[key] = [];
        dataBuffers[key].push({ time: now, value: parseFloat(item.value) });
        if (dataBuffers[key].length > MAX_CHART_POINTS) dataBuffers[key].shift();
    });

    // Prevent memory leak: limit total buffer keys
    const allBufferKeys = Object.keys(dataBuffers);
    if (allBufferKeys.length > 100) {
        allBufferKeys.slice(0, allBufferKeys.length - 100).forEach(k => delete dataBuffers[k]);
    }

    const keys = Object.keys(dataBuffers);
    if (keys.length === 0) return;

    const timeSet = new Set();
    keys.forEach(k => dataBuffers[k].forEach(d => timeSet.add(d.time)));
    const times = Array.from(timeSet).sort().slice(-MAX_CHART_POINTS);

    const series = keys.map(key => {
        const map = {};
        dataBuffers[key].forEach(d => { map[d.time] = d.value; });
        return {
            name: getLabel(key),
            type: 'line',
            smooth: true,
            symbol: 'none',
            lineStyle: { width: 1.5 },
            data: times.map(t => map[t] ?? null),
        };
    });

    trendChart.setOption({
        legend: { data: series.map(s => s.name) },
        xAxis: { data: times },
        series,
    });
}

// ========== 能源柱状图 ==========
function updateEnergyChart(data) {
    if (!energyChart) return;
    const powerData = {};
    data.forEach(item => {
        if (item.register_name && item.register_name.toLowerCase().includes('power')) {
            const name = deviceNameCache[item.device_id] || item.device_id;
            powerData[name] = (powerData[name] || 0) + parseFloat(item.value || 0);
        }
    });
    const names = Object.keys(powerData).slice(0, 8);
    const values = names.map(n => powerData[n].toFixed(1));
    energyChart.setOption({
        xAxis: { data: names },
        series: [{ data: values }],
    });
}

// ========== SPC 图 ==========
function updateSPCChart(data) {
    if (!spcChart) return;
    // 取第一个温度变量做 SPC 演示
    const tempData = data.filter(d => d.register_name && d.register_name.toLowerCase().includes('temperature'));
    if (tempData.length === 0) return;
    const values = tempData.slice(0, 30).map(d => parseFloat(d.value).toFixed(1));
    spcChart.setOption({ series: [{ data: values }] });
}

// ========== 报警列表 ==========
function updateAlarmList(alarms) {
    const container = document.getElementById('alarm-list');
    if (!alarms || alarms.length === 0) {
        container.innerHTML = '<div class="alarm-empty">暂无报警</div>';
        return;
    }
    container.innerHTML = alarms.slice(0, 10).map(a => {
        const level = a.alarm_level === 'critical' ? 'critical' : 'warning';
        const time = new Date(a.timestamp).toLocaleTimeString('zh-CN');
        return `<div class="alarm-item ${level}">
            <div class="alarm-time">${escapeHtml(time)}</div>
            <div class="alarm-msg">${escapeHtml(a.alarm_message || a.alarm_id)} — ${escapeHtml(a.device_id)}</div>
        </div>`;
    }).join('');
}

function addAlarm(data) {
    alarmList.unshift(data);
    if (alarmList.length > 50) alarmList.length = 50;
    updateAlarmList(alarmList);
}

// ========== 工具函数 ==========
function setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
}

function formatUptime(s) {
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d}天${h}时`;
    if (h > 0) return `${h}时${m}分`;
    return `${m}分${Math.floor(s % 60)}秒`;
}

function getLabel(name) {
    const map = {
        'temperature': '温度', 'boiler_temperature': '锅炉温度',
        'pressure': '压力', 'boiler_pressure': '锅炉压力',
        'flow': '流量', 'steam_flow': '蒸汽流量',
        'level': '液位', 'voltage': '电压', 'current': '电流',
        'power': '功率', 'vibration': '振动', 'ph': 'pH值',
    };
    if (map[name]) return map[name];
    const lower = name.toLowerCase();
    for (const [k, v] of Object.entries(map)) {
        if (lower.includes(k)) return v;
    }
    return name;
}

// ========== WebSocket 实时更新 ==========
document.addEventListener('DOMContentLoaded', () => {
    function attachSocketHandlers() {
        const sk = window.socket;
        if (!sk) return;
        sk.on('data_update', (data) => {
            if (!data) return;
            Object.entries(data).forEach(([regName, info]) => {
                if (!info || typeof info !== 'object') return;
                const devId = info.device_id;
                const val = info.value;
                if (!devId || val == null) return;
                const key = `${devId}:${regName}`;
                const formatted = typeof val === 'number' ? val.toFixed(1) : String(val);
                lastDeviceValues[key] = formatted;
            });
        });
        sk.on('alarm', () => loadData());
        // Subscribe to all visible devices on connect
        sk.on('connect', () => {
            console.log('WS connected (screen)');
            const select = document.getElementById('trend-device-select');
            if (select) {
                Array.from(select.options).forEach(opt => {
                    if (opt.value) sk.emit('subscribe', {device_id: opt.value});
                });
            } else if (selectedDeviceId) {
                sk.emit('subscribe', {device_id: selectedDeviceId});
            }
        });
    }
    if (window.socket) {
        attachSocketHandlers();
    } else {
        const timer = setInterval(() => {
            if (window.socket) { clearInterval(timer); attachSocketHandlers(); }
        }, 200);
    }
});
