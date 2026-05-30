/**
 * 工业SCADA系统主JavaScript文件
 */

// XSS 安全转义
function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// 全局变量
const API_BASE = '/api';
let socket = null;
let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 50;
const BASE_RECONNECT_DELAY = 1000;  // 1秒

/**
 * 初始化WebSocket连接（带断连重连 + 指数退避）
 */
function initWebSocket() {
    const wsToken = localStorage.getItem('auth_token');
    if (!wsToken) return;

    socket = io({
        query: {token: wsToken},
        reconnection: true,
        reconnectionAttempts: MAX_RECONNECT_ATTEMPTS,
        reconnectionDelay: BASE_RECONNECT_DELAY,
        reconnectionDelayMax: 30000,  // 最大30秒
        timeout: 10000,
    });

    window.socket = socket;  // 暴露给其他JS文件

    socket.on('connect', () => {
        console.log('WebSocket connected');
        reconnectAttempts = 0;
        updateSystemStatus('online');
        updateConnectionStatus('connected');

        // 重新订阅所有设备
        resubscribeAll();
    });

    socket.on('disconnect', (reason) => {
        console.log('WebSocket disconnected:', reason);
        updateSystemStatus('offline');
        updateConnectionStatus('disconnected');

        if (reason === 'io server disconnect') {
            // 服务器主动断开，需要手动重连
            setTimeout(() => socket.connect(), BASE_RECONNECT_DELAY);
        }
        // 其他情况socket.io自动重连
    });

    socket.on('connect_error', (error) => {
        console.error('WebSocket connection error:', error.message);
        reconnectAttempts++;
        updateConnectionStatus('error');

        // Token过期，跳转登录
        if (error.message.includes('401') || error.message.includes('auth')) {
            localStorage.removeItem('auth_token');
            window.location.href = '/login';
            return;
        }
    });

    socket.on('reconnect', (attemptNumber) => {
        console.log(`WebSocket reconnected after ${attemptNumber} attempts`);
        updateSystemStatus('online');
        updateConnectionStatus('connected');
    });

    socket.on('reconnect_failed', () => {
        console.error('WebSocket reconnection failed after max attempts');
        updateConnectionStatus('failed');
    });

    // 数据事件处理
    setupSocketHandlers(socket);
}

/**
 * 设置WebSocket数据事件处理器
 */
function setupSocketHandlers(sock) {
    sock.on('data_update', function(data) {
        handleDataUpdate(data);
    });

    sock.on('alarm', function(data) {
        handleAlarm(data);
    });

    sock.on('system_status', function(data) {
        updateSystemStats(data);
    });
}

/**
 * 更新系统状态显示
 */
function updateSystemStatus(status) {
    const statusElement = document.getElementById('system-status');
    if (statusElement) {
        if (status === 'online') {
            statusElement.innerHTML = '<i class="bi bi-circle-fill text-success"></i> 系统正常';
        } else {
            statusElement.innerHTML = '<i class="bi bi-circle-fill text-danger"></i> 连接断开';
        }
    }
}

/**
 * 更新WebSocket连接状态指示器（右下角小圆点）
 */
function updateConnectionStatus(status) {
    const indicator = document.getElementById('ws-status');
    if (!indicator) return;

    const states = {
        'connected': {color: '#52c41a', text: '已连接'},
        'disconnected': {color: '#ff4d4f', text: '已断开'},
        'error': {color: '#faad14', text: '连接错误'},
        'reconnecting': {color: '#1890ff', text: '重连中...'},
        'failed': {color: '#ff4d4f', text: '连接失败'},
    };

    const state = states[status] || states['disconnected'];
    indicator.style.color = state.color;
    indicator.textContent = '● ' + state.text;
    indicator.title = `WebSocket: ${state.text}`;
}

/**
 * 重连后重新订阅所有设备
 */
function resubscribeAll() {
    if (!socket || !socket.connected) return;

    // 从设备下拉框获取设备列表
    const deviceSelect = document.getElementById('trend-device-select');
    if (deviceSelect) {
        Array.from(deviceSelect.options).forEach(opt => {
            if (opt.value) {
                socket.emit('subscribe', {device_id: opt.value});
            }
        });
    }

    // 从设备网格获取设备列表
    const deviceCards = document.querySelectorAll('[data-device-id]');
    deviceCards.forEach(card => {
        const deviceId = card.dataset.deviceId;
        if (deviceId) {
            socket.emit('subscribe', {device_id: deviceId});
        }
    });
}

/**
 * 处理数据更新
 */
function handleDataUpdate(data) {
    // 更新实时数据表格（兼容函数，非仪表盘页面调用）
    if (typeof updateRealtimeTable === 'function') {
        updateRealtimeTable(data);
    }

    // 更新图表（兼容函数，非仪表盘页面调用）
    if (typeof updateCharts === 'function') {
        updateCharts(data);
    }
}

/**
 * 处理报警
 */
function handleAlarm(data) {
    // 去重检查：冷却窗口内同一报警不重复显示
    if (!AlarmDedupManager.shouldShow(data)) return;
    AlarmDedupManager.recordShow(data);
    // 保存最近报警数据，供dismiss按钮调用recordDismiss
    window._lastAlarmData = data;
    // 更新顶部报警条（不弹窗，不打断操作）
    updateAlarmBanner(data);
    // 更新导航栏计数
    updateAlarmCount();
    // 播放报警声音
    playAlarmSound(data);
}

/**
 * 报警去重管理器（前端）
 * 防止同一报警重复弹窗：
 * 1. 冷却窗口：同一 dedup_key 在冷却期内只显示一次 toast
 * 2. 用户dismissed跟踪：用户手动关闭的报警在冷却期内不重复显示
 * 3. 从后端动态获取去重配置
 */
const AlarmDedupManager = {
    // 去重配置（初始默认值，会从后端加载）
    config: {
        enabled: true,
        emit_cooldown_seconds: 60,
        acknowledge_suppress_seconds: 300,
        max_visible_toasts: 3,
        critical_toast_duration: 30,
        warning_toast_duration: 10,
    },

    // 推送记录：dedup_key -> last_show_timestamp
    _emitHistory: {},

    // 用户手动关闭记录：dedup_key -> dismiss_timestamp
    _dismissed: {},

    // 配置加载标记
    _configLoaded: false,

    /**
     * 从后端加载去重配置
     */
    async loadConfig() {
        try {
            const _token = localStorage.getItem('auth_token');
            const resp = await fetch(`${API_BASE}/alarms/dedup-config`, {
                headers: _token ? { 'Authorization': `Bearer ${_token}` } : {}
            });
            if (resp.ok) {
                const data = await resp.json();
                if (data.config) {
                    Object.assign(this.config, data.config);
                    this._configLoaded = true;
                    console.log('[AlarmDedup] 配置已加载:', this.config);
                }
            }
        } catch (e) {
            console.warn('[AlarmDedup] 加载配置失败，使用默认值:', e);
        }
    },

    /**
     * 生成去重key
     */
    _getKey(alarm) {
        return alarm.dedup_key || `${alarm.alarm_id || alarm.id || ''}:${alarm.device_id || ''}:${alarm.register_name || ''}`;
    },

    /**
     * 检查是否应该显示此报警
     * @returns {boolean} true=应该显示, false=应被去重
     */
    shouldShow(alarm) {
        if (!this.config.enabled) return true;

        const key = this._getKey(alarm);
        const now = Date.now();
        const cooldownMs = this.config.emit_cooldown_seconds * 1000;
        const suppressMs = this.config.acknowledge_suppress_seconds * 1000;

        // 检查用户dismissed记录
        const dismissTime = this._dismissed[key];
        if (dismissTime && (now - dismissTime) < suppressMs) {
            console.log(`[AlarmDedup] 被用户dismissed抑制: ${key}`);
            return false;
        }

        // 检查冷却窗口
        const lastShow = this._emitHistory[key];
        if (lastShow && (now - lastShow) < cooldownMs) {
            console.log(`[AlarmDedup] 在冷却窗口内: ${key}`);
            return false;
        }

        return true;
    },

    /**
     * 记录报警已显示
     */
    recordShow(alarm) {
        const key = this._getKey(alarm);
        this._emitHistory[key] = Date.now();
        // 清除dismissed记录（新报警覆盖旧的dismissed）
        delete this._dismissed[key];
    },

    /**
     * 记录用户手动关闭（dismissed）
     */
    recordDismiss(alarm) {
        const key = this._getKey(alarm);
        this._dismissed[key] = Date.now();
    },

    /**
     * 清理过期记录（防止内存泄漏）
     */
    cleanup() {
        const now = Date.now();
        const maxAge = Math.max(
            this.config.emit_cooldown_seconds,
            this.config.acknowledge_suppress_seconds
        ) * 1000 * 2;

        for (const key in this._emitHistory) {
            if (now - this._emitHistory[key] > maxAge) {
                delete this._emitHistory[key];
            }
        }
        for (const key in this._dismissed) {
            if (now - this._dismissed[key] > maxAge) {
                delete this._dismissed[key];
            }
        }
    }
};

// 定期清理过期记录
setInterval(() => AlarmDedupManager.cleanup(), 60000);

/**
 * 更新顶部报警条（不弹窗，不打断操作）
 */
function updateAlarmBanner(alarm) {
    const banner = document.getElementById('alarm-banner');
    if (!banner) return;

    const isCritical = alarm.level === 'critical' || alarm.alarm_level === 'critical';
    const msg = alarm.alarm_message || alarm.message || '新报警';
    const device = alarm.device_id || '';

    // 更新文字
    const textEl = document.getElementById('alarm-banner-text');
    if (textEl) {
        textEl.textContent = isCritical
            ? `紧急：${msg} (${device})`
            : `警告：${msg} (${device})`;
    }

    // 更新背景色
    banner.style.background = isCritical ? '#fee2e2' : '#fff3cd';
    banner.style.borderBottomColor = isCritical ? '#dc2626' : '#f59e0b';

    // 显示
    banner.style.display = 'block';

    // 闪一下提醒（不遮挡操作）
    banner.style.opacity = '0.6';
    setTimeout(() => { banner.style.opacity = '1'; }, 200);
    setTimeout(() => { banner.style.opacity = '0.7'; }, 400);
    setTimeout(() => { banner.style.opacity = '1'; }, 600);

    // 更新计数
    updateAlarmCount();
}

/**
 * 播放报警提示音
 */
function playAlarmSound() {
    try {
        // 使用Web Audio API播放简单提示音
        const audioContext = new (window.AudioContext || window.webkitAudioContext)();
        const oscillator = audioContext.createOscillator();
        const gainNode = audioContext.createGain();
        
        oscillator.connect(gainNode);
        gainNode.connect(audioContext.destination);
        
        oscillator.frequency.value = 800;
        oscillator.type = 'sine';
        gainNode.gain.value = 0.3;
        
        oscillator.start();
        oscillator.stop(audioContext.currentTime + 0.2);
    } catch (e) {
        // 忽略音频播放错误
    }
}

/**
 * 更新报警计数
 */
function updateAlarmCount() {
    const token = localStorage.getItem('auth_token');
    fetch(`${API_BASE}/alarms/active`, {
        headers: token ? { 'Authorization': `Bearer ${token}` } : {}
    })
        .then(response => response.json())
        .then(data => {
            const alarms = data.alarms || [];
            const count = alarms.length;

            // 导航栏计数
            const el = document.getElementById('active-alarms');
            if (el) el.textContent = count;

            // 报警卡片
            const countCard = document.getElementById('alarm-count-card');
            if (countCard) countCard.textContent = count;

            // 报警条
            const banner = document.getElementById('alarm-banner');
            if (!banner) return;

            if (count === 0) {
                banner.style.display = 'none';
                return;
            }

            // 有报警 → 显示报警条
            const critCount = alarms.filter(a => a.alarm_level === 'critical').length;
            const warnCount = alarms.filter(a => a.alarm_level === 'warning').length;

            // 显示最新一条报警信息
            const latest = alarms[0];
            const textEl = document.getElementById('alarm-banner-text');
            if (textEl && latest) {
                const msg = latest.alarm_message || latest.alarm_id || '报警';
                const device = latest.device_id || '';
                const isCrit = latest.alarm_level === 'critical';
                textEl.textContent = isCrit
                    ? `紧急：${msg} (${device})`
                    : `警告：${msg} (${device})`;
                banner.style.background = isCrit ? '#fee2e2' : '#fff3cd';
                banner.style.borderBottomColor = isCrit ? '#dc2626' : '#f59e0b';
            }

            // 更新徽章
            const critBadge = document.getElementById('alarm-banner-crit');
            const warnBadge = document.getElementById('alarm-banner-warn');
            const countBadge = document.getElementById('alarm-banner-count');

            if (critBadge) {
                critBadge.textContent = '紧急 ' + critCount;
                critBadge.style.display = critCount > 0 ? '' : 'none';
            }
            if (warnBadge) {
                warnBadge.textContent = '警告 ' + warnCount;
                warnBadge.style.display = warnCount > 0 ? '' : 'none';
            }
            if (countBadge) countBadge.textContent = count;

            banner.style.display = 'block';
        })
        .catch(err => {
            console.error('updateAlarmCount fetch error:', err);
        });
}

/**
 * 静默更新报警计数（不弹窗）
 */
function updateAlarmCountSilent() {
    const token = localStorage.getItem('auth_token');
    fetch(`${API_BASE}/alarms/active`, {
        headers: token ? { 'Authorization': `Bearer ${token}` } : {}
    })
        .then(response => response.json())
        .then(data => {
            const count = data.alarms ? data.alarms.length : 0;
            const alarmEl = document.getElementById('active-alarms');
            if (alarmEl) {
                alarmEl.textContent = count;
            }
            
            const countCard = document.getElementById('alarm-count-card');
            if (countCard) {
                countCard.textContent = count;
            }
        })
        .catch(() => {}); // 静默失败
}

/**
 * 更新系统统计信息
 */
function updateSystemStats(stats) {
    // 更新设备数量（安全设置，元素不存在不报错）
    if (stats.devices) {
        const devs = Array.isArray(stats.devices) ? stats.devices : Object.values(stats.devices);
        const deviceCount = devs.length;
        const onlineCount = devs.filter(d => d.connected).length;

        const el1 = document.getElementById('device-count');
        const el2 = document.getElementById('device-online');
        const el3 = document.getElementById('device-offline');
        if (el1) el1.textContent = deviceCount;
        if (el2) el2.textContent = onlineCount;
        if (el3) el3.textContent = deviceCount - onlineCount;
    }

    // 更新数据采集统计
    if (stats.collector_stats) {
        const el = document.getElementById('data-count');
        if (el) el.textContent = stats.collector_stats.total_collections || 0;
    }

    // 更新运行时间
    if (stats.start_time) {
        const el = document.getElementById('start-time');
        if (el) el.textContent = stats.start_time;
        updateUptime(stats.start_time);
    }
}

/**
 * 更新运行时间
 */
function updateUptime(startTime) {
    const el = document.getElementById('uptime');
    if (!el) return;  // 仪表盘页面没有这个元素，跳过

    const start = new Date(startTime);
    const now = new Date();
    const diff = now - start;

    const hours = Math.floor(diff / (1000 * 60 * 60));
    const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
    const seconds = Math.floor((diff % (1000 * 60)) / 1000);

    el.textContent = `${hours}:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
}

/**
 * API请求封装
 */
async function apiRequest(url, options = {}) {
    const token = localStorage.getItem('auth_token');
    const defaultOptions = {
        headers: {
            'Content-Type': 'application/json',
            ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
        },
    };

    const response = await fetch(`${API_BASE}${url}`, { ...defaultOptions, ...options });

    // 处理认证失败
    if (response.status === 401) {
        localStorage.removeItem('auth_token');
        if (!window.location.pathname.includes('/login')) {
            window.location.href = '/login';
        }
        return null;
    }

    if (!response.ok) {
        console.error(`API error: ${response.status} ${url}`);
        return null;
    }

    try {
        return await response.json();
    } catch(e) {
        console.error(`JSON parse error for ${url}:`, e);
        return null;
    }
}

/**
 * 格式化日期时间
 */
function formatDateTime(dateString) {
    const date = new Date(dateString);
    return date.toLocaleString('zh-CN');
}

/**
 * 格式化数字
 */
function formatNumber(value, decimals = 2) {
    if (value === null || value === undefined) return '-';
    return Number(value).toFixed(decimals);
}

/**
 * 页面加载完成后初始化
 */
document.addEventListener('DOMContentLoaded', function() {
    // 初始化WebSocket
    initWebSocket();

    // 加载去重配置
    AlarmDedupManager.loadConfig();

    // 加载初始数据
    loadInitialData();

    // 加载报警状态（含报警条显示）
    updateAlarmCount();

    // 定时更新报警（5秒，保持报警条实时）
    setInterval(updateAlarmCount, 5000);

    // 空闲超时 - 15分钟无操作自动登出 (GB/T 33008: 终端锁定)
    let idleTimer = null;
    const IDLE_TIMEOUT = 15 * 60 * 1000;  // 15 minutes

    function resetIdleTimer() {
        clearTimeout(idleTimer);
        idleTimer = setTimeout(() => {
            // Show warning first
            if (confirm('会话即将超时，是否继续？')) {
                resetIdleTimer();
            } else {
                logout();
            }
        }, IDLE_TIMEOUT);
    }

    // Reset on user activity
    ['mousedown', 'mousemove', 'keypress', 'scroll', 'touchstart'].forEach(event => {
        document.addEventListener(event, resetIdleTimer, { passive: true });
    });

    // Start timer
    resetIdleTimer();
});

/**
 * 加载初始数据
 */
async function loadInitialData() {
    try {
        // 加载系统状态
        const status = await apiRequest('/system/status');
        updateSystemStats(status);
        
        // 加载设备列表
        const devices = await apiRequest('/devices');
        updateDeviceStatusList(devices.devices);
        
        // 加载最新报警
        const alarms = await apiRequest('/alarms/active');
        updateLatestAlarms(alarms.alarms);
        
    } catch (error) {
        console.error('加载初始数据失败:', error);
    }
}

/**
 * 更新设备状态列表（兼容函数，非仪表盘页面调用）
 */
function updateDeviceStatusList(devices) {
    // 仪表盘页面有自己的updateDeviceGrid，这里只更新计数
    if (!devices) return;
    const devs = Array.isArray(devices) ? devices : Object.values(devices);
    const el1 = document.getElementById('device-count');
    const el2 = document.getElementById('device-online');
    const el3 = document.getElementById('device-offline');
    if (el1) el1.textContent = devs.length;
    if (el2) el2.textContent = devs.filter(d => d.connected).length;
    if (el3) el3.textContent = devs.filter(d => !d.connected).length;
}

/**
 * 更新最新报警列表
 */
function updateLatestAlarms(alarms) {
    const container = document.getElementById('latest-alarms');
    if (!container) return;
    
    if (!alarms || alarms.length === 0) {
        container.innerHTML = '<p class="text-muted text-center">暂无报警</p>';
        return;
    }
    
    container.innerHTML = alarms.slice(0, 5).map(alarm => `
        <div class="alert alert-${alarm.alarm_level === 'critical' ? 'danger' : 'warning'} py-2 mb-2">
            <small>
                <strong>${escapeHtml(alarm.alarm_message)}</strong><br>
                ${escapeHtml(alarm.device_id)} | 值: ${escapeHtml(alarm.actual_value?.toFixed(2) || '-')}
            </small>
        </div>
    `).join('');
}
