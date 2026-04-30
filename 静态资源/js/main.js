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
 * 显示报警通知
 */
function showAlarmNotification(alarm) {
    // 创建通知元素
    const notification = document.createElement('div');
    notification.className = `alert alert-${alarm.level === 'critical' ? 'danger' : 'warning'} alert-dismissible fade show`;
    notification.innerHTML = `
        <strong><i class="bi bi-bell-fill"></i> ${alarm.message}</strong>
        <br>
        <small>设备: ${alarm.device_id} | 参数: ${alarm.register_name} | 值: ${alarm.value}</small>
        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
    `;
    
    // 添加到页面
    const container = document.querySelector('.container-fluid');
    container.insertBefore(notification, container.firstChild);
    
    // 5秒后自动消失
    setTimeout(() => {
        notification.remove();
    }, 5000);
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
    const defaultOptions = {
        headers: {
            'Content-Type': 'application/json',
        },
    };
    
    const response = await fetch(`${API_BASE}${url}`, { ...defaultOptions, ...options });
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
