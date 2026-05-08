/**
 * 工业SCADA系统主JavaScript文件
 */

// 全局变量
const API_BASE = '/api';
let socket = null;

/**
 * 初始化WebSocket连接
 */
function initWebSocket() {
    socket = io();
    
    socket.on('connect', function() {
        console.log('WebSocket连接成功');
        updateSystemStatus('online');
    });
    
    socket.on('disconnect', function() {
        console.log('WebSocket断开');
        updateSystemStatus('offline');
    });
    
    socket.on('data_update', function(data) {
        handleDataUpdate(data);
    });
    
    socket.on('alarm', function(data) {
        handleAlarm(data);
    });
    
    socket.on('system_status', function(data) {
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
 * 处理数据更新
 */
function handleDataUpdate(data) {
    // 更新实时数据表格
    updateRealtimeTable(data);
    
    // 更新图表
    updateCharts(data);
}

/**
 * 处理报警
 */
function handleAlarm(data) {
    // 显示报警通知
    showAlarmNotification(data);
    
    // 更新报警计数
    updateAlarmCount();
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
            const resp = await fetch(`${API_BASE}/alarms/dedup-config`);
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
 * 显示报警通知（非侵入式Toast通知，带去重逻辑）
 */
function showAlarmNotification(alarm) {
    // 去重检查
    if (!AlarmDedupManager.shouldShow(alarm)) {
        console.log('[Alarm] 报警被去重跳过:', alarm.dedup_key || alarm.alarm_id);
        return;
    }

    // 记录已显示
    AlarmDedupManager.recordShow(alarm);

    // 创建Toast容器（如果不存在）
    let toastContainer = document.getElementById('alarm-toast-container');
    if (!toastContainer) {
        toastContainer = document.createElement('div');
        toastContainer.id = 'alarm-toast-container';
        toastContainer.style.cssText = `
            position: fixed;
            bottom: 20px;
            right: 20px;
            z-index: 9999;
            max-width: 350px;
            display: flex;
            flex-direction: column-reverse;
            gap: 8px;
        `;
        document.body.appendChild(toastContainer);
    }
    
    // 创建Toast元素
    const toast = document.createElement('div');
    const isCritical = alarm.level === 'critical' || alarm.alarm_level === 'critical';
    
    toast.className = `toast show align-items-center border-0`;
    toast.style.cssText = `
        background-color: ${isCritical ? '#dc3545' : '#ffc107'};
        color: ${isCritical ? 'white' : '#333'};
        box-shadow: 0 4px 12px rgba(0,0,0,0.2);
        animation: slideInRight 0.3s ease-out;
        max-width: 100%;
    `;
    
    toast.innerHTML = `
        <div class="d-flex">
            <div class="toast-body">
                <div class="d-flex align-items-center mb-1">
                    <i class="bi bi-${isCritical ? 'exclamation-triangle-fill' : 'exclamation-circle-fill'} me-2"></i>
                    <strong class="me-auto">${isCritical ? '严重报警' : '警告'}</strong>
                    <small>${new Date().toLocaleTimeString()}</small>
                </div>
                <div class="mb-1">${alarm.message || alarm.alarm_message || '报警'}</div>
                <small class="opacity-75">
                    ${alarm.device_id ? '设备: ' + alarm.device_id : ''}
                    ${alarm.register_name ? ' | 参数: ' + alarm.register_name : ''}
                    ${alarm.value || alarm.actual_value ? ' | 值: ' + (alarm.value || alarm.actual_value) : ''}
                </small>
            </div>
            <button type="button" class="btn-close btn-close-white me-2 m-auto" id="dismiss-${Date.now()}"></button>
        </div>
    `;
    
    // 绑定关闭按钮事件（记录dismissed）
    const closeBtn = toast.querySelector('.btn-close');
    if (closeBtn) {
        closeBtn.addEventListener('click', function() {
            AlarmDedupManager.recordDismiss(alarm);
            toast.style.animation = 'slideOutRight 0.3s ease-in';
            setTimeout(() => toast.remove(), 300);
        });
    }
    
    // 添加到容器
    toastContainer.appendChild(toast);
    
    // 播放提示音（仅严重报警）
    if (isCritical) {
        playAlarmSound();
    }
    
    // 自动消失时间：使用后端配置
    const dismissTime = isCritical
        ? (AlarmDedupManager.config.critical_toast_duration * 1000)
        : (AlarmDedupManager.config.warning_toast_duration * 1000);
    setTimeout(() => {
        if (toast.parentNode) {
            toast.style.animation = 'slideOutRight 0.3s ease-in';
            setTimeout(() => toast.remove(), 300);
        }
    }, dismissTime);
    
    // 限制最多显示的通知数（使用后端配置）
    const maxToasts = AlarmDedupManager.config.max_visible_toasts || 3;
    while (toastContainer.children.length > maxToasts) {
        // 移除最旧的toast时也记录dismissed
        const oldest = toastContainer.firstChild;
        if (oldest) oldest.remove();
    }
    
    // 更新报警计数（静默更新，不弹窗）
    updateAlarmCountSilent();
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
    fetch(`${API_BASE}/alarms/active`)
        .then(response => response.json())
        .then(data => {
            const count = data.alarms ? data.alarms.length : 0;
            document.getElementById('active-alarms').textContent = count;
            
            // 更新卡片
            const countCard = document.getElementById('alarm-count-card');
            if (countCard) {
                countCard.textContent = count;
            }
        });
}

/**
 * 静默更新报警计数（不弹窗）
 */
function updateAlarmCountSilent() {
    fetch(`${API_BASE}/alarms/active`)
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
    // 更新设备数量
    if (stats.devices) {
        const deviceCount = stats.devices.length;
        const onlineCount = stats.devices.filter(d => d.connected).length;
        
        document.getElementById('device-count').textContent = deviceCount;
        document.getElementById('device-online').textContent = onlineCount;
        document.getElementById('device-offline').textContent = deviceCount - onlineCount;
    }
    
    // 更新数据采集统计
    if (stats.collector_stats) {
        document.getElementById('data-count').textContent = stats.collector_stats.total_collections || 0;
    }
    
    // 更新运行时间
    if (stats.start_time) {
        document.getElementById('start-time').textContent = stats.start_time;
        updateUptime(stats.start_time);
    }
}

/**
 * 更新运行时间
 */
function updateUptime(startTime) {
    const start = new Date(startTime);
    const now = new Date();
    const diff = now - start;
    
    const hours = Math.floor(diff / (1000 * 60 * 60));
    const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
    const seconds = Math.floor((diff % (1000 * 60)) / 1000);
    
    document.getElementById('uptime').textContent = 
        `${hours}:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
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
        // 清除无效token
        localStorage.removeItem('auth_token');
        // 如果不是登录页面，跳转到登录页
        if (!window.location.pathname.includes('/login')) {
            window.location.href = '/login';
        }
    }
    
    return response.json();
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
    
    // 定时更新
    setInterval(updateAlarmCount, 30000);  // 每30秒更新报警计数
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
                <strong>${alarm.alarm_message}</strong><br>
                ${alarm.device_id} | 值: ${alarm.actual_value?.toFixed(2) || '-'}
            </small>
        </div>
    `).join('');
}
