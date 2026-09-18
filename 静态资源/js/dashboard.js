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
const MAX_CHART_POINTS = 200;

// ========== 分页（100+设备场景） ==========
const PAGE_SIZE = 50;  // 100台设备以内不需要分页
let currentPage = 1;
let allDevices = [];            // 完整设备列表缓存
let trendSelectBound = false;   // 设备下拉框只绑定一次，避免重复监听

// ========== 主题色读取 ==========
// 颜色一律来自 design-tokens.css，JS 里不写死色值（fallback 仅用于变量缺失时兜底）
function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback || '';
}

function cssVarPx(name, fallback) {
    const n = parseFloat(cssVar(name, ''));
    return isNaN(n) ? fallback : n;
}

// 图表色板：--chart-1..8 顺序固定（色盲可辨）
function chartPalette() {
    const list = [];
    for (let i = 1; i <= 8; i++) list.push(cssVar(`--chart-${i}`, '#64748b'));
    return list;
}

// KPI 迷你趋势：仅用轮询真实样本，样本不足时不出图
const kpiHistory = { rate: [] };
const KPI_HISTORY_MAX = 30;

function setKpiBar(id, ratio) {
    const el = document.getElementById(id);
    if (!el) return;
    const pct = Math.max(0, Math.min(1, isFinite(ratio) ? ratio : 0)) * 100;
    el.style.width = pct.toFixed(1) + '%';
    const track = el.parentElement;
    if (track && track.hasAttribute('aria-valuenow')) {
        track.setAttribute('aria-valuenow', String(Math.round(pct)));
    }
}

function pushKpiSample(key, value) {
    const arr = kpiHistory[key];
    if (!arr || !isFinite(value)) return;
    arr.push(value);
    if (arr.length > KPI_HISTORY_MAX) arr.shift();
    renderSparkline(document.getElementById('kpi-rate-spark'), arr);
}

function renderSparkline(el, values) {
    if (!el) return;
    if (!values || values.length < 2) { el.innerHTML = ''; return; }
    const W = 100, H = 22, PAD = 2;
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = (max - min) || 1;
    const step = W / (values.length - 1);
    const points = values.map((v, i) => {
        const x = (i * step).toFixed(2);
        const y = (H - PAD - ((v - min) / span) * (H - PAD * 2)).toFixed(2);
        return x + ',' + y;
    }).join(' ');
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><polyline points="${points}"></polyline></svg>`;
}

// ========== 初始化 ==========
document.addEventListener('DOMContentLoaded', () => {
    initClock();
    initTrendChart();
    loadData();
    setInterval(loadData, 5000);  // 30台设备时5秒轮询足够

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
    let r;
    try {
        r = await fetch('/api' + url, { headers: h });
    } catch (e) {
        // 网络错误 → 不清token，不跳转
        console.error('Network error:', url, e);
        return null;
    }
    if (r.status === 401) {
        localStorage.removeItem('auth_token');
        window.location.href = '/login';
        return null;
    }
    if (!r.ok) return null;
    return r.json();
}

// ========== 主数据加载 ==========
let loadDataInProgress = false;

function scheduleUpdate(status) {
    updateDeviceGrid(status);
}

async function loadData() {
    if (loadDataInProgress) return;
    loadDataInProgress = true;
    const gen = ++loadGeneration;
    try {
        const status = await apiFetch('/system/status?brief=1');
        if (gen !== loadGeneration) return;
        if (!status) return;
        updateKPI(status);
        updateStatusBar(status);

        // 先获取实时数据填充缓存，再渲染网格（避免首次显示"--"）
        const data = await apiFetch('/data/realtime?limit=5000');
        if (gen !== loadGeneration) return;
        if (data && data.data) {
            data.data.forEach(item => {
                if (item.device_id && item.register_name && item.value != null) {
                    const key = `${item.device_id}:${item.register_name}`;
                    lastDeviceValues[key] = typeof item.value === 'number'
                        ? item.value.toFixed(1) : String(item.value);
                }
            });
            updateTrendChart(data.data);
        }

        // 数据缓存就绪后，再渲染设备网格
        scheduleUpdate(status);

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
        if (dot) dot.className = 'status-dot status-dot--danger';
        setText('status-text', '连接异常');
    } finally {
        loadDataInProgress = false;
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
        // 在线率进度条（由在线/总数算出）
        setKpiBar('kpi-devices-bar', devs.length ? online / devs.length : 0);
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
        const rate = Math.floor((c.total_collections || 0) / Math.max((stats.uptime_seconds || 1) / 60, 1));
        setText('kpi-rate', rate);
        pushKpiSample('rate', rate);   // 迷你趋势：滚动真实采样
        const total = (c.successful_collections || 0) + (c.failed_collections || 0);
        const q = total > 0 ? Math.round(c.successful_collections / total * 100) : 100;
        setText('kpi-quality-val', q + '%');
        // 成功率进度条（0-100%）
        setKpiBar('kpi-quality-bar', q / 100);
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
        badge.classList.toggle('sim-badge--on', !!stats.simulation_mode);
    }
}

// ========== 设备卡片网格（简化版：每次轮询全量重建） ==========
function updateDeviceGrid(stats) {
    if (!stats || !stats.devices) {
        allDevices = [];
        renderCurrentPage([]);   // 无设备时给出规范空提示，不滞留在"正在加载设备…"
        return;
    }
    let devs = Array.isArray(stats.devices) ? stats.devices : Object.values(stats.devices);

    // 缓存完整设备列表
    allDevices = devs;

    // 更新缓存
    devs.forEach(d => {
        const id = d.device_id || d.id;
        deviceCache[id] = d;
    });

    // 直接渲染，不做快照比较
    renderCurrentPage(devs);

    // 填充设备选择下拉框（只绑定一次，避免轮询重复挂监听）
    const select = document.getElementById('trend-device-select');
    if (select && !trendSelectBound) {
        select.innerHTML = devs.length
            ? devs.map(d => {
                const id = d.device_id || d.id;
                return `<option value="${escapeHtml(id)}" ${id === selectedDeviceId ? 'selected' : ''}>${escapeHtml(d.name || id)}</option>`;
            }).join('')
            : '<option value="">暂无设备</option>';
        select.addEventListener('change', function() {
            selectedDeviceId = this.value;
            Object.keys(dataBuffers).forEach(k => delete dataBuffers[k]);
            if (trendChart) trendChart.clear();
        });
        trendSelectBound = true;
        if (!selectedDeviceId && devs.length > 0) {
            selectedDeviceId = devs[0].device_id || devs[0].id;
        }
    }
}

// 获取当前页设备
function getPageDevices() {
    const start = (currentPage - 1) * PAGE_SIZE;
    return allDevices.slice(start, start + PAGE_SIZE);
}

// 构建设备卡片HTML（状态 = .tag--* + .status-dot--*，数值等宽字体）
function buildDeviceCard(d) {
    const id = d.device_id || d.id;
    const name = d.name || id;
    const online = d.connected;
    const stopped = d.stopped;
    const hasAlarm = d.status === 'fault' || d.status === 'warning';
    const category = d.device_category || 'sensor';

    // 状态映射：success(运行中) / info(已停止) / warning(告警) / offline(离线)
    let stateMod = 'offline';
    let statusText = '离线';
    if (online && stopped) { stateMod = 'info'; statusText = '已停止'; }
    else if (online && hasAlarm) { stateMod = 'warning'; statusText = '告警'; }
    else if (online) { stateMod = 'success'; statusText = '运行中'; }

    const regs = d.registers || [];
    const valStr = regs.slice(0, 2).map(r => {
        const label = getShortLabel(r.name);
        const cacheKey = `${id}:${r.name}`;
        const cached = lastDeviceValues[cacheKey] ?? '--';
        const quality = lastDeviceQuality[cacheKey];
        const qualityDot = quality != null
            ? `<span class="quality-dot" style="background:${getQualityColor(quality)}" title="数据质量: ${getQualityLabel(quality)} (${quality})"></span>`
            : '';
        return `<span class="dev-val"><span class="label">${escapeHtml(label)}</span> <span class="num" id="dv-${escapeHtml(id)}-${escapeHtml(r.name)}">${escapeHtml(cached)}</span>${qualityDot}</span>`;
    }).join('');

    const ctrlBtn = category === 'mechanical' && online
        ? `<button class="dev-ctrl-btn ${stopped ? 'start' : 'stop'}" onclick="event.stopPropagation();toggleDevice('${escapeHtml(id)}',${!stopped})" title="${stopped ? '启动' : '停止'}">${stopped ? '▶' : '■'}</button>`
        : '';

    return `<div class="dev-card" data-device-id="${escapeHtml(id)}" onclick="selectDevice('${escapeHtml(id)}')" title="${escapeHtml(name)}">
        <div class="dev-status dev-status--${stateMod}"></div>
        <div class="dev-info">
            <div class="dev-name">
                <span class="dev-name__text">${escapeHtml(name)}</span>
                <span class="tag tag--${stateMod}"><span class="status-dot status-dot--${stateMod}"></span>${statusText}</span>
                ${d.zone ? `<span class="dev-zone-tag">${escapeHtml(d.zone)}</span>` : ''}
            </div>
            <div class="dev-meta">${escapeHtml(d.protocol || 'modbus_tcp')} · <span class="dev-host">${escapeHtml(d.host || '--')}</span></div>
            <div class="dev-values">${valStr}</div>
        </div>
        ${ctrlBtn}
    </div>`;
}

// 渲染当前页设备卡片（全量替换，确保host等字段始终显示）
function renderCurrentPage(devs) {
    if (!devs) devs = allDevices;

    const grid = document.getElementById('device-grid');
    if (!grid) return;

    // 空数据 → 统一空提示
    if (!devs || devs.length === 0) {
        grid.innerHTML = `<div class="empty-state"><i class="bi bi-hdd-rack"></i><span>暂无设备（等待采集服务上报）</span></div>`;
        renderPagination(0, 1);
        return;
    }

    const totalPages = Math.ceil(devs.length / PAGE_SIZE);
    if (currentPage > totalPages) currentPage = totalPages || 1;

    const start = (currentPage - 1) * PAGE_SIZE;
    const pageDevs = devs.slice(start, start + PAGE_SIZE);

    // 全量替换当前页卡片（同时清除首次加载占位符）
    grid.innerHTML = pageDevs.map(d => buildDeviceCard(d)).join('');

    // 渲染分页控件
    renderPagination(devs.length, totalPages);
}

// 分页控件
function renderPagination(total, totalPages) {
    let pager = document.getElementById('device-pager');
    if (!pager) {
        pager = document.createElement('div');
        pager.id = 'device-pager';
        pager.className = 'device-pager';
        const grid = document.getElementById('device-grid');
        if (grid && grid.parentNode) {
            grid.parentNode.insertBefore(pager, grid.nextSibling);
        }
    }

    if (totalPages <= 1) {
        pager.innerHTML = `<span class="pager-info">共 ${total} 台设备</span>`;
        return;
    }

    const start = (currentPage - 1) * PAGE_SIZE + 1;
    const end = Math.min(currentPage * PAGE_SIZE, total);

    pager.innerHTML = `
        <span class="pager-info">${start}-${end} / 共 ${total} 台</span>
        <button class="pager-btn" onclick="goToPage(1)" ${currentPage === 1 ? 'disabled' : ''} title="首页">&laquo;</button>
        <button class="pager-btn" onclick="goToPage(${currentPage - 1})" ${currentPage === 1 ? 'disabled' : ''} title="上一页">&lsaquo;</button>
        <span class="pager-current">${currentPage} / ${totalPages}</span>
        <button class="pager-btn" onclick="goToPage(${currentPage + 1})" ${currentPage === totalPages ? 'disabled' : ''} title="下一页">&rsaquo;</button>
        <button class="pager-btn" onclick="goToPage(${totalPages})" ${currentPage === totalPages ? 'disabled' : ''} title="末页">&raquo;</button>
    `;
}

function goToPage(page) {
    const totalPages = Math.ceil(allDevices.length / PAGE_SIZE);
    if (page < 1 || page > totalPages) return;
    currentPage = page;
    renderCurrentPage();
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
// 等级映射：prio 用于等级文字标签，bar 用于左侧 .level-bar--* 色条
function alarmMeta(alarmLevel) {
    if (alarmLevel === 'critical') return { prio: 'critical', prioText: 'CRIT', bar: 'critical' };
    if (alarmLevel === 'warning')  return { prio: 'warning',  prioText: 'HIGH', bar: 'warning' };
    return { prio: 'low', prioText: 'LOW', bar: 'info' };
}

// 兼容 severity 字段（critical/high/medium/low/info）
function alarmMetaBySeverity(sev) {
    if (sev === 'critical') return { prio: 'critical', prioText: 'CRIT', bar: 'critical' };
    if (sev === 'high' || sev === 'medium' || sev === 'warning') {
        return { prio: 'high', prioText: 'HIGH', bar: 'warning' };
    }
    return { prio: 'low', prioText: 'LOW', bar: 'info' };
}

function formatClock(ts) {
    const d = ts ? new Date(ts) : new Date();
    if (isNaN(d.getTime())) return '--:--:--';
    return d.toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}

function alarmPvText(alarm) {
    // 最新值（优先用last_value，其次actual_value）
    const latestVal = alarm.last_value != null ? alarm.last_value : alarm.actual_value;
    if (latestVal == null || latestVal === '') return '';
    const num = parseFloat(latestVal);
    return isNaN(num) ? `PV:${latestVal}` : `PV:${num.toFixed(1)}`;
}

function createSpan(cls, txt) {
    const s = document.createElement('span');
    s.className = cls;
    s.textContent = txt;
    return s;
}

// 单条报警行：左等级色条 + 两行（等级/消息；时间/设备/数值/确认）
// 未确认只让色条呼吸（.level-bar--pulse-*），不做整行透明度闪烁
function createAlarmRow(info) {
    const row = document.createElement('div');
    row.className = 'alarm-row' + (info.acked ? '' : ' unacked');

    let barCls = 'level-bar level-bar--' + info.meta.bar;
    if (!info.acked) {
        if (info.meta.bar === 'critical') barCls += ' level-bar--pulse-critical';
        else if (info.meta.bar === 'warning') barCls += ' level-bar--pulse-warning';
    }
    row.appendChild(createSpan(barCls, ''));

    const main = document.createElement('div');
    main.className = 'alarm-main';

    const line1 = document.createElement('div');
    line1.className = 'alarm-line1';
    line1.appendChild(createSpan('alarm-prio ' + info.meta.prio, info.meta.prioText));
    line1.appendChild(createSpan('alarm-msg', info.msg));

    const line2 = document.createElement('div');
    line2.className = 'alarm-line2';
    line2.appendChild(createSpan('alarm-time', info.time));
    line2.appendChild(createSpan('alarm-device', info.device));
    if (info.pv) line2.appendChild(createSpan('alarm-pv', info.pv));
    if (info.count > 1) line2.appendChild(createSpan('alarm-count', '×' + info.count));
    if (!info.acked) {
        const btn = document.createElement('button');
        btn.className = 'alarm-ack-btn';
        btn.dataset.alarmId = info.alarmId || '';
        btn.dataset.deviceId = info.deviceId || '';
        btn.dataset.register = info.registerName || '';
        btn.textContent = '确认';
        line2.appendChild(btn);
    }

    main.appendChild(line1);
    main.appendChild(line2);
    row.appendChild(main);
    return row;
}

function updateAlarmPanel(alarms) {
    const list = document.getElementById('alarm-list');
    if (!list) return;

    if (!alarms || alarms.length === 0) {
        list.innerHTML = '<div class="alarm-empty">暂无活动报警</div>';
        setText('kpi-unacked', 0);
        return;
    }

    // 统计未确认
    const unacked = alarms.filter(a => !a.acknowledged).length;
    setText('kpi-unacked', unacked);

    const rows = alarms.slice(0, 20).map(a => createAlarmRow({
        meta: alarmMeta(a.alarm_level),
        time: formatClock(a.last_trigger_time || a.timestamp),
        device: (a.device_id || '').substring(0, 12),
        msg: a.alarm_message || a.alarm_id || '-',
        pv: alarmPvText(a),
        count: a.trigger_count || 1,
        acked: !!a.acknowledged,
        alarmId: a.alarm_id,
        deviceId: a.device_id,
        registerName: a.register_name || ''
    }));

    list.replaceChildren(...rows);
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

// 实时告警 DOM 插入（结构与 updateAlarmPanel 保持一致，防 XSS 用 DOM API）
function prependAlarmItem(alarm) {
    const alarmList = document.getElementById('alarm-list');
    if (!alarmList) return;

    // 移除空状态提示
    const empty = alarmList.querySelector('.alarm-empty');
    if (empty) empty.remove();

    const row = createAlarmRow({
        meta: alarmMetaBySeverity(alarm.severity || alarm.alarm_level),
        time: formatClock(alarm.timestamp),
        device: (alarm.device_id || '').substring(0, 12),
        msg: alarm.alarm_message || alarm.message || alarm.alarm_id || '-',
        pv: alarmPvText(alarm),
        count: 1,
        acked: false,
        alarmId: alarm.alarm_id,
        deviceId: alarm.device_id,
        registerName: alarm.register_name || ''
    });

    alarmList.insertAdjacentElement('afterbegin', row);

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

// 设备值更新（质量颜色取自设计令牌的质量阈值）
function updateDeviceValue(deviceId, registerName, value, quality) {
    const el = document.getElementById(`dv-${deviceId}-${registerName}`);
    if (!el) return;
    el.textContent = typeof value === 'number' ? value.toFixed(2) : value;
    if (quality !== undefined && quality !== null) {
        el.style.color = getQualityColor(quality);
        el.title = `数据质量: ${getQualityLabel(quality)} (${quality})`;
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
        if (dataBuffers[key].length > MAX_CHART_POINTS) dataBuffers[key].shift();
        matched++;
    });

    // Prevent memory leak: limit total buffer keys
    const allKeys = Object.keys(dataBuffers);
    if (allKeys.length > 100) {
        allKeys.slice(0, allKeys.length - 100).forEach(k => delete dataBuffers[k]);
    }

    const keys = Object.keys(dataBuffers);

    // 空数据态：无曲线数据时给出规范空提示，不画空白坐标轴
    const emptyEl = document.getElementById('trend-empty');
    if (emptyEl) emptyEl.classList.toggle('is-hidden', keys.length > 0);
    if (matched === 0 && keys.length === 0) return;

    const timeSet = new Set();
    keys.forEach(k => dataBuffers[k].forEach(d => timeSet.add(d.t)));
    const times = Array.from(timeSet).sort().slice(-MAX_CHART_POINTS);

    // 统一色板：--chart-1..8 循环（顺序固定，色盲可辨）
    const palette = chartPalette();

    // 网格/坐标轴/文字颜色统一取自设计令牌，深浅色切换后下一次刷新自动生效
    const axisText = cssVar('--chart-axis-text', '#64748b');
    const labelText = cssVar('--chart-label-text', '#475569');
    const gridColor = cssVar('--chart-grid', '#e2e8f0');
    const gridStrong = cssVar('--chart-grid-strong', '#cbd5e1');
    const tooltipBg = cssVar('--chart-tooltip-bg', 'rgba(15,23,42,0.92)');
    const tooltipText = cssVar('--chart-tooltip-text', '#f8fafc');
    const fontSize = cssVarPx('--font-xs', 12);

    const series = keys.map((key, i) => {
        const map = {};
        dataBuffers[key].forEach(d => { map[d.t] = d.v; });
        return {
            name: getShortLabel(key),
            type: 'line',
            smooth: true,
            symbol: 'none',
            lineStyle: { width: 1.5, color: palette[i % palette.length] },
            itemStyle: { color: palette[i % palette.length] },
            data: times.map(t => map[t] ?? null),
        };
    });

    trendChart.setOption({
        backgroundColor: 'transparent',
        color: palette,
        tooltip: {
            trigger: 'axis',
            backgroundColor: tooltipBg,
            borderColor: gridStrong,
            textStyle: { color: tooltipText, fontSize: fontSize },
        },
        legend: {
            top: 0,
            right: 0,
            textStyle: { color: labelText, fontSize: fontSize },
            itemWidth: 12,
            itemHeight: 2,
        },
        grid: { left: 56, right: 12, top: 28, bottom: 24 },
        xAxis: {
            type: 'category',
            data: times,
            boundaryGap: false,
            axisLine: { lineStyle: { color: gridStrong } },
            axisLabel: { color: axisText, fontSize: fontSize },
            splitLine: { show: false },
        },
        yAxis: {
            type: 'value',
            axisLine: { show: false },
            axisLabel: { color: axisText, fontSize: fontSize },
            splitLine: { lineStyle: { color: gridColor } },
        },
        series,
    });
}

// ========== 数据导出 ==========
function exportChartData() {
    const keys = Object.keys(dataBuffers);
    if (keys.length === 0) { alert('暂无数据可导出'); return; }

    // 收集所有时间点
    const timeSet = new Set();
    keys.forEach(k => dataBuffers[k].forEach(d => timeSet.add(d.t)));
    const times = Array.from(timeSet).sort();

    // 构建CSV
    const header = ['时间', ...keys.map(k => getShortLabel(k))];
    const rows = times.map(t => {
        return [t, ...keys.map(k => {
            const item = dataBuffers[k].find(d => d.t === t);
            return item ? item.v : '';
        })];
    });

    const csv = '﻿' + [header, ...rows].map(r => r.join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `trend_${selectedDeviceId || 'all'}_${new Date().toISOString().slice(0,19).replace(/:/g,'-')}.csv`;
    a.click();
    URL.revokeObjectURL(url);
}

function exportAllDeviceData() {
    const token = localStorage.getItem('auth_token');
    const headers = token ? { 'Authorization': `Bearer ${token}` } : {};

    fetch('/api/data/realtime?limit=10000', { headers })
        .then(r => r.json())
        .then(data => {
            if (!data.data || data.data.length === 0) { alert('暂无数据'); return; }

            const header = ['设备ID', '寄存器', '值', '单位', '时间'];
            const rows = data.data.map(d => [
                d.device_id, d.register_name, d.value, d.unit || '', d.timestamp
            ]);

            const csv = '﻿' + [header, ...rows].map(r => r.join(',')).join('\n');
            const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `scada_all_${new Date().toISOString().slice(0,19).replace(/:/g,'-')}.csv`;
            a.click();
            URL.revokeObjectURL(url);
        })
        .catch(e => { alert('导出失败: ' + e.message); });
}

// ========== 设备状态栏 ==========
function updateStatusBar(stats) {
    if (!stats) return;

    const dot = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    if (dot) dot.className = 'status-dot status-dot--success';
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
                    // OPC UA 数据质量指示（颜色取设计令牌）
                    const quality = info.quality;
                    if (quality != null) {
                        lastDeviceQuality[key] = quality;
                        el.style.color = getQualityColor(quality);
                        el.title = `数据质量: ${getQualityLabel(quality)} (${quality})`;
                    }
                }
            });
        });
        // 实时更新告警面板
        sk.on('alarm', (data) => {
            if (data && data.alarm_id) {
                prependAlarmItem(data);
                // 顶部报警条由 main.js handleAlarm -> updateAlarmBanner 统一处理，避免重复提示
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
// 质量分级沿用后端 OPC UA 约定（≥192 Good / ≥64 Uncertain / 其它 Bad），颜色取设计令牌
function getQualityColor(quality) {
    if (quality >= 192) return cssVar('--color-success', '#16a34a');
    if (quality >= 64) return cssVar('--color-warning', '#d97706');
    return cssVar('--color-danger', '#dc2626');
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
