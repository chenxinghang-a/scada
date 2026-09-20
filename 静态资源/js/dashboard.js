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

// ========== 设备卡增强所需状态（全部只在内存，5s 轮询驱动） ==========
// 点位来源顺序：Modbus → OPC UA → MQTT → REST（同一设备可同时存在多种）
const POINT_SOURCES = ['registers', 'nodes', 'topics', 'endpoints'];
const SERIES_MAX_POINTS = 20;   // 每点位保留最近 20 个样本（5s 轮询 ≈ 100s 趋势）
const SERIES_MAX_KEYS = 400;    // series 键上限，防内存无界增长
const deviceSeries = {};        // {"device_id:register_name": [数值…]} 迷你趋势样本
const alarmRuleIndex = {};      // {"device_id:register_name": {threshold, condition, level}}
const alarmsByDevice = {};      // {device_id: {count, level}} 由活动报警列表重建
let alarmRulesLoaded = false;   // 报警阈值/量程只在首次拉取一次
let expandedDeviceId = null;    // 就地展开全部点位的设备（同时只展开一个）
let gridErrorMsg = '';          // 设备区加载失败原因
let lastApiError = '';          // 最近一次接口错误（用于区分"加载失败"与"确实无设备"）


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

// 迷你折线（内联 SVG）：只在样本 ≥2 时出图，避免画一条无意义的直线
function sparklineMarkup(values) {
    if (!values || values.length < 2) return '';
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
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><polyline points="${points}"></polyline></svg>`;
}

function renderSparkline(el, values) {
    if (!el) return;
    el.innerHTML = values && values.length >= 2 ? sparklineMarkup(values) : '';
}

// ========== 点位归一（修"设备卡只剩名字"的核心） ==========
// 30 台设备里 22 台是 Modbus（点位在 registers），另外 8 台（MQTT/OPC UA/REST）的
// 点位分别在 topics / nodes / endpoints，registers 为空数组。
// 后端 /api/system/status?brief=1 已把四者统一成 [{name, unit}]，
// 这里仍兼容完整配置的原始字段（address/topic/node_id/path），避免换接口后再次失明。
function pointDisplayName(p) {
    if (!p || typeof p !== 'object') return '';
    const raw = p.name ?? p.description ?? p.address ?? p.topic ?? p.node_id ?? p.path ?? '';
    return String(raw).trim();
}

function pointAddress(p) {
    if (!p || typeof p !== 'object') return '';
    const raw = p.address ?? p.topic ?? p.node_id ?? p.path ?? p.endpoint ?? '';
    return raw == null ? '' : String(raw);
}

function pointUnit(p) {
    return p && p.unit ? String(p.unit) : '';
}

// registers → nodes → topics → endpoints 合并去重，每项至少要有可显示的名字。
// 归一化后仍保留原始字段（min/max/scale 等），供量表条判断真实量程。
function devicePoints(d) {
    const out = [];
    const seen = Object.create(null);
    if (!d) return out;
    const sources = Array.isArray(POINT_SOURCES) ? POINT_SOURCES : [];
    sources.forEach(src => {
        const list = d[src];
        if (!Array.isArray(list)) return;
        list.forEach(p => {
            const name = pointDisplayName(p);
            if (!name) return;
            const key = name.toLowerCase();
            if (seen[key]) return;      // 同名点位只保留靠前来源（Modbus 优先）
            seen[key] = true;
            out.push(Object.assign({}, (p && typeof p === 'object') ? p : {}, {
                name,
                address: pointAddress(p),
                unit: pointUnit(p),
                source: src,
            }));
        });
    });
    return out;
}

// ========== 数值 / 迷你趋势样本 ==========
function toNumber(v) {
    if (v === null || v === undefined || v === '') return null;
    const n = typeof v === 'number' ? v : parseFloat(v);
    return isFinite(n) ? n : null;
}

// 关键值格式化：全是数值就统一 1 位小数（等宽数字下不会左右跳）
function formatMeasure(v) {
    const n = toNumber(v);
    return n === null ? (v === null || v === undefined ? '--' : String(v)) : n.toFixed(1);
}

function pushDeviceSample(deviceId, registerName, value) {
    if (!deviceId || !registerName) return;
    const n = toNumber(value);
    if (n === null) return;
    const key = `${deviceId}:${registerName}`;
    let arr = deviceSeries[key];
    if (!arr) {
        arr = deviceSeries[key] = [];
        const keys = Object.keys(deviceSeries);
        if (keys.length > SERIES_MAX_KEYS) {   // 防内存无界增长
            keys.slice(0, keys.length - SERIES_MAX_KEYS).forEach(k => delete deviceSeries[k]);
        }
    }
    arr.push(n);
    if (arr.length > SERIES_MAX_POINTS) arr.shift();
}

// 量测条用量：优先取样本末值（与 sparkline 同源），退回 lastDeviceValues 的显示值
function lastNumericValue(deviceId, registerName) {
    const arr = deviceSeries[`${deviceId}:${registerName}`];
    if (arr && arr.length) return arr[arr.length - 1];
    return toNumber(lastDeviceValues[`${deviceId}:${registerName}`]);
}

// ========== 量表条：真实量程 > 相对阈值位置 > 只有值与单位 ==========
function pointRange(p) {
    const min = toNumber(p && (p.min ?? p.min_value ?? p.range_min));
    const max = toNumber(p && (p.max ?? p.max_value ?? p.range_max));
    return (min !== null && max !== null && max > min) ? { min, max } : null;
}

function pointThreshold(deviceId, registerName) {
    const rule = alarmRuleIndex[`${deviceId}:${registerName}`];
    if (!rule) return null;
    const thr = toNumber(rule.threshold);
    if (thr === null || thr === 0) return null;   // 0 阈值无相对刻度意义，不画
    return { threshold: thr, condition: rule.condition || 'greater_than', level: rule.level || 'warning' };
}

// 关键值旁边的量表条。不造量程：
//   有真实 min/max → 用真实量程填充；
//   只有报警阈值 → 画"相对阈值位置"（阈值刻度固定 50%，明确标注非量程）；
//   都没有 → 返回空字符串，只显示值与单位。
function pointGaugeMarkup(deviceId, p) {
    const cacheKey = `${deviceId}:${p.name}`;
    const shown = lastDeviceValues[cacheKey];
    const v = lastNumericValue(deviceId, p.name);
    if (v === null) return '';               // 尚无数据不画条，避免误导
    const unit = p.unit ? escapeHtml(p.unit) : '';

    const range = pointRange(p);
    if (range) {
        const ratio = Math.max(0, Math.min(1, (v - range.min) / (range.max - range.min)));
        const text = `量程 ${range.min}–${range.max}${unit ? ' ' + unit : ''}`;
        return `<span class="point-gauge" title="${escapeHtml(text)}"><span class="point-gauge__fill" style="width:${(ratio * 100).toFixed(1)}%"></span></span>`;
    }

    const thr = pointThreshold(deviceId, p.name);
    if (thr) {
        const span = Math.abs(thr.threshold) * 2;   // 阈值落在 50% 处
        const ratio = Math.max(0, Math.min(1, v / span));
        const violated = thr.condition === 'less_than' ? v < thr.threshold : v > thr.threshold;
        const near = !violated && Math.abs(v - thr.threshold) / Math.abs(thr.threshold) <= 0.1;
        const mod = violated ? ' point-gauge--alarm' : (near ? ' point-gauge--warn' : '');
        const condText = thr.condition === 'less_than' ? '低于' : '超过';
        const text = `相对阈值位置：${condText} ${thr.threshold}${unit ? ' ' + unit : ''} 报警（刻度 50% 为阈值，非量程）当前 ${shown || ''}`;
        return `<span class="point-gauge${mod}" title="${escapeHtml(text)}"><span class="point-gauge__fill" style="width:${(ratio * 100).toFixed(1)}%"></span><span class="point-gauge__tick" style="left:50%"></span></span>`;
    }

    return '';
}

// 报警阈值索引（GET /api/alarm-rules 返回 {rules:[{device_id, register_name, threshold, condition, level}]}）
async function loadAlarmRules() {
    if (alarmRulesLoaded) return;
    alarmRulesLoaded = true;
    const data = await apiFetch('/alarm-rules');
    if (!data || !Array.isArray(data.rules)) { alarmRulesLoaded = false; return; }
    data.rules.forEach(r => {
        if (!r || !r.device_id || !r.register_name) return;
        if (r.enabled === false) return;
        alarmRuleIndex[`${r.device_id}:${r.register_name}`] = {
            threshold: r.threshold, condition: r.condition, level: r.level
        };
    });
    // 阈值到位后立即补上量表条，不必等下一轮询（此时还没有设备就不重绘，避免闪出空态）
    if (allDevices.length) renderCurrentPage();
}


// ========== 初始化 ==========
document.addEventListener('DOMContentLoaded', () => {
    // 仪表盘页面锁定视口：滚动只发生在设备区/报警列表内部（只有一层滚动条）
    document.body.classList.add('dashboard-page');
    syncMainHeight();
    window.addEventListener('resize', syncMainHeight);
    if (typeof ResizeObserver !== 'undefined') {
        new ResizeObserver(syncMainHeight).observe(document.body);
    }

    initClock();
    initTrendChart();
    loadAlarmRules();
    loadData();
    setInterval(loadData, 5000);  // 30台设备时5秒轮询足够

    // 用户信息
    try {
        const u = JSON.parse(localStorage.getItem('scada_user') || '{}');
        setText('status-user', u.display_name || u.username || 'operator');
    } catch(e) {}
});

// 量出 .main 真实可用高度（视口 - 上方导航/面包屑 - 页脚），写入 --dash-main-h。
// 不再用 calc(100vh - 44px) 硬减一个顶栏高度。
function syncMainHeight() {
    const el = document.querySelector('.main');
    if (!el) return;
    const top = el.getBoundingClientRect().top + (window.scrollY || 0);
    const footer = document.querySelector('.app-footer');
    const footerH = footer ? footer.offsetHeight : 0;
    const h = Math.max(0, Math.round(window.innerHeight - top - footerH - 8));
    if (h <= 0) return;   // 量不到就不写死高度，交给 CSS 兜底
    const cur = parseFloat(document.documentElement.style.getPropertyValue('--dash-main-h'));
    if (isFinite(cur) && Math.abs(cur - h) < 1) return;   // 防 ResizeObserver 自激循环
    document.documentElement.style.setProperty('--dash-main-h', h + 'px');
}


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
        lastApiError = `网络不可达（${url}）: ${e.message || e}`;
        return null;
    }
    if (r.status === 401) {
        localStorage.removeItem('auth_token');
        window.location.href = '/login';
        return null;
    }
    if (!r.ok) {
        lastApiError = `接口 ${url} 返回 HTTP ${r.status}`;
        return null;
    }
    lastApiError = '';
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
        if (!status) {
            // 失败不再静默：首次失败给出原因+重试；已有卡片则保留旧数据并标注连接异常
            if (!allDevices.length) {
                gridErrorMsg = lastApiError || '设备状态接口无响应';
                renderCurrentPage([]);
            }
            const dot = document.getElementById('status-dot');
            if (dot) dot.className = 'status-dot status-dot--danger';
            setText('status-text', '连接异常');
            return;
        }
        updateKPI(status);
        updateStatusBar(status);
        gridErrorMsg = '';   // 本轮成功：清掉上一轮失败态，避免它继续压住正常渲染

        // 先获取实时数据填充缓存，再渲染网格（避免首次显示"--"）
        const data = await apiFetch('/data/realtime?limit=5000');
        if (gen !== loadGeneration) return;
        if (data && data.data) {
            data.data.forEach(item => {
                if (item.device_id && item.register_name && item.value != null) {
                    const key = `${item.device_id}:${item.register_name}`;
                    lastDeviceValues[key] = formatMeasure(item.value);
                    // 迷你趋势样本：只保留最近 SERIES_MAX_POINTS 点
                    pushDeviceSample(item.device_id, item.register_name, item.value);
                }
            });
            updateTrendChart(data.data);
        }

        // 先拿活动报警：设备卡据此置顶/打标（放在渲染之前，避免排序滞后一轮）
        const alarms = await apiFetch('/alarms?limit=50');
        if (gen !== loadGeneration) return;
        if (alarms && alarms.alarms) {
            updateAlarmPanel(alarms.alarms);
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
    } catch (e) {
        console.error('loadData:', e);
        if (!allDevices.length) {
            gridErrorMsg = '渲染异常: ' + (e.message || e);
            renderCurrentPage([]);
        }
        const dot = document.getElementById('status-dot');
        if (dot) dot.className = 'status-dot status-dot--danger';
        setText('status-text', '连接异常');
    } finally {
        loadDataInProgress = false;
    }
}

// 设备区重试：清掉失败态重新拉取
function retryDeviceLoad() {
    gridErrorMsg = '';
    lastApiError = '';
    const grid = document.getElementById('device-grid');
    if (grid) {
        grid.innerHTML = skeletonCardsHtml();
        grid.setAttribute('aria-busy', 'true');
    }
    loadDataInProgress = false;
    loadData();
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

// ========== 设备卡片网格（每次轮询全量重建） ==========
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

    refreshTrendSelect(devs);
}

// 有活动报警/故障的设备置顶（同档内保持原顺序，避免设备位置乱跳）
function deviceSortRank(d) {
    if (!d) return 1;
    const id = d.device_id || d.id;
    return (alarmsByDevice[id] || d.status === 'warning' || d.status === 'fault') ? 0 : 1;
}

// 设备下拉框：选项变化时才重建，监听只绑定一次
function refreshTrendSelect(devs) {
    const select = document.getElementById('trend-device-select');
    if (!select) return;
    const ids = (devs || []).map(d => d.device_id || d.id);
    const signature = ids.join('|');
    if (select.dataset.signature !== signature) {
        select.innerHTML = devs.length
            ? devs.map(d => {
                const id = d.device_id || d.id;
                return `<option value="${escapeHtml(id)}">${escapeHtml(d.name || id)}</option>`;
            }).join('')
            : '<option value="">暂无设备</option>';
        select.dataset.signature = signature;
        if (!trendSelectBound) {
            select.addEventListener('change', function() {
                selectedDeviceId = this.value;
                Object.keys(dataBuffers).forEach(k => delete dataBuffers[k]);
                if (trendChart) trendChart.clear();
            });
            trendSelectBound = true;
        }
    }
    if (!selectedDeviceId && ids.length > 0) selectedDeviceId = ids[0];
    if (selectedDeviceId && ids.indexOf(selectedDeviceId) >= 0) select.value = selectedDeviceId;
}

// 加载中骨架（与 dashboard.html 首屏骨架保持一致）
function skeletonCardsHtml() {
    let cards = '';
    for (let i = 0; i < 4; i++) {
        cards += `<div class="dev-card dev-card--skeleton" aria-hidden="true">
            <div class="dev-status"></div>
            <div class="dev-info">
                <div class="sk-line sk-line--name"></div>
                <div class="sk-line sk-line--meta"></div>
                <div class="sk-line sk-line--value"></div>
            </div>
        </div>`;
    }
    return cards + '<p class="grid-loading-note"><i class="bi bi-hourglass-split"></i> 正在加载设备…</p>';
}

// 获取当前页设备
function getPageDevices() {
    const start = (currentPage - 1) * PAGE_SIZE;
    return allDevices.slice(start, start + PAGE_SIZE);
}

// 设备状态：success(运行中) / info(已停止) / warning·danger(告警) / offline(离线)
function deviceStateInfo(d) {
    const id = d.device_id || d.id;
    const online = !!d.connected;
    const stopped = !!d.stopped;
    const alarm = alarmsByDevice[id];
    if (online && !stopped && alarm) {
        return { mod: alarm.level === 'critical' ? 'danger' : 'warning', text: '告警', alarmed: true };
    }
    if (online && stopped) return { mod: 'info', text: '已停止', alarmed: false };
    if (online && (d.status === 'warning' || d.status === 'fault')) {
        return { mod: 'warning', text: '告警', alarmed: true };
    }
    if (online) return { mod: 'success', text: '运行中', alarmed: false };
    return { mod: 'offline', text: '离线', alarmed: false };
}

// 关键值（第三行）：标签 / 大号等宽值+单位+质量点 / 量表条 + 迷你趋势
function buildPointValue(deviceId, p) {
    const cacheKey = `${deviceId}:${p.name}`;
    const shown = lastDeviceValues[cacheKey] ?? '--';
    const quality = lastDeviceQuality[cacheKey];
    const qualityDot = quality != null
        ? `<span class="quality-dot" style="background:${getQualityColor(quality)}" title="数据质量: ${getQualityLabel(quality)} (${quality})"></span>`
        : '';
    const gauge = pointGaugeMarkup(deviceId, p);
    const spark = sparklineMarkup(deviceSeries[cacheKey]);
    const meters = (gauge || spark)
        ? `<div class="dev-val__meters">${gauge}${spark ? `<span class="dev-spark" title="最近 ${deviceSeries[cacheKey].length} 个采样点趋势">${spark}</span>` : ''}</div>`
        : '';
    const label = getShortLabel(p.name);
    const labelTitle = p.address ? `${p.name} · ${p.address}` : p.name;
    return `<div class="dev-val">
        <div class="dev-val__label" title="${escapeHtml(labelTitle)}">${escapeHtml(label)}</div>
        <div class="dev-val__row">
            <span class="num" id="dv-${escapeHtml(deviceId)}-${escapeHtml(p.name)}">${escapeHtml(shown)}</span>
            ${p.unit ? `<span class="unit">${escapeHtml(p.unit)}</span>` : ''}
            ${qualityDot}
        </div>
        ${meters}
    </div>`;
}

// 展开后的单条点位（含地址/主题，值用 dvx- 前缀避免与关键值重复 id）
function buildPointRow(deviceId, p) {
    const cacheKey = `${deviceId}:${p.name}`;
    const shown = lastDeviceValues[cacheKey] ?? '--';
    const quality = lastDeviceQuality[cacheKey];
    const qualityDot = quality != null
        ? `<span class="quality-dot" style="background:${getQualityColor(quality)}" title="数据质量: ${getQualityLabel(quality)} (${quality})"></span>`
        : '';
    const title = p.address ? `${p.name} · ${p.address}` : p.name;
    return `<div class="dev-point" title="${escapeHtml(title)}">
        <span class="dev-point__name">${escapeHtml(getShortLabel(p.name))}</span>
        <span class="dev-point__val" id="dvx-${escapeHtml(deviceId)}-${escapeHtml(p.name)}">${escapeHtml(shown)}</span>
        ${p.unit ? `<span class="dev-point__unit">${escapeHtml(p.unit)}</span>` : ''}
        ${p.address ? `<span class="dev-point__addr">${escapeHtml(p.address)}</span>` : ''}
        ${qualityDot}
    </div>`;
}

// 构建设备卡片HTML
//   主行：设备名 + 状态标签（+ 告警标记/区域）
//   次行：协议 · host[:port] · 点位数
//   三行：关键值（大号等宽）+ 单位 + 质量点 + 量表条 + 迷你趋势
//   展开：该设备全部点位（就地展开，不跳页）
function buildDeviceCard(d) {
    const id = d.device_id || d.id;
    const name = d.name || id;
    const category = d.device_category || 'sensor';
    const state = deviceStateInfo(d);
    const points = devicePoints(d);
    const keyPoints = points.slice(0, 2);
    const expanded = expandedDeviceId === id;
    const alarm = alarmsByDevice[id];

    const valuesHtml = keyPoints.length
        ? keyPoints.map(p => buildPointValue(id, p)).join('')
        : `<div class="dev-val dev-val--none">无点位配置（registers/nodes/topics/endpoints 均为空）</div>`;

    const more = points.length > keyPoints.length
        ? `<span class="dev-meta__points"> · 展开 ${points.length} 点</span>`
        : '';

    const pointsHtml = expanded
        ? `<div class="dev-points">${points.map(p => buildPointRow(id, p)).join('')}</div>`
        : '';

    const alarmFlag = alarm
        ? `<span class="dev-alarm-flag" title="活动报警 ${alarm.count} 条（${alarm.level === 'critical' ? '紧急' : '警告'}）"><i class="bi bi-exclamation-triangle-fill"></i>${alarm.count}</span>`
        : '';

    const ctrlBtn = category === 'mechanical' && d.connected
        ? `<button class="dev-ctrl-btn ${d.stopped ? 'start' : 'stop'}" onclick="event.stopPropagation();toggleDevice('${escapeHtml(id)}',${!d.stopped})" title="${d.stopped ? '启动' : '停止'}">${d.stopped ? '▶' : '■'}</button>`
        : '';

    const portText = d.port ? ':' + escapeHtml(String(d.port)) : '';
    const meta = `<span class="dev-meta__proto">${escapeHtml(d.protocol || 'modbus_tcp')}</span> · <span class="dev-host">${escapeHtml(d.host || '--')}${portText}</span> · <span class="dev-meta__points">${points.length} 点</span>${more}`;

    return `<div class="dev-card${state.alarmed ? ' dev-card--alarm' : ''}" data-device-id="${escapeHtml(id)}"
            onclick="toggleDeviceCard('${escapeHtml(id)}')" title="${escapeHtml(name)}${d.host ? ' · ' + escapeHtml(d.host) : ''}"
            role="button" tabindex="0" aria-expanded="${expanded ? 'true' : 'false'}">
        <div class="dev-status dev-status--${state.mod}"></div>
        <div class="dev-info">
            <div class="dev-name">
                <span class="dev-name__text">${escapeHtml(name)}</span>
                <span class="tag tag--${state.mod}"><span class="status-dot status-dot--${state.mod}"></span>${state.text}</span>
                ${alarmFlag}
                ${d.zone ? `<span class="dev-zone-tag">${escapeHtml(d.zone)}</span>` : ''}
            </div>
            <div class="dev-meta">${meta}</div>
            <div class="dev-values">${valuesHtml}</div>
            ${pointsHtml}
        </div>
        <div class="dev-card__side">
            ${ctrlBtn}
            <span class="dev-expand" aria-hidden="true">${expanded ? '▴' : '▾'}</span>
        </div>
    </div>`;
}

// 就地展开/收起该设备全部点位（同时保留原有的"选中设备"行为，趋势图跟随）
function toggleDeviceCard(id) {
    // 已选中的设备不再重复 select，避免每次展开都把趋势缓存清空
    if (selectedDeviceId !== id) selectDevice(id);
    expandedDeviceId = (expandedDeviceId === id) ? null : id;
    renderCurrentPage();
}

// 渲染当前页设备卡片（全量替换，确保host等字段始终显示）
function renderCurrentPage(devs) {
    if (!devs) devs = allDevices;

    const grid = document.getElementById('device-grid');
    if (!grid) return;

    // 失败态：给出原因 + 重试入口（不再与"无设备"混为一谈）
    if (gridErrorMsg && (!devs || devs.length === 0)) {
        grid.setAttribute('aria-busy', 'false');
        grid.innerHTML = `<div class="empty-state empty-state--error">
            <i class="bi bi-exclamation-triangle"></i>
            <span class="empty-state__title">设备列表加载失败</span>
            <span class="empty-state__hint">原因：${escapeHtml(gridErrorMsg)}</span>
            <button class="empty-state__retry" onclick="retryDeviceLoad()">重试</button>
        </div>`;
        renderPagination(0, 1);
        return;
    }

    // 空数据 → 统一空提示（接口正常但确实没有设备）
    if (!devs || devs.length === 0) {
        grid.setAttribute('aria-busy', 'false');
        grid.innerHTML = `<div class="empty-state"><i class="bi bi-hdd-rack"></i><span>暂无设备（等待采集服务上报）</span></div>`;
        renderPagination(0, 1);
        return;
    }

    const totalPages = Math.ceil(devs.length / PAGE_SIZE);
    if (currentPage > totalPages) currentPage = totalPages || 1;

    // 告警设备置顶（Array.sort 在现代引擎里稳定，同档保持原顺序）
    const ordered = devs.slice().sort((a, b) => deviceSortRank(a) - deviceSortRank(b));

    const start = (currentPage - 1) * PAGE_SIZE;
    const pageDevs = ordered.slice(start, start + PAGE_SIZE);

    // 全量替换当前页卡片（同时清除首屏骨架）
    grid.setAttribute('aria-busy', 'false');
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

    // 重建"每设备活动报警"索引：设备卡据此置顶 + 打告警标记
    Object.keys(alarmsByDevice).forEach(k => delete alarmsByDevice[k]);
    (alarms || []).forEach(a => {
        const did = a && a.device_id;
        if (!did) return;
        const cur = alarmsByDevice[did] || (alarmsByDevice[did] = { count: 0, level: 'warning' });
        cur.count++;
        if (a.alarm_level === 'critical') cur.level = 'critical';
    });

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

// 事件委托：设备卡键盘展开/收起（卡片是 role=button 的 div）
document.addEventListener('keydown', function(e) {
    if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
    const card = e.target.closest && e.target.closest('.dev-card');
    if (!card || e.target !== card) return;
    const id = card.dataset.deviceId;
    if (!id) return;
    e.preventDefault();
    toggleDeviceCard(id);
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
// 关键值（dv-）与展开行（dvx-）共用同一份数据，避免出现"展开后值不动"
function updateDeviceValue(deviceId, registerName, value, quality) {
    const formatted = formatMeasure(value);
    [`dv-${deviceId}-${registerName}`, `dvx-${deviceId}-${registerName}`].forEach(domId => {
        const el = document.getElementById(domId);
        if (!el) return;
        el.textContent = formatted;
        if (quality !== undefined && quality !== null) {
            el.style.color = getQualityColor(quality);
            el.title = `数据质量: ${getQualityLabel(quality)} (${quality})`;
        }
    });
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
                lastDeviceValues[key] = formatMeasure(val);
                if (info.quality != null) lastDeviceQuality[key] = info.quality;
                // 关键值与展开行统一由 updateDeviceValue 落地（含 OPC UA 数据质量颜色）
                updateDeviceValue(devId, regName, val, info.quality);
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
    if (!name) return '';
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
        // ---- MQTT 设备（topics） ----
        'vibration_x': 'X轴振动', 'vibration_y': 'Y轴振动', 'vibration_z': 'Z轴振动',
        'bearing_temperature': '轴承温度', 'dissolved_oxygen': '溶解氧',
        'turbidity': '浊度', 'ph': 'pH值', 'water_temperature': '水温',
        // ---- REST 设备（endpoints） ----
        'ambient_temperature': '环境温度', 'system_pressure': '系统压力',
        'production_count': '生产计数', 'line_status': '产线状态',
        'oee': 'OEE', 'quality_rate': '合格率', 'defect_rate': '不良率',
        'planned_quantity': '计划产量', 'actual_quantity': '实际产量',
        // ---- OPC UA 设备（nodes） ----
        'motor_speed': '电机转速', 'running_status': '运行状态',
        'robot_status': '机器人状态', 'gripper_state': '夹爪状态',
        'cycle_time': '节拍时间', 'alarm_code': '报警码',
        'reactor_status': '反应釜状态', 'feed_valve_1': '进料阀1',
        'feed_valve_2': '进料阀2', 'discharge_valve': '出料阀', 'cooling_valve': '冷却阀',
        // ---- 数控/加工中心（REST） ----
        'spindle_speed': '主轴转速', 'spindle_load': '主轴负载', 'feed_override': '进给倍率',
        'program_number': '程序号', 'program_runtime': '运行时长', 'current_tool': '当前刀具',
        'tool_life': '刀具寿命', 'part_count': '零件计数', 'machine_mode': '机床模式',
        // ---- 通用 ----
        'temperature': '温度', 'pressure': '压力',
        'flow': '流量', 'level': '液位',
        'voltage': '电压', 'current': '电流', 'power': '功率',
    };
    if (map[name]) return map[name];

    const lower = String(name).toLowerCase();

    // 规则优先于子串匹配：规则带 ^/$ 锚点，比 includes 更精确。
    // 非 Modbus 设备点位命名无固定词表（joint_1 / reactor1_temp / tc_reactor_1 …）。
    // 注意 reactor1_pressure / reactor2_pressure 若走子串会都叫"压力"，无法区分。
    const rules = [
        [/^vibration_([xyz])$/, (m) => `${m[1].toUpperCase()}轴振动`],
        [/^joint_(\d+)$/, (m) => `关节${m[1]}`],
        [/^reactor(\d+)_(temp|temperature)$/, (m) => `反应釜${m[1]}温度`],
        [/^reactor(\d+)_pressure$/, (m) => `反应釜${m[1]}压力`],
        [/^reactor(\d+)_level$/, (m) => `反应釜${m[1]}液位`],
        [/^reactor(\d+)_speed$/, (m) => `反应釜${m[1]}转速`],
        [/^tc_reactor_(\d+)$/, (m) => `反应釜${m[1]}热电偶`],
        [/^rtd_pipeline_(\d+)$/, (m) => `管线${m[1]}热电阻`],
        [/^tc_exhaust_(\d+)$/, (m) => `排烟热电偶${m[1]}`],
        [/_valve$|_valve_\d+$/, () => '阀门'],
        [/_torque$|^torque_/, () => '扭矩'],
        [/_count$|^part_count$/, () => '计数'],
        [/_health$/, () => '健康度'],
        [/_error_count$/, () => '通信错误'],
        [/^pressure_|_pressure$/, () => '压力'],
        [/_temperature$|_temp$/, () => '温度'],
        [/_speed$/, () => '转速'],
        [/_load$/, () => '负载'],
        [/_level$/, () => '液位'],
        [/_flow$|^flow_|^totalizer_/, () => '流量'],
        [/_override$/, () => '倍率'],
        [/_life$/, () => '寿命'],
        [/_state$|_status$/, () => '状态'],
    ];
    for (const [re, fn] of rules) {
        const m = lower.match(re);
        if (m) return fn(m);
    }

    for (const [k, v] of Object.entries(map)) {
        if (lower.includes(k)) return v;
    }

    return name.length > 6 ? name.slice(0, 6) : name;
}


