/**
 * 数据大屏 JavaScript
 * 从 Flask API 拉取数据，ECharts 渲染，WebSocket 实时更新
 *
 * 视觉约定（与 design-tokens.css 对齐）：
 *   - 所有图表颜色 / 字体 / 坐标轴颜色都通过 CSS 变量读取，不写死色值
 *     → 深色底由 <html data-theme="dark"> 提供，令牌自动给出深色值
 *   - 图表字号 ≥12px（大屏远距离可读），关键数值用 36px 等宽数字
 *   - 报警等级只用 .level-bar--* 色条表达，不做整行 opacity 闪烁
 */

// ========== XSS 安全转义 ==========
function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ========== 设计令牌读取 ==========
function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const CHART_FONT_SIZE = 12;   // 坐标轴 / 提示框下限
const LABEL_FONT_SIZE = 14;   // 大屏上的数据标签
const BIG_FONT_SIZE = 36;     // 关键数值（远距离可读下限）

function readTheme() {
    return {
        fontSans: cssVar('--font-sans'),
        fontMono: cssVar('--font-mono'),
        textPrimary: cssVar('--text-primary'),
        textMuted: cssVar('--text-muted'),
        surface: cssVar('--bg-surface'),
        grid: cssVar('--chart-grid'),
        axisText: cssVar('--chart-axis-text'),
        labelText: cssVar('--chart-label-text'),
        tooltipBg: cssVar('--chart-tooltip-bg'),
        tooltipText: cssVar('--chart-tooltip-text'),
        success: cssVar('--color-success'),
        warning: cssVar('--color-warning'),
        danger: cssVar('--color-danger'),
        offline: cssVar('--color-offline'),
        info: cssVar('--color-info'),
        brand: cssVar('--color-brand'),
        palette: [1, 2, 3, 4, 5, 6, 7, 8].map(i => cssVar('--chart-' + i)),
    };
}

// 统一的坐标轴/提示框/文字样式
function axisStyle(t) {
    return {
        axisLine: { lineStyle: { color: t.grid } },
        axisTick: { show: false },
        axisLabel: { color: t.axisText, fontSize: CHART_FONT_SIZE, fontFamily: t.fontSans },
    };
}

function tooltipStyle(t, extra) {
    return Object.assign({
        backgroundColor: t.tooltipBg,
        borderWidth: 0,
        textStyle: { color: t.tooltipText, fontSize: CHART_FONT_SIZE, fontFamily: t.fontSans },
    }, extra || {});
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
    const t = readTheme();

    // 设备状态环图（在线 / 离线）
    deviceStatusChart = echarts.init(document.getElementById('device-status-chart'));
    deviceStatusChart.setOption({
        backgroundColor: 'transparent',
        tooltip: tooltipStyle(t, { trigger: 'item' }),
        legend: {
            bottom: 0,
            itemWidth: 12,
            itemHeight: 12,
            textStyle: { color: t.labelText, fontSize: CHART_FONT_SIZE, fontFamily: t.fontSans },
        },
        series: [{
            type: 'pie',
            radius: ['48%', '68%'],
            center: ['50%', '44%'],
            itemStyle: { borderColor: t.surface, borderWidth: 2 },
            label: {
                show: true,
                color: t.labelText,
                fontSize: LABEL_FONT_SIZE,
                fontFamily: t.fontMono,
                formatter: '{c}',
            },
            labelLine: { lineStyle: { color: t.grid } },
            data: [
                { value: 0, name: '在线', itemStyle: { color: t.success } },
                { value: 0, name: '离线', itemStyle: { color: t.offline } },
            ],
        }],
    });

    // OEE 综合效率：分段色量表盘 + 36px 等宽数值
    oeeChart = echarts.init(document.getElementById('oee-chart'));
    oeeChart.setOption({
        backgroundColor: 'transparent',
        tooltip: { show: false },
        series: [{
            type: 'gauge',
            startAngle: 205,
            endAngle: -25,
            min: 0,
            max: 100,
            radius: '92%',
            center: ['50%', '60%'],
            pointer: { show: false },
            axisLine: {
                lineStyle: {
                    width: 18,
                    color: [[0.6, t.danger], [0.85, t.warning], [1, t.success]],
                },
            },
            axisTick: { show: false },
            splitLine: { show: false },
            axisLabel: { show: false },
            progress: { show: false },
            detail: {
                valueAnimation: false,
                formatter: '{value}%',
                fontSize: BIG_FONT_SIZE,
                fontFamily: t.fontMono,
                color: t.textPrimary,
                offsetCenter: [0, '0%'],
            },
            title: { show: true, offsetCenter: [0, '40%'], color: t.textMuted, fontSize: CHART_FONT_SIZE, fontFamily: t.fontSans },
            data: [{ value: 0, name: '综合效率' }],
        }],
    });

    // 能源功率分布（横向条形，便于按设备比较）
    energyChart = echarts.init(document.getElementById('energy-chart'));
    energyChart.setOption({
        backgroundColor: 'transparent',
        tooltip: tooltipStyle(t, { trigger: 'axis', axisPointer: { type: 'shadow' } }),
        grid: { left: 8, right: 56, top: 8, bottom: 8, containLabel: true },
        xAxis: {
            type: 'value',
            axisLine: { show: false },
            axisTick: { show: false },
            splitLine: { lineStyle: { color: t.grid } },
            axisLabel: { color: t.axisText, fontSize: CHART_FONT_SIZE, fontFamily: t.fontSans },
        },
        yAxis: Object.assign(axisStyle(t), { type: 'category', data: [] }),
        series: [{
            type: 'bar',
            data: [],
            barWidth: 14,
            itemStyle: { color: t.palette[0], borderRadius: [0, 3, 3, 0] },
            label: {
                show: true,
                position: 'right',
                color: t.labelText,
                fontSize: LABEL_FONT_SIZE,
                fontFamily: t.fontMono,
            },
        }],
    });

    // 实时数据趋势
    trendChart = echarts.init(document.getElementById('trend-chart'));
    trendChart.setOption({
        backgroundColor: 'transparent',
        color: t.palette,
        tooltip: tooltipStyle(t, { trigger: 'axis' }),
        legend: {
            top: 0,
            type: 'scroll',
            itemWidth: 14,
            itemHeight: 8,
            textStyle: { color: t.labelText, fontSize: CHART_FONT_SIZE, fontFamily: t.fontSans },
        },
        grid: { left: 8, right: 20, top: 36, bottom: 8, containLabel: true },
        xAxis: Object.assign(axisStyle(t), { type: 'category', data: [], boundaryGap: false }),
        yAxis: Object.assign(axisStyle(t), {
            type: 'value',
            axisLine: { show: false },
            splitLine: { lineStyle: { color: t.grid } },
        }),
        series: [],
    });

    // SPC 控制图：参考线来自面板内实际数据计算，不预设常数
    spcChart = echarts.init(document.getElementById('spc-chart'));
    spcChart.setOption({
        backgroundColor: 'transparent',
        color: t.palette,
        tooltip: tooltipStyle(t, { trigger: 'axis' }),
        grid: { left: 8, right: 46, top: 12, bottom: 8, containLabel: true },
        xAxis: Object.assign(axisStyle(t), { type: 'category', data: [] }),
        yAxis: Object.assign(axisStyle(t), {
            type: 'value',
            scale: true,
            axisLine: { show: false },
            splitLine: { lineStyle: { color: t.grid } },
        }),
        series: [{
            type: 'line',
            data: [],
            smooth: false,
            symbol: 'circle',
            symbolSize: 5,
            lineStyle: { color: t.palette[0], width: 2 },
            itemStyle: { color: t.palette[0] },
        }],
    });

    // 设备健康度（横向条形，按分值排序后便于比较）
    healthChart = echarts.init(document.getElementById('health-chart'));
    healthChart.setOption({
        backgroundColor: 'transparent',
        tooltip: tooltipStyle(t, { trigger: 'axis', axisPointer: { type: 'shadow' } }),
        grid: { left: 8, right: 56, top: 8, bottom: 8, containLabel: true },
        xAxis: Object.assign(axisStyle(t), {
            type: 'value',
            max: 100,
            axisLine: { show: false },
            splitLine: { lineStyle: { color: t.grid } },
        }),
        yAxis: Object.assign(axisStyle(t), { type: 'category', data: [], inverse: true }),
        series: [{
            type: 'bar',
            data: [],
            barWidth: 14,
            itemStyle: { borderRadius: [0, 3, 3, 0] },
            label: {
                show: true,
                position: 'right',
                formatter: '{c}',
                color: t.labelText,
                fontSize: LABEL_FONT_SIZE,
                fontFamily: t.fontMono,
            },
        }],
    });

    window.addEventListener('resize', resizeAllCharts);
    // 栅格/字体布局落定后再校准一次，避免画布被按 0 尺寸初始化
    requestAnimationFrame(resizeAllCharts);
}

function resizeAllCharts() {
    [trendChart, oeeChart, energyChart, spcChart, healthChart, deviceStatusChart]
        .forEach(c => { if (c) { try { c.resize(); } catch (e) { /* 容器暂不可见时忽略 */ } } });
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
                    // 只清空系列，保留令牌化的坐标轴/图例样式
                    if (trendChart) {
                        trendChart.setOption({ series: [], legend: { data: [] }, xAxis: { data: [] } }, { replaceMerge: 'series' });
                    }
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

// ========== 设备状态环图 ==========
function updateDeviceStatus(stats) {
    if (!stats.devices || !deviceStatusChart) return;
    const t = readTheme();
    let devices = stats.devices;
    if (typeof devices === 'object' && !Array.isArray(devices)) devices = Object.values(devices);
    const online = devices.filter(d => d.connected).length;
    const offline = devices.length - online;
    deviceStatusChart.setOption({ series: [{ data: [
        { value: online, name: '在线', itemStyle: { color: t.success } },
        { value: offline, name: '离线', itemStyle: { color: t.offline } },
    ] }] });
}

// ========== 健康度 ==========
function updateHealth(stats) {
    if (!stats.devices || !healthChart) return;
    const t = readTheme();
    let devices = stats.devices;
    if (typeof devices === 'object' && !Array.isArray(devices)) devices = Object.values(devices);
    // 分高者排在上方，横向条形便于跨设备比较
    const rows = devices
        .map(d => ({
            name: (d.name || d.device_id || '').substring(0, 10),
            score: d.connected ? 100 : 0,
        }))
        .sort((a, b) => b.score - a.score);
    const color = (v) => v >= 80 ? t.success : v >= 60 ? t.warning : t.danger;
    healthChart.setOption({
        yAxis: { data: rows.map(r => r.name) },
        series: [{
            data: rows.map(r => ({ value: r.score, itemStyle: { color: color(r.score), borderRadius: [0, 3, 3, 0] } })),
        }],
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
            lineStyle: { width: 2 },
            data: times.map(t => map[t] ?? null),
        };
    });

    trendChart.setOption({
        legend: { data: series.map(s => s.name) },
        xAxis: { data: times },
        series,
    });
}

// ========== 能源功率分布（横向条形，按功率排序） ==========
function updateEnergyChart(data) {
    if (!energyChart) return;
    const t = readTheme();
    const powerData = {};
    data.forEach(item => {
        if (item.register_name && item.register_name.toLowerCase().includes('power')) {
            const name = deviceNameCache[item.device_id] || item.device_id;
            powerData[name] = (powerData[name] || 0) + parseFloat(item.value || 0);
        }
    });
    const rows = Object.keys(powerData)
        .map(name => ({ name, value: powerData[name] }))
        .sort((a, b) => b.value - a.value)
        .slice(0, 8);

    energyChart.setOption({
        yAxis: { data: rows.map(r => r.name) },
        series: [{
            data: rows.map(r => ({ value: Number(r.value.toFixed(1)), itemStyle: { color: t.palette[0], borderRadius: [0, 3, 3, 0] } })),
        }],
    });
}

// ========== SPC 图 ==========
/**
 * X 控制图参考线：CL = 均值，UCL / LCL = 均值 ± 3σ
 * 完全由面板内正在显示的数据计算得出；点数不足（< 10）时不画，
 * 避免用预设常数冒充控制限。
 */
function computeControlLimits(values) {
    const nums = (values || []).map(v => parseFloat(v)).filter(v => !isNaN(v));
    if (nums.length < 10) return null;
    const mean = nums.reduce((a, b) => a + b, 0) / nums.length;
    const variance = nums.reduce((a, b) => a + (b - mean) * (b - mean), 0) / (nums.length - 1);
    const sigma = Math.sqrt(variance);
    if (!isFinite(sigma) || sigma === 0) return null;
    return { cl: mean, ucl: mean + 3 * sigma, lcl: mean - 3 * sigma };
}

function updateSPCChart(data) {
    if (!spcChart) return;
    const t = readTheme();
    // 取第一个温度变量做 SPC 演示
    const tempData = data.filter(d => d.register_name && d.register_name.toLowerCase().includes('temperature'));
    if (tempData.length === 0) return;
    const raw = tempData.slice(0, 30).map(d => d.value);
    const values = raw.map(v => Number(parseFloat(v).toFixed(2)));
    const limits = computeControlLimits(values);

    const series = {
        data: values,
        itemStyle: {
            color: (params) => {
                if (!limits) return t.palette[0];
                return (params.value > limits.ucl || params.value < limits.lcl) ? t.danger : t.palette[0];
            },
        },
    };

    if (limits) {
        const f = (v) => v.toFixed(2);
        series.markLine = {
            silent: true,
            symbol: 'none',
            data: [
                { yAxis: limits.ucl, lineStyle: { color: t.danger, type: 'dashed' }, label: { formatter: 'UCL ' + f(limits.ucl), color: t.danger, fontSize: CHART_FONT_SIZE, position: 'insideEndTop' } },
                { yAxis: limits.cl, lineStyle: { color: t.textMuted, type: 'solid' }, label: { formatter: 'CL ' + f(limits.cl), color: t.textMuted, fontSize: CHART_FONT_SIZE, position: 'insideEndTop' } },
                { yAxis: limits.lcl, lineStyle: { color: t.danger, type: 'dashed' }, label: { formatter: 'LCL ' + f(limits.lcl), color: t.danger, fontSize: CHART_FONT_SIZE, position: 'insideEndBottom' } },
            ],
        };
    }

    spcChart.setOption({
        xAxis: { data: values.map((_, i) => i + 1) },
        series: [series],
    });
}

// ========== 报警列表 ==========
// 等级修饰名与 style.css 保持一致：alarm-critical / alarm-warning / alarm-info
function alarmLevel(a) {
    const raw = String(a.alarm_level || '').toLowerCase();
    if (raw === 'critical' || raw === 'high') return 'critical';
    if (raw === 'info' || raw === 'low') return 'info';
    return 'warning';
}

function updateAlarmList(alarms) {
    const container = document.getElementById('alarm-list');
    if (!container) return;
    if (!alarms || alarms.length === 0) {
        container.innerHTML = '<div class="alarm-empty">暂无报警</div>';
        return;
    }
    container.innerHTML = alarms.slice(0, 10).map(a => {
        const level = alarmLevel(a);
        const time = new Date(a.timestamp).toLocaleTimeString('zh-CN');
        // 未确认的紧急/警告报警：只在左侧色条上做呼吸强调，整行不闪烁
        const acked = a.acknowledged === true || a.acknowledged === 1;
        const pulse = (!acked && (level === 'critical' || level === 'warning'))
            ? ` level-bar--pulse-${level}` : '';
        return `<div class="alarm-item alarm-${level}">
            <span class="level-bar level-bar--${level}${pulse}"></span>
            <div class="alarm-main">
                <div class="alarm-time">${escapeHtml(time)}</div>
                <div class="alarm-msg">${escapeHtml(a.alarm_message || a.alarm_id)} — ${escapeHtml(a.device_id)}</div>
            </div>
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
