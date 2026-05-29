<template>
  <div class="dashboard">
    <!-- 顶部统计卡片 -->
    <el-row :gutter="16" class="stat-cards">
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card card-blue">
          <div class="stat-content">
            <div>
              <div class="stat-label">设备总数</div>
              <div class="stat-value">{{ status?.devices_total || 0 }}</div>
              <div class="stat-sub">在线 {{ status?.devices_connected || 0 }} / 离线 {{ (status?.devices_total || 0) - (status?.devices_connected || 0) }}</div>
            </div>
            <el-icon :size="48" class="stat-icon"><Monitor /></el-icon>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card card-green">
          <div class="stat-content">
            <div>
              <div class="stat-label">今日采集</div>
              <div class="stat-value">{{ todayCollections }}</div>
              <div class="stat-sub">采集器 {{ status?.data_collector_running ? '运行中' : '已停止' }}</div>
            </div>
            <el-icon :size="48" class="stat-icon"><DataLine /></el-icon>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card card-red">
          <div class="stat-content">
            <div>
              <div class="stat-label">活跃报警</div>
              <div class="stat-value">{{ status?.alarms_active || 0 }}</div>
              <div class="stat-sub">严重 {{ criticalCount }} / 警告 {{ warningCount }}</div>
            </div>
            <el-icon :size="48" class="stat-icon"><Bell /></el-icon>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card card-purple">
          <div class="stat-content">
            <div>
              <div class="stat-label">运行模式</div>
              <div class="stat-value">{{ status?.simulation_mode ? '模拟' : '实时' }}</div>
              <div class="stat-sub">运行时间 {{ uptime }}</div>
            </div>
            <el-icon :size="48" class="stat-icon"><Cpu /></el-icon>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 仪表盘 + 设备热力图 -->
    <el-row :gutter="16" class="mb-16">
      <el-col :span="8">
        <el-card shadow="hover">
          <template #header><span>温度</span></template>
          <div ref="gaugeTempRef" class="gauge-container"></div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover">
          <template #header><span>压力</span></template>
          <div ref="gaugePressureRef" class="gauge-container"></div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover">
          <template #header><span>电压</span></template>
          <div ref="gaugeVoltageRef" class="gauge-container"></div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 趋势图 + 设备热力图 -->
    <el-row :gutter="16" class="mb-16">
      <el-col :span="16">
        <el-card shadow="hover">
          <template #header>
            <div class="card-header">
              <span>实时趋势</span>
              <el-radio-group v-model="trendRange" size="small" @change="updateTrendChart">
                <el-radio-button value="1h">1小时</el-radio-button>
                <el-radio-button value="6h">6小时</el-radio-button>
                <el-radio-button value="24h">24小时</el-radio-button>
              </el-radio-group>
            </div>
          </template>
          <div ref="trendChartRef" class="chart-container"></div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover">
          <template #header><span>设备状态</span></template>
          <div class="heatmap-grid">
            <div v-for="d in devices" :key="d.device_id" class="heatmap-cell" :class="d.connected ? 'cell-online' : 'cell-offline'" :title="`${d.device_name} (${d.device_id})`">
              <div class="cell-label">{{ d.device_name?.slice(0, 4) || d.device_id?.slice(-4) }}</div>
            </div>
          </div>
          <div class="heatmap-legend">
            <span class="legend-item"><span class="legend-dot online"></span> 在线</span>
            <span class="legend-item"><span class="legend-dot offline"></span> 离线</span>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 实时数据 + 最近报警 + 系统健康 -->
    <el-row :gutter="16">
      <el-col :span="10">
        <el-card shadow="hover">
          <template #header><span>实时数据</span></template>
          <el-table :data="realtimeData.slice(0, 15)" size="small" max-height="350" stripe>
            <el-table-column prop="device_id" label="设备" width="130" show-overflow-tooltip />
            <el-table-column prop="register_name" label="参数" width="100" show-overflow-tooltip />
            <el-table-column prop="value" label="值" width="80">
              <template #default="{ row }">
                <span :class="getValueClass(row)">{{ formatValue(row.value) }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="timestamp" label="更新时间" show-overflow-tooltip>
              <template #default="{ row }">{{ formatTime(row.timestamp) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
      <el-col :span="7">
        <el-card shadow="hover">
          <template #header><span>最近报警</span></template>
          <div class="alarm-list">
            <div v-for="a in latestAlarms" :key="a.id" class="alarm-item" :class="`alarm-${a.alarm_level}`">
              <div class="alarm-level">{{ a.alarm_level === 'critical' ? '严重' : a.alarm_level === 'warning' ? '警告' : '信息' }}</div>
              <div class="alarm-msg">{{ a.alarm_message }}</div>
              <div class="alarm-time">{{ formatTime(a.timestamp) }}</div>
            </div>
            <el-empty v-if="latestAlarms.length === 0" description="暂无报警" :image-size="60" />
          </div>
        </el-card>
      </el-col>
      <el-col :span="7">
        <el-card shadow="hover">
          <template #header><span>系统健康</span></template>
          <div class="health-panel">
            <div class="health-item">
              <div class="health-label">CPU 使用率</div>
              <el-progress :percentage="health.cpu" :color="getProgressColor(health.cpu)" :stroke-width="18" />
            </div>
            <div class="health-item">
              <div class="health-label">内存使用率</div>
              <el-progress :percentage="health.memory" :color="getProgressColor(health.memory)" :stroke-width="18" />
            </div>
            <div class="health-item">
              <div class="health-label">数据库大小</div>
              <el-progress :percentage="health.dbPercent" :color="getProgressColor(health.dbPercent)" :stroke-width="18" />
              <div class="health-detail">{{ health.dbSize }}</div>
            </div>
            <el-divider />
            <div class="health-stats">
              <div class="health-stat">
                <div class="health-stat-value">{{ health.totalRecords }}</div>
                <div class="health-stat-label">总记录数</div>
              </div>
              <div class="health-stat">
                <div class="health-stat-value">{{ health.collectionsPerMin }}</div>
                <div class="health-stat-label">次/分钟</div>
              </div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, reactive, computed } from 'vue'
import * as echarts from 'echarts'
import { io } from 'socket.io-client'
import { systemApi, devicesApi, dataApi, alarmsApi, type SystemStatus, type Device, type RealtimeData, type Alarm } from '@/api'

const status = ref<SystemStatus | null>(null)
const devices = ref<Device[]>([])
const realtimeData = ref<RealtimeData[]>([])
const latestAlarms = ref<Alarm[]>([])
const trendRange = ref('1h')

const gaugeTempRef = ref<HTMLElement>()
const gaugePressureRef = ref<HTMLElement>()
const gaugeVoltageRef = ref<HTMLElement>()
const trendChartRef = ref<HTMLElement>()
let gaugeTemp: echarts.ECharts | null = null
let gaugePressure: echarts.ECharts | null = null
let gaugeVoltage: echarts.ECharts | null = null
let trendChart: echarts.ECharts | null = null
let socket: ReturnType<typeof io> | null = null
let statusTimer: ReturnType<typeof setInterval>

const todayCollections = ref(0)
const criticalCount = ref(0)
const warningCount = ref(0)

const health = reactive({
  cpu: 35,
  memory: 52,
  dbPercent: 20,
  dbSize: '12 MB',
  totalRecords: '0',
  collectionsPerMin: '0',
})

const uptime = computed(() => {
  if (!status.value?.uptime) return '-'
  const s = status.value.uptime
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h > 0 ? `${h}时${m}分` : `${m}分`
})

onMounted(async () => {
  await loadData()
  initGauges()
  initTrendChart()
  connectSocket()
  statusTimer = setInterval(loadData, 10000)
})

onUnmounted(() => {
  gaugeTemp?.dispose()
  gaugePressure?.dispose()
  gaugeVoltage?.dispose()
  trendChart?.dispose()
  socket?.disconnect()
  clearInterval(statusTimer)
})

async function loadData() {
  try {
    const [s, d, r, a] = await Promise.all([
      systemApi.getStatus(),
      devicesApi.getAll(),
      dataApi.getRealtime(),
      alarmsApi.getActive(),
    ])
    status.value = s
    devices.value = d.devices || []
    realtimeData.value = r.data || []
    latestAlarms.value = (a.alarms || []).slice(0, 5)
    criticalCount.value = latestAlarms.value.filter(x => x.alarm_level === 'critical').length
    warningCount.value = latestAlarms.value.filter(x => x.alarm_level === 'warning').length
    updateGauges()
  } catch { /* ignore */ }
}

function initGauges() {
  const gaugeOption = (name: string, min: number, max: number, unit: string, color: string) => ({
    series: [{
      type: 'gauge',
      min, max,
      progress: { show: true, width: 18, itemStyle: { color } },
      axisLine: { lineStyle: { width: 18, color: [[1, '#e6ebf5']] } },
      axisTick: { show: false },
      splitLine: { length: 10, lineStyle: { width: 2, color: '#999' } },
      axisLabel: { distance: 25, fontSize: 12 },
      pointer: { width: 5, length: '60%' },
      title: { fontSize: 14, offsetCenter: [0, '70%'] },
      detail: { fontSize: 24, offsetCenter: [0, '40%'], valueAnimation: true, formatter: `{value} ${unit}`, color: '#333' },
      data: [{ value: 0, name }],
    }],
  })

  if (gaugeTempRef.value) {
    gaugeTemp = echarts.init(gaugeTempRef.value)
    gaugeTemp.setOption(gaugeOption('温度', 0, 100, '°C', '#f56c6c'))
  }
  if (gaugePressureRef.value) {
    gaugePressure = echarts.init(gaugePressureRef.value)
    gaugePressure.setOption(gaugeOption('压力', 0, 1.6, 'MPa', '#e6a23c'))
  }
  if (gaugeVoltageRef.value) {
    gaugeVoltage = echarts.init(gaugeVoltageRef.value)
    gaugeVoltage.setOption(gaugeOption('电压', 0, 500, 'V', '#409eff'))
  }
}

function updateGauges() {
  // 从实时数据中找温度/压力/电压
  const temp = realtimeData.value.find(d => d.register_name?.includes('temp') || d.register_name?.includes('temperature'))
  const pressure = realtimeData.value.find(d => d.register_name?.includes('pressure'))
  const voltage = realtimeData.value.find(d => d.register_name?.includes('voltage'))

  if (gaugeTemp && temp) {
    gaugeTemp.setOption({ series: [{ data: [{ value: Number(temp.value.toFixed(1)), name: '温度' }] }] })
  }
  if (gaugePressure && pressure) {
    gaugePressure.setOption({ series: [{ data: [{ value: Number(pressure.value.toFixed(2)), name: '压力' }] }] })
  }
  if (gaugeVoltage && voltage) {
    gaugeVoltage.setOption({ series: [{ data: [{ value: Number(voltage.value.toFixed(0)), name: '电压' }] }] })
  }
}

function initTrendChart() {
  if (!trendChartRef.value) return
  trendChart = echarts.init(trendChartRef.value)
  trendChart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: [] },
    grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
    xAxis: { type: 'category', boundaryGap: false, data: [] },
    yAxis: { type: 'value' },
    series: [],
  })
}

// 趋势数据缓存
const trendCache: Record<string, { times: string[]; values: number[] }> = {}

function updateTrendChart() {
  if (!trendChart || realtimeData.value.length === 0) return
  const grouped: Record<string, RealtimeData[]> = {}
  realtimeData.value.forEach(d => {
    const key = `${d.device_id}/${d.register_name}`
    if (!grouped[key]) grouped[key] = []
    grouped[key].push(d)
  })
  const keys = Object.keys(grouped).slice(0, 5)
  const now = new Date().toLocaleTimeString()
  keys.forEach(key => {
    if (!trendCache[key]) trendCache[key] = { times: [], values: [] }
    trendCache[key].times.push(now)
    trendCache[key].values.push(grouped[key][0]?.value || 0)
    if (trendCache[key].times.length > 30) {
      trendCache[key].times.shift()
      trendCache[key].values.shift()
    }
  })
  trendChart.setOption({
    legend: { data: keys },
    xAxis: { data: trendCache[keys[0]]?.times || [] },
    series: keys.map(key => ({
      name: key, type: 'line', smooth: true, showSymbol: false,
      areaStyle: { opacity: 0.1 },
      data: trendCache[key]?.values || [],
    })),
  })
}

function connectSocket() {
  const socketUrl = import.meta.env.DEV ? window.location.origin : 'http://localhost:5000'
  socket = io(socketUrl, { transports: ['websocket', 'polling'] })
  socket.on('data_update', (data: any) => {
    if (data.data) {
      const idx = realtimeData.value.findIndex(
        d => d.device_id === data.data.device_id && d.register_name === data.data.register_name
      )
      if (idx >= 0) realtimeData.value[idx] = data.data
      else realtimeData.value.push(data.data)
      updateGauges()
      updateTrendChart()
    }
  })
  socket.on('alarm', (data: any) => {
    if (data.alarm) {
      latestAlarms.value.unshift(data.alarm)
      if (latestAlarms.value.length > 5) latestAlarms.value.pop()
    }
  })
}

function formatValue(v: number) {
  return v !== undefined && v !== null ? v.toFixed(2) : '-'
}

function formatTime(t: string) {
  if (!t) return '-'
  const d = new Date(t)
  return d.toLocaleTimeString()
}

function getValueClass(row: RealtimeData) {
  // 根据值的范围给颜色（简单逻辑）
  if (row.value > 80) return 'text-danger'
  if (row.value > 60) return 'text-warning'
  return 'text-success'
}

function getProgressColor(p: number) {
  if (p > 80) return '#f56c6c'
  if (p > 60) return '#e6a23c'
  return '#67c23a'
}
</script>

<style scoped>
.stat-cards { margin-bottom: 16px; }
.stat-card { border-radius: 8px; }
.stat-card .stat-content { display: flex; align-items: center; justify-content: space-between; }
.stat-label { font-size: 14px; color: #909399; margin-bottom: 4px; }
.stat-value { font-size: 32px; font-weight: bold; }
.stat-sub { font-size: 12px; color: #909399; margin-top: 4px; }
.stat-icon { opacity: 0.15; }
.card-blue .stat-value { color: #409eff; }
.card-green .stat-value { color: #67c23a; }
.card-red .stat-value { color: #f56c6c; }
.card-purple .stat-value { color: #909399; }
.mb-16 { margin-bottom: 16px; }
.gauge-container { height: 220px; }
.chart-container { height: 300px; }
.card-header { display: flex; align-items: center; justify-content: space-between; }

/* 热力图 */
.heatmap-grid { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 0; }
.heatmap-cell { width: 60px; height: 40px; border-radius: 4px; display: flex; align-items: center; justify-content: center; font-size: 11px; color: #fff; cursor: default; }
.cell-online { background: #67c23a; }
.cell-offline { background: #f56c6c; }
.cell-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding: 0 4px; }
.heatmap-legend { margin-top: 8px; display: flex; gap: 16px; font-size: 12px; color: #909399; }
.legend-item { display: flex; align-items: center; gap: 4px; }
.legend-dot { width: 10px; height: 10px; border-radius: 2px; }
.legend-dot.online { background: #67c23a; }
.legend-dot.offline { background: #f56c6c; }

/* 报警列表 */
.alarm-list { max-height: 350px; overflow-y: auto; }
.alarm-item { padding: 8px 10px; border-left: 3px solid #dcdfe6; margin-bottom: 6px; background: #fafafa; border-radius: 0 4px 4px 0; }
.alarm-critical { border-left-color: #f56c6c; background: #fef0f0; }
.alarm-warning { border-left-color: #e6a23c; background: #fdf6ec; }
.alarm-info { border-left-color: #909399; background: #f4f4f5; }
.alarm-level { font-size: 11px; font-weight: bold; margin-bottom: 2px; }
.alarm-critical .alarm-level { color: #f56c6c; }
.alarm-warning .alarm-level { color: #e6a23c; }
.alarm-msg { font-size: 13px; color: #303133; }
.alarm-time { font-size: 11px; color: #c0c4cc; margin-top: 2px; }

/* 系统健康 */
.health-panel { padding: 4px 0; }
.health-item { margin-bottom: 16px; }
.health-label { font-size: 13px; color: #606266; margin-bottom: 4px; }
.health-detail { font-size: 12px; color: #909399; text-align: right; }
.health-stats { display: flex; justify-content: space-around; }
.health-stat { text-align: center; }
.health-stat-value { font-size: 20px; font-weight: bold; color: #303133; }
.health-stat-label { font-size: 12px; color: #909399; }

.text-danger { color: #f56c6c; font-weight: bold; }
.text-warning { color: #e6a23c; }
.text-success { color: #67c23a; }
</style>
