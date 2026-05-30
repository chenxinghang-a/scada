/**
 * SCADA 仪表盘 — ISA-101 标准
 * 正常=灰色低调，异常=红色醒目，3秒看懂工厂状态
 */

let trendChart = null;
let selectedDeviceId = null;
let loadGeneration = 0;
const deviceCache = {};     // {id: {name, connected, registers, ...}}
const dataBuffers = {};     // {register_name: [{t, v}]}
const lastDeviceValues = {}; // {"device_id:register_name": "formatted_value"}
const lastDeviceQuality = {}; // {"device_id:register_name": quality_code}
const MAX_POINTS = 60;

// ========== 初始化 ==========
document.addEventListener('DOMContentLoaded', () => {
    initClock();
    initTrendChart();
    loadData();
    setInterval(loadData, 3000);

    // 用户信息
    try {
        const u = JSON.parse(localStorage.getItem('scada_user') || '{}');
        setText('status-user', u.display_name || u.username || 'operator');
    } catch(e) {}
});

function initClock() {
    function tick() {
        const el = document.getElementById('topbar-time');
        if (el) el.textContent = new Date().toTimeString().slice(0, 8);
    }
    tick();
    setInterval(tick, 1000);
}

// ========== API ==========
async function apiFetch(url) {
    const token = localStorage.getItem('auth_token');
    const h = token ? { 'Authorization': `Bearer ${token}` } : {};
    const r = await fetch('/api' + url, { headers: h });
    if (r.status === 401) {
        localStorage.removeItem('auth_token');
        window.location.href = '/login';
        return null;
    }
    if (!r.ok) return null;
    return r.json();
}

// ========== 主数据加载 ==========
async function loadData() {
    const gen = ++loadGeneration;
    try {
        const status = await apiFetch('/system/status');
        if (gen !== loadGeneration) return;
        if (!status) return;
        updateKPI(status);
        updateDeviceGrid(status);
        updateStatusBar(status);

        const data = await apiFetch('/data/realtime?limit=5000');
        if (gen !== loadGeneration) return;
        if (data && data.data) {
            // 用API返回的最新数据填充缓存
            data.data.forEach(item => {
                if (item.device_id && item.register_name && item.value != null) {
                    const key = `${item.device_id}:${item.register_name}`;
                    lastDeviceValues[key] = typeof item.value === 'number'
                        ? item.value.toFixed(1) : String(item.value);
                }
            });
            updateTrendChart(data.data);
        }

        // 首次加载后，订阅所有设备的WebSocket推送
        if (window.socket && status.devices) {
            const devs = Array.isArray(status.devices) ? status.devices : Object.values(status.devices);
            devs.forEach(d => {
                const id = d.device_id || d.id;
                window.socket.emit('subscribe', {device_id: id});
            });
        }

        const alarms = await apiFetch('/alarms?limit=50');
        if (gen !== loadGeneration) return;
        if (alarms && alarms.alarms) {
            updateAlarmPanel(alarms.alarms);
        }
    } catch (e) {
        console.error('loadData:', e);
        const dot = document.getElementById('status-dot');
        if (dot) dot.className = 'status-dot red';
        setText('status-text', '连接异常');
    }
}

// ========== KPI 更新 ==========
function updateKPI(stats) {
    // 设备
    if (stats.devices) {
        let devs = Array.isArray(stats.devices) ? stats.devices : Object.values(stats.devices);
        const online = devs.filter(d => d.connected).length;
        setText('kpi-online', online);
        setText('kpi-total', devs.length);

        // 更新 KPI 卡片状态色
        const el = document.getElementById('kpi-devices');
        if (el) {
            el.className = 'kpi' + (online < devs.length ? ' warn' : '');
        }
    }

    // 报警
    if (stats.alarms) {
        const total = stats.alarms.total_active_alarms || 0;
        setText('kpi-alarm-count', total);
        const el = document.getElementById('kpi-alarms');
        if (el) el.className = 'kpi' + (total > 0 ? ' alarm' : '');

        // 报警徽章
        const byLevel = stats.alarms.by_level || {};
        setText('badge-crit', 'CRIT: ' + (byLevel.critical || 0));
        setText('badge-high', 'HIGH: ' + (byLevel.high || byLevel.warning || 0));
        setText('badge-med', 'MED: ' + (byLevel.medium || 0));
    }

    // 采集
    if (stats.collector) {
        const c = stats.collector;
        setText('kpi-rate', Math.floor((c.total_collections || 0) / Math.max((stats.uptime_seconds || 1) / 60, 1)));
        const total = (c.successful_collections || 0) + (c.failed_collections || 0);
        const q = total > 0 ? Math.round(c.successful_collections / total * 100) : 100;
        setText('kpi-quality-val', q + '%');
    }

    // 运行时间
    if (stats.uptime_seconds !== undefined) {
        setText('kpi-uptime-val', formatUptime(stats.uptime_seconds));
        setText('kpi-mode', stats.simulation_mode ? '模拟模式' : '真实设备');
    }

    // 模拟模式标记
    const badge = document.getElementById('sim-mode-badge');
    if (badge && stats.simulation_mode !== undefined) {
        badge.textContent = stats.simulation_mode ? '[ 模拟 ]' : '';
        badge.style.color = '#eab308';
        badge.style.fontSize = '11px';
    }
}

// ========== 设备卡片网格 ==========
function updateDeviceGrid(stats) {
    if (!stats.devices) return;
    let devs = Array.isArray(stats.devices) ? stats.devices : Object.values(stats.devices);

    // 更新缓存
    devs.forEach(d => {
        const id = d.device_id || d.id;
        deviceCache[id] = d;
    });

    const grid = document.getElementById('device-grid');
    if (!grid) return;

    grid.innerHTML = devs.map(d => {
        const id = d.device_id || d.id;
        const name = d.name || id;
        const online = d.connected;
        const stopped = d.stopped;
        const hasAlarm = d.status === 'fault' || d.status === 'warning';
        const category = d.device_category || 'sensor';

        let statusClass = 'offline';
        let statusText = '离线';
        if (online && stopped) {
            statusClass = 'stopped';
            statusText = '已停止';
        } else if (online && hasAlarm) {
            statusClass = 'warning';
            statusText = '告警';
        } else if (online) {
            statusClass = 'online';
            statusText = '运行中';
        }

        // 取前 2 个寄存器值（用缓存值，不显示"--"）
        const regs = d.registers || [];
        const valStr = regs.slice(0, 2).map(r => {
            const label = getShortLabel(r.name);
            const cacheKey = `${id}:${r.name}`;
            const cached = lastDeviceValues[cacheKey] ?? '--';
            const quality = lastDeviceQuality[cacheKey];
            const qualityDot = quality != null
                ? `<span class="quality-dot" style="background:${getQualityColor(quality)}" title="${getQualityLabel(quality)}"></span>`
                : '';
            return `<span class="dev-val"><span class="label">${label}</span> <span class="num" id="dv-${id}-${r.name}">${cached}</span>${qualityDot}</span>`;
        }).join('');

        // 机械类设备显示启停按钮
        const ctrlBtn = category === 'mechanical' && online
            ? `<button class="dev-ctrl-btn ${stopped ? 'start' : 'stop'}" onclick="event.stopPropagation();toggleDevice('${id}',${!stopped})" title="${stopped ? '启动' : '停止'}">${stopped ? '▶' : '■'}</button>`
            : '';

        return `<div class="dev-card" onclick="selectDevice('${id}')" title="${name}">
            <div class="dev-status ${statusClass}"></div>
            <div class="dev-info">
                <div class="dev-name">${name} <span class="dev-state-tag ${statusClass}">${statusText}</span></div>
                <div class="dev-meta">${d.protocol || 'modbus_tcp'} · ${d.host || ''}</div>
                <div class="dev-values">${valStr}</div>
            </div>
            ${ctrlBtn}
        </div>`;
    }).join('');

    // 填充设备选择下拉框
    const select = document.getElementById('trend-device-select');
    if (select && select.children.length <= 1) {
        select.innerHTML = devs.map(d => {
            const id = d.device_id || d.id;
            return `<option value="${id}" ${id === selectedDeviceId ? 'selected' : ''}>${d.name || id}</option>`;
        }).join('');
        select.addEventListener('change', function() {
            selectedDeviceId = this.value;
            Object.keys(dataBuffers).forEach(k => delete dataBuffers[k]);
            if (trendChart) trendChart.clear();
        });
        if (!selectedDeviceId && devs.length > 0) {
            selectedDeviceId = devs[0].device_id || devs[0].id;
        }
    }
}

function selectDevice(id) {
    selectedDeviceId = id;
    const select = document.getElementById('trend-device-select');
    if (select) select.value = id;
    Object.keys(dataBuffers).forEach(k => delete dataBuffers[k]);
    if (trendChart) trendChart.clear();
}

// ========== 设备启停控制 ==========
async function toggleDevice(deviceId, stop) {
    const action = stop ? 'stop' : 'start';
    if (!confirm(`确认${stop ? '停止' : '启动'}设备 ${deviceId}？`)) return;

    try {
        const resp = await fetch(`/api/devices/${deviceId}/${action}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...getAuthHeaders() }
        });
        const data = await resp.json();
        if (data.success) {
            loadData(); // 刷新状态
        } else {
            alert(data.message || '操作失败');
        }
    } catch (e) {
        alert('操作异常: ' + e.message);
    }
}

// ========== 报警面板 ==========
function updateAlarmPanel(alarms) {
    const list = document.getElementById('alarm-list');
    if (!list) return;

    if (!alarms || alarms.length === 0) {
        list.innerHTML = '<div class="alarm-empty">暂无活动报警</div>';
        return;
    }

    // 统计未确认
    const unacked = alarms.filter(a => !a.acknowledged).length;
    setText('kpi-unacked', unacked);

    list.innerHTML = alarms.slice(0, 20).map(a => {
        const level = a.alarm_level === 'critical' ? 'critical'
                    : a.alarm_level === 'warning' ? 'warning' : 'low';
        const prioText = level === 'critical' ? 'CRIT' : level === 'warning' ? 'HIGH' : 'LOW';
        const time = new Date(a.timestamp).toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
        const device = (a.device_id || '').substring(0, 12);
        const msg = a.alarm_message || a.alarm_id || '-';

        // 最新值（优先用last_value，其次actual_value）
        const latestVal = a.last_value != null ? a.last_value : a.actual_value;
        const pv = latestVal != null ? `PV:${parseFloat(latestVal).toFixed(1)}` : '';

        // 触发次数
        const count = a.trigger_count || 1;
        const countStr = count > 1 ? `<span class="alarm-count">×${count}</span>` : '';

        // 最后触发时间
        const lastTime = a.last_trigger_time
            ? new Date(a.last_trigger_time).toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit', second:'2-digit'})
            : time;

        const acked = a.acknowledged;

        return `<div class="alarm-row ${acked ? '' : 'unacked'}">
            <span class="alarm-prio ${level}">${prioText}</span>
            <span class="alarm-time">${lastTime}</span>
            <span class="alarm-device">${escapeHtml(device)}</span>
            <span class="alarm-msg">${escapeHtml(msg)}</span>
            <span class="alarm-pv">${pv}</span>
            ${countStr}
            ${acked ? '' : `<button class="alarm-ack-btn" data-alarm-id="${escapeHtml(a.alarm_id)}" data-device-id="${escapeHtml(a.device_id)}" data-register="${escapeHtml(a.register_name || '')}">确认</button>`}
        </div>`;
    }).join('');
}

async function ackAlarm(alarmId, deviceId, regName) {
    try {
        await fetch(`/api/alarms/${alarmId}/acknowledge`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
            body: JSON.stringify({ device_id: deviceId, register_name: regName, acknowledged_by: 'operator' })
        });
        loadData();
    } catch (e) { console.error('ackAlarm:', e); }
}

// 事件委托：确认按钮（防 XSS，不用 inline onclick）
document.addEventListener('click', function(e) {
    if (e.target.classList.contains('alarm-ack-btn')) {
        const alarmId = e.target.dataset.alarmId;
        const deviceId = e.target.dataset.deviceId;
        const register = e.target.dataset.register;
        ackAlarm(alarmId, deviceId, register);
    }
});

// XSS 安全转义
function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/[&<>"']/g, function(c) {
        return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
}

// 实时告警 DOM 插入
function prependAlarmItem(alarm) {
    const alarmList = document.getElementById('alarm-list');
    if (!alarmList) return;

    // 移除空状态提示
    const empty = alarmList.querySelector('.alarm-empty');
    if (empty) empty.remove();

    const severityColors = {
        critical: '#dc2626', high: '#ea580c',
        medium: '#ca8a04', low: '#0891b2', info: '#4f46e5'
    };
    const color = severityColors[alarm.severity || alarm.alarm_level] || '#d9d9d9';
    const prioText = (alarm.severity === 'critical' || alarm.alarm_level === 'critical') ? 'CRIT'
        : (alarm.severity === 'high' || alarm.alarm_level === 'warning') ? 'HIGH' : 'LOW';
    const time = alarm.timestamp
        ? new Date(alarm.timestamp).toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit', second:'2-digit'})
        : new Date().toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
    const device = (alarm.device_id || '').substring(0, 12);
    const msg = alarm.alarm_message || alarm.message || alarm.alarm_id || '-';
    const latestVal = alarm.last_value != null ? alarm.last_value : alarm.actual_value;
    const pv = latestVal != null ? `PV:${parseFloat(latestVal).toFixed(1)}` : '';

    const html = `<div class="alarm-row unacked" style="border-left: 3px solid ${color};">
        <span class="alarm-prio ${alarm.severity || alarm.alarm_level || 'low'}">${prioText}</span>
        <span class="alarm-time">${time}</span>
        <span class="alarm-device">${escapeHtml(device)}</span>
        <span class="alarm-msg">${escapeHtml(msg)}</span>
        <span class="alarm-pv">${pv}</span>
        <button class="alarm-ack-btn" data-alarm-id="${escapeHtml(alarm.alarm_id || '')}" data-device-id="${escapeHtml(alarm.device_id || '')}" data-register="${escapeHtml(alarm.register_name || '')}">确认</button>
    </div>`;

    alarmList.insertAdjacentHTML('afterbegin', html);

    // 保持最多 20 条
    while (alarmList.children.length > 20) {
        alarmList.removeChild(alarmList.lastChild);
    }

    // 更新未确认计数
    updateAlarmCountFromDOM();
}

// 从 DOM 统计未确认数
function updateAlarmCountFromDOM() {
    const alarmList = document.getElementById('alarm-list');
    if (!alarmList) return;
    const unacked = alarmList.querySelectorAll('.alarm-row.unacked').length;
    setText('kpi-unacked', unacked);
}

// 告警闪烁横幅
function flashAlarmBanner(alarm) {
    const banner = document.getElementById('alarm-banner');
    if (!banner) return;
    const colors = {critical: '#dc2626', high: '#ea580c', medium: '#ca8a04', warning: '#ca8a04'};
    const sev = alarm.severity || alarm.alarm_level || 'critical';
    banner.style.backgroundColor = colors[sev] || '#dc2626';
    banner.style.display = 'block';
    banner.style.opacity = '1';
    banner.textContent = `${sev.toUpperCase()}: ${alarm.alarm_message || alarm.message || 'New alarm'}`;
    setTimeout(() => { banner.style.opacity = '0.7'; }, 5000);
}

// 设备值更新（带质量颜色）
function updateDeviceValue(deviceId, registerName, value, quality) {
    const el = document.getElementById(`dv-${deviceId}-${registerName}`);
    if (!el) return;
    el.textContent = typeof value === 'number' ? value.toFixed(2) : value;
    if (quality !== undefined) {
        if (quality >= 192) {
            el.style.color = '#52c41a';  // Good - green
        } else if (quality >= 64) {
            el.style.color = '#faad14';  // Uncertain - yellow
        } else {
            el.style.color = '#ff4d4f';  // Bad - red
        }
    }
}

function getAuthHeaders() {
    const t = localStorage.getItem('auth_token');
    return t ? { 'Authorization': `Bearer ${t}` } : {};
}

// ========== 趋势图 ==========
function initTrendChart() {
    const dom = document.getElementById('trend-chart');
    if (!dom) return;
    trendChart = echarts.init(dom);
}

function updateTrendChart(data) {
    if (!trendChart) return;

    if (!selectedDeviceId && data.length > 0) {
        selectedDeviceId = data[0].device_id;
    }
    if (!selectedDeviceId) return;

    // 同步下拉框
    const select = document.getElementById('trend-device-select');
    if (select && select.value !== selectedDeviceId) {
        select.value = selectedDeviceId;
    }

    const now = new Date().toTimeString().slice(0, 8) + '.' + String(new Date().getMilliseconds()).padStart(3, '0');

    // 缓存选中设备的数据
    let matched = 0;
    data.forEach(item => {
        if (item.device_id !== selectedDeviceId) return;
        if (item.value === null || item.value === undefined) return;
        const key = item.register_name;
        if (!dataBuffers[key]) dataBuffers[key] = [];
        dataBuffers[key].push({ t: now, v: parseFloat(item.value) });
        if (dataBuffers[key].length > MAX_POINTS) dataBuffers[key].shift();
        matched++;
    });

    if (matched === 0) return;

    // Prevent memory leak: limit total buffer keys
    const allKeys = Object.keys(dataBuffers);
    if (allKeys.length > 100) {
        allKeys.slice(0, allKeys.length - 100).forEach(k => delete dataBuffers[k]);
    }

    const keys = Object.keys(dataBuffers);
    const timeSet = new Set();
    keys.forEach(k => dataBuffers[k].forEach(d => timeSet.add(d.t)));
    const times = Array.from(timeSet).sort().slice(-MAX_POINTS);

    const colors = ['#6366f1', '#06b6d4', '#f59e0b', '#ef4444', '#22c55e', '#ec4899'];

    const series = keys.slice(0, 4).map((key, i) => {
        const map = {};
        dataBuffers[key].forEach(d => { map[d.t] = d.v; });
        return {
            name: getShortLabel(key),
            type: 'line',
            smooth: true,
            symbol: 'none',
            lineStyle: { width: 1.5, color: colors[i % colors.length] },
            data: times.map(t => map[t] ?? null),
        };
    });

    trendChart.setOption({
        backgroundColor: 'transparent',
        tooltip: {
            trigger: 'axis',
            backgroundColor: 'rgba(255,255,255,0.95)',
            borderColor: '#e2e5ea',
            textStyle: { color: '#1a1a2e', fontSize: 11 },
        },
        legend: {
            top: 0,
            right: 0,
            textStyle: { color: '#666', fontSize: 11 },
            itemWidth: 12,
            itemHeight: 2,
        },
        grid: { left: 50, right: 10, top: 25, bottom: 20 },
        xAxis: {
            type: 'category',
            data: times,
            boundaryGap: false,
            axisLine: { lineStyle: { color: '#e2e5ea' } },
            axisLabel: { color: '#999', fontSize: 10 },
            splitLine: { show: false },
        },
        yAxis: {
            type: 'value',
            axisLine: { show: false },
            axisLabel: { color: '#999', fontSize: 10 },
            splitLine: { lineStyle: { color: '#f0f0f0' } },
        },
        series,
    });
}

// ========== 设备状态栏 ==========
function updateStatusBar(stats) {
    if (!stats) return;

    const dot = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    if (dot) dot.className = 'status-dot green';
    if (text) text.textContent = '系统运行中';

    if (stats.database) {
        setText('status-db', `DB: ${(stats.database.total_records || 0).toLocaleString()} 条`);
    }
}

// ========== 设备值实时更新 ==========
// WebSocket 更新设备值 — 使用 main.js 的 window.socket
document.addEventListener('DOMContentLoaded', () => {
    function attachSocketHandlers() {
        const sk = window.socket;
        if (!sk) return;
        sk.on('data_update', (data) => {
            if (!data) return;
            // 服务器发送格式: {register_name: {device_id, register_name, value, quality, ...}, ...}
            Object.entries(data).forEach(([regName, info]) => {
                if (!info || typeof info !== 'object') return;
                const devId = info.device_id;
                const val = info.value;
                if (!devId || val == null) return;
                const key = `${devId}:${regName}`;
                const formatted = typeof val === 'number' ? val.toFixed(1) : String(val);
                lastDeviceValues[key] = formatted;
                const el = document.getElementById(`dv-${devId}-${regName}`);
                if (el) {
                    el.textContent = formatted;
                    // OPC UA quality color indicator
                    const quality = info.quality;
                    if (quality != null) {
                        lastDeviceQuality[key] = quality;
                        el.style.color = getQualityColor(quality);
                        el.title = `Quality: ${getQualityLabel(quality)} (${quality})`;
                    }
                }
            });
        });
        // 实时更新告警面板
        sk.on('alarm', (data) => {
            if (data && data.alarm_id) {
                prependAlarmItem(data);
                flashAlarmBanner(data);
            } else {
                loadData(); // fallback
            }
        });
        // Subscribe to all visible devices
        sk.on('connect', () => {
            const select = document.getElementById('trend-device-select');
            if (select) {
                Array.from(select.options).forEach(opt => {
                    if (opt.value) sk.emit('subscribe', {device_id: opt.value});
                });
            }
        });
    }
    // main.js socket may not be ready yet; retry briefly
    if (window.socket) {
        attachSocketHandlers();
    } else {
        const timer = setInterval(() => {
            if (window.socket) { clearInterval(timer); attachSocketHandlers(); }
        }, 200);
    }
});

// ========== 工具函数 ==========
function setText(id, v) {
    const el = document.getElementById(id);
    if (el) el.textContent = v;
}

function formatUptime(s) {
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d}天${h}时`;
    if (h > 0) return `${h}时${m}分`;
    return `${m}分`;
}

// ========== OPC UA 数据质量标志 ==========
function getQualityColor(quality) {
    if (quality >= 192) return '#52c41a';  // Good - green
    if (quality >= 64) return '#faad14';   // Uncertain - yellow
    return '#ff4d4f';                       // Bad - red
}

function getQualityLabel(quality) {
    if (quality >= 192) return 'Good';
    if (quality >= 64) return 'Uncertain';
    return 'Bad';
}

function getShortLabel(name) {
    const map = {
        'boiler_temperature': '锅炉温度', 'boiler_pressure': '锅炉压力',
        'heat_exchanger_temperature': '换热器温度', 'flue_gas_temperature': '排烟温度',
        'steam_flow': '蒸汽流量', 'feed_water_level': '给水液位',
        'oxygen_content': '含氧量', 'boiler_status': '锅炉状态',
        'mold_temperature': '模具温度', 'injection_pressure': '注射压力',
        'injection_speed': '注射速度', 'barrel_temperature': '料筒温度',
        'spray_pressure': '喷涂压力', 'oven_temperature': '烘干温度',
        'voltage_a': 'A相电压', 'current_a': 'A相电流',
        'active_power': '总有功', 'frequency': '频率',
        'temperature': '温度', 'pressure': '压力',
        'flow': '流量', 'level': '液位',
        'voltage': '电压', 'current': '电流', 'power': '功率',
    };
    if (map[name]) return map[name];
    const lower = name.toLowerCase();
    for (const [k, v] of Object.entries(map)) {
        if (lower.includes(k)) return v;
    }
    return name.length > 6 ? name.slice(0, 6) : name;
}
